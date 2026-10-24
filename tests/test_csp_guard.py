# tests/test_csp_guard.py
"""Keeps markup and scripts inside the page CSP (script-src 'self', no
'unsafe-inline', no 'unsafe-eval'; see CSP_PLAN.md).

The browser would block any of these silently in production: the e2e CSP
recorder catches what the e2e flows reach, and this catches the rest at the
source. Fix a failure by moving the code into a static file under
app/static/js and binding it with a data attribute (see ui.js and the
htmx.onLoad set-up in chat.js / admin.js), not by loosening the check.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = sorted((ROOT / "app" / "templates").rglob("*.html"))
OUR_JS = sorted((ROOT / "app" / "static" / "js").glob("*.js"))  # not local/ (vendored)
PYTHON = sorted(p for p in (ROOT / "app").rglob("*.py"))

SCRIPT_TAG = re.compile(r"<script\b([^>]*)>", re.I)
# An on* attribute in a tag: onclick="...", onsubmit='...' (not data-onx).
HANDLER_ATTR = re.compile(r"(?<![\w-])on[a-z]+\s*=\s*[\"']", re.I)
# Inside an HTML string built in JS or Python: <tag ... onclick= / style=
HTML_WITH_HANDLER = re.compile(r"<[a-zA-Z][^<>]*\son[a-z]+\s*=", re.I)
HTML_WITH_STYLE = re.compile(r"<[a-zA-Z][^<>]*\sstyle\s*=", re.I)
# A style attribute in a template (style-src-attr 'none'); not data-style.
STYLE_ATTR = re.compile(r"(?<![\w-])style\s*=\s*[\"'{]", re.I)
EVAL_ATTRS = [
    (
        re.compile(r"(?<![\w-])(data-)?hx-on\b"),
        "hx-on (evaluated: needs 'unsafe-eval')",
    ),
    (re.compile(r"(?<![\w-])(data-)?hx-vars\b"), "hx-vars (evaluated)"),
    (
        re.compile(r"hx-vals\s*=\s*[\"']\s*(js|javascript):", re.I),
        "hx-vals with js: (evaluated)",
    ),
    (
        re.compile(r"hx-trigger\s*=\s*[\"'][^\"']*\["),
        "hx-trigger event filter [...] (evaluated)",
    ),
    (re.compile(r"javascript:", re.I), "javascript: URL"),
]
JS_EVAL = re.compile(
    r"\beval\s*\(|new\s+Function\s*\(|set(Timeout|Interval)\s*\(\s*[\"'`]"
)


def _rel(path):
    return str(path.relative_to(ROOT))


def _line(text, index):
    return text.count("\n", 0, index) + 1


def _inline_scripts(text):
    for m in SCRIPT_TAG.finditer(text):
        attrs = m.group(1)
        # <script src=...> runs a file; a JSON data block never runs.
        if re.search(r"\bsrc\s*=", attrs) or re.search(
            r"type\s*=\s*[\"']application/json[\"']", attrs
        ):
            continue
        yield m.start(), f"inline {m.group(0)}"


def _handler_and_eval_attributes(text):
    for m in HANDLER_ATTR.finditer(text):
        yield m.start(), f"inline handler {m.group(0)!r}"
    for m in STYLE_ATTR.finditer(text):
        yield m.start(), "style attribute (use a class; runtime values via the CSSOM)"
    for pattern, what in EVAL_ATTRS:
        for m in pattern.finditer(text):
            yield m.start(), what


def _built_markup(text, *, js):
    checks = [
        (HTML_WITH_HANDLER, "HTML string with an on* handler"),
        (HTML_WITH_STYLE, "HTML string with a style attribute"),
    ]
    if js:
        checks += [
            (JS_EVAL, "eval / new Function / string timer"),
            (re.compile(r"javascript:", re.I), "javascript: URL"),
        ]
    for pattern, what in checks:
        for m in pattern.finditer(text):
            yield m.start(), what


def _violations(paths, finder):
    found = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        found += [
            f"{_rel(path)}:{_line(text, at)}: {what}" for at, what in finder(text)
        ]
    return found


def test_templates_have_no_inline_scripts():
    assert _violations(TEMPLATES, _inline_scripts) == []


def test_templates_have_no_handler_or_eval_attributes():
    assert _violations(TEMPLATES, _handler_and_eval_attributes) == []


def test_scripts_build_no_handlers_styles_or_eval():
    assert _violations(OUR_JS, lambda t: _built_markup(t, js=True)) == []


def test_python_builds_no_html_with_handlers_or_styles():
    assert _violations(PYTHON, lambda t: _built_markup(t, js=False)) == []


def test_the_guard_sees_the_files():
    assert len(TEMPLATES) > 50
    assert {p.name for p in OUR_JS} >= {"chat.js", "ui.js", "admin.js", "pager.js"}


@pytest.mark.parametrize(
    "snippet, caught",
    [
        ("<script>alert(1)</script>", True),
        ('<script type="text/javascript">x()</script>', True),
        ('<script src="/js/chat.js" defer></script>', False),
        ('<script type="application/json" id="d">{}</script>', False),
    ],
)
def test_the_script_check(snippet, caught):
    assert bool(list(_inline_scripts(snippet))) is caught


@pytest.mark.parametrize(
    "snippet, caught",
    [
        ('<a onclick="go()">', True),
        ("<form onsubmit='return confirm(1)'>", True),
        ('<button hx-on:click="x()">', True),
        ("<input hx-trigger=\"keyup[key=='Enter']\">", True),
        ('<div data-onboarding="x">', False),
        ('<option value="a">', False),
        ('<input hx-trigger="keyup changed delay:300ms">', False),
        ('<div style="display: none">', True),
        ('<div {% if x %}style="width: 4px"{% endif %}>', True),
        ('<div data-style="x" class="is-hidden">', False),
    ],
)
def test_the_attribute_check(snippet, caught):
    assert bool(list(_handler_and_eval_attributes(snippet))) is caught


@pytest.mark.parametrize(
    "snippet, caught",
    [
        ("el.innerHTML = '<span style=\"color:red\">x</span>';", True),
        ("html = `<img src=x onerror=${f}>`;", True),
        ("setTimeout('go()', 10);", True),
        ("el.style.width = '40px';", False),
        ("setTimeout(() => go(), 10);", False),
    ],
)
def test_the_built_markup_check(snippet, caught):
    assert bool(list(_built_markup(snippet, js=True))) is caught


def test_every_avatar_size_has_a_class():
    """_avatar_display.html sizes avatars with .avatar-<size> classes; a size
    without one would render a full-size image."""
    css = (ROOT / "app" / "static" / "css" / "style.css").read_text(encoding="utf-8")
    sizes = set()
    for path in TEMPLATES:
        text = path.read_text(encoding="utf-8")
        sizes |= set(re.findall(r"\bsize\s*=\s*(\d+)", text))
    assert sizes, "no avatar sizes found"
    missing = sorted(s for s in sizes if f".avatar-{s} " not in css)
    assert missing == [], f"add .avatar-<size> rules to style.css for {missing}"
