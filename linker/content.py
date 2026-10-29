"""Find the sentences in a WordPress draft that are safe to rewrite, and apply edits back.

Works on the raw post content (Gutenberg block markup or classic HTML). Only plain sentences
inside <p> tags are offered for editing; anything the linker shouldn't touch is excluded:
headings, lists, tables, FAQ sections/blocks, the intro paragraph, sentences that already
contain a link or inline formatting.

The LLM never writes HTML. It returns plain text plus the anchor; we build the <a> tag here.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from .text import split_sentences

P_OR_H = re.compile(r"<(p|h[1-6])\b[^>]*>(.*?)</\1>", re.S | re.I)
FAQ_BLOCK = re.compile(r"<!--\s*wp:[\w-]+/faq[\w-]*.*?<!--\s*/wp:[\w-]+/faq[\w-]*\s*-->", re.S | re.I)
HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)


@dataclass
class Sentence:
    id: str          # "p4.s2"
    block: int       # paragraph index within the post
    text: str        # plain text, exactly as it appears (unescaped)
    html: str        # how it appears inside the paragraph's raw HTML


@dataclass
class Paragraph:
    index: int
    start: int       # offsets of the inner HTML within the raw content
    end: int
    inner: str


@dataclass
class ParsedDraft:
    raw: str
    paragraphs: dict[int, Paragraph]
    sentences: dict[str, Sentence]
    word_count: int
    existing_links: set[str]


def _in_ranges(pos: int, ranges) -> bool:
    return any(a <= pos < b for a, b in ranges)


def parse(raw: str, skip_headings: list[str], skip_first: int = 1, min_words: int = 8) -> ParsedDraft:
    faq_ranges = [m.span() for m in FAQ_BLOCK.finditer(raw)]
    skip_words = [s.lower() for s in skip_headings]

    paragraphs: dict[int, Paragraph] = {}
    sentences: dict[str, Sentence] = {}
    words = 0
    in_faq = False
    p_index = -1

    for m in P_OR_H.finditer(raw):
        tag, inner = m.group(1).lower(), m.group(2)
        text = html.unescape(BeautifulSoup(inner, "html.parser").get_text(" ", strip=True))
        if tag.startswith("h"):
            in_faq = any(w in text.lower() for w in skip_words)
            continue
        p_index += 1
        words += len(text.split())
        if in_faq or _in_ranges(m.start(), faq_ranges) or p_index < skip_first:
            continue
        para = Paragraph(p_index, m.start(2), m.end(2), inner)
        offered = False
        for s_i, sent in enumerate(split_sentences(text)):
            if len(sent.split()) < min_words:
                continue
            as_html = _locate(sent, inner)
            if as_html is None:          # sentence carries a link/formatting: leave it alone
                continue
            sid = f"p{p_index}.s{s_i}"
            sentences[sid] = Sentence(sid, p_index, sent, as_html)
            offered = True
        if offered:
            paragraphs[p_index] = para

    return ParsedDraft(raw, paragraphs, sentences, words, set(HREF.findall(raw)))


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
