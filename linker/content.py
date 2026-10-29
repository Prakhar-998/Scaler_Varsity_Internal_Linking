"""Find the sentences in a WordPress draft that are safe to rewrite, and apply edits back.

Works on the raw post content (Gutenberg block markup or classic HTML). Only plain sentences
inside <p> tags are offered for editing; anything the linker shouldn't touch is excluded:
headings, lists, tables, FAQ sections/blocks, the intro paragraph, sentences that already
contain a link or inline formatting.

Link hygiene: every link the writer already added is recorded (internal vs external), so the
planner can count existing internal links toward the budget and keep new links away from
existing ones (no new link in or right next to a sentence that already has one).

The LLM never writes HTML. It returns plain text plus the anchor; we build the <a> tag here.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from urllib.parse import urljoin

from .text import norm_url, split_sentences

P_OR_H = re.compile(r"<(p|h[1-6])\b[^>]*>(.*?)</\1>", re.S | re.I)
FAQ_BLOCK = re.compile(r"<!--\s*wp:[\w-]+/faq[\w-]*.*?<!--\s*/wp:[\w-]+/faq[\w-]*\s*-->", re.S | re.I)
HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)


@dataclass
class Sentence:
    id: str          # "p4.s2"
    block: int       # paragraph index within the post
    text: str        # plain text, exactly as it appears (unescaped)
    html: str        # how it appears inside the paragraph's raw HTML
    order: int = 0   # position among ALL sentences of the post, used for link spacing


@dataclass
class Paragraph:
    index: int
    start: int       # offsets of the inner HTML within the raw content
    end: int
    inner: str


@dataclass
class ExistingLink:
    url: str         # normalised absolute URL
    anchor: str
    kind: str        # "internal" (Varsity blog / program page) | "external"


@dataclass
class ParsedDraft:
    raw: str
    paragraphs: dict[int, Paragraph]
    sentences: dict[str, Sentence]          # sentences the LLM may edit
    word_count: int
    existing: list[ExistingLink]            # every link the writer already put in the post
    linked_orders: set[int]                 # sentence positions that already carry a link
    para_link_count: dict[int, int]         # existing links per paragraph index

    @property
    def existing_links(self) -> set[str]:
        return {e.url for e in self.existing}

    @property
    def existing_internal(self) -> int:
        return sum(1 for e in self.existing if e.kind == "internal")

    @property
    def existing_external(self) -> int:
        return sum(1 for e in self.existing if e.kind == "external")


def _in_ranges(pos: int, ranges) -> bool:
    return any(a <= pos < b for a, b in ranges)


def resolve(href: str, site_base: str) -> str | None:
    """Absolute, normalised URL for an href (relative links resolved against the blog).
    None for links that aren't pages: in-page anchors, mailto:, tel:, javascript:."""
    href = (href or "").strip()
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return None
    return norm_url(urljoin(site_base, href))


