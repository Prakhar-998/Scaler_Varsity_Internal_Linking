"""The LLM layer. Its only job: pick sentences and rewrite them lightly so an anchor fits.
It sees a short, pre-scored shortlist (never the whole site) and returns structured JSON."""
from __future__ import annotations

import json
import re
import time

from .scoring import Candidate

PROMPT = """You are an SEO editor adding internal links to a blog draft for InterviewBit Varsity.

ARTICLE: "{title}"

You must place up to {n} links. Each link goes into ONE existing sentence, which you may lightly
rewrite so the anchor reads naturally.

RULES
- Use only sentence IDs and target IDs listed below. Never invent URLs.
- Targets marked REQUIRED must be placed if any sentence can carry them naturally.
- Prefer targets higher in the list; skip a target rather than force it.
- One link per sentence, at most one link per paragraph (the paragraph is the "p" number in the ID), each target at most once.
- Never pick two sentences that are next to each other (e.g. p4.s1 and p4.s2); spread links through the article.
- Anchor: 2-6 words, a descriptive keyword phrase with clear intent. A single distinctive term
  that IS the target's topic is fine too ("RLHF", "Quantization", "Perplexity"). Never "click here", "read more", "this article", "here".
- The anchor must describe the TARGET page: what the reader gets by clicking. Never use this
  article's own topic ("{title}") as the anchor for a different page.
- The anchor must be accurate for the target: e.g. don't anchor "classical X" to a page about "quantum X".
- The anchor's main (last) word must be the target's topic: "inventory management" for an inventory
  page is right; "logistics costs" or "logistics executives" for a logistics page is wrong.
- Start anchors with a meaningful word, never with "on", "in", "the", "a", "this" or similar.
- Anchors are complete noun phrases ("Hadamard gates"), never a fragment cut mid-phrase
  ("sequence of Hadamard") and never led by a verb ("apply Hadamard gates").
- Link each concept at most once per article.
- Each anchor must be distinct; don't link two different pages with near-identical wording.
- Vary anchors; don't reuse the exact same wording as the target's page title.
- BLOG targets: the anchor MUST be words the sentence already contains, and "rewritten" must be the
  sentence exactly as given. Never insert, replace or reorder words for a blog link. If no sentence
  already mentions a blog target's topic, skip that target.
{program_rule}
- Keep the rewrite minimal: same meaning, same facts, same tone. Do not add numbers, claims, names or statistics that are not in the original sentence. Add at most about 15 words.
- The "rewritten" sentence must contain the anchor text exactly once, character for character.
- Plain text only in "rewritten" (no HTML, no markdown).

TARGETS (id | type | page | anchor ideas | note)
{targets}

SENTENCES YOU MAY EDIT (id: text)
{sentences}

Return JSON only:
{{"edits": [{{"sentence_id": "p4.s1", "target_id": "T1", "anchor": "...", "rewritten": "..."}}]}}
"""


PROGRAM_VERBATIM = """- PROGRAM targets: same as blog targets. The anchor MUST be words the sentence already contains
  (e.g. "AI-powered operations", "quantum computing", "generative AI") and "rewritten" must be the
  sentence exactly as given. Never insert a phrase like "such as a ... course". If no sentence
  already names the programme's topic, skip the programme link."""

PROGRAM_INSERT = """- PROGRAM targets: prefer a sentence that already mentions the programme topic or learning
  (course, certification, training, upskilling, career). You may INSERT a short phrase (max {k} words)
  containing the anchor; never delete or change the writer's words, punctuation or quotes. If no
  sentence is about learning or the programme's topic, skip the programme link."""


def build_prompt(title: str, n: int, shortlist: list[Candidate], sentences: dict,
                 program_insert_words: int = 0) -> tuple[str, dict]:
    """`sentences` are only the eligible ones (spaced away from existing links)."""
    ids, lines = {}, []
    for i, c in enumerate(shortlist, 1):
        tid = f"T{i}"
        ids[tid] = c
        note = []
        if c.required:
            note.append("REQUIRED")
        if c.hint_keyword:
            note.append(f'phrase "{c.hint_keyword}" already appears in the draft')
        anchors = "; ".join(c.target.keywords[:4]) or c.target.title
        lines.append(f"{tid} | {c.target.type} | {c.target.title} | {anchors} | {', '.join(note) or '-'}")
    sents = "\n".join(f"{s.id}: {s.text}" for s in sentences.values())
    rule = PROGRAM_INSERT.format(k=program_insert_words) if program_insert_words > 0 else PROGRAM_VERBATIM
    return PROMPT.format(title=title, n=n, targets="\n".join(lines), sentences=sents, program_rule=rule), ids


BRIDGE_PROMPT = """You are an editor for the InterviewBit Varsity blog. The article below has no natural
place to link to its related programme, so you will ADD ONE NEW SENTENCE at the end of one paragraph.

ARTICLE: "{title}"
PROGRAMME: {program}

Write one sentence that follows naturally from the end of the chosen paragraph and points a reader
who wants to build these skills to the programme. It must:
- contain exactly one of these anchor phrases, word for word: {anchors}
- be 12-{max_words} words, plain and factual, in the article's tone
- not use numbers, rankings or claims (no "best", "top", "leading", salaries, placements, guarantees)
- not repeat what the paragraph already says, and not start with "This" or "Click"

Choose the paragraph where the sentence fits best. Paragraphs marked BEST FIT talk about skills,
learning or the programme's topic; use the one marked LAST RESORT (the conclusion) only if none of
the others work. If nothing fits, return no edits.

PARAGRAPHS (id | fit | text)
{paragraphs}

Return JSON only:
{{"edits": [{{"paragraph_id": "B3", "anchor": "...", "sentence": "..."}}]}}
"""


