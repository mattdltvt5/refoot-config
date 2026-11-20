"""Guardrail for the pages GitHub Pages publishes from this repo (pages.yml
copies every root-level file). See SEO.md in refoot_flutter.

Everything here is an internal tool or a copy, so none of it belongs in search:
every root *.html carries <meta name="robots" content="noindex, nofollow">, and
robots.txt disallows all crawlers. (A GitHub Pages project site can't serve a
robots.txt at the domain root, which is the only place crawlers read it - so the
meta tag is what really keeps these pages out of search.)

The legal pages are copies of the app's indexed pages: they also keep a canonical
to the app's domain. And this is an admin tool, not worth tracking: no Google
Analytics / Google tag code may come back.
"""
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = "https://refoot-highlights-app.web.app"

PAGES = sorted(p.name for p in ROOT.glob("*.html"))
# Copies of the app's legal pages: canonical on the app's domain.
CANONICAL = {"privacy.html": f"{APP}/privacy", "terms.html": f"{APP}/terms"}

# Google Analytics / Google tag / Consent Mode / Firebase Analytics markers.
TRACKING = re.compile(
    r"googletagmanager\.com|google-analytics\.com|\bgtag\s*\(|\bdataLayer\b|"
    r"measurementId|getAnalytics|logEvent|firebase/analytics|firebase-analytics|"
    r"firebase\.analytics\s*\(",
    re.IGNORECASE,
)


class _Head(HTMLParser):
    def __init__(self):
        super().__init__()
        self.robots = None
        self.canonical = None
        self.lang = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.lang = a.get("lang")
        elif tag == "meta" and (a.get("name") or "").lower() == "robots":
            self.robots = a.get("content")
        elif tag == "link" and (a.get("rel") or "").lower() == "canonical":
            self.canonical = a.get("href")


def _head(name: str) -> _Head:
    h = _Head()
    h.feed((ROOT / name).read_text(encoding="utf-8"))
    return h


def test_there_are_pages():
    assert {"admin.html", "gallery.html", *CANONICAL} <= set(PAGES)


@pytest.mark.parametrize("name", PAGES)
def test_every_page_is_noindex_nofollow(name):
    assert _head(name).robots == "noindex, nofollow", (
        f'{name} needs <meta name="robots" content="noindex, nofollow"> '
        "(every page published from this repo stays out of search)"
    )


@pytest.mark.parametrize("name,url", sorted(CANONICAL.items()))
def test_legal_copies_point_to_the_app(name, url):
    h = _head(name)
    assert h.canonical == url, f"{name}: canonical must be {url} (found {h.canonical!r})"
    assert h.lang, f"{name}: <html> needs a lang attribute"


def test_robots_txt_blocks_all_crawlers():
    lines = [l.strip() for l in (ROOT / "robots.txt").read_text(encoding="utf-8").splitlines()]
    assert "User-agent: *" in lines
    assert "Disallow: /" in lines


@pytest.mark.parametrize(
    "name", sorted(p.name for p in ROOT.iterdir()
                   if p.is_file() and p.suffix in {".html", ".js", ".webmanifest"}))
def test_no_google_analytics_or_tag_code(name):
    hits = sorted({m.group(0) for m in TRACKING.finditer((ROOT / name).read_text(encoding="utf-8"))})
    assert not hits, f"{name}: remove Google Analytics / Google tag code ({hits})"
