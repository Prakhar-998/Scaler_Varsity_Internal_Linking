"""Deterministic checks on every LLM edit. An edit that fails any check is dropped and logged;
the LLM can never introduce a URL, touch a sentence it wasn't offered, or rewrite too much."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .content import ParsedDraft
from .scoring import Candidate
from .text import similarity

GENERIC_ANCHORS = {"click here", "read more", "here", "this article", "this post", "learn more",
                   "this guide", "this link", "check this out"}
NUM = re.compile(r"\d+(?:[.,]\d+)?")


@dataclass
class Checked:
    accepted: list[dict]
    rejected: list[tuple[dict, str]]
    notes: list[str]


def check(raw_edits: list[dict], ids: dict[str, Candidate], parsed: ParsedDraft,
          n: int, settings: dict) -> Checked:
    max_drop = settings["llm"]["max_rewrite_similarity_drop"]
    per_para = settings["linking"]["max_links_per_paragraph"]
    prog_min = settings["linking"]["program_links_min"]

    accepted, rejected = [], []
    used_targets, used_sentences, para_count = set(), set(), {}

    for e in raw_edits:
        sid, tid = str(e.get("sentence_id", "")), str(e.get("target_id", ""))
        anchor, rewritten = str(e.get("anchor", "")).strip(), str(e.get("rewritten", "")).strip()
        s, cand = parsed.sentences.get(sid), ids.get(tid)

        reason = None
        if s is None:
            reason = "unknown sentence_id"
        elif cand is None:
            reason = "unknown target_id"
        elif tid in used_targets:
            reason = "target already used"
        elif sid in used_sentences:
            reason = "sentence already has a link"
        elif para_count.get(s.block, 0) >= per_para:
            reason = "paragraph already has a link"
        elif not 2 <= len(anchor.split()) <= 8:
            reason = "anchor length out of range"
        elif anchor.lower() in GENERIC_ANCHORS:
            reason = "generic anchor"
        elif rewritten.count(anchor) != 1:
            reason = "anchor not present exactly once in rewrite"
        elif any(ch in rewritten for ch in "<>[]"):
            reason = "markup in rewrite"
        elif similarity(s.text, rewritten) < 1 - max_drop:
            reason = "rewrite changed too much"
        elif len(rewritten.split()) > len(s.text.split()) + 20:
            reason = "rewrite too long"
        elif set(NUM.findall(rewritten)) - set(NUM.findall(s.text)):
            reason = "rewrite introduced new numbers"
        elif len(accepted) >= n:
            reason = "over link budget"

        if reason:
            rejected.append((e, reason))
            continue
        used_targets.add(tid)
        used_sentences.add(sid)
        para_count[s.block] = para_count.get(s.block, 0) + 1
        accepted.append({
            "sentence_id": sid, "anchor": anchor, "rewritten": rewritten,
            "url": cand.target.url, "candidate": cand, "original": s.text,
        })

    notes = []
    progs = sum(1 for a in accepted if a["candidate"].target.type == "program")
    if any(c.required for c in ids.values()) and progs < prog_min:
        notes.append("required program link not placed")
    return Checked(accepted, rejected, notes)
