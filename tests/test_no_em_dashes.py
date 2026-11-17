# tests/test_no_em_dashes.py
"""Keeps the em dash out of text people read in D8-Chat.

Kenny: "We want to remove the em dash everywhere that would make people think
AI had written text." That covers templates, strings in our own JavaScript
(toasts, labels, built markup), and Python strings (flash messages, errors,
push titles, seeded names and descriptions). Comments and docstrings are for
developers and stay as they are.

Fix a failure by rewording: a period, a comma, a colon or parentheses. Don't
swap in a lookalike (an en dash, or two hyphens).
"""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = sorted((ROOT / "app" / "templates").rglob("*.html"))
OUR_JS = sorted((ROOT / "app" / "static" / "js").glob("*.js"))  # not local/ (vendored)
PYTHON = sorted((ROOT / "app").rglob("*.py")) + [
    ROOT / "seed.py",
    ROOT / "init_db.py",
    *sorted((ROOT / "migrations").glob("*.py")),
]

EM_DASH = "\u2014"
# The character itself, and the ways markup or a JS string can spell it.
EM_DASH_TEXT = re.compile(
    "\u2014|&mdash;|&#8212;|&#x2014;|\\\\u2014|\\\\u\\{2014\\}", re.IGNORECASE
)
TEMPLATE_COMMENT = re.compile(r"\{#.*?#\}|<!--.*?-->", re.DOTALL)

# Where a "/" starts a regex literal rather than a division.
REGEX_AFTER = set("(,=:[!&|?{};+-*%<>~^")
REGEX_AFTER_WORDS = {
    "return", "typeof", "case", "do", "else", "in", "of", "new",
    "delete", "void", "throw", "instanceof", "yield", "await",
}  # fmt: skip


def _rel(path):
    return str(path.relative_to(ROOT))


def _line(text, index):
    return text.count("\n", 0, index) + 1


def _outside(spans, index):
    return not any(start <= index < end for start, end in spans)


def _template_hits(text):
    comments = [m.span() for m in TEMPLATE_COMMENT.finditer(text)]
    for m in EM_DASH_TEXT.finditer(text):
        if _outside(comments, m.start()):
            yield m.start()


def _js_comment_spans(src):
    """Spans of // and /* */ comments, stepping over strings, template
    literals (with ${...} inside) and regex literals so a quote or slash in
    one of those can't open a fake comment."""
    spans = []
    n = len(src)

    def string(i, quote):
        j = i + 1
        while j < n:
            if src[j] == "\\":
                j += 2
            elif src[j] == quote or src[j] == "\n":
                return j + 1
            else:
                j += 1
        return n

    def regex(i):
        j, in_class = i + 1, False
        while j < n:
            c = src[j]
            if c == "\\":
                j += 2
                continue
            if c == "\n":
                return j
            if in_class:
                in_class = c != "]"
            elif c == "[":
                in_class = True
            elif c == "/":
                j += 1
                while j < n and src[j].isalnum():
                    j += 1
                return j
            j += 1
        return n

    def template(i):
        j = i + 1
        while j < n:
            if src[j] == "\\":
                j += 2
            elif src[j] == "`":
                return j + 1
            elif src.startswith("${", j):
                j = code(j + 2, in_expression=True)
            else:
                j += 1
        return n

    def code(i, in_expression=False):
        depth, prev, word = 0, "", ""
        while i < n:
            c = src[i]
            if c.isspace():
                i += 1
            elif src.startswith("//", i):
                end = src.find("\n", i)
                end = n if end < 0 else end
                spans.append((i, end))
                i = end
            elif src.startswith("/*", i):
                end = src.find("*/", i + 2)
                end = n if end < 0 else end + 2
                spans.append((i, end))
                i = end
            elif c in "'\"":
                i, prev, word = string(i, c), "a", ""
            elif c == "`":
                i, prev, word = template(i), "a", ""
            elif c == "/" and (
                not prev or prev in REGEX_AFTER or word in REGEX_AFTER_WORDS
            ):
                i, prev, word = regex(i), "a", ""
            elif c.isalnum() or c in "_$":
                j = i
                while j < n and (src[j].isalnum() or src[j] in "_$"):
                    j += 1
                prev, word, i = "a", src[i:j], j
            else:
                if in_expression and c == "{":
                    depth += 1
                elif in_expression and c == "}":
                    if depth == 0:
                        return i + 1
                    depth -= 1
                prev, word, i = c, "", i + 1
        return i

    code(0)
    return spans


