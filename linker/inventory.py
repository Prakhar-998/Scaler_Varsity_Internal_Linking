"""The link inventory: every page a draft may link to, with its type, categories, anchor
variants and current inbound-link count. Rebuilt from live sources on every run."""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from .config import Config
from .sheets import SheetRow
from .text import norm_url
from .wordpress import PublishedPost


@dataclass
class Target:
    url: str                      # normalised key, used only for comparisons
    title: str
    type: str                     # "program" | "pillar" | "blog"
    categories: list[str]
    keywords: list[str]
    inbound: int = 0
    post_id: int | None = None
    program_id: str | None = None
    href: str = ""                # the exact URL written into the post (live canonical form)
    extras: dict = field(default_factory=dict)

    @property
    def link(self) -> str:
        return self.href or self.url


# Words that end the "topic" part of a blog title: "Pauli Gates Explained Simply ..." -> "pauli gates".
_TITLE_CUT = {"explained", "explain", "simply", "step", "made", "guide", "tutorial", "in", "for",
              "with", "better", "beginner", "beginners", "basics", "overview", "everything",
              "why", "how", "what", "when", "you", "your", "like", "-", "–", "—", "|"}
_TITLE_LEAD = {"the", "a", "an", "inside", "understanding", "intro", "introduction", "to", "best", "top"}


def _title_phrase(title: str) -> str | None:
    """Short topic phrase from a title, used as an anchor idea and for relevance matching.
    'The Hadamard Gate Explained Better Than Your Textbook' -> 'hadamard gate'
    'Best Quantum Computing Certifications in India (2026): ...' -> 'best quantum computing certifications'
    'Qubit vs Bit: The Real Difference ...' -> 'qubit vs bit'"""
    head = re.split(r"[:?(]", title, maxsplit=1)[0]
    words = [w.strip(",.!'’\"") for w in head.split()]
    while words and words[0].lower() in _TITLE_LEAD:
        words.pop(0)
    out = []
    for w in words:
        if w.lower() in _TITLE_CUT:
            break
        out.append(w)
    phrase = " ".join(out).strip().lower()
    return phrase if 1 <= len(phrase.split()) <= 6 else None


def _dedupe(items):
    seen, out = set(), []
    for it in items:
        key = (it or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(it.strip())
    return out


def inbound_counts(posts: list[PublishedPost]) -> dict[str, int]:
    """How many distinct published posts link to each URL (self-links excluded). Counted for
    every href, so program pages on another domain are covered too; lookups are by target URL."""
    sources = defaultdict(set)
    for p in posts:
        own = norm_url(p.url)
        for a in BeautifulSoup(p.content_html, "html.parser").find_all("a", href=True):
            u = norm_url(a["href"])
            if u and u != own:
                sources[u].add(p.id)
    return {u: len(s) for u, s in sources.items()}


def build_inventory(posts: list[PublishedPost], categories: dict[int, str],
                    sheet_rows: list[SheetRow], cfg: Config) -> list[Target]:
    inbound = inbound_counts(posts)

    kw_by_url: dict[str, list[str]] = defaultdict(list)
    for r in sheet_rows:
        if r.live_link and r.primary_keyword:
            kw_by_url[norm_url(r.live_link)].append(r.primary_keyword)

    targets: list[Target] = []
    for p in posts:
        url = norm_url(p.url)
        kws = kw_by_url.get(url, []) + cfg.keyword_overrides.get(url, [])
        tp = _title_phrase(p.title)
        if tp:
            kws.append(tp)
        targets.append(Target(
            url=url, title=p.title,
            type="pillar" if url in cfg.pillar_urls else "blog",
            categories=[categories.get(c, str(c)) for c in p.category_ids],
            keywords=_dedupe(kws), inbound=inbound.get(url, 0), post_id=p.id, href=p.url,
        ))

    for prog in cfg.programs:
        if not prog.url:
            print(f"[warn] program '{prog.id}' has no url in config/program_pages.csv; skipping it")
            continue
        if not prog.categories:
            print(f"[info] program '{prog.id}' has no categories; it is only linked where highly relevant")
        url = norm_url(prog.url)
        targets.append(Target(
            url=url, title=prog.name, type="program", categories=prog.categories,
            keywords=_dedupe(prog.keywords), inbound=inbound.get(url, 0), program_id=prog.id,
            href=prog.url,
        ))
    return targets
