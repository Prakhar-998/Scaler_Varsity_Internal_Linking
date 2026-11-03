"""The linking pipeline for ONE article, with no I/O: no WordPress writes, no sheet writes.
Used by the scheduled runner (main.py) and by the preview tool (preview.py), so a preview shows
exactly what a real run would do."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import content, scoring, validate
from .config import Config
from .inventory import Target
from .llm import build_bridge_prompt, build_prompt
from .text import contains_phrase, content_tokens, norm_url, tokens


@dataclass
class Analysis:
    parsed: content.ParsedDraft
    draft: scoring.Draft
    candidates: list[scoring.Candidate]
    n: int                                   # new links to place
    shortlist: list[scoring.Candidate]
    offered: dict                            # sentences eligible for a new link
    budget: int                              # total internal-link budget for the article
    common_words: set = field(default_factory=set)  # words in many titles ("quantum"): not distinctive

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
                    scoring.target_link_count(d.word_count, cfg), common_words(inventory), _cfg=cfg)


def common_words(inventory: list[Target], share: float = 0.02, floor: int = 4) -> set[str]:
    """Words that appear in many page titles (e.g. "quantum", "ai") are not distinctive enough to
    treat two anchors as "the same term". Learned from the blog's own titles on every run."""
    df: dict[str, int] = {}
    for t in inventory:
        for w in content_tokens(t.title):
            df[w] = df.get(w, 0) + 1
    cutoff = max(floor, int(share * len(inventory)))
    return {w for w, n in df.items() if n >= cutoff}


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


def keyword_seeds(a: Analysis, ids: dict) -> list[dict]:
    """Deterministic first pass, no LLM: wherever a shortlisted page's own keyword phrase already
    appears in an eligible sentence ("Quantum Fourier Transform", "ABC analysis", "generative AI"),
    propose linking exactly those words. Highest-priority targets first; the validator decides."""
    seeds = []
    sentences = sorted(a.offered.values(), key=lambda s: s.order)
    for tid, cand in ids.items():
        kws = sorted({k for k in cand.target.keywords if k}, key=lambda k: -len(k))   # longest phrase first
        if cand.target.type == "program":
            bridge = {b.lower() for b in cand.target.extras.get("bridge_anchors", [])}
            kws = [k for k in kws if k.lower() not in bridge] or kws
        found = 0
        for s in sentences:
            for kw in kws:
                m = re.search(r"(?<![\w-])" + re.escape(kw) + r"s?(?![\w-])", s.text, re.I)
                if m:
                    seeds.append({"sentence_id": s.id, "target_id": tid, "anchor": m.group(0),
                                  "rewritten": s.text, "seed": True})
                    found += 1
                    break
            if found >= 3:                 # a few alternatives per page is plenty
                break
    return seeds


def propose(a: Analysis, llm, cfg: Config, link_attrs: str = "") -> Result:
    """1. link the pages whose keyword already appears in the article (no LLM);
    2. ask the LLM for the remaining slots; 3. one more round for anything still missing
    (with the validator's reasons); 4. the programme's added sentence if it's still unlinked."""
    offered = select_sentences(a.offered, a.shortlist, cfg.get("llm", "max_sentences_for_llm", default=40))
    prompt, ids = build_prompt(a.draft.title, a.n, a.shortlist, offered,
                               cfg.get("linking", "program_max_added_words", default=0))
    own_text = f"{a.draft.title} {a.draft.primary_keyword}"

    def run_check(edits):
        return validate.check(edits, ids, a.parsed, a.n, cfg.settings, a.offered, own_text, a.common_words)

    seeded = run_check(keyword_seeds(a, ids))
    checked = validate.Checked(seeded.accepted, [], [])     # unused seed alternatives aren't "rejections"
    if seeded.accepted:
        print(f"[link] {len(seeded.accepted)} link(s) on keywords already in the text", flush=True)
    all_rejected, note_llm = [], None
    rounds = 1 + cfg.get("llm", "repair_rounds", default=1)
    for i in range(rounds):
        missing = a.n - len(checked.accepted)
        if missing <= 0:
            break
        kept = [e["raw"] for e in checked.accepted]
        ask = prompt if not kept and i == 0 else prompt + "\n\n" + repair_note(checked, ids, missing)
        print(f"[llm] asking for up to {missing} more link(s)", flush=True)
        try:
            more = llm.propose(ask)
        except Exception as ex:
            if not kept:
                raise                      # nothing placed: fail the post so it's retried later
            print(f"[llm] unavailable ({type(ex).__name__}); keeping the keyword links", flush=True)
            note_llm = f"LLM unavailable ({type(ex).__name__}): keyword links only"
            break
        checked = run_check(kept + more)
        all_rejected += [(e, r + (" (2nd pass)" if i else "")) for e, r in checked.rejected]
        if i and not checked.rejected and len(checked.accepted) == len(kept):
            break                          # the LLM found nothing more: stop asking
    checked = validate.check([e["raw"] for e in checked.accepted], ids, a.parsed, a.n, cfg.settings,
                             a.offered, own_text, a.common_words)   # recompute notes on the final set
    checked.rejected = all_rejected
    if note_llm:
        checked.notes.append(note_llm)
    add_bridge(a, checked, llm, cfg)
    new_raw = content.apply_edits(a.parsed, checked.accepted, link_attrs) if checked.accepted else None
    return Result(checked, new_raw, prompt)


