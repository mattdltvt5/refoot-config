"""Icons on the published pages (admin.html, gallery.html, privacy.html, ...)
come only from the UIcons Bold Rounded font (CLAUDE.md "Icon usage"):

  * no Unicode symbols / emoji standing in for icons (check marks, crosses,
    warning signs, gears, stars, play triangles, grips, arrows other than the
    prose "→" used in breadcrumbs and messages);
  * no HTML entities for those symbols;
  * every fi-* class is a fi-br-* class that exists in the bundled font;
  * no inline <svg> except Google's sign-in mark (data-brand="google"), which
    Google's branding guidelines require as-is.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGES = sorted(ROOT.glob("*.html"))
FONT_CSS = ROOT / "uicons" / "css" / "uicons-bold-rounded.css"

# Symbol characters that read as icons. "→" is allowed: it is prose here
# ("Settings → Pages", "Approved X → Y", commit messages), not an icon.
ICON_CHARS = re.compile(
    "[←↑↓-⇿"      # arrows except → (U+2192)
    "⌀-⏿"                    # misc technical (⌘, ⏎, ...)
    "■-◿"                    # geometric shapes (▶ ► ■ ●)
    "☀-➿"                    # misc symbols + dingbats (⚠ ⚙ ★ ✓ ✗ ✔)
    "⠀-⣿"                    # braille (⠿ grips)
    "⬀-⯿"                    # misc symbols and arrows
    "\U0001f300-\U0001faff"            # emoji
    "×]"                          # × used as a close button
)
ICON_ENTITIES = re.compile(
    r"&#(?:x(?:2[1-9a-f][0-9a-f]{2}|1f[3-9a-f][0-9a-f]{2})|(?:8[4-9]\d{2}|9\d{3}|10\d{3}|12\d{4}));"
    r"|&(?:times|check|cross|starf?|larr|uarr|darr|harr|hearts|bull);",
    re.IGNORECASE,
)


def _strip_comments(text: str) -> str:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", text)


def _font_classes() -> set[str]:
    return set(re.findall(r"\.(fi-br-[a-z0-9-]+):before", FONT_CSS.read_text(encoding="utf-8")))


def test_pages_exist():
    assert {p.name for p in PAGES} >= {"admin.html", "gallery.html"}


def test_no_unicode_symbols_or_emoji_as_icons():
    problems = []
    for page in PAGES:
        for n, line in enumerate(_strip_comments(page.read_text(encoding="utf-8")).split("\n"), 1):
            for m in ICON_CHARS.finditer(line):
                problems.append(f"{page.name}: {m.group(0)!r} (U+{ord(m.group(0)):04X}) in {line.strip()[:80]!r}")
            for m in ICON_ENTITIES.finditer(line):
                problems.append(f"{page.name}: entity {m.group(0)} in {line.strip()[:80]!r}")
    assert not problems, "Use a UIcons <i class=\"fi-br-...\"> icon instead:\n" + "\n".join(problems)


def test_only_bold_rounded_classes_that_exist():
    available = _font_classes()
    assert len(available) > 1000
    problems = []
    for page in PAGES:
        text = page.read_text(encoding="utf-8")
        for cls in set(re.findall(r"\bfi-[a-z]{2}-[a-z0-9-]+", text)):
            if not cls.startswith("fi-br-"):
                problems.append(f"{page.name}: {cls} is not UIcons Bold Rounded")
            elif cls not in available:
                problems.append(f"{page.name}: {cls} is not in uicons-bold-rounded.css")
    assert not problems, "\n".join(problems)


def test_pages_with_icons_load_the_font():
    for page in PAGES:
        text = page.read_text(encoding="utf-8")
        if "fi-br-" in text:
            assert "uicons/css/uicons-bold-rounded.css" in text, page.name


def test_no_inline_svg_except_google_mark():
    problems = []
    for page in PAGES:
        for m in re.finditer(r"<svg\b[^>]*>", page.read_text(encoding="utf-8")):
            if 'data-brand="google"' not in m.group(0):
                problems.append(f"{page.name}: {m.group(0)[:60]}")
    assert not problems, "Inline SVG icons aren't allowed; use UIcons:\n" + "\n".join(problems)


def test_no_foreign_icon_sets():
    for page in PAGES:
        text = page.read_text(encoding="utf-8")
        for pat in ("material-icons", "font-awesome", "fontawesome", "bootstrap-icons", "cdn.jsdelivr.net/npm/@mdi"):
            assert pat not in text.lower(), f"{page.name}: {pat}"


def test_refresh_buttons_spin_while_loading():
    text = (ROOT / "admin.html").read_text(encoding="utf-8")
    assert ".is-refreshing .fi-br-refresh" in text and "animation: spin" in text
    assert "prefers-reduced-motion" in text
    for btn, fn in [("reload-btn", "loadAll"), ("wf-refresh-btn", "loadWorkflowStatus"),
                    ("cand-refresh-btn", "loadCandidates"), ("aliasgaps-refresh-btn", "loadAliasGaps")]:
        assert f"getElementById('{btn}').addEventListener('click', e => withSpin(e.currentTarget, {fn}))" in text, btn
