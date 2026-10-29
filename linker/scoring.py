"""Weightage function + quota-based selection. Deterministic: no LLM involved.

    score = W_type[type] x W_cat[match] x (0.3 + 0.7 x relevance) + orphan_boost

- W_type     program 1.0 > pillar 0.7 > blog 0.5            (settings.weights.type)
- W_cat      same 1.0 > adjacent 0.4 > other 0.1            (settings.weights.category)
- relevance  0-1, from keyword-phrase hits in the draft + topical token overlap
             (the 0.3 floor keeps a relevant-category program page in play even when the draft
             never says its keyword; the LLM then writes a sentence that does)
- orphan     up to +0.3 for targets with few inbound links, so new posts stop being orphans

Then quotas turn scores into a plan: N links from article length, program links between min and
max, at least `same_category_min_share` of blog links from the post's own category.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Config
from .inventory import Target
from .text import content_tokens, contains_phrase, norm_url, tokens


@dataclass
class Candidate:
    target: Target
    score: float
    relevance: float
    cat_match: str             # same | adjacent | other
    hint_keyword: str | None   # keyword variant found verbatim in the draft, if any
    required: bool = False


@dataclass
class Draft:
    post_id: int
    title: str
    categories: list[str]
    primary_keyword: str
    sentences: list[str]       # linkable sentences only (see content.py)
    word_count: int
    existing_links: set[str]
    url: str = ""
    existing_internal: int = 0     # internal links the writer already added
    existing_external: int = 0
    existing_program: int = 0      # of which point at a program landing page


def category_match(target: Target, post_cats: list[str], adjacent: dict[str, list[str]]) -> str:
    tc, pc = set(target.categories), set(post_cats)
    if tc & pc:
        return "same"
    near = {a for c in pc for a in adjacent.get(c, [])}
    return "adjacent" if tc & near else "other"


def relevance(target: Target, draft: Draft) -> tuple[float, str | None]:
    sent_toks = [tokens(s) for s in draft.sentences]
    best, hint = 0.0, None
    for kw in target.keywords:
        kt = tokens(kw)
        if not kt:
            continue
        if any(contains_phrase(st, kt) for st in sent_toks):
            return 1.0, kw                                   # exact phrase present: best case
        kset = set(kt)
        if len(kset) > 1 and any(kset <= set(st) for st in sent_toks):
            if best < 0.7:
                best, hint = 0.7, kw                         # all words in one sentence
    topic = content_tokens(" ".join(target.keywords + [target.title]))
    body = content_tokens(" ".join(draft.sentences))
    overlap = len(topic & body) / len(topic) if topic else 0.0
    return max(best, 0.6 * overlap), hint


def score_targets(targets: list[Target], draft: Draft, cfg: Config) -> list[Candidate]:
    w = cfg.get("weights")
    own = norm_url(draft.url) if draft.url else None
    existing = {norm_url(u) for u in draft.existing_links}
    own_kw = draft.primary_keyword.strip().lower()

    out = []
    for t in targets:
        if t.url == own or t.post_id == draft.post_id or t.url in existing:
            continue
        # Cannibalisation guard: never point this post at another page targeting the same keyword.
        if own_kw and t.type != "program" and own_kw in {k.lower() for k in t.keywords}:
            continue
        rel, hint = relevance(t, draft)
        if t.type != "program" and rel < w["min_relevance"]:
            continue
        match = category_match(t, draft.categories, cfg.adjacent)
        boost = w["orphan_boost_max"] * max(0.0, 1 - t.inbound / w["orphan_inbound_cap"])
        s = w["type"][t.type] * w["category"][match] * (0.3 + 0.7 * rel) + boost
        out.append(Candidate(t, round(s, 4), round(rel, 3), match, hint))
    return sorted(out, key=lambda c: c.score, reverse=True)


def target_link_count(word_count: int, cfg: Config) -> int:
    lk = cfg.get("linking")
    n = round(word_count / lk["words_per_link"])
    return max(lk["min_links"], min(lk["max_links"], n))


def new_link_budget(draft: Draft, cfg: Config) -> int:
    """Total internal-link budget for the article minus links the writer already added."""
    lk = cfg.get("linking")
    existing = draft.existing_internal + (draft.existing_external if lk.get("count_external_in_budget") else 0)
    return max(0, target_link_count(draft.word_count, cfg) - existing)


def plan(candidates: list[Candidate], draft: Draft, cfg: Config) -> tuple[int, list[Candidate]]:
    """Return (new links to place, shortlist for the LLM). The shortlist is the planned set first,
    then backups the LLM may use if a planned target has no natural spot."""
    lk = cfg.get("linking")
    n = new_link_budget(draft, cfg)
    if n == 0:
        return 0, []

    # Program links the writer already added count toward the program quota.
    prog_min = max(0, lk["program_links_min"] - draft.existing_program)
    prog_max = min(n, max(0, lk["program_links_max"] - draft.existing_program))

    programs = [c for c in candidates if c.target.type == "program"]
    own_programs = [c for c in programs if c.cat_match == "same"]
    other_programs = [c for c in programs if c.cat_match != "same"]
    blogs = [c for c in candidates if c.target.type != "program"]

    chosen: list[Candidate] = []
    for c in own_programs[:prog_max]:
        c.required = len(chosen) < prog_min
        chosen.append(c)
    # A further program slot only goes to another program if it is genuinely relevant.
    for c in other_programs:
        if len(chosen) >= prog_max:
            break
        if c.relevance >= 0.7:
            chosen.append(c)

    blog_slots = max(0, n - len(chosen))
    same_needed = math.ceil(lk["same_category_min_share"] * blog_slots)
    same = [c for c in blogs if c.cat_match == "same"]
    rest = [c for c in blogs if c.cat_match != "same"]
    picked = same[:same_needed]
    pool = sorted(same[same_needed:] + rest, key=lambda c: c.score, reverse=True)
    picked += pool[: blog_slots - len(picked)]
    chosen += sorted(picked, key=lambda c: c.score, reverse=True)

    used = {c.target.url for c in chosen}
    backups = [c for c in blogs if c.target.url not in used]
    shortlist = (chosen + backups)[: max(lk["max_candidates_for_llm"], len(chosen))]
    return min(n, len(shortlist)), shortlist
