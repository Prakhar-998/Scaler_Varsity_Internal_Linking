"""WordPress REST API client.

Reads published posts to build the link inventory, and edits ONLY posts whose current status is
in settings.wordpress.editable_statuses (draft/pending by default). Status is re-checked
immediately before every write, so a post published in the meantime is never touched.
"""
from __future__ import annotations

import html
from dataclasses import dataclass

import requests

TIMEOUT = 30


@dataclass
class PublishedPost:
    id: int
    url: str
    title: str
    category_ids: list[int]
    content_html: str


class NotEditable(Exception):
    pass


class WordPress:
    def __init__(self, base_url: str, username: str, app_password: str, editable_statuses: list[str]):
        self.api = base_url.rstrip("/") + "/wp-json/wp/v2"
        self.s = requests.Session()
        self.s.auth = (username, app_password.replace(" ", ""))
        self.s.headers["User-Agent"] = "varsity-internal-linker/1.0"
        self.editable = set(editable_statuses)

    def _get(self, path: str, **params):
        r = self.s.get(f"{self.api}{path}", params=params, timeout=TIMEOUT)
        r.raise_for_status()
        return r

    def _paged(self, path: str, **params):
        page = 1
        while True:
            r = self._get(path, page=page, per_page=100, **params)
            yield from r.json()
            if page >= int(r.headers.get("X-WP-TotalPages", 1)):
                break
            page += 1

    def categories(self) -> dict[int, str]:
        return {c["id"]: html.unescape(c["name"]) for c in self._paged("/categories", _fields="id,name")}

    def published_posts(self) -> list[PublishedPost]:
        out = []
        for p in self._paged("/posts", status="publish", _fields="id,link,title,categories,content"):
            out.append(PublishedPost(
                id=p["id"],
                url=p["link"].rstrip("/") + "/",
                title=html.unescape(p["title"]["rendered"]),
                category_ids=p.get("categories", []),
                content_html=p["content"]["rendered"],
            ))
        return out

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

    def update_draft(self, post_id: int, new_raw: str) -> None:
        current = self.get_draft(post_id)
        if current["status"] not in self.editable:
            raise NotEditable(f"post {post_id} is '{current['status']}', refusing to edit")
        r = self.s.post(f"{self.api}/posts/{post_id}", json={"content": new_raw}, timeout=TIMEOUT)
        r.raise_for_status()
