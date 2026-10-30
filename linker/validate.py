"""Deterministic checks on every LLM edit. An edit that fails any check is dropped and logged;
the LLM can never introduce a URL, touch a sentence it wasn't offered, or rewrite too much."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .content import ParsedDraft
from .scoring import Candidate
from .text import STOPWORDS, content_tokens, similarity, tokens

GENERIC_ANCHORS = {"click here", "read more", "here", "this article", "this post", "learn more",
                   "this guide", "this link", "check this out"}
NUM = re.compile(r"\d+(?:[.,]\d+)?")


@dataclass
class Checked:
    accepted: list[dict]
    rejected: list[tuple[dict, str]]
    notes: list[str]


FILLER_START = {"on", "in", "at", "the", "a", "an", "this", "that", "these", "those", "by", "for",
                "with", "to", "of", "from", "and", "or", "but", "its", "their", "our", "your", "as",
                "into", "about", "while", "when", "which", "it",
                # vague nouns: "parts of their superposition", "sequence of Hadamard"
                "part", "parts", "kind", "kinds", "type", "types", "way", "ways", "sequence",
                "set", "range", "number", "lot", "lots", "piece", "pieces", "series", "aspects"}


def _bigrams(toks: list[str]) -> set[tuple[str, str]]:
    toks = [t for t in toks if t not in STOPWORDS and len(t) > 1]
    return set(zip(toks, toks[1:]))


def anchor_problem(anchor: str, target, own_text: str) -> str | None:
    """Anchor rules that keep link signals honest:
    - it must describe the target page (share a topic word with it), not the current article
    - it must not carry the current article's own topic phrase to a different page
    - it must not open with a filler word ("On real hardware", "the ...")"""
    words = anchor.split()
    if words and words[0].lower().strip(",.;:") in FILLER_START:
        return "anchor starts with a filler word"
    a_tok = content_tokens(anchor)
    target_tok = content_tokens(" ".join(target.keywords + [target.title]))
    if not a_tok & target_tok:
        return "anchor doesn't describe the target page"
    own_bi = _bigrams(tokens(own_text))
    for bi in _bigrams(tokens(anchor)):
        if bi in own_bi and not set(bi) <= target_tok:
            return "anchor uses this article's own topic for another page"
    return None


def _share_in(anchor: str, original: str) -> float:
    """Share of the anchor's content words that the writer's sentence already contained. A blog
    link should sit on what the sentence is already about, not on a topic the LLM bolted on."""
    a = content_tokens(anchor)
    return len(a & content_tokens(original)) / len(a) if a else 0.0


def _overlap(a: set, b: set) -> float:
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def check(raw_edits: list[dict], ids: dict[str, Candidate], parsed: ParsedDraft,
          n: int, settings: dict, offered: dict | None = None, own_text: str = "",
          common_words: set | None = None) -> Checked:
    """`offered` is the set of sentences shown to the LLM (eligible ones); defaults to all.
    `own_text` is this article's title + primary keyword, used to stop self-topic anchors."""
    lk = settings["linking"]
    max_drop = settings["llm"]["max_rewrite_similarity_drop"]
    new_per_para = lk["max_new_links_per_paragraph"]
    total_per_para = lk["max_total_links_per_paragraph"]
    gap = lk["min_sentence_gap"]
    offered = parsed.sentences if offered is None else offered
    existing_anchors = {e.anchor.strip().lower() for e in parsed.existing if e.anchor}
    common = common_words or set()

    accepted, rejected = [], []
    used_targets, used_sentences, para_new = set(), set(), {}
    linked_orders = set(parsed.linked_orders)    # grows as new links are accepted

    for e in raw_edits:
        sid, tid = str(e.get("sentence_id", "")), str(e.get("target_id", ""))
        anchor, rewritten = str(e.get("anchor", "")).strip(), str(e.get("rewritten", "")).strip()
        s, cand = offered.get(sid), ids.get(tid)
        if s is not None and anchor and s.text.count(anchor) == 1:
            rewritten = s.text          # the anchor already fits: never touch the writer's wording

        reason = None
        if s is None:
            reason = "unknown sentence_id"
        elif cand is None:
            reason = "unknown target_id"
        elif tid in used_targets:
            reason = "target already used"
        elif cand.target.url in parsed.existing_links:
            reason = "target already linked in the post"
        elif sid in used_sentences:
            reason = "sentence already has a link"
        elif any(abs(s.order - o) <= gap for o in linked_orders):
            reason = "too close to another link"
        elif para_new.get(s.block, 0) >= new_per_para:
            reason = "paragraph already has a new link"
        elif parsed.para_link_count.get(s.block, 0) + para_new.get(s.block, 0) >= total_per_para:
            reason = "paragraph link cap reached"
        elif anchor.lower() in existing_anchors:
            reason = "anchor duplicates an existing link's anchor"
        elif not 2 <= len(anchor.split()) <= 8:
            reason = "anchor length out of range"
        elif anchor.lower() in GENERIC_ANCHORS:
            reason = "generic anchor"
        elif (bad := anchor_problem(anchor, cand.target, own_text)):
            reason = bad
        elif any(_overlap(content_tokens(anchor) - common, content_tokens(a["anchor"]) - common) >= 0.6
                 for a in accepted):
            reason = "anchor too similar to another new link"
        elif cand.target.type != "program" and _share_in(anchor, s.text) < 0.5:
            reason = "anchor topic isn't in the original sentence (forced insertion)"
        elif any((content_tokens(anchor) & content_tokens(a["anchor"])) - common
                 for a in accepted):
            reason = "anchor repeats a term already linked in this article"
        elif rewritten.count(anchor) != 1:
            reason = "anchor not present exactly once in rewrite"
        elif rewritten != s.text and tokens(rewritten) == tokens(s.text):
            reason = "rewrite only changed punctuation"
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
        linked_orders.add(s.order)
        para_new[s.block] = para_new.get(s.block, 0) + 1
        accepted.append({
            "sentence_id": sid, "anchor": anchor, "rewritten": rewritten,
            "url": cand.target.link, "candidate": cand, "original": s.text, "raw": e,
        })

    notes = []
    placed = {a["candidate"].target.url for a in accepted}
    if any(c.required and c.target.url not in placed for c in ids.values()):
        notes.append("required program link not placed")
    return Checked(accepted, rejected, notes)
