"""Audit live posts: what WOULD the linker add? Results go to the control sheet only.

    python -m linker.audit --sample 10                     # 10 random posts, spread across categories
    python -m linker.audit --sample 5 --category "Generative AI"
    python -m linker.audit --posts "https://.../rlhf/, https://.../dmaic-methodology/"

Nothing is written to WordPress: this module has no write path at all (and the WordPress client
also refuses to edit anything that isn't a draft). It writes:
  - control sheet tabs "Audit Summary" (one row per post) and "Audit Links" (one row per link)
  - out/audit/<slug>.html before/after reports with new links highlighted, plus out/audit/index.html
"""
from __future__ import annotations

import argparse
import html
import os
import random
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import pipeline
from .config import load_config
from .inventory import build_inventory
from .main import category_names
from .preview import NEW_LINK_ATTRS, render_report
from .text import norm_url
from .wordpress import WordPress


def pick_posts(posts, cat_names, sample: int, category: str | None, seed: int, explicit: list[str]):
    """Explicit URLs win; otherwise a random sample spread round-robin across categories so the
    audit isn't all one programme."""
    if explicit:
        wanted = {norm_url(u) for u in explicit}
        by_slug = {u.rstrip("/").rsplit("/", 1)[-1] for u in explicit}
        chosen = [p for p in posts if norm_url(p.url) in wanted or p.url.rstrip("/").rsplit("/", 1)[-1] in by_slug]
        missing = len(explicit) - len(chosen)
        if missing:
            print(f"[audit] {missing} of the given URLs aren't published posts; skipped", flush=True)
        return chosen

    rng = random.Random(seed)
    groups: dict[str, list] = defaultdict(list)
    for p in posts:
        cats = category_names(p.category_ids, cat_names, ["Uncategorized"])
        if category and category.lower() not in {c.lower() for c in cats}:
            continue
        groups[cats[0]].append(p)
    for g in groups.values():
        rng.shuffle(g)
    chosen, keys = [], sorted(groups)
    while len(chosen) < sample and any(groups.values()):
        for k in keys:
            if groups[k] and len(chosen) < sample:
                chosen.append(groups[k].pop())
    return chosen


