"""Entry point: python -m linker.main [--post-id 123] [--dry-run]

One run:
 1. read the MasterSheet (read-only) for drafts that are ready and not yet linked
 2. rebuild the link inventory from WordPress + MasterSheet + config/programs.yaml
 3. for each draft: score targets -> plan -> Gemini proposes rewrites -> validate -> apply
 4. log every accepted/rejected edit to the control sheet; update the WP draft unless dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import traceback
from pathlib import Path

from . import content, scoring, validate
from .config import load_config
from .inventory import build_inventory
from .llm import Gemini, build_prompt
from .sheets import ControlSheet, MasterSheet, _now
from .wordpress import NotEditable, WordPress

OUT = Path("out")


def ready_rows(rows, settings, done: set[int], only: int | None):
    ok_status = set(settings["mastersheet"]["ready_content_statuses"])
    seen, out = set(), []
    for r in rows:
        pid = r.post_id
        if pid is None or pid in seen:
            continue
        if only is not None:
            if pid == only:
                seen.add(pid)
                out.append(r)
            continue
        if r.content_status in ok_status and r.publishing_status.lower() != "published" and pid not in done:
            seen.add(pid)
            out.append(r)
    return out


def process_post(row, wp, inventory, cat_names, llm, cfg, control, dry_run) -> dict:
    lk = cfg.get("linking")
    draft = wp.get_draft(row.post_id)
    if draft["status"] not in cfg.get("wordpress", "editable_statuses"):
        control.log_post(row.post_id, draft["title"], "", "skipped", 0, dry_run,
                         f"WP status is '{draft['status']}'")
        return {"post_id": row.post_id, "status": "skipped", "reason": draft["status"]}

    parsed = content.parse(draft["raw"], lk["skip_after_headings"], lk["skip_first_paragraphs"])
    cats = [cat_names.get(c, "") for c in draft["category_ids"]] or row.wp_categories
    cats = [c for c in cats if c and c.lower() != "uncategorized"] or row.wp_categories
    d = scoring.Draft(
        post_id=row.post_id, title=draft["title"], categories=cats,
        primary_keyword=row.primary_keyword,
        sentences=[s.text for s in parsed.sentences.values()],
        word_count=parsed.word_count, existing_links=parsed.existing_links,
    )

    cands = scoring.score_targets(inventory, d, cfg)
    n, shortlist = scoring.plan(cands, d, cfg)
    if n == 0 or not parsed.sentences:
        control.log_post(row.post_id, d.title, ", ".join(cats), "no_links", 0, dry_run,
                         "no candidates or no editable sentences")
        return {"post_id": row.post_id, "status": "no_links"}

    prompt, ids = build_prompt(d.title, n, shortlist, parsed)
    proposed = llm.propose(prompt)
    checked = validate.check(proposed, ids, parsed, n, cfg.settings)

    if checked.accepted:
        new_raw = content.apply_edits(parsed, checked.accepted)
        if not dry_run:
            wp.update_draft(row.post_id, new_raw)

    run_at = _now()
    change_rows = [[row.post_id, d.title, parsed.sentences[a["sentence_id"]].block, a["url"],
                    a["anchor"], a["candidate"].target.type, a["candidate"].score,
                    a["original"], a["rewritten"], dry_run, run_at] for a in checked.accepted]
    control.log_changes(change_rows)
    notes = checked.notes + [f"rejected {len(checked.rejected)}: " +
                             "; ".join(sorted({r for _, r in checked.rejected}))] if checked.rejected else checked.notes
    status = "linked" if checked.accepted else "no_links"
    control.log_post(row.post_id, d.title, ", ".join(cats), status, len(checked.accepted), dry_run,
                     " | ".join(notes))
    return {
        "post_id": row.post_id, "title": d.title, "status": status, "planned": n,
        "accepted": [{k: v for k, v in a.items() if k != "candidate"} for a in checked.accepted],
        "rejected": [{"edit": e, "reason": r} for e, r in checked.rejected],
        "notes": checked.notes,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--post-id", type=int, help="process just this WP post id (ignores 'done' state)")
    ap.add_argument("--dry-run", action="store_true", help="force dry run regardless of DRY_RUN env")
    args = ap.parse_args(argv)

    cfg = load_config()
    sec = cfg.secrets
    dry_run = args.dry_run or cfg.dry_run
    print(f"[run] dry_run={dry_run}")

    control = ControlSheet(sec["GOOGLE_SERVICE_ACCOUNT_JSON"], sec["CONTROL_SHEET_ID"])
    rows = MasterSheet(sec["GOOGLE_SERVICE_ACCOUNT_JSON"], sec["MASTERSHEET_ID"], cfg.settings).rows()
    todo = ready_rows(rows, cfg.settings, control.done_post_ids(), args.post_id)
    todo = todo[: cfg.get("run", "max_posts_per_run")]
    print(f"[run] {len(rows)} sheet rows, {len(todo)} drafts to link")
    if not todo:
        return

    wp = WordPress(cfg.get("wordpress", "base_url"), sec["WP_USERNAME"], sec["WP_APP_PASSWORD"],
                   cfg.get("wordpress", "editable_statuses"))
    cat_names = wp.categories()
    inventory = build_inventory(wp.published_posts(), cat_names, rows, cfg)
    print(f"[run] inventory: {len(inventory)} targets "
          f"({sum(t.type == 'program' for t in inventory)} program pages)")

    model = sec.get("GEMINI_MODEL") or cfg.get("llm", "default_model")
    llm = Gemini(sec["GEMINI_API_KEY"], model, cfg.get("llm", "temperature"))

    OUT.mkdir(exist_ok=True)
    report = []
    for row in todo:
        try:
            res = process_post(row, wp, inventory, cat_names, llm, cfg, control, dry_run)
        except NotEditable as e:
            res = {"post_id": row.post_id, "status": "skipped", "reason": str(e)}
        except Exception as e:  # one bad post must not stop the run
            traceback.print_exc()
            control.log_post(row.post_id, row.topic, "", "error", 0, dry_run, f"{type(e).__name__}: {e}")
            res = {"post_id": row.post_id, "status": "error", "reason": str(e)}
        print(f"[post {row.post_id}] {res['status']} " + str(len(res.get('accepted', []))) + " links")
        report.append(res)

    (OUT / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(f"## Internal linker ({'dry run' if dry_run else 'live'})\n\n| Post | Status | Links |\n|---|---|---|\n")
            for r in report:
                fh.write(f"| {r.get('title', r['post_id'])} | {r['status']} | {len(r.get('accepted', []))} |\n")


if __name__ == "__main__":
    main()
