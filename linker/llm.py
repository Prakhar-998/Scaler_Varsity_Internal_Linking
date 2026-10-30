"""The LLM layer. Its only job: pick sentences and rewrite them lightly so an anchor fits.
It sees a short, pre-scored shortlist (never the whole site) and returns structured JSON."""
from __future__ import annotations

import json

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
    def __init__(self, api_key: str, model: str, temperature: float = 0.2):
        from google import genai  # imported lazily so offline tests don't need the SDK configured
        self._genai = genai
        from google.genai import types
        # Fail after 2 minutes instead of hanging the run if the API stalls.
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=120_000))
        self.model = model
        self.temperature = temperature

    def propose(self, prompt: str) -> list[dict]:
        from google.genai import types
        resp = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=self.temperature,
                response_mime_type="application/json",
            ),
        )
        data = json.loads(resp.text)
        return data.get("edits", []) if isinstance(data, dict) else []