def _slug(url: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", url.rstrip("/").rsplit("/", 1)[-1].lower())[:80] or "post"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--posts", default="", help="comma/newline-separated published post URLs")
    ap.add_argument("--sample", type=int, default=10, help="random posts to audit (max 30)")
    ap.add_argument("--category", default="", help="only sample from this WordPress category")
    ap.add_argument("--seed", type=int, default=None, help="random seed, to repeat the same sample")
    ap.add_argument("--out", default="out/audit")
    args = ap.parse_args(argv)

    cfg = load_config(require_secrets=False)
    sec = cfg.secrets
    for need in ("GEMINI_API_KEY", "GOOGLE_SERVICE_ACCOUNT_JSON", "CONTROL_SHEET_ID"):
        if not sec.get(need):
            raise SystemExit(f"{need} is not set")

    seed = args.seed if args.seed is not None else int(time.time()) % 100000
    run_id = datetime.now(timezone.utc).strftime("audit-%Y%m%d-%H%M")
    print(f"[audit] run {run_id} (seed {seed}); no WordPress changes will be made", flush=True)

    # Read-only WordPress access: credentials are deliberately not passed in.
    wp = WordPress(cfg.get("wordpress", "base_url"), None, None, cfg.get("wordpress", "editable_statuses"))
    rows = []
    if sec.get("MASTERSHEET_ID"):
        from .sheets import MasterSheet
        rows = MasterSheet(sec["GOOGLE_SERVICE_ACCOUNT_JSON"], sec["MASTERSHEET_ID"], cfg.settings).rows()
    kw_by_url = {norm_url(r.live_link): r.primary_keyword for r in rows if r.live_link and r.primary_keyword}

    cat_names = wp.categories()
    posts = wp.published_posts()
    inventory = build_inventory(posts, cat_names, rows, cfg)
    explicit = [u.strip() for u in re.split(r"[,\n]+", args.posts) if u.strip()]
    chosen = pick_posts(posts, cat_names, min(args.sample, 30), args.category or None, seed, explicit)
    print(f"[audit] auditing {len(chosen)} posts", flush=True)

    from .llm import gemini_from_config
    from .sheets import AuditSheet, _now
    llm = gemini_from_config(cfg, sec)
    sheet = AuditSheet(sec["GOOGLE_SERVICE_ACCOUNT_JSON"], sec["CONTROL_SHEET_ID"])
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    index = []

    for i, p in enumerate(chosen, 1):
        cats = category_names(p.category_ids, cat_names, [])
        keyword = kw_by_url.get(norm_url(p.url), "")
        print(f"[audit] {i}/{len(chosen)} {p.title}", flush=True)
        notes, res = [], None
        try:
            a = pipeline.analyse(p.content_html, p.title, cats, keyword, p.id, inventory, cfg, url=p.url)
            if a.skip_reason:
                notes.append(a.skip_reason)
            else:
                res = pipeline.propose(a, llm, cfg, link_attrs=NEW_LINK_ATTRS)
                notes += res.checked.notes
        except Exception as e:  # one bad post must not stop the audit
            notes.append(f"error: {type(e).__name__}: {str(e)[:200]}")
            sheet.append([run_id, _now(), p.title, p.url, ", ".join(cats), keyword] + [""] * 8
                         + [getattr(llm, "used_model", "") or "", " | ".join(notes)], [])
            print(f"  ! {notes[-1]}", flush=True)
            continue

        accepted = res.checked.accepted if res else []
        rejected = res.checked.rejected if res else []
        unchanged = sum(1 for e in accepted if e["rewritten"] == e["original"])
        sheet.append(
            [run_id, _now(), p.title, p.url, ", ".join(cats), keyword, a.draft.word_count, a.budget,
             a.draft.existing_internal, a.draft.existing_external, a.n, len(accepted),
             f"{unchanged}/{len(accepted)}" if accepted else "", len(rejected),
             getattr(llm, "used_model", "") or "", " | ".join(notes)],
            [[run_id, p.title, p.url, e["anchor"], e["candidate"].target.title, e["url"],
              e["candidate"].target.type, e["candidate"].cat_match, e["candidate"].score,
              "no" if e["rewritten"] == e["original"] else "yes", e["original"], e["rewritten"]]
             for e in accepted])
        report = out / f"{_slug(p.url)}.html"
        report.write_text(render_report({"url": p.url, "notes": [a.existing_note] + notes}, a, res,
                                        p.content_html), encoding="utf-8")
        index.append((p.title, report.name, ", ".join(cats), len(accepted), a.n))
        print(f"  + {len(accepted)} links proposed ({unchanged} with the writer's sentence untouched)",
              flush=True)
        time.sleep(3)  # be gentle with the Gemini rate limit

    rows_html = "".join(
        f"<tr><td><a href='{html.escape(f)}'>{html.escape(t)}</a></td><td>{html.escape(c)}</td>"
        f"<td>{n}</td><td>{planned}</td></tr>" for t, f, c, n, planned in index)
    (out / "index.html").write_text(
        f"<!doctype html><meta charset=utf-8><title>Linker audit {run_id}</title>"
        f"<style>body{{font:15px system-ui;max-width:900px;margin:24px auto;padding:0 16px}}"
        f"td,th{{padding:6px 8px;border-bottom:1px solid #ddd;text-align:left}}</style>"
        f"<h1>Internal linker audit</h1><p>{run_id} · {len(index)} live posts · proposals only, "
        f"nothing was changed on the site</p><table><tr><th>Post</th><th>Category</th>"
        f"<th>Links proposed</th><th>Planned</th></tr>{rows_html}</table>", encoding="utf-8")

    total = sum(n for *_, n, _ in index)
    print(f"[audit] done: {total} links proposed across {len(index)} posts. See the control sheet "
          f"tabs 'Audit Summary' and 'Audit Links' (run_id {run_id}).", flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(f"## Linker audit `{run_id}`\n\n{len(index)} live posts, {total} links proposed. "
                     "Nothing was changed on the site.\n\n| Post | Category | Proposed | Planned |\n|---|---|---|---|\n")
            for t, _, c, n, planned in index:
                fh.write(f"| {t} | {c} | {n} | {planned} |\n")
            fh.write("\nFull detail: control sheet tabs **Audit Summary** and **Audit Links**; "
                     "per-post before/after pages in the `linker-audit` artifact.\n")


if __name__ == "__main__":
    main()
