"""Tests for the shared markdown renderer (packages/app-theme/objects/site/markdown.py),
served at /markdown as window.dbbasicMarkdown -- the one place markdown
rendering lives, per that file's own docstring. Behavioral: executes the
real function under node against realistic input, not a source-text match.
"""

import ast
import json
import shutil
import subprocess
from pathlib import Path

import pytest

MARKDOWN_PATH = (
    Path(__file__).resolve().parents[1]
    / "packages" / "app-theme" / "objects" / "site" / "markdown.py"
)


def _markdown_js():
    tree = ast.parse(MARKDOWN_PATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "_JS" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("_JS not found in markdown.py")


def _render(*texts, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    probe = (
        "global.window = {};\n"
        + _markdown_js()
        + "\nconsole.log(JSON.stringify("
        + json.dumps([{"text": t} for t in texts])
        + ".map(o => window.dbbasicMarkdown(o.text))));\n"
    )
    probe_path = tmp_path / "markdown_probe.js"
    probe_path.write_text(probe)
    result = subprocess.run([node, str(probe_path)], capture_output=True, text=True)
    assert result.returncode == 0, f"node probe failed:\n{result.stderr}"
    return json.loads(result.stdout)


def test_headings_render_as_real_heading_tags(tmp_path):
    [html] = _render("# Title\n\n## Sub", tmp_path=tmp_path)
    assert html == "<h1>Title</h1><h2>Sub</h2>"


def test_inline_bold_italic_code_and_autolink(tmp_path):
    [html] = _render(
        "Some **bold** and *italic* and `code` and https://example.com/page here.",
        tmp_path=tmp_path,
    )
    assert "<strong>bold</strong>" in html
    assert "<em>italic</em>" in html
    assert "<code>code</code>" in html
    assert '<a href="https://example.com/page" target="_blank" rel="noopener">https://example.com/page</a>' in html


def test_unordered_and_ordered_lists(tmp_path):
    [ul] = _render("- one\n- two\n- three", tmp_path=tmp_path)
    assert ul == "<ul><li>one</li><li>two</li><li>three</li></ul>"
    [ol] = _render("1. first\n2. second", tmp_path=tmp_path)
    assert ol == "<ol><li>first</li><li>second</li></ol>"


def test_fenced_code_block_preserves_language_and_does_not_apply_inline_markdown(tmp_path):
    [html] = _render("```js\nconst x = **not bold**;\n```", tmp_path=tmp_path)
    assert html == '<pre><code class="language-js">const x = **not bold**;</code></pre>'


def test_blockquote_joins_lines(tmp_path):
    [html] = _render("> line one\n> line two", tmp_path=tmp_path)
    assert html == "<blockquote>line one line two</blockquote>"


def test_horizontal_rule(tmp_path):
    [html] = _render("above\n\n---\n\nbelow", tmp_path=tmp_path)
    assert "<hr>" in html
    assert "<p>above</p>" in html
    assert "<p>below</p>" in html


def test_gfm_table(tmp_path):
    [html] = _render("| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |", tmp_path=tmp_path)
    assert html == (
        "<table><thead><tr><th>A</th><th>B</th></tr></thead>"
        "<tbody><tr><td>1</td><td>2</td></tr><tr><td>3</td><td>4</td></tr></tbody></table>"
    )
    # Never a doubled close tag -- a real bug caught while writing this.
    assert html.count("</table>") == 1


def test_paragraphs_separated_by_blank_lines(tmp_path):
    [html] = _render("First paragraph.\n\nSecond paragraph.", tmp_path=tmp_path)
    assert html == "<p>First paragraph.</p><p>Second paragraph.</p>"


def test_raw_html_is_escaped_before_any_markdown_parsing(tmp_path):
    """The whole point: untrusted text (an AI reply, a user note, a
    published article) must never inject a live tag, no matter how it's
    combined with real markdown syntax on adjacent lines."""
    [html] = _render("<script>alert(1)</script>\n\n# **bold** header", tmp_path=tmp_path)
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<h1><strong>bold</strong> header</h1>" in html


def test_empty_and_none_input_does_not_throw(tmp_path):
    results = _render("", tmp_path=tmp_path)
    assert results == [""]

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    probe = (
        "global.window = {};\n" + _markdown_js()
        + "\nconsole.log(JSON.stringify([window.dbbasicMarkdown(null), window.dbbasicMarkdown(undefined)]));\n"
    )
    probe_path = tmp_path / "markdown_null_probe.js"
    probe_path.write_text(probe)
    result = subprocess.run([node, str(probe_path)], capture_output=True, text=True)
    assert result.returncode == 0, f"node probe failed:\n{result.stderr}"
    assert json.loads(result.stdout) == ["", ""]
