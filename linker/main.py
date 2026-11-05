"""Entry point: python -m linker.main [--post-id 123 | --event-post-id 123] [--dry-run]

One run:
 1. check posts the bot linked earlier that are now published: are its links still there?
 2. read the MasterSheet (read-only) for drafts that are ready and not yet linked
 3. rebuild the link inventory from WordPress + MasterSheet + config/program_pages.csv
 4. for each draft: skip if someone edited it in the last few minutes; score targets -> plan ->
    Gemini -> validate -> save, but only if nobody saved the draft while we worked
 5. log every accepted/rejected edit to the control sheet
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import pipeline
from .config import load_config
from .inventory import build_inventory
from .llm import gemini_from_config
from .sheets import ControlSheet, MasterSheet, SheetRow, _now
from .text import norm_url
from .wordpress import Conflict, NotEditable, WordPress

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


def category_names(ids, cat_names: dict, fallback: list[str]) -> list[str]:
    cats = [cat_names.get(c, "") for c in ids]
    return [c for c in cats if c and c.lower() != "uncategorized"] or list(fallback)


def _parse_gmt(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "")).replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def recently_edited(post: dict, minutes: int, now: datetime | None = None) -> int | None:
    """Minutes since the post was last saved, if that's within the cool-off window."""
    if minutes <= 0:
        return None
    modified = _parse_gmt(post.get("modified_gmt", ""))
    if modified is None:
        return None
    age = (now or datetime.now(timezone.utc)) - modified
    return int(age.total_seconds() // 60) if age < timedelta(minutes=minutes) else None


def process_post(row, wp, inventory, cat_names, llm, cfg, control, dry_run, *,
                 skip_cooloff: bool = False, mode: str = "draft", allow_published: bool = False,
                 reviewed: list[dict] | None = None) -> dict:
    """`reviewed`: apply these dry-run rows (Changes tab) exactly, instead of asking the LLM."""
    draft = wp.get_draft(row.post_id)
    allowed = set(cfg.get("wordpress", "editable_statuses")) | ({"publish"} if allow_published else set())
    if draft["status"] not in allowed:
        control.log_post(row.post_id, draft["title"], "", "skipped", 0, dry_run,
                         f"WP status is '{draft['status']}'", mode=mode, url=draft.get("url", ""))
        return {"post_id": row.post_id, "status": "skipped", "reason": draft["status"]}

    cooloff = cfg.get("run", "cooloff_minutes", default=10)
    age = None if skip_cooloff else recently_edited(draft, cooloff)
    if age is not None:
        # Someone saved it minutes ago and may still have it open: come back next run.
        # Not written to the sheet (it would add a row every 30 minutes); the run log shows it.
        return {"post_id": row.post_id, "title": draft["title"], "status": "waiting",
                "reason": f"edited {age} min ago; waiting {cooloff} min before touching it"}

    cats = category_names(draft["category_ids"], cat_names, row.wp_categories)
    a = pipeline.analyse(draft["raw"], draft["title"], cats, row.primary_keyword, row.post_id, inventory, cfg)
    parsed, d, n, existing_note = a.parsed, a.draft, a.n, a.existing_note
    if a.skip_reason:
        why = a.skip_reason
        control.log_post(row.post_id, d.title, ", ".join(cats), "no_links", 0, dry_run,
                         f"{why} | {existing_note}", mode=mode, url=draft.get("url", ""))
        return {"post_id": row.post_id, "status": "no_links", "reason": why}

    res = pipeline.replay(a, reviewed, cfg) if reviewed is not None else pipeline.propose(a, llm, cfg)
    checked = res.checked
    if res.new_raw and not dry_run:
        try:
            wp.update_draft(row.post_id, res.new_raw, expected_modified=draft.get("modified_gmt") or None,
                            allow_published=allow_published)
        except Conflict as e:
            # Someone saved the post while Gemini was working. Their version wins; the post is
            # not marked done, so the next run starts again from what they saved.
            control.log_post(row.post_id, d.title, ", ".join(cats), "retry_later", 0, dry_run,
                             f"changed while processing: {e}", mode=mode, url=draft.get("url", ""))
            return {"post_id": row.post_id, "title": d.title, "status": "retry_later", "reason": str(e)}

    run_at = _now()
    change_rows = [[row.post_id, d.title, a.get("block", parsed.sentences[a["sentence_id"]].block if a["sentence_id"] in parsed.sentences else ""), a["url"],
                    a["anchor"], a["candidate"].target.type, a["candidate"].score,
                    a["original"], a["rewritten"], dry_run, run_at] for a in checked.accepted]
    control.log_changes(change_rows)
    notes = [existing_note] + pipeline.hygiene_notes(a, cfg) + checked.notes
    if reviewed is not None:
        notes.append(f"applied reviewed dry-run links: {len(checked.accepted)} of {len(reviewed)}")
    elif getattr(llm, "used_model", None):
        notes.append(f"model: {llm.used_model}")
    if checked.rejected:
        notes.append(f"rejected {len(checked.rejected)}: " + "; ".join(sorted({r for _, r in checked.rejected})))
    status = "linked" if checked.accepted else "no_links"
    control.log_post(row.post_id, d.title, ", ".join(cats), status, len(checked.accepted), dry_run,
                     " | ".join(notes), mode=mode, url=draft.get("url", ""))
    return {
        "post_id": row.post_id, "title": d.title, "status": status, "planned": n,
        "accepted": [{k: v for k, v in a.items() if k not in ("candidate", "raw")} for a in checked.accepted],
        "rejected": [{"edit": e, "reason": r} for e, r in checked.rejected],
        "notes": checked.notes,
    }


HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)


