"""SEO guardrail for the pages GitHub Pages publishes from this repo (pages.yml
copies every root-level file). See SEO.md in refoot_flutter.

Every root *.html must either stay out of search (noindex) or point search at
its canonical copy on the app's domain - a GitHub Pages project site can't have
its own robots.txt, so these meta tags are the only control.
"""
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = "https://refoot-highlights-app.web.app"

# Pages that must never be indexed (admin + dev tools).
NOINDEX = {"admin.html", "gallery.html"}
# Copies of the app's legal pages: canonical on the app's domain.
CANONICAL = {"privacy.html": f"{APP}/privacy", "terms.html": f"{APP}/terms"}


class _Head(HTMLParser):
    def __init__(self):
        super().__init__()
        self.robots = ""
        self.canonical = None
        self.lang = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.lang = a.get("lang")
        elif tag == "meta" and (a.get("name") or "").lower() == "robots":
            self.robots = (a.get("content") or "").lower()
        elif tag == "link" and (a.get("rel") or "").lower() == "canonical":
            self.canonical = a.get("href")


def _head(name: str) -> _Head:
    h = _Head()
    h.feed((ROOT / name).read_text(encoding="utf-8"))
    return h


def test_every_published_page_is_classified():
    pages = {p.name for p in ROOT.glob("*.html")}
    unclassified = pages - NOINDEX - set(CANONICAL)
    assert not unclassified, (
        f"New page(s) on GitHub Pages: {sorted(unclassified)}. Add each to NOINDEX "
        "(admin/dev) or CANONICAL (copy of an app page) here, with the matching meta tag."
    )


@pytest.mark.parametrize("name", sorted(NOINDEX))
def test_admin_and_dev_pages_are_noindex(name):
    assert "noindex" in _head(name).robots, f"{name} needs <meta name=\"robots\" content=\"noindex,nofollow\">"


@pytest.mark.parametrize("name,url", sorted(CANONICAL.items()))
def test_legal_copies_point_to_the_app(name, url):
    h = _head(name)
    assert h.canonical == url, f"{name}: canonical must be {url} (found {h.canonical!r})"
    assert "noindex" not in h.robots
    assert h.lang, f"{name}: <html> needs a lang attribute"