def _norm(t: str) -> str:
    return " ".join(t.split())


def bridge_paragraphs(a: Analysis, accepted: list[dict], cand, cfg: Config) -> dict[str, tuple]:
    """Paragraphs that may get the programme's added sentence: {pid: (block, fit, text, last_sentence)}.
    Up to 4 "BEST FIT" paragraphs (about learning, skills or the programme's topic) plus the
    conclusion as "LAST RESORT". Same spacing rules as any other link: no links already in the
    paragraph, none in or next to its last sentence."""
    gap = cfg.get("linking", "min_sentence_gap", default=1)
    parsed = a.parsed
    used = {e.get("block", parsed.sentences[e["sentence_id"]].block if e["sentence_id"] in parsed.sentences else -1)
            for e in accepted}
    linked = set(parsed.linked_orders) | {parsed.sentences[e["sentence_id"]].order for e in accepted
                                          if not e.get("added") and e["sentence_id"] in parsed.sentences}
    topic = content_tokens(" ".join(cand.target.keywords))
    options = []
    for b in sorted({s.block for s in a.offered.values()}):
        if b in used or parsed.para_link_count.get(b, 0) > 0:
            continue
        p = parsed.paragraphs[b]
        last = content.last_sentence(p)
        tail = max((s for s in parsed.sentences.values() if s.block == b), key=lambda s: s.order)
        if _norm(tail.text) != _norm(last) or tail.id not in a.offered:
            continue                       # paragraph ends in a formatted/short sentence: leave it
        if any(abs(tail.order + 0.5 - o) <= gap for o in linked):
            continue
        text = content.paragraph_text(p)
        words = {w.lower() for w in text.split()}
        score = len(content_tokens(text) & topic) + 2 * len(words & validate.LEARNING_WORDS)
        options.append((b, score, text, last))
    if not options:
        return {}
    conclusion = options[-1]
    best = sorted([o for o in options[:-1] if o[1] > 0], key=lambda o: -o[1])[:4]
    chosen = sorted(best, key=lambda o: o[0]) + [conclusion]
    return {f"B{o[0]}": (o[0], "LAST RESORT" if o is conclusion else "BEST FIT", o[2][:700], o[3])
            for o in chosen}


def _bridge_edit(e: dict, cand, paras: dict, a: Analysis, cfg: Config):
    """Validate one proposed added sentence; returns (accepted_edit, None) or (None, reason)."""
    pid, sentence = str(e.get("paragraph_id", "")), _norm(str(e.get("sentence", "")))
    anchor = str(e.get("anchor", "")).strip()
    if pid not in paras:
        return None, "unknown or ineligible paragraph for the added sentence"
    i = sentence.lower().find(anchor.lower()) if anchor else -1
    if i >= 0:
        anchor = sentence[i:i + len(anchor)]          # use the sentence's own casing
    b, _, _, last = paras[pid]
    bad = validate.bridge_problem(sentence, anchor, cand.target, last,
                                  cfg.get("linking", "bridge_max_words", default=30))
    if bad:
        return None, bad
    return {"sentence_id": f"p{b}.add", "block": b, "added": True, "anchor": anchor, "rewritten": sentence,
            "original": content.ADDED_MARK + last, "url": cand.target.link, "candidate": cand, "raw": e}, None


def bridge_target(a: Analysis, accepted: list[dict], cfg: Config):
    """The programme that should get an added sentence, if any: the post's own (required)
    programme, when no programme link was placed on the writer's words and the cap allows one more."""
    if not cfg.get("linking", "program_bridge_sentence", default=False):
        return None, None
    if any(e["candidate"].target.type == "program" for e in accepted):
        return None, None
    req = [c for c in a.shortlist if c.required and c.target.type == "program"]
    if not req:
        return None, None
    if a.draft.existing_internal + len(accepted) + 1 > cfg.get("linking", "max_links"):
        return None, "programme sentence skipped: article already at the link cap"
    return req[0], None


def add_bridge(a: Analysis, checked: validate.Checked, llm, cfg: Config) -> None:
    """Last step for the programme link: if it couldn't go on the writer's own words, ask for ONE
    short added sentence at the end of a fitting paragraph (the conclusion only as a last resort)."""
    cand, note = bridge_target(a, checked.accepted, cfg)
    if note:
        checked.notes.append(note)
    if cand is None:
        return
    paras = bridge_paragraphs(a, checked.accepted, cand, cfg)
    if not paras:
        checked.notes.append("programme sentence skipped: no paragraph with room for it")
        return
    bridge_anchors = cand.target.extras.get("bridge_anchors") or cand.target.keywords
    prompt = build_bridge_prompt(a.draft.title, cand.target.title, bridge_anchors,
                                 [(pid, fit, text) for pid, (_, fit, text, _) in paras.items()],
                                 cfg.get("linking", "bridge_max_words", default=30))
    try:
        proposals = llm.propose(prompt)[:2]
    except Exception as ex:          # the main links are already validated: never lose them over this
        checked.notes.append(f"programme sentence skipped: {type(ex).__name__}")
        return
    for e in proposals:
        edit, reason = _bridge_edit(e, cand, paras, a, cfg)
        if edit:
            checked.accepted.append(edit)
            checked.notes = [n for n in checked.notes if n != "required program link not placed"]
            checked.notes.append("programme link added as a new sentence")
            return
        checked.rejected.append(({"anchor": e.get("anchor", ""), "rewritten": e.get("sentence", "")},
                                 f"added sentence: {reason}"))