def check_live_links(control, wp, cfg, now: datetime | None = None) -> list[dict]:
    """Once a post the bot linked is published (and at least an hour has passed), record whether
    the bot's links are still in it. One row per post in the "Link Checks" tab; never re-adds."""
    now = now or datetime.now(timezone.utc)
    min_age = timedelta(minutes=cfg.get("run", "link_check_after_minutes", default=60))
    limit = cfg.get("run", "link_checks_per_run", default=15)
    checked, results = control.checked_post_ids(), []
    for pid, info in control.live_links().items():
        if pid in checked or len(results) >= limit:
            continue
        linked_at = None
        try:
            linked_at = datetime.strptime(info["linked_at"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        if linked_at and now - linked_at < min_age:
            continue
        try:
            post = wp.get_draft(pid)
        except Exception as e:
            print(f"[check] post {pid}: couldn't read ({type(e).__name__})")
            continue
        if post["status"] != "publish":
            continue                       # still a draft: check once it's live
        present = {norm_url(h) for h in HREF.findall(post["raw"])}
        wanted = {norm_url(u) for u in info["urls"] if u}
        missing = sorted(wanted - present)
        control.log_link_check([pid, post["title"], post.get("url", ""), info["linked_at"],
                                now.strftime("%Y-%m-%d %H:%M UTC"), len(wanted), len(wanted) - len(missing),
                                ", ".join(missing)])
        results.append({"post_id": pid, "missing": missing})
        print(f"[check] post {pid}: {len(wanted) - len(missing)}/{len(wanted)} bot links still present")
    return results


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--post-id", type=int, help="process just this WP post id (ignores 'done' state)")
    ap.add_argument("--event-post-id", default="",
                    help="run triggered by the MasterSheet right after these drafts were created (one ID or "
                         "comma-separated): process all ready drafts as usual, skipping the cool-off wait "
                         "for these, since the only recent save is the script's own")
    ap.add_argument("--dry-run", action="store_true", help="force dry run regardless of DRY_RUN env")
    args = ap.parse_args(argv)

    cfg = load_config()
    sec = cfg.secrets
    dry_run = args.dry_run or cfg.dry_run
    exempt = {int(x) for x in re.findall(r"\d+", args.event_post_id or "")}
    if args.post_id is not None:
        exempt.add(args.post_id)
    print(f"[run] dry_run={dry_run}" + (f", triggered for post(s) {sorted(exempt)}" if args.event_post_id else ""))

    control = ControlSheet(sec["GOOGLE_SERVICE_ACCOUNT_JSON"], sec["CONTROL_SHEET_ID"])
    wp = WordPress(cfg.get("wordpress", "base_url"), sec["WP_USERNAME"], sec["WP_APP_PASSWORD"],
                   cfg.get("wordpress", "editable_statuses"))
    try:
        check_live_links(control, wp, cfg)
    except Exception as e:  # a reporting step must never block linking
        print(f"[check] skipped: {type(e).__name__}: {e}")
    rows = MasterSheet(sec["GOOGLE_SERVICE_ACCOUNT_JSON"], sec["MASTERSHEET_ID"], cfg.settings).rows()
    todo = ready_rows(rows, cfg.settings, control.done_post_ids(), args.post_id)
    if args.post_id is not None and not todo:
        # A post ID given by hand works even if it isn't in the MasterSheet (e.g. a test draft):
        # no primary keyword, categories come from WordPress.
        print(f"[run] post {args.post_id} isn't in the MasterSheet; running it on WordPress data alone")
        todo = [SheetRow(tab="(manual)", topic=f"post {args.post_id}", primary_keyword="",
                         content_status="", publishing_status="",
                         draft_link=f"post.php?post={args.post_id}", live_link="", wp_categories=[])]
    todo = todo[: cfg.get("run", "max_posts_per_run")]
    print(f"[run] {len(rows)} sheet rows, {len(todo)} drafts to link")
    if not todo:
        return

    cat_names = wp.categories()
    inventory = build_inventory(wp.published_posts(), cat_names, rows, cfg)
    print(f"[run] inventory: {len(inventory)} targets "
          f"({sum(t.type == 'program' for t in inventory)} program pages)")

    llm = gemini_from_config(cfg, sec)

    OUT.mkdir(exist_ok=True)
    report = []
    started = time.monotonic()
    budget_min = cfg.get("run", "max_run_minutes", default=15)
    for i, row in enumerate(todo):
        if time.monotonic() - started > budget_min * 60:
            # Stop before the job's hard time limit; the rest are picked up by the next run
            # (nothing is half-written: a post is saved only after it's fully processed).
            print(f"[run] {budget_min}-minute budget used; {len(todo) - i} draft(s) left for the next run")
            break
        try:
            res = process_post(row, wp, inventory, cat_names, llm, cfg, control, dry_run,
                               skip_cooloff=row.post_id in exempt)
        except NotEditable as e:
            res = {"post_id": row.post_id, "status": "skipped", "reason": str(e)}
        except Exception as e:  # one bad post must not stop the run
            traceback.print_exc()
            control.log_post(row.post_id, row.topic, "", "error", 0, dry_run, f"{type(e).__name__}: {e}")
            res = {"post_id": row.post_id, "status": "error", "reason": str(e)}
        print(f"[post {row.post_id}] {res['status']} " + str(len(res.get('accepted', []))) + " links"
              + (f" ({res['reason']})" if res.get("reason") else ""))
        report.append(res)

    (OUT / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(f"## Internal linker ({'dry run' if dry_run else 'live'})\n\n| Post | Status | Links |\n|---|---|---|\n")
            for r in report:
                fh.write(f"| {r.get('title', r['post_id'])} | {r['status']} | {len(r.get('accepted', []))} |\n")


if __name__ == "__main__":
    main()
