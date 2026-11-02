"""Backfill: add internal links to posts that are ALREADY PUBLISHED, in small reviewed batches.

    python -m linker.backfill --posts "https://.../rlhf/, https://.../dmaic-methodology/"   # dry run
    python -m linker.backfill --posts "https://.../rlhf/" --live                             # apply
    python -m linker.backfill --next 10 [--category "Generative AI"] [--live]

Same pipeline and rules as drafts. Differences:
  - it may edit published posts (only here, only with --live);
  - progress is tracked in the control sheet's "Processed" tab with mode = backfill: a post that was
    linked (or found to have nothing to add) by a LIVE run is never picked again by --next;
  - every proposed/applied link goes to the "Changes" tab, like drafts.
Dry run is the default: review the Changes rows, then run the same posts again with --live.
"""
from __future__ import annotations

import argparse
import os
import re

from .config import load_config
from .inventory import build_inventory
from .main import category_names, process_post
from .sheets import ControlSheet, SheetRow
from .text import norm_url
from .wordpress import NotEditable, WordPress

DONE_STATUSES = ("linked", "no_links")


def pick(posts, cat_names, done: set[int], explicit: list[str], next_n: int, category: str | None):
    """Explicit URLs (even if done before) or the next N not-yet-backfilled posts, newest first."""
    if explicit:
        wanted = {norm_url(u) for u in explicit}
        slugs = {u.rstrip("/").rsplit("/", 1)[-1] for u in explicit}
        chosen = [p for p in posts if norm_url(p.url) in wanted or p.url.rstrip("/").rsplit("/", 1)[-1] in slugs]
        if len(chosen) < len(explicit):
            print(f"[backfill] {len(explicit) - len(chosen)} URL(s) aren't published posts; skipped", flush=True)
        return chosen
    out = []
    for p in posts:
        if p.id in done:
            continue
        if category and category.lower() not in {c.lower() for c in category_names(p.category_ids, cat_names, [])}:
            continue
        out.append(p)
        if len(out) >= next_n:
            break
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--posts", default="", help="comma/newline-separated published post URLs")
    ap.add_argument("--next", type=int, default=0, help="the next N published posts not yet backfilled")
    ap.add_argument("--category", default="", help="with --next: only this WordPress category")
    ap.add_argument("--live", action="store_true", help="apply the links (default: dry run, log only)")
    args = ap.parse_args(argv)

    explicit = [u.strip() for u in re.split(r"[,\n]+", args.posts) if u.strip()]
    if not explicit and args.next <= 0:
        raise SystemExit("Give --posts URLs or --next N")
    cfg = load_config()
    sec = cfg.secrets
    dry_run = not args.live
    print(f"[backfill] {'LIVE: published posts will be edited' if args.live else 'dry run: nothing is edited'}",
          flush=True)

    control = ControlSheet(sec["GOOGLE_SERVICE_ACCOUNT_JSON"], sec["CONTROL_SHEET_ID"])
    from .sheets import MasterSheet
    rows = MasterSheet(sec["GOOGLE_SERVICE_ACCOUNT_JSON"], sec["MASTERSHEET_ID"], cfg.settings).rows()
    kw_by_url = {norm_url(r.live_link): r.primary_keyword for r in rows if r.live_link and r.primary_keyword}

    wp = WordPress(cfg.get("wordpress", "base_url"), sec["WP_USERNAME"], sec["WP_APP_PASSWORD"],
                   cfg.get("wordpress", "editable_statuses"))
    cat_names = wp.categories()
    posts = wp.published_posts()
    inventory = build_inventory(posts, cat_names, rows, cfg)
    done = control.done_post_ids(DONE_STATUSES)
    chosen = pick(posts, cat_names, done, explicit, min(args.next, 30) or len(explicit),
                  args.category or None)
    remaining = sum(1 for p in posts if p.id not in done)
    print(f"[backfill] {len(chosen)} post(s) this batch; {remaining} published posts not yet backfilled",
          flush=True)

    from .llm import gemini_from_config
    llm = gemini_from_config(cfg, sec)
    report = []
    for p in chosen:
        if p.id in done:
            print(f"[backfill] note: {p.title} was already backfilled live; its earlier links count toward "
                  "the budget, so only the remaining room is used", flush=True)
        row = SheetRow(tab="(backfill)", topic=p.title, primary_keyword=kw_by_url.get(norm_url(p.url), ""),
                       content_status="", publishing_status="published",
                       draft_link=f"post.php?post={p.id}", live_link=p.url, wp_categories=[])
        try:
            res = process_post(row, wp, inventory, cat_names, llm, cfg, control, dry_run,
                               mode="backfill", allow_published=True)
        except NotEditable as e:
            res = {"post_id": p.id, "status": "skipped", "reason": str(e)}
        except Exception as e:  # one bad post must not stop the batch
            control.log_post(p.id, p.title, "", "error", 0, dry_run, f"{type(e).__name__}: {e}",
                             mode="backfill", url=p.url)
            res = {"post_id": p.id, "status": "error", "reason": str(e)}
        res.setdefault("title", p.title)
        res["url"] = p.url
        n = len(res.get("accepted", []))
        print(f"[backfill] {p.title}: {res['status']} {n} links"
              + (f" ({res['reason']})" if res.get("reason") else ""), flush=True)
        for e in res.get("accepted", []):
            print(f"    + [{e['anchor']}] -> {e['url']}", flush=True)
        report.append(res)

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(f"## Backfill ({'live' if args.live else 'dry run'})\n\n| Post | Status | Links |\n|---|---|---|\n")
            for r in report:
                fh.write(f"| [{r['title']}]({r['url']}) | {r['status']} | {len(r.get('accepted', []))} |\n")
            fh.write(f"\n{remaining} published posts not yet backfilled. Review each link in the control "
                     "sheet's **Changes** tab; progress is in **Processed** (mode = backfill).\n")


if __name__ == "__main__":
    main()
