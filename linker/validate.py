"""Deterministic checks on every LLM edit. An edit that fails any check is dropped and logged;
the LLM can never introduce a URL, touch a sentence it wasn't offered, or rewrite too much."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .content import ParsedDraft
from .scoring import Candidate
from .text import STOPWORDS, content_tokens, similarity, split_sentences, tokens

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


# Nouns that continue a phrase: "generative AI | roadmap", "agentic AI | course". An anchor that
# stops right before one of these links half a phrase ("generative AI" inside "generative AI roadmap").
CONTINUATION = {"roadmap", "roadmaps", "course", "courses", "project", "projects", "engineer", "engineers",
                "engineering", "model", "models", "tool", "tools", "framework", "frameworks", "application",
                "applications", "certification", "certifications", "job", "jobs", "role", "roles", "interview",
                "interviews", "question", "questions", "system", "systems", "skill", "skills", "guide", "guides",
                "program", "programme", "programs", "programmes", "platform", "platforms", "stack", "pipeline",
                "pipelines", "workflow", "workflows", "strategy", "strategies", "tutorial", "salary", "salaries",
                "team", "teams", "market", "adoption", "ideas", "idea", "career", "careers", "agent", "agents"}


def cut_mid_phrase(sentence: str, anchor: str) -> bool:
    i = sentence.find(anchor)
    if i < 0:
        return False
    nxt = re.match(r"\s+([A-Za-z]+)", sentence[i + len(anchor):])
    return bool(nxt) and nxt.group(1).lower() in CONTINUATION


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
    # The anchor's main word (its last content word: "logistics COSTS", "operations MANAGER") must be
    # the target's topic. One shared side word isn't enough: "logistics costs" or "logistics
    # executives" is not an anchor for a page about next-gen logistics.
    ordered = [t for t in tokens(anchor) if t not in STOPWORDS and len(t) > 1]
    if len(ordered) >= 2 and ordered[-1] not in target_tok:
        return "anchor's main word doesn't describe the target page"
    own_bi = _bigrams(tokens(own_text))
    for bi in _bigrams(tokens(anchor)):
        if bi in own_bi and not set(bi) <= target_tok:
            return "anchor uses this article's own topic for another page"
    return None


def _word_count(anchor: str) -> int:
    """Hyphenated terms count per part: "prompt-injection" is a 2-word anchor, as the writer wrote it."""
    return len([w for w in re.split(r"[\s\-–]+", anchor) if w])


def _ok_single_word(anchor: str, target, common: set) -> bool:
    """One-word anchors are fine for distinctive terms that ARE the target's topic:
    "RLHF", "Quantization", "Perplexity" - but not "AI", "models" or "data"."""
    words = anchor.split()
    if len(words) != 1 or _word_count(anchor) != 1:
        return False
    w = words[0].strip(".,;:()'\"’")
    tok = content_tokens(w)
    if not tok or tok & common:
        return False
    acronym = w.isupper() and len(w) >= 3
    if not (acronym or len(w) >= 5):
        return False
    topic = content_tokens(" ".join(target.keywords[:3] + [target.title.split(":")[0]]))
    return bool(tok & topic)


def _added_words(original: str, rewritten: str, anchor: str) -> set[str]:
    """Content words the rewrite introduced that are neither in the writer's sentence nor in the
    anchor - e.g. "Popular", "Techniques like", "in NLP benchmarks". Blog links allow none."""
    return content_tokens(rewritten) - content_tokens(original) - content_tokens(anchor)


LEARNING_WORDS = {"course", "courses", "programme", "programmes", "program", "programs", "certification",
                  "certifications", "certificate", "certificates", "training", "upskill", "upskilling",
                  "learn", "learning", "learner", "learners", "study", "studying", "degree", "curriculum",
                  "bootcamp", "career", "careers", "syllabus", "cohort"}


def _pure_insertion(original: str, rewritten: str) -> bool:
    """True if `rewritten` is `original` with one span inserted and nothing else changed."""
    if len(rewritten) < len(original):
        return False
    p = 0
    while p < len(original) and original[p] == rewritten[p]:
        p += 1
    q = 0
    while q < len(original) - p and original[-1 - q] == rewritten[-1 - q]:
        q += 1
    return p + q >= len(original)


def program_problem(original: str, rewritten: str, anchor: str, target, max_added: int) -> str | None:
    """Program links may add a short phrase, but only where the writer is already talking about
    learning/courses (or the programme's own topic), and only by inserting - never by replacing."""
    if rewritten == original:
        return None
    if max_added <= 0:
        return "program links must use the writer's own words (no inserted phrase)"
    words = {w.lower() for w in re.findall(r"[A-Za-z]+", original)}
    topic = content_tokens(" ".join(target.keywords[:2])) - {"programme", "program", "course", "certification"}
    if not (words & LEARNING_WORDS or content_tokens(original) & topic):
        return "program link in a sentence that isn't about learning or the programme's topic"
    if not _pure_insertion(original, rewritten):
        return "program link changed the writer's words (only inserting a short phrase is allowed)"
    if len(_added_words(original, rewritten, "")) > max_added:
        return "program link inserted too many words"
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
        if s is not None and anchor:
            low = s.text.lower()
            if low.count(anchor.lower()) == 1:
                # The anchor is already in the writer's sentence (any casing): keep the sentence
                # exactly as written and link the writer's own words.
                i = low.index(anchor.lower())
                anchor, rewritten = s.text[i:i + len(anchor)], s.text

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
        elif not (2 <= _word_count(anchor) <= 8 or _ok_single_word(anchor, cand.target, common)):
            reason = "anchor length out of range"
        elif anchor.lower() in GENERIC_ANCHORS:
            reason = "generic anchor"
        elif (bad := anchor_problem(anchor, cand.target, own_text)):
            reason = bad
        elif content_tokens(anchor) and any(content_tokens(anchor) == content_tokens(a["anchor"]) for a in accepted):
            reason = "same anchor words already link to another page"
        elif any(_overlap(content_tokens(anchor) - common, content_tokens(a["anchor"]) - common) >= 0.6
                 for a in accepted):
            reason = "anchor too similar to another new link"
        elif cand.target.type != "program" and lk.get("blog_anchor_verbatim", True) and rewritten != s.text:
            reason = "blog links must use the writer's own words (anchor isn't in the sentence)"
        elif cand.target.type != "program" and _share_in(anchor, s.text) < 0.5:
            reason = "anchor topic isn't in the original sentence (forced insertion)"
        elif cand.target.type != "program" and _added_words(s.text, rewritten, anchor):
            reason = "rewrite adds words beyond the anchor (" + ", ".join(sorted(_added_words(s.text, rewritten, anchor))) + ")"
        elif cand.target.type == "program" and (bad := program_problem(s.text, rewritten, anchor, cand.target,
                                                                       lk.get("program_max_added_words", 0))):
            reason = bad
        elif any((content_tokens(anchor) & content_tokens(a["anchor"])) - common
                 for a in accepted):
            reason = "anchor repeats a term already linked in this article"
        elif rewritten.count(anchor) != 1:
            reason = "anchor not present exactly once in rewrite"
        elif cut_mid_phrase(rewritten, anchor):
            reason = "anchor cuts a phrase in half (e.g. 'generative AI' in 'generative AI roadmap')"
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


# ---------- programme bridge sentence: one NEW sentence appended to a paragraph ----------

BRIDGE_BANNED = {"best", "top", "leading", "guarantee", "guaranteed", "placement", "placements", "salary",
                 "salaries", "click", "premier", "world", "class", "unmatched", "hike", "assured"}


def bridge_problem(sentence: str, anchor: str, target, last_sentence: str, max_words: int) -> str | None:
    """The added sentence must be short, plain, claim-free, and carry one of the programme's own
    anchor phrases (program_pages.csv `anchors`) exactly once."""
    allowed = [a.lower() for a in target.extras.get("bridge_anchors", []) or target.keywords]
    if anchor.lower() not in allowed:
        return "added sentence must use one of the programme's anchor phrases"
    if sentence.count(anchor) != 1:
        return "anchor not present exactly once in the added sentence"
    words = len(sentence.split())
    if not 10 <= words <= max_words:
        return "added sentence length out of range"
    if len(split_sentences(sentence)) != 1 or not sentence.rstrip().endswith("."):
        return "added text must be one plain sentence"
    if any(ch.isdigit() for ch in sentence) or "%" in sentence:
        return "added sentence contains numbers"
    if any(ch in sentence for ch in "<>[]#*"):
        return "markup in added sentence"
    if {w.lower() for w in re.findall(r"[A-Za-z]+", sentence)} & BRIDGE_BANNED:
        return "added sentence makes a promotional claim"
    if last_sentence and similarity(sentence, last_sentence) > 0.7:
        return "added sentence repeats the paragraph"
    return None