def parse(raw: str, skip_headings: list[str], skip_first: int = 1, min_words: int = 8,
          site_base: str = "https://www.interviewbit.com/varsity/blog/",
          internal_prefixes: list[str] | None = None) -> ParsedDraft:
    internal_prefixes = [norm_url(p) for p in (internal_prefixes or [site_base])]
    faq_ranges = [m.span() for m in FAQ_BLOCK.finditer(raw)]
    skip_words = [s.lower() for s in skip_headings]

    # Every link anywhere in the post (lists, tables and FAQs included) counts for hygiene.
    existing: list[ExistingLink] = []
    for a in BeautifulSoup(raw, "html.parser").find_all("a", href=True):
        url = resolve(a["href"], site_base)
        if url is None:
            continue
        kind = "internal" if any(url.startswith(p) for p in internal_prefixes) else "external"
        existing.append(ExistingLink(url, a.get_text(" ", strip=True), kind))

    words = len(BeautifulSoup(raw, "html.parser").get_text(" ").split())

    paragraphs: dict[int, Paragraph] = {}
    sentences: dict[str, Sentence] = {}
    linked_orders: set[int] = set()
    para_link_count: dict[int, int] = {}
    in_faq = False
    p_index = -1
    order = -1

    for m in P_OR_H.finditer(raw):
        tag, inner = m.group(1).lower(), m.group(2)
        soup = BeautifulSoup(inner, "html.parser")
        text = html.unescape(soup.get_text(" ", strip=True))
        if tag.startswith("h"):
            in_faq = any(w in text.lower() for w in skip_words)
            order += 1                      # a heading breaks adjacency between paragraphs
            continue
        p_index += 1
        order += 1                          # a paragraph break also counts as separation
        anchors = [a.get_text(" ", strip=True) for a in soup.find_all("a", href=True)]
        anchors = [x for x in anchors if x]
        para_link_count[p_index] = len(anchors)
        skip_para = in_faq or _in_ranges(m.start(), faq_ranges) or p_index < skip_first
        para = Paragraph(p_index, m.start(2), m.end(2), inner)
        offered = False
        for s_i, sent in enumerate(split_sentences(text)):
            order += 1
            if any(x in sent for x in anchors):
                linked_orders.add(order)      # sentence already has a link
                continue
            if skip_para or len(sent.split()) < min_words:
                continue
            as_html = _locate(sent, inner)
            if as_html is None:               # sentence carries formatting: leave it alone
                continue
            sid = f"p{p_index}.s{s_i}"
            sentences[sid] = Sentence(sid, p_index, sent, as_html, order)
            offered = True
        if offered:
            paragraphs[p_index] = para

    return ParsedDraft(raw, paragraphs, sentences, words, existing, linked_orders, para_link_count)


def eligible_sentences(parsed: ParsedDraft, gap: int, max_total_per_para: int) -> dict[str, Sentence]:
    """Sentences that may receive a NEW link: not within `gap` sentences of an existing link,
    and in a paragraph that hasn't already reached its total link cap."""
    out = {}
    for sid, s in parsed.sentences.items():
        if any(abs(s.order - o) <= gap for o in parsed.linked_orders):
            continue
        if parsed.para_link_count.get(s.block, 0) >= max_total_per_para:
            continue
        out[sid] = s
    return out


def _locate(sentence: str, inner: str) -> str | None:
    """Return the sentence as it is written in the paragraph HTML, if it appears there as plain
    text (no tags inside it). Tries the literal text, then the HTML-escaped form."""
    for form in (sentence, html.escape(sentence, quote=False)):
        if form in inner:
            return form
    return None


def link_html(rewritten: str, anchor: str, url: str) -> str:
    esc = html.escape(rewritten, quote=False)
    esc_anchor = html.escape(anchor, quote=False)
    i = esc.find(esc_anchor)
    if i < 0:
        raise ValueError("anchor not in rewritten sentence")
    a = f'<a href="{html.escape(url, quote=True)}">{esc_anchor}</a>'
    return esc[:i] + a + esc[i + len(esc_anchor):]


def apply_edits(parsed: ParsedDraft, edits: list[dict]) -> str:
    """edits: [{sentence_id, anchor, rewritten, url}] that already passed validation."""
    new_inner = {i: p.inner for i, p in parsed.paragraphs.items()}
    for e in edits:
        s = parsed.sentences[e["sentence_id"]]
        replacement = link_html(e["rewritten"], e["anchor"], e["url"])
        if s.html not in new_inner[s.block]:
            raise ValueError(f"sentence {s.id} no longer found in its paragraph")
        new_inner[s.block] = new_inner[s.block].replace(s.html, replacement, 1)

    raw = parsed.raw
    for i in sorted(new_inner, reverse=True):       # back to front keeps offsets valid
        p = parsed.paragraphs[i]
        if new_inner[i] != p.inner:
            raw = raw[:p.start] + new_inner[i] + raw[p.end:]
    return raw
