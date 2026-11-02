# Setup: secrets and first run

Everything sensitive lives in **GitHub → repo → Settings → Secrets and variables → Actions**.
Nothing secret is ever committed. Six secrets, two variables, about 20 minutes.

| Name | Type | What it is |
|---|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Secret | Full JSON key of the Google service account |
| `MASTERSHEET_ID` | Secret | ID of *Varsity Articles: MasterSheet* (read-only for the bot) |
| `CONTROL_SHEET_ID` | Secret | ID of the bot's own log sheet |
| `WP_USERNAME` | Secret | WordPress username the Application Password belongs to |
| `WP_APP_PASSWORD` | Secret | WordPress Application Password |
| `GEMINI_API_KEY` | Secret | Gemini API key |
| `DRY_RUN` | Variable | `true` (default) logs proposals only; `false` edits drafts |
| `GEMINI_MODEL` | Variable | Optional model override, e.g. `gemini-3.8-flash` (the default) |

A sheet ID is the long string in its URL: `docs.google.com/spreadsheets/d/<THIS PART>/edit`.

---

## 1. Google service account (reads the MasterSheet, writes the control sheet)

1. Open **Google Cloud Console** (console.cloud.google.com) with your work account.
   Create a project, e.g. `varsity-internal-linker` (or reuse the one from the GSC pipeline).
2. **APIs & Services → Library** → enable **Google Sheets API**.
3. **IAM & Admin → Service Accounts → Create service account**. Name it `varsity-linker`.
   Skip the optional role and access steps.
4. Open the new account → **Keys → Add key → Create new key → JSON**. A `.json` file downloads.
   Treat it like a password: don't email it, don't commit it.
5. Copy the service account's email (`varsity-linker@<project>.iam.gserviceaccount.com`).
6. **Share the MasterSheet with that email as _Viewer_.** Viewer is the important part: together
   with the read-only scope in code, the bot cannot change the live sheet.
7. Create a new empty Google Sheet, e.g. **"Varsity Internal Linking – Control"**, and share it with
   the same email as **Editor**. The bot creates the `Processed` and `Changes` tabs itself.
8. In GitHub, add secret `GOOGLE_SERVICE_ACCOUNT_JSON` and paste the **entire** contents of the JSON
   file. Then add `MASTERSHEET_ID` and `CONTROL_SHEET_ID`.

> If your Workspace admin blocks service-account key creation ("Disable service account key creation"
> policy), ask IT for an exception for this project.

## 2. WordPress Application Password (edits drafts only)

Best practice is a dedicated user, so every edit shows up as the bot in post revisions:

