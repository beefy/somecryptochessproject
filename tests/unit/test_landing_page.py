"""The apex landing page stays static, crawlable and pointed at the app.

`proofofchess.com` is the project's first page and `app.proofofchess.com` is the
application. That split is easy to undo by accident: a link written against the
apex turns the landing page into a router, and a script added for a live counter
turns the first page into a blank document when the bundle fails. This test
holds the two properties that matter most -- a canonical apex document that
renders without JavaScript, and every application entry point on `app.`.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
LANDING = ROOT / "src" / "landing" / "index.html"
APP = "https://app.proofofchess.com"
APEX = "https://proofofchess.com"


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.canonical: str | None = None
        self.title = ""
        self.scripts = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self.links.append(values["href"] or "")
        if tag == "link" and values.get("rel") == "canonical":
            self.canonical = values.get("href")
        if tag == "script":
            self.scripts += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


def _page() -> _Page:
    page = _Page()
    page.feed(LANDING.read_text(encoding="utf-8"))
    return page


def test_the_apex_page_is_static_and_canonical() -> None:
    page = _page()

    assert page.title.startswith("Proof of Chess")
    assert page.canonical == f"{APEX}/"
    assert page.scripts == 0, "the landing page must render without JavaScript"


def test_every_application_entry_point_is_on_the_app_origin() -> None:
    page = _page()
    expected = {
        f"{APP}/",
        f"{APP}/#/",
        f"{APP}/#/wallet",
        f"{APP}/#/play",
        f"{APP}/#/dex",
        f"{APP}/#/bridge",
    }

    assert expected <= set(page.links)


def test_no_application_route_is_bookmarked_on_the_apex() -> None:
    page = _page()

    for link in page.links:
        parsed = urlparse(link)
        if parsed.netloc == "proofofchess.com" and parsed.path == "/":
            assert not parsed.fragment, f"apex route should live on app.: {link}"


def test_the_page_links_to_the_deployment_and_not_the_retired_names() -> None:
    page = _page()
    hosts = {urlparse(link).netloc for link in page.links if link.startswith("http")}

    assert "app.proofofchess.com" in hosts
    assert "github.com" in hosts
    assert not any(host.endswith(".chessproofofwork.com") for host in hosts)
