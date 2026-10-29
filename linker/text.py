"""Small, dependency-free text helpers used for keyword matching."""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "with", "by", "at", "from",
    "is", "are", "be", "how", "what", "why", "your", "you", "it", "its", "this", "that", "vs",
    "explained", "guide", "2026", "2027", "india", "best", "top", "examples", "example",
}
_WORD = re.compile(r"[a-z0-9#+]+")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(text: str) -> list[str]:
    return [stem(w) for w in _WORD.findall(text.lower())]


def content_tokens(text: str) -> set[str]:
    return {t for t in tokens(text) if t not in STOPWORDS and len(t) > 1}


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text.strip()) if s.strip()]


def contains_phrase(sentence_tokens: list[str], phrase_tokens: list[str]) -> bool:
    n = len(phrase_tokens)
    if n == 0 or n > len(sentence_tokens):
        return False
    return any(sentence_tokens[i:i + n] == phrase_tokens for i in range(len(sentence_tokens) - n + 1))


def norm_url(url: str) -> str:
    """Comparable form of a URL: https, lowercase host, no query/fragment, trailing slash."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    path = parts.path or "/"
    if not path.endswith("/") and "." not in path.rsplit("/", 1)[-1]:
        path += "/"
    return urlunsplit(("https", parts.netloc.lower(), path, "", ""))


def similarity(a: str, b: str) -> float:
    """Token-level Dice similarity in [0, 1]; used to stop the LLM rewriting too much."""
    ta, tb = tokens(a), tokens(b)
    if not ta and not tb:
        return 1.0
    sa, sb = set(ta), set(tb)
    return 2 * len(sa & sb) / (len(sa) + len(sb))