1. WP admin → **Users → Add New**: username `linker-bot`, role **Editor**.
   (Editor is needed to edit other authors' drafts. The code additionally refuses to touch any post
   that isn't `draft`/`pending`.)
2. Log in as that user (or edit it as admin) → **Users → Profile → Application Passwords**.
   Name it `github-actions` → **Add New Application Password**. Copy it now; WordPress only shows it once.
3. Add GitHub secrets `WP_USERNAME` = `linker-bot` and `WP_APP_PASSWORD` = the generated password
   (spaces are fine; the code strips them).

If the Application Passwords section is missing, a security plugin or the host has disabled it, or
the site isn't served over HTTPS. If the API returns 401/403 from GitHub's servers, Cloudflare or a
WAF may be blocking the REST API; ask the tech team to allow `/varsity/blog/wp-json/` for
authenticated requests.

Undo is built in: every update creates a WordPress revision, so any post can be rolled back from
the editor's **Revisions** panel.

## 3. Gemini API key

1. Google AI Studio (aistudio.google.com) → **Get API key** → create a key in a project.
2. Add it as secret `GEMINI_API_KEY`.
3. Optional: add variable `GEMINI_MODEL` to switch models without a code change.

## 4. Program landing pages

Program pages are listed in `config/program_pages.csv`, pre-filled with the 7 programs from the Varsity
site header and footer. To add one, add a row; to pause one, set `active` to `no`. Each row's
`categories` must match WordPress category names exactly. This sets which blog categories link to it
most.

## 5. Test on any post first (no setup beyond the Gemini key)

The preview runs the full pipeline on one post and changes nothing anywhere.

**In GitHub:** Actions → **Preview on a post** → Run workflow → paste a published post URL
(e.g. `https://www.interviewbit.com/varsity/blog/dmaic-methodology/`) → Run. When it finishes, the run
page shows a summary table; download the **linker-preview** artifact and open `preview.html` for
the full before/after view with new links highlighted. Tick *Plan only* to see targets and scores
without calling Gemini.

**On your Mac:**
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export GEMINI_API_KEY=...            # not needed with --plan-only
python -m linker.preview --post https://www.interviewbit.com/varsity/blog/dmaic-methodology/
open out/preview.html
```

A published post is a fine test subject, but its existing links count toward the budget, so it may
get fewer new links than a fresh draft. To preview a draft, pass its post ID and set `WP_USERNAME` /
`WP_APP_PASSWORD`.

## 6. Going live

1. **Actions → Internal linker**: make sure the workflow is enabled (if it shows "This workflow was
   disabled", click *Enable workflow*).
2. Optional shadow period: keep the repository variable `DRY_RUN` = `true` for a few days. Every
   30 minutes the bot logs what it *would* add for drafts marked *Ready to Publish* to the control
   sheet's **Changes** tab, without touching WordPress.
3. Go live: set `DRY_RUN` = `false` (Settings → Secrets and variables → Actions → Variables).
   From the next scheduled run, drafts that are *Ready to Publish*, have a Draft Link and aren't
   published get linked automatically.
4. To pause at any time: set `DRY_RUN` back to `true`, or disable the *Internal linker* workflow.

What triggers a draft: MasterSheet *Content Status* = `Ready to Publish`, a Draft Link
(`post.php?post=ID`), *Publishing Status* not `Published`, WP status `draft` or `pending`, and not
already linked. Each post is linked once; to re-run one, delete its `linked` row in **Processed** or
run the workflow manually with its post ID.

Undo: open the draft in WordPress → Revisions → restore the revision before "Internal Linker".

Editing at the same time is handled: the bot never saves over a newer version (it re-checks the
post's "last modified" stamp right before saving and retries next run instead), and it skips drafts
saved in the last 10 minutes. One case it can't see: an editor who opened the draft *before* the bot
linked it and saves later will overwrite the links from their browser copy. The **Link Checks** tab
shows when that happened. Simplest habit: finish edits before marking a row *Ready to Publish*.

## 7. Instant trigger (optional, recommended)

Without it, a new draft waits up to 30 minutes for the schedule, and could be published before the
bot sees it. With it, the bot starts the moment the MasterSheet's Apps Script creates drafts.

1. GitHub → your avatar → **Settings → Developer settings → Personal access tokens → Fine-grained
   tokens → Generate new token**. Repository access: *Only select repositories* → this repo.
   Repository permissions: **Contents: Read and write**. Copy the token.
2. MasterSheet → **Extensions → Apps Script** → **+ → Script**, name it `trigger_internal_linker`,
   paste `apps_script/trigger_internal_linker.gs`.
3. Apps Script → **Project Settings → Script properties**: add `GITHUB_TOKEN` (the token) and
   `GITHUB_REPO` = `Prakhar-998/Scaler_Varsity_Internal_Linking`.
4. Run `testInternalLinkerTrigger` once and approve the permission prompt. GitHub → Actions →
   *Internal linker* shows a run started by `repository_dispatch`.
5. In the function that writes Draft Links, after the loop, add one call with all new post IDs:
   `triggerInternalLinker(newPostIds);` (use `postIdFromDraftLink(link)` to get each ID).

The 30-minute schedule stays on as a safety net.

## 8. Backfilling published posts

Actions → **Backfill published posts**. Every run is logged in **Processed** (`mode` = backfill) and
**Changes**; a post that a *live* run linked, or found nothing to add to, is never picked by "next" again.

1. **Try a few you know well:** paste 2–3 post URLs, keep *dry run* ticked, run. Read their rows in
   **Changes** (original vs new sentence).
2. **Apply them:** run again with the **same URLs** and *dry run* unticked. Check the live pages;
   undo any post from WordPress → Revisions.
3. **Batches:** leave URLs empty, set *next* to 5–10 (optionally a category), dry run first, then the
   live run with the **post URLs from the dry run's summary** (so exactly the reviewed posts are
   applied). Repeat until the run summary says 0 posts left.

Drafts that got published before the bot reached them show as `skipped` in **Processed**; they aren't
"done", so backfill picks them up too.
