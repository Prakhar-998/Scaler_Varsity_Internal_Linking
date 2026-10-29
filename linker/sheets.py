"""Google Sheets access.

MasterSheet: opened with a READ-ONLY scope. The bot can never write to it, and the service
account should also only be a Viewer on it, so there are two independent guards.

Control sheet: a separate sheet the bot owns, used for run state and a review log of every edit.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

READONLY_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
WRITE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

MONTH_TAB = re.compile(r"^[A-Z][a-z]{2,4}'\d{2}$")  # Aug'26, Sept'26, Oct'26 ...
POST_ID = re.compile(r"[?&]post=(\d+)")

PROCESSED_TAB = "Processed"
CHANGES_TAB = "Changes"
PROCESSED_HEADER = ["post_id", "title", "category", "status", "links_added", "dry_run", "run_at", "notes"]
CHANGES_HEADER = ["post_id", "title", "block_index", "target_url", "anchor", "target_type",
                  "score", "original_sentence", "rewritten_sentence", "dry_run", "run_at"]


@dataclass
class SheetRow:
    tab: str
    topic: str
    primary_keyword: str
    content_status: str
    publishing_status: str
    draft_link: str
    live_link: str
    wp_categories: list[str]

    @property
    def post_id(self) -> int | None:
        m = POST_ID.search(self.draft_link or "")
        return int(m.group(1)) if m else None


def _client(service_account_json: str, scopes: list[str]) -> gspread.Client:
    info = json.loads(service_account_json)
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


def _pick(row: dict, names) -> str:
    if isinstance(names, str):
        names = [names]
    for n in names:
        val = row.get(n.strip().lower())
        if val:
            return str(val).strip()
    return ""


def parse_tab(tab_name: str, values: list[list[str]], cols: dict) -> list[SheetRow]:
    """Turn a tab's raw grid into SheetRows. Pure function, so it is unit-testable offline."""
    if not values:
        return []
    header = [h.strip().lower() for h in values[0]]   # tabs differ in casing ("Live link" vs "Live Link")
    rows = []
    for raw in values[1:]:
        row = {header[i]: (raw[i] if i < len(raw) else "") for i in range(len(header)) if header[i]}
        topic = _pick(row, cols["topic"])
        if not topic:
            continue
        cats = [c for c in (_pick(row, cols["wp_category"]), _pick(row, cols["wp_category_2"])) if c]
        rows.append(SheetRow(
            tab=tab_name,
            topic=topic,
            primary_keyword=_pick(row, cols["primary_keyword"]),
            content_status=_pick(row, cols["content_status"]),
            publishing_status=_pick(row, cols["publishing_status"]),
            draft_link=_pick(row, cols["draft_link"]),
            live_link=_pick(row, cols["live_link"]).rstrip("`").strip(),
            wp_categories=cats,
        ))
    return rows


class MasterSheet:
    """Read-only view of the writers' MasterSheet."""

    def __init__(self, service_account_json: str, sheet_id: str, settings: dict):
        self._book = _client(service_account_json, READONLY_SCOPES).open_by_key(sheet_id)
        self._ms = settings["mastersheet"]

    def rows(self) -> list[SheetRow]:
        out: list[SheetRow] = []
        wanted = set(self._ms.get("extra_tabs") or [])
        for ws in self._book.worksheets():
            if (self._ms.get("auto_detect_month_tabs") and MONTH_TAB.match(ws.title)) or ws.title in wanted:
                out.extend(parse_tab(ws.title, ws.get_all_values(), self._ms["columns"]))
        return out


class ControlSheet:
    """The bot's own sheet: which posts are done, and a reviewable log of every rewrite."""

    def __init__(self, service_account_json: str, sheet_id: str):
        self._book = _client(service_account_json, WRITE_SCOPES).open_by_key(sheet_id)
        self.processed = self._ensure(PROCESSED_TAB, PROCESSED_HEADER)
        self.changes = self._ensure(CHANGES_TAB, CHANGES_HEADER)

    def _ensure(self, title: str, header: list[str]):
        try:
            ws = self._book.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self._book.add_worksheet(title=title, rows=1000, cols=len(header))
        if ws.row_values(1) != header:
            ws.update(range_name="A1", values=[header])
        return ws

    def done_post_ids(self) -> set[int]:
        """Posts already linked for real. Dry runs don't count, so they re-run once live."""
        done = set()
        for rec in self.processed.get_all_records():
            if str(rec.get("status")) == "linked" and str(rec.get("dry_run")).upper() != "TRUE":
                try:
                    done.add(int(rec["post_id"]))
                except (TypeError, ValueError):
                    pass
        return done

    def log_post(self, post_id, title, category, status, links_added, dry_run, notes=""):
        self.processed.append_row(
            [post_id, title, category, status, links_added, dry_run, _now(), notes],
            value_input_option="RAW")

    def log_changes(self, rows: list[list]):
        if rows:
            self.changes.append_rows(rows, value_input_option="RAW")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