def build_bridge_prompt(title: str, program, anchors: list[str], paragraphs: list[tuple[str, str, str]],
                        max_words: int) -> str:
    lines = "\n".join(f"{pid} | {fit} | {text}" for pid, fit, text in paragraphs)
    return BRIDGE_PROMPT.format(title=title, program=program, anchors="; ".join(f'"{a}"' for a in anchors),
                                paragraphs=lines, max_words=max_words)


class Gemini:
    RETRYABLE = {429, 500, 502, 503, 504}

    def __init__(self, api_key: str, model: str, temperature: float | None = None,
                 thinking_level: str | None = "low", timeout_s: int = 300, retries: int = 2,
                 fallback_models: list[str] | None = None):
        from google import genai  # imported lazily so offline tests don't need the SDK configured
        from google.genai import types
        self._types = types
        # The SDK also sends this as the server-side deadline, so it must be generous.
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_s * 1000))
        self.model = model
        self.temperature = temperature
        self.thinking_level = thinking_level
        self.retries = retries
        self.models = [model] + [m for m in (fallback_models or []) if m and m != model]
        self.used_model = None
        self.busy_until: dict[str, float] = {}   # model -> time it may be tried again this run
        self.busy_pause_s = 600

    def _config(self):
        t = self._types
        kw = {"response_mime_type": "application/json"}
        if self.temperature is not None:
            kw["temperature"] = self.temperature
        if self.thinking_level:
            # Light reasoning is plenty for "pick a sentence and add a phrase", and much faster.
            kw["thinking_config"] = t.ThinkingConfig(thinking_level=self.thinking_level)
        return t.GenerateContentConfig(**kw)

    def _call(self, model: str, prompt: str, retries: int | None = None):
        from google.genai import errors
        retries = self.retries if retries is None else retries
        attempt = 0
        while True:
            attempt += 1
            try:
                return self.client.models.generate_content(model=model, contents=prompt,
                                                           config=self._config())
            except errors.APIError as e:
                code = getattr(e, "code", None)
                if code == 400 and self.thinking_level and "think" in str(e).lower():
                    print(f"[llm] {model} rejected thinking_level; retrying without it", flush=True)
                    self.thinking_level = None
                    continue
                if code == 429:
                    # Quota used up: retrying straight away only burns time. Daily quota -> skip
                    # this model for the rest of the run; per-minute quota -> skip it for 2 min.
                    daily = bool(re.search(r"per ?day|PerDay|daily", str(e), re.I))
                    self.busy_until[model] = time.time() + (10**9 if daily else 120)
                    print(f"[llm] 429 from {model}: {'daily' if daily else 'per-minute'} quota used up; "
                          f"skipping it {'for this run' if daily else 'for 2 min'}", flush=True)
                    raise
                if code in self.RETRYABLE and attempt <= retries:
                    wait = 15 * attempt
                    print(f"[llm] {code} from {model}; retry {attempt}/{retries} in {wait}s", flush=True)
                    time.sleep(wait)
                    continue
                raise

    def propose(self, prompt: str) -> list[dict]:
        """Try the models in order. A model that stays overloaded is skipped for the next 10 minutes
        of this run (no more waiting on it for every post); while a fallback exists, the main model
        gets only one quick retry before handing over."""
        from google.genai import errors
        now = time.time()
        ready = [m for m in self.models if self.busy_until.get(m, 0) <= now] or self.models[-1:]
        last = None
        for i, model in enumerate(ready):
            has_next = i + 1 < len(ready)
            try:
                resp = self._call(model, prompt, retries=min(1, self.retries) if has_next else None)
                self.used_model = model
                if model != self.models[0]:
                    print(f"[llm] answered by fallback model {model}", flush=True)
                break
            except errors.APIError as e:
                code = getattr(e, "code", None)
                if code in self.RETRYABLE or code == 404:
                    last = e
                    if code != 429:              # 429 already set its own pause in _call
                        self.busy_until[model] = time.time() + self.busy_pause_s
                    if has_next:
                        print(f"[llm] {model} unavailable ({code}); using {ready[i + 1]} for the next "
                              f"{self.busy_pause_s // 60} min", flush=True)
                    continue
                raise
        else:
            raise last
        data = json.loads(resp.text)
        return data.get("edits", []) if isinstance(data, dict) else []


def gemini_from_config(cfg, secrets: dict) -> Gemini:
    llm = cfg.get("llm")
    model = secrets.get("GEMINI_MODEL") or llm["default_model"]
    return Gemini(secrets["GEMINI_API_KEY"], model, llm.get("temperature"),
                  thinking_level=llm.get("thinking_level"), timeout_s=llm.get("timeout_seconds", 300),
                  retries=llm.get("retries", 2), fallback_models=llm.get("fallback_models") or [])
