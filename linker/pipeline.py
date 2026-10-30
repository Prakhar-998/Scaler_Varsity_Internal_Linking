"""The linking pipeline for ONE article, with no I/O: no WordPress writes, no sheet writes.
Used by the scheduled runner (main.py) and by the preview tool (preview.py), so a preview shows
exactly what a real run would do."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import content, scoring, validate
from .config import Config
from .inventory import Target
from .llm import build_prompt
from .text import contains_phrase, content_tokens, tokens


@dataclass
class Analysis:
    parsed: content.ParsedDraft
    draft: scoring.Draft
    candidates: list[scoring.Candidate]
    n: int                                   # new links to place
    shortlist: list[scoring.Candidate]
    offered: dict                            # sentences eligible for a new link
    budget: int                              # total internal-link budget for the article

    @property
    def skip_reason(self) -> str | None:
        if self.n == 0:
            if scoring.new_link_budget(self.draft, self._cfg) == 0:
                return "internal link budget already met by existing links"
            return "no relevant link targets"
        if not self.offered:
            return "no eligible sentences (all too close to existing links, or none plain enough)"
        return None

    @property
    def existing_note(self) -> str:
        d = self.draft
        return (f"existing links: {d.existing_internal} internal, {d.existing_external} external; "
                f"budget {self.budget}")

    _cfg: Config = field(default=None, repr=False)


@dataclass
class Result:
    checked: validate.Checked
    new_raw: str | None
    prompt: str


def analyse(raw: str, title: str, categories: list[str], primary_keyword: str, post_id: int,
            inventory: list[Target], cfg: Config, url: str = "") -> Analysis:
    lk = cfg.get("linking")
    program_urls = {t.url for t in inventory if t.type == "program"}
    prefixes = list(cfg.get("wordpress", "internal_link_prefixes", default=[])) + sorted(program_urls)
    parsed = content.parse(raw, lk["skip_after_headings"], lk["skip_first_paragraphs"],
                           site_base=cfg.get("wordpress", "base_url").rstrip("/") + "/",
                           internal_prefixes=prefixes)
    d = scoring.Draft(
        post_id=post_id, title=title, categories=categories, primary_keyword=primary_keyword,
        sentences=[s.text for s in parsed.sentences.values()],
        word_count=parsed.word_count, existing_links=parsed.existing_links, url=url,
        existing_internal=parsed.existing_internal, existing_external=parsed.existing_external,
        existing_program=sum(1 for e in parsed.existing if e.url in program_urls),
    )
    cands = scoring.score_targets(inventory, d, cfg)
    n, shortlist = scoring.plan(cands, d, cfg)
    offered = content.eligible_sentences(parsed, lk["min_sentence_gap"], lk["max_total_links_per_paragraph"])
    return Analysis(parsed, d, cands, n, shortlist, offered,
                    scoring.target_link_count(d.word_count, cfg), _cfg=cfg)


def select_sentences(offered: dict, shortlist: list, limit: int) -> dict:
    """Keep the prompt small: the sentences most likely to carry one of the shortlisted links,
    plus a spread of others (program links often fit a general sentence), in article order."""
    if len(offered) <= limit:
        return offered
    topics = []
    for c in shortlist:
        topics.append((content_tokens(" ".join(c.target.keywords + [c.target.title])),
                       [tokens(k) for k in c.target.keywords]))
    scored = []
    for sid, s in offered.items():
        st, toks = content_tokens(s.text), tokens(s.text)
        best = 0.0
        for topic, phrases in topics:
            if any(contains_phrase(toks, ph) for ph in phrases if ph):
                best = max(best, 2.0)
            elif topic:
                best = max(best, len(st & topic) / len(topic))
        scored.append((best, sid))
    top_n = max(1, int(limit * 0.75))
    keep = {sid for _, sid in sorted(scored, key=lambda x: -x[0])[:top_n]}
    rest = [sid for sid in offered if sid not in keep]
    step = max(1, len(rest) // max(1, limit - len(keep)))
    keep.update(rest[::step][: limit - len(keep)])
    return {sid: s for sid, s in offered.items() if sid in keep}


def propose(a: Analysis, llm, cfg: Config, link_attrs: str = "") -> Result:
    """Ask the LLM for edits, validate them, and build the new post content (not saved anywhere)."""
    offered = select_sentences(a.offered, a.shortlist, cfg.get("llm", "max_sentences_for_llm", default=40))
    prompt, ids = build_prompt(a.draft.title, a.n, a.shortlist, offered)
    checked = validate.check(llm.propose(prompt), ids, a.parsed, a.n, cfg.settings, offered)
    new_raw = content.apply_edits(a.parsed, checked.accepted, link_attrs) if checked.accepted else None
    return Result(checked, new_raw, prompt)
