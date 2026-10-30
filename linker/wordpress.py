"""WordPress REST API client.

Reads published posts to build the link inventory, and edits ONLY posts whose current status is
in settings.wordpress.editable_statuses (draft/pending by default). Status is re-checked
immediately before every write, so a post published in the meantime is never touched.
"""
from __future__ import annotations

import html
import time
from urllib.parse import urlsplit
from dataclasses import dataclass

import requests

TIMEOUT = (10, 60)          # (connect, read) seconds
CONTENT_PAGE_SIZE = 20      # article bodies are heavy; fetch them in small pages


@dataclass
class PublishedPost:
    id: int
    url: str
    title: str
    category_ids: list[int]
    content_html: str


class NotEditable(Exception):
    pass


class WordPressError(Exception):
    pass


class WordPress:
    def __init__(self, base_url: str, username: str | None, app_password: str | None,
                 editable_statuses: list[str]):
        """username/app_password may be None for read-only access to published content."""
        self.api = base_url.rstrip("/") + "/wp-json/wp/v2"
        self.s = requests.Session()
        self.authed = bool(username and app_password)
        if self.authed:
            self.s.auth = (username, app_password.replace(" ", ""))
        self.s.headers["User-Agent"] = "varsity-internal-linker/1.0"
        self.editable = set(editable_statuses)

    def _get(self, path: str, retries: int = 3, **params):
        """GET with retries. WordPress sometimes answers a heavy request with an empty body or an
        HTML error page instead of JSON (PHP timeout / memory limit), or with 429/5xx."""
        url = f"{self.api}{path}"
        last = ""
        for attempt in range(1, retries + 1):
            r = None
            try:
                r = self.s.get(url, params=params, timeout=TIMEOUT)
                if r.status_code in (401, 403, 404):
                    r.raise_for_status()               # not worth retrying
                if r.status_code < 400:
                    r.json()                           # make sure it really is JSON
                    return r
                last = f"HTTP {r.status_code}"
            except requests.HTTPError:
                raise
            except (requests.RequestException, ValueError) as e:
                if r is not None:
                    body = (r.text or "")[:160].replace("\n", " ")
                    last = (f"HTTP {r.status_code}, {r.headers.get('content-type', '?')}, "
                            f"{len(r.content)} bytes: {body!r}")
                else:
                    last = f"{type(e).__name__}: {e}"
            wait = 3 * attempt
            print(f"[wp] {path} {params.get('page', '')} failed ({last}); retry {attempt}/{retries} in {wait}s",
                  flush=True)
            time.sleep(wait)
        raise WordPressError(f"{path} failed after {retries} tries: {last}")

    def _paged(self, path: str, per_page: int = 100, **params):
        page = 1
        while True:
            r = self._get(path, page=page, per_page=per_page, **params)
            total = int(r.headers.get("X-WP-TotalPages", 1))
            print(f"[wp] {path} page {page}/{total}", flush=True)
            yield from r.json()
            if page >= total:
                break
            page += 1

    def categories(self) -> dict[int, str]:
        return {c["id"]: html.unescape(c["name"]) for c in self._paged("/categories", _fields="id,name")}

    def published_posts(self, with_content: bool = True) -> list[PublishedPost]:
        """All published posts. Two passes so one heavy request can't sink the run:
        1) light metadata (id, link, title, categories), 100 per page - required;
        2) article HTML in small pages, only used to count inbound links - optional: if it fails,
           the run continues without inbound counts (the orphan boost is just switched off)."""
        posts = {}
        for p in self._paged("/posts", status="publish", _fields="id,link,title,categories"):
            posts[p["id"]] = PublishedPost(
                id=p["id"], url=p["link"].rstrip("/") + "/",
                title=html.unescape(p["title"]["rendered"]),
                category_ids=p.get("categories", []), content_html="",
            )
        print(f"[wp] {len(posts)} published posts", flush=True)
        if with_content:
            try:
                for p in self._paged("/posts", per_page=CONTENT_PAGE_SIZE, status="publish", _fields="id,content"):
                    if p["id"] in posts:
                        posts[p["id"]].content_html = p["content"]["rendered"]
            except WordPressError as e:
                print(f"[warn] could not read article bodies ({e}); continuing without inbound-link counts",
                      flush=True)
                for p in posts.values():
                    p.content_html = ""
        return list(posts.values())

    def get_draft(self, post_id: int) -> dict:
        """Raw (editable) content of a post. Needs auth, so this also proves the credentials work."""
        p = self._get(f"/posts/{post_id}", context="edit",
                      _fields="id,status,link,title,categories,content").json()
        return {
            "id": p["id"],
            "status": p["status"],
            "title": html.unescape(p["title"]["raw"] or p["title"].get("rendered", "")),
            "category_ids": p.get("categories", []),
            "raw": p["content"]["raw"],
        }

    def published_post(self, url_or_id) -> dict:
        """A published post by URL, slug or ID, readable without credentials. Returns the same shape
        as get_draft, using the rendered HTML (WordPress may encode quotes as entities)."""
        fields = "id,status,link,title,categories,content"
        if str(url_or_id).isdigit():
            p = self._get(f"/posts/{url_or_id}", _fields=fields).json()
        else:
            slug = urlsplit(str(url_or_id)).path.rstrip("/").rsplit("/", 1)[-1] or str(url_or_id)
            found = self._get("/posts", slug=slug, _fields=fields).json()
            if not found:
                raise SystemExit(f"No published post found with slug '{slug}'")
            p = found[0]
        return {
            "id": p["id"],
            "status": p["status"],
            "url": p["link"],
            "title": html.unescape(p["title"]["rendered"]),
            "category_ids": p.get("categories", []),
            "raw": p["content"]["rendered"],
        }

    def update_draft(self, post_id: int, new_raw: str) -> None:
        current = self.get_draft(post_id)
        if current["status"] not in self.editable:
            raise NotEditable(f"post {post_id} is '{current['status']}', refusing to edit")
        r = self.s.post(f"{self.api}/posts/{post_id}", json={"content": new_raw}, timeout=TIMEOUT)
        r.raise_for_status()
