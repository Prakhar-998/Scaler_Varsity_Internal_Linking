"""The linking pipeline for ONE article, with no I/O: no WordPress writes, no sheet writes.
Used by the scheduled runner (main.py) and by the preview tool (preview.py), so a preview shows
exactly what a real run would do."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import content, scoring, validate
from .config import Config
from .inventory import Target
from .llm import build_prompt


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


def propose(a: Analysis, llm, cfg: Config, link_attrs: str = "") -> Result:
    """Ask the LLM for edits, validate them, and build the new post content (not saved anywhere)."""
    prompt, ids = build_prompt(a.draft.title, a.n, a.shortlist, a.offered)
    checked = validate.check(llm.propose(prompt), ids, a.parsed, a.n, cfg.settings, a.offered)
    new_raw = content.apply_edits(a.parsed, checked.accepted, link_attrs) if checked.accepted else None
    return Result(checked, new_raw, prompt)
