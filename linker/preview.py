"""Preview the linker on one post without changing anything.

    python -m linker.preview --post https://www.interviewbit.com/varsity/blog/dmaic-methodology/
    python -m linker.preview --post 1940                # a draft (needs WP_USERNAME / WP_APP_PASSWORD)
    python -m linker.preview --post <url> --plan-only   # no Gemini call: just the targets and scores

Writes out/preview.html (before/after with new links highlighted) and out/preview.json.
Never writes to WordPress, the MasterSheet or the control sheet.

Needs only GEMINI_API_KEY (not even that with --plan-only). If GOOGLE_SERVICE_ACCOUNT_JSON and
MASTERSHEET_ID are set, MasterSheet primary keywords are used as anchor variants, as in a real run.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
from pathlib import Path

from . import pipeline
from .config import load_config
from .inventory import build_inventory
from .main import category_names
from .text import norm_url
from .wordpress import WordPress

NEW_LINK_ATTRS = 'class="ln-new" data-linker="new"'


def _load_sheet_rows(cfg):
    sec = cfg.secrets
    if not (sec.get("GOOGLE_SERVICE_ACCOUNT_JSON") and sec.get("MASTERSHEET_ID")):
        print("[preview] MasterSheet not configured: anchor variants come from post titles only")
        return []
    from .sheets import MasterSheet
    rows = MasterSheet(sec["GOOGLE_SERVICE_ACCOUNT_JSON"], sec["MASTERSHEET_ID"], cfg.settings).rows()
    print(f"[preview] read {len(rows)} MasterSheet rows (read-only)")
    return rows


def _fetch_post(wp: WordPress, ref: str) -> dict:
    if ref.isdigit() and wp.authed:
        d = wp.get_draft(int(ref))
        d.setdefault("url", "")
        return d
    return wp.published_post(ref)


def _strip_scripts(raw: str) -> str:
    return re.sub(r"<(script|iframe|style)\b.*?</\1>", "", raw, flags=re.S | re.I)


def _mark(text: str, anchor: str) -> str:
    esc, a = html.escape(text), html.escape(anchor)
    return esc.replace(a, f"<mark>{a}</mark>", 1)


def render_report(meta: dict, a: pipeline.Analysis, res, raw: str) -> str:
    rows_plan = "".join(
        f"<tr><td>{i}</td><td><a href='{html.escape(c.target.link)}'>{html.escape(c.target.title)}</a>"
        f"<div class=u>{html.escape(c.target.link)}</div></td><td>{c.target.type}</td><td>{c.cat_match}</td>"
        f"<td>{c.relevance:.2f}</td><td>{c.score:.2f}</td><td>{c.target.inbound}</td>"
        f"<td>{'required' if c.required else ('planned' if i <= a.n else 'backup')}</td></tr>"
        for i, c in enumerate(a.shortlist, 1))
    existing = "".join(
        f"<tr><td>{e.kind}</td><td>{html.escape(e.anchor)}</td><td class=u>{html.escape(e.url)}</td></tr>"
        for e in a.parsed.existing) or "<tr><td colspan=3>none</td></tr>"

    if res is None:
        edits = "<p class=muted>Plan-only run: Gemini was not called.</p>"
        rejected = ""
    else:
        edits = "".join(
            f"<div class=edit><div class=hd><b>{html.escape(e['anchor'])}</b> → "
            f"<a href='{html.escape(e['url'])}'>{html.escape(e['candidate'].target.title)}</a> "
            f"<span class=tag>{e['candidate'].target.type}</span></div>"
            f"<div class=before>{html.escape(e['original'])}</div>"
            f"<div class=after>{_mark(e['rewritten'], e['anchor'])}</div></div>"
            for e in res.checked.accepted) or "<p class=muted>No edits passed validation.</p>"
        rejected = "".join(
            f"<tr><td>{html.escape(r)}</td><td>{html.escape(str(e.get('anchor', '')))}</td>"
            f"<td>{html.escape(str(e.get('rewritten', '')))}</td></tr>" for e, r in res.checked.rejected)
        rejected = (f"<h2>Rejected by validator ({len(res.checked.rejected)})</h2><table><tr><th>Reason</th>"
                    f"<th>Anchor</th><th>Proposed sentence</th></tr>{rejected}</table>") if rejected else ""

    article = _strip_scripts(res.new_raw if res is not None and res.new_raw else raw)
    notes = "".join(f"<li>{html.escape(n)}</li>" for n in meta["notes"])
    stat = lambda k, v: f"<div class=stat><span>{k}</span><b>{v}</b></div>"
    stats = "".join([
        stat("Words", a.draft.word_count), stat("Link budget (total)", a.budget),
        stat("Existing internal", a.draft.existing_internal), stat("Existing external", a.draft.existing_external),
        stat("New links planned", a.n), stat("Eligible sentences", len(a.offered)),
        stat("New links placed", "-" if res is None else len(res.checked.accepted)),
    ])
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Linker preview</title><style>
:root{{--bg:#fff;--fg:#1d1d1f;--mut:#6e6e73;--line:#e5e5ea;--new:#fff3a3;--card:#f7f7f9;--acc:#1a56db}}
body{{background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,system-ui,Segoe UI,sans-serif;margin:0}}
main{{max-width:980px;margin:0 auto;padding:24px 16px 64px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:32px 0 10px}}
.muted,.u{{color:var(--mut)}} .u{{font-size:12px;word-break:break-all}}
.stats{{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;margin:16px 0}}
.stat{{background:var(--card);border-radius:8px;padding:10px 12px}} .stat span{{display:block;font-size:12px;color:var(--mut)}}
.stat b{{font-size:20px}} table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}}
.tablewrap{{overflow-x:auto}} .edit{{border:1px solid var(--line);border-radius:8px;padding:12px;margin:10px 0}}
.before{{color:var(--mut);text-decoration:line-through;text-decoration-color:#c7c7cc;margin:6px 0}}
.after mark,a.ln-new{{background:var(--new);color:inherit;padding:0 2px;border-radius:3px}}
.tag{{font-size:11px;background:var(--card);padding:1px 6px;border-radius:10px;color:var(--mut)}}
article{{border-top:1px solid var(--line);margin-top:12px;padding-top:8px}} article a{{color:var(--acc)}}
article a.ln-new{{outline:2px solid #e3b500}} article img{{max-width:100%;height:auto}}
</style></head><body><main>
<h1>{html.escape(a.draft.title)}</h1>
<div class=u>{html.escape(meta['url'] or '')} · categories: {html.escape(', '.join(a.draft.categories) or 'none')}
 · primary keyword: {html.escape(a.draft.primary_keyword or 'n/a')}</div>
<div class=stats>{stats}</div>
<ul class=muted>{notes}</ul>
<h2>Proposed edits</h2>{edits}
{rejected}
<h2>Shortlisted targets (in priority order)</h2><div class=tablewrap><table>
<tr><th>#</th><th>Target</th><th>Type</th><th>Category</th><th>Relevance</th><th>Score</th><th>Inbound</th><th>Role</th></tr>
{rows_plan or '<tr><td colspan=8>none</td></tr>'}</table></div>
<h2>Links already in the post</h2><div class=tablewrap><table><tr><th>Kind</th><th>Anchor</th><th>URL</th></tr>
{existing}</table></div>
<h2>Article with new links highlighted</h2><article>{article}</article>
</main></body></html>"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--post", required=True, help="published post URL or slug, or a post ID")
    ap.add_argument("--keyword", default="", help="primary keyword of the post (optional)")
    ap.add_argument("--plan-only", action="store_true", help="skip Gemini; show targets and scores only")
    ap.add_argument("--out", default="out", help="output folder (default: out)")
    args = ap.parse_args(argv)

    cfg = load_config(require_secrets=False)
    sec = cfg.secrets
    if not args.plan_only and not sec.get("GEMINI_API_KEY"):
        raise SystemExit("GEMINI_API_KEY is not set. Set it, or use --plan-only.")

    wp = WordPress(cfg.get("wordpress", "base_url"), sec.get("WP_USERNAME"), sec.get("WP_APP_PASSWORD"),
                   cfg.get("wordpress", "editable_statuses"))
    post = _fetch_post(wp, args.post.strip())
    print(f"[preview] post {post['id']} ({post['status']}): {post['title']}")

    rows = _load_sheet_rows(cfg)
    keyword = args.keyword
    if not keyword and post.get("url"):
        own = norm_url(post["url"])
        keyword = next((r.primary_keyword for r in rows if r.live_link and norm_url(r.live_link) == own), "")
    if not keyword:
        keyword = next((r.primary_keyword for r in rows if r.post_id == post["id"]), "")

    print("[preview] reading categories and published posts from WordPress...", flush=True)
    cat_names = wp.categories()
    posts = wp.published_posts()
    inventory = build_inventory(posts, cat_names, rows, cfg)
    print(f"[preview] inventory: {len(inventory)} targets "
          f"({sum(t.type == 'program' for t in inventory)} program pages)")

    cats = category_names(post["category_ids"], cat_names, [])
    a = pipeline.analyse(post["raw"], post["title"], cats, keyword, post["id"], inventory, cfg,
                         url=post.get("url", ""))
    notes = [a.existing_note]
    if post["status"] == "publish":
        notes.append("This is a published post, used as a test subject. Its own existing links count "
                     "toward the budget, so fewer new links may be planned than for a fresh draft.")

    res = None
    if a.skip_reason:
        notes.append(f"No links planned: {a.skip_reason}")
    elif not args.plan_only:
        from .llm import Gemini
        model = sec.get("GEMINI_MODEL") or cfg.get("llm", "default_model")
        print(f"[preview] asking {model} for {a.n} links over {len(a.offered)} eligible sentences "
              "(can take a minute)...", flush=True)
        res = pipeline.propose(a, Gemini(sec["GEMINI_API_KEY"], model, cfg.get("llm", "temperature")),
                               cfg, link_attrs=NEW_LINK_ATTRS)
        notes += res.checked.notes

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    meta = {"url": post.get("url", ""), "notes": notes}
    (out / "preview.html").write_text(render_report(meta, a, res, post["raw"]), encoding="utf-8")
    report = {
        "post_id": post["id"], "title": post["title"], "url": post.get("url"), "categories": cats,
        "primary_keyword": keyword, "words": a.draft.word_count, "budget_total": a.budget,
        "existing_internal": a.draft.existing_internal, "existing_external": a.draft.existing_external,
        "new_links_planned": a.n, "eligible_sentences": len(a.offered), "notes": notes,
        "shortlist": [{"title": c.target.title, "url": c.target.link, "type": c.target.type,
                       "category_match": c.cat_match, "relevance": c.relevance, "score": c.score,
                       "required": c.required} for c in a.shortlist],
        "accepted": [] if res is None else [{k: v for k, v in e.items() if k != "candidate"}
                                            for e in res.checked.accepted],
        "rejected": [] if res is None else [{"reason": r, "edit": e} for e, r in res.checked.rejected],
        "prompt": None if res is None else res.prompt,
    }
    (out / "preview.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    placed = "-" if res is None else len(res.checked.accepted)
    print(f"[preview] budget {a.budget}, existing internal {a.draft.existing_internal}, "
          f"planned {a.n}, placed {placed}")
    if res is not None:
        for e in res.checked.accepted:
            print(f"  + [{e['anchor']}] -> {e['url']}\n    {e['rewritten']}")
        for e, r in res.checked.rejected:
            print(f"  - rejected ({r}): {e.get('anchor', '')}")
    print(f"[preview] report: {out / 'preview.html'}")

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(f"## Linker preview: {post['title']}\n\n"
                     f"Budget **{a.budget}** · existing internal **{a.draft.existing_internal}** · "
                     f"planned **{a.n}** · placed **{placed}**\n\n")
            if res is not None and res.checked.accepted:
                fh.write("| Anchor | Target | Rewritten sentence |\n|---|---|---|\n")
                for e in res.checked.accepted:
                    fh.write(f"| {e['anchor']} | {e['url']} | {e['rewritten'].replace('|', '/')} |\n")
            fh.write("\nDownload the `linker-preview` artifact below for the full before/after report.\n")


if __name__ == "__main__":
    main()
