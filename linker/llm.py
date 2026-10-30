"""The LLM layer. Its only job: pick sentences and rewrite them lightly so an anchor fits.
It sees a short, pre-scored shortlist (never the whole site) and returns structured JSON."""
from __future__ import annotations

import json
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
- Anchor: 2-6 words, a descriptive keyword phrase with clear intent. Never "click here", "read more", "this article", "here".
- The anchor must describe the TARGET page: what the reader gets by clicking. Never use this
  article's own topic ("{title}") as the anchor for a different page.
- The anchor must be accurate for the target: e.g. don't anchor "classical X" to a page about "quantum X".
- Start anchors with a meaningful word, never with "on", "in", "the", "a", "this" or similar.
- Each anchor must be distinct; don't link two different pages with near-identical wording.
- Vary anchors; don't reuse the exact same wording as the target's page title.
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


def build_prompt(title: str, n: int, shortlist: list[Candidate], sentences: dict) -> tuple[str, dict]:
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
    return PROMPT.format(title=title, n=n, targets="\n".join(lines), sentences=sents), ids


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

    def _config(self):
        t = self._types
        kw = {"response_mime_type": "application/json"}
        if self.temperature is not None:
            kw["temperature"] = self.temperature
        if self.thinking_level:
            # Light reasoning is plenty for "pick a sentence and add a phrase", and much faster.
            kw["thinking_config"] = t.ThinkingConfig(thinking_level=self.thinking_level)
        return t.GenerateContentConfig(**kw)

    def _call(self, model: str, prompt: str):
        from google.genai import errors
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
                if code in self.RETRYABLE and attempt <= self.retries:
                    wait = 15 * attempt
                    print(f"[llm] {code} from {model}; retry {attempt}/{self.retries} in {wait}s", flush=True)
                    time.sleep(wait)
                    continue
                raise

    def propose(self, prompt: str) -> list[dict]:
        """Try the main model; if it stays overloaded/unavailable, fall back to the next model."""
        from google.genai import errors
        last = None
        for i, model in enumerate(self.models):
            try:
                resp = self._call(model, prompt)
                self.used_model = model
                if i:
                    print(f"[llm] answered by fallback model {model}", flush=True)
                break
            except errors.APIError as e:
                code = getattr(e, "code", None)
                if code in self.RETRYABLE or code == 404:
                    last = e
                    if i + 1 < len(self.models):
                        print(f"[llm] {model} unavailable ({code}); switching to {self.models[i + 1]}", flush=True)
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
                  fallback_models=llm.get("fallback_models") or [])