def replay(a: Analysis, reviewed: list[dict], cfg: Config) -> Result:
    """Re-apply links someone reviewed in a dry run (rows from the Changes tab), exactly as proposed,
    without asking the LLM again. Each one is re-checked against the post as it is NOW and against
    the current rules; anything that no longer fits is dropped with a reason, never re-worded."""
    by_text = {" ".join(s.text.split()): s for s in a.parsed.sentences.values()}
    by_url = {}
    for c in a.candidates:
        by_url.setdefault(c.target.url, c)
        by_url.setdefault(norm_url(c.target.link), c)
    ids, edits, pre_rejected = {}, [], []
    added_rows = []
    for i, row in enumerate(reviewed, 1):
        e = {"anchor": str(row.get("anchor", "")), "rewritten": str(row.get("rewritten_sentence", "")),
             "target_url": str(row.get("target_url", ""))}
        if str(row.get("original_sentence", "")).startswith(content.ADDED_MARK):
            added_rows.append((row, e))
            continue
        s = by_text.get(" ".join(str(row.get("original_sentence", "")).split()))
        cand = by_url.get(norm_url(e["target_url"]))
        if s is None:
            pre_rejected.append((e, "sentence changed since the dry run"))
        elif cand is None:
            pre_rejected.append((e, "target no longer allowed for this post"))
        else:
            ids[f"R{i}"] = cand
            edits.append({**e, "sentence_id": s.id, "target_id": f"R{i}"})
    own_text = f"{a.draft.title} {a.draft.primary_keyword}"
    budget = scoring.new_link_budget(a.draft, cfg)
    checked = validate.check(edits, ids, a.parsed, budget, cfg.settings, a.offered, own_text, a.common_words)
    checked.rejected = pre_rejected + checked.rejected
    checked.notes = []                    # "required programme link" is a planning note, not a replay one
    for row, e in added_rows:             # the reviewed added sentence: same paragraph, same words
        cand = by_url.get(norm_url(e["target_url"]))
        after = _norm(str(row.get("original_sentence", ""))[len(content.ADDED_MARK):])
        paras = bridge_paragraphs(a, checked.accepted, cand, cfg) if cand else {}
        pid = next((k for k, v in paras.items() if _norm(v[3]) == after), None)
        if cand is None:
            checked.rejected.append((e, "target no longer allowed for this post"))
        elif pid is None:
            checked.rejected.append((e, "paragraph for the added sentence changed or now has a link"))
        elif len(checked.accepted) >= budget + 1:
            checked.rejected.append((e, "over link budget"))
        else:
            edit, reason = _bridge_edit({"paragraph_id": pid, "sentence": e["rewritten"], "anchor": e["anchor"]},
                                        cand, paras, a, cfg)
            if edit:
                checked.accepted.append(edit)
            else:
                checked.rejected.append((e, f"added sentence: {reason}"))
    new_raw = content.apply_edits(a.parsed, checked.accepted) if checked.accepted else None
    return Result(checked, new_raw, "")


def repair_note(checked, ids, missing: int) -> str:
    tid_of = {id(c): t for t, c in ids.items()}
    placed = "; ".join(f'{tid_of.get(id(e["candidate"]), "?")} in {e["sentence_id"]} ("{e["anchor"]}")'
                       for e in checked.accepted) or "none"
    bad = "\n".join(f'- {e.get("target_id", "?")} / {e.get("sentence_id", "?")} anchor "{e.get("anchor", "")}": {r}'
                     for e, r in checked.rejected)
    return (f"FOLLOW-UP. Already placed (keep, don't repeat these targets or sentences): {placed}.\n"
            f"Rejected by the validator:\n{bad or '- none'}\n"
            f"Propose up to {missing} NEW edits that fix these problems, using other targets or other "
            "sentences if needed. Skip rather than force. Return JSON in the same format with only the new edits.")


def hygiene_notes(a: Analysis, cfg: Config) -> list[str]:
    """Findings worth showing the team even when no link is added."""
    notes = []
    known = {c for p in cfg.programs for c in p.categories} | set(cfg.adjacent)
    unknown = [c for c in a.draft.categories if c not in known]
    if unknown:
        notes.append(f"category not in config (no programme / adjacency): {', '.join(unknown)}")
    for url, count in a.parsed.duplicate_internal.items():
        notes.append(f"writer links {url} {count}x (image/banner links included)")
    return notes