def _js_hits(src):
    comments = _js_comment_spans(src)
    for m in EM_DASH_TEXT.finditer(src):
        if _outside(comments, m.start()):
            yield m.start()


def _python_hits(src):
    """Line numbers of string literals holding an em dash, docstrings aside."""
    tree = ast.parse(src)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and EM_DASH in node.value
            and id(node) not in docstrings
        ):
            yield node.lineno


def _violations(paths, finder):
    found = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for index in finder(text):
            found.append(f"{_rel(path)}:{_line(text, index)}")
    return found


def test_templates_have_no_em_dashes():
    assert _violations(TEMPLATES, _template_hits) == []


def test_scripts_have_no_em_dashes_outside_comments():
    assert _violations(OUR_JS, _js_hits) == []


def test_python_strings_have_no_em_dashes():
    found = []
    for path in PYTHON:
        found += [
            f"{_rel(path)}:{line}"
            for line in _python_hits(path.read_text(encoding="utf-8"))
        ]
    assert found == []


def test_the_guard_sees_the_files():
    assert len(TEMPLATES) > 50
    assert any(p.name == "chat.js" for p in OUR_JS)
    assert any(p.name == "seed.py" for p in PYTHON)
    assert any(p.parent.name == "migrations" for p in PYTHON)


@pytest.mark.parametrize(
    "snippet, caught",
    [
        (f"<p>Nobody could be paged {EM_DASH} check the policy.</p>", True),
        ("<td>{{ name if name else '" + EM_DASH + "' }}</td>", True),
        ("<p>a &mdash; b</p>", True),
        ("<p>a &#8212; b</p>", True),
        (f"{{# a note {EM_DASH} for developers #}}", False),
        (f"<!-- a note {EM_DASH} for developers -->", False),
        ("<p>Nobody could be paged. Check the policy.</p>", False),
    ],
)
def test_the_template_check(snippet, caught):
    assert bool(list(_template_hits(snippet))) is caught


@pytest.mark.parametrize(
    "snippet, caught",
    [
        (f"ToastManager.show('Saved', 'Done {EM_DASH} reload');", True),
        (f"const t = `Hi ${{name}} {EM_DASH} welcome`;", True),
        (f'el.textContent = "a {EM_DASH} b"; // fine', True),
        ("const s = 'a \\u2014 b';", True),
        (f"// a note {EM_DASH} for developers", False),
        (f"/* a note {EM_DASH}\n   for developers */", False),
        # A quote or slash in a regex or string must not open a fake comment.
        (f"const re = /['\"]/g; // a note {EM_DASH} here", False),
        (f"const url = 'https://x'; // a note {EM_DASH} here", False),
        (f"const q = `${{a / b}}`; /* {EM_DASH} */", False),
        (f"const re = /\\/\\//; const s = '{EM_DASH}';", True),
    ],
)
def test_the_script_check(snippet, caught):
    assert bool(list(_js_hits(snippet))) is caught


@pytest.mark.parametrize(
    "snippet, caught",
    [
        (f'flash("Saved {EM_DASH} reload")', True),
        (f'title = f"{{who}} replied {EM_DASH} in #{{channel}}"', True),
        ('msg = "a \\u2014 b"', True),
        (f'def f():\n    """A docstring {EM_DASH} for developers."""\n', False),
        (f"x = 1  # a note {EM_DASH} for developers", False),
    ],
)
def test_the_python_check(snippet, caught):
    assert bool(list(_python_hits(snippet))) is caught
