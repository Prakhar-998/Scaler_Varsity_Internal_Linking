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
| `GEMINI_MODEL` | Variable | Optional model override, e.g. `gemini-2.5-flash` |

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

## 5. First runs (dry run first)

1. **Actions → Internal linker → Run workflow**, leave *Dry run* ticked, and optionally enter one
   post ID (the number in the MasterSheet's Draft Link, `post.php?post=1940`).
2. Check the control sheet's **Changes** tab: original vs rewritten sentence, anchor, target.
   The run summary and the `linker-report` artifact show rejected edits and why.
3. When the proposals look right, run once with *Dry run* unticked on a single post and check the draft
   in WordPress.
4. Then set repository **variable** `DRY_RUN` = `false` so the 30-minute schedule edits drafts.

A post is marked done only after a live (non-dry) run links it, so dry runs can be repeated freely.
To re-run a post, delete its `linked` row in the `Processed` tab or run with its post ID.
