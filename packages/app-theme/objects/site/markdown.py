"""Shared markdown renderer, served as one object at /markdown.

Any page includes <script src="/markdown"></script> and gets
`window.dbbasicMarkdown(text)` — defined once, reused everywhere. As of
this pass that's the shell/Talk captions, view_render.py's standalone
`markdown` view block, and /form's read-only rendering of any `textarea`
field (articles, notes, anything long-form) — three call sites that used
to each carry their own copy of a *lighter* transform (bold/italic/links/
line-breaks only) before converging here. Rendering markdown is a shared
UI utility, so it lives in the design system alongside /style and /nav,
never copied into each page — see 2026-08's incident: two of those three
copies were written in the same session before this one was noticed.

Real block-level markdown now, not just inline transforms: headings (# -
######), fenced code blocks (```lang ... ```), blockquotes (>), ordered
and unordered lists (no nesting -- see below), horizontal rules, and GFM
tables, on top of the inline set (bold, italic, inline code, autolinked
URLs). This codebase's stated posture (see object_reader.py) is stdlib/
no-new-dependency on the server; the client-side JS here is unbundled
vanilla JS with no npm/build step at all, so a hand-rolled parser matches
how every other renderer in this codebase is built (forms, lists, detail
pages), not a gap to fill with a vendored library.

Deliberately NOT attempted: nested lists (one level only -- a nested `- `
line is treated as its own top-level bullet, which is wrong but never
silently drops content), tables with column alignment markers (`:---:`
parses as a plain separator, alignment is ignored), and fenced-block
diagram languages (a ```mermaid block renders as a plain code block here,
not a live-rendered diagram -- honestly worse than nothing for diagrams
specifically, but never worse than escaping the raw text). All three are
real, scoped follow-ups, not oversights to silently paper over.

Escapes the input FIRST, so untrusted text (an AI reply, a user note) can
never inject HTML -- markdown SYNTAX characters (#, *, `, -, >, |) survive
HTML-escaping unchanged, so escaping the whole raw line before recognizing
its block type is safe and is what every regex below assumes.
"""

_JS = r"""
(function () {
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

  // Inline transforms only -- applied to one line/cell's worth of text
  // that has ALREADY been through block-level recognition. Order matters:
  // inline code first (so `**not bold**` inside backticks is not touched
  // by the bold/italic passes that run after it).
  function renderInline(text) {
    let html = esc(text);
    html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    html = html.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
    html = html.replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
    return html;
  }

  function parseTableRow(line) {
    const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
    return trimmed.split("|").map((c) => c.trim());
  }
  const TABLE_SEPARATOR_RE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

  window.dbbasicMarkdown = function (text) {
    const lines = String(text == null ? "" : text).replace(/\r\n/g, "\n").split("\n");
    const out = [];
    let paragraph = [];
    let listType = null;      // "ul" | "ol" | null
    let listItems = [];
    let blockquote = [];

    function flushParagraph() {
      if (paragraph.length) { out.push("<p>" + renderInline(paragraph.join(" ")) + "</p>"); paragraph = []; }
    }
    function flushList() {
      if (listType) {
        out.push("<" + listType + ">" + listItems.map((it) => "<li>" + renderInline(it) + "</li>").join("") + "</" + listType + ">");
        listType = null; listItems = [];
      }
    }
    function flushBlockquote() {
      if (blockquote.length) { out.push("<blockquote>" + renderInline(blockquote.join(" ")) + "</blockquote>"); blockquote = []; }
    }
    function flushAll() { flushParagraph(); flushList(); flushBlockquote(); }

    let i = 0;
    while (i < lines.length) {
      const line = lines[i];

      const fence = line.match(/^```\s*([a-zA-Z0-9_+-]*)\s*$/);
      if (fence) {
        flushAll();
        const lang = fence[1];
        const codeLines = [];
        i++;
        while (i < lines.length && !/^```\s*$/.test(lines[i])) { codeLines.push(lines[i]); i++; }
        i++; // skip the closing fence (or run off the end if unterminated)
        const cls = lang ? ' class="language-' + esc(lang) + '"' : "";
        out.push("<pre><code" + cls + ">" + esc(codeLines.join("\n")) + "</code></pre>");
        continue;
      }

      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        flushAll();
        const level = heading[1].length;
        out.push("<h" + level + ">" + renderInline(heading[2].trim()) + "</h" + level + ">");
        i++;
        continue;
      }

      if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        flushAll();
        out.push("<hr>");
        i++;
        continue;
      }

      // GFM table: a "| a | b |" row immediately followed by a
      // "|---|---|"-shaped separator row.
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && TABLE_SEPARATOR_RE.test(lines[i + 1])) {
        flushAll();
        const headerCells = parseTableRow(line);
        i += 2;
        const bodyRows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { bodyRows.push(parseTableRow(lines[i])); i++; }
        let table = "<table><thead><tr>" + headerCells.map((c) => "<th>" + renderInline(c) + "</th>").join("") + "</tr></thead>";
        if (bodyRows.length) {
          table += "<tbody>" + bodyRows.map((row) =>
            "<tr>" + row.map((c) => "<td>" + renderInline(c) + "</td>").join("") + "</tr>"
          ).join("") + "</tbody>";
        }
        out.push(table + "</table>");
        continue;
      }

      const quote = line.match(/^>\s?(.*)$/);
      if (quote) {
        flushParagraph(); flushList();
        blockquote.push(quote[1]);
        i++;
        continue;
      }
      if (blockquote.length) flushBlockquote();

      const ul = line.match(/^[-*+]\s+(.*)$/);
      if (ul) {
        flushParagraph(); flushBlockquote();
        if (listType && listType !== "ul") flushList();
        listType = "ul";
        listItems.push(ul[1]);
        i++;
        continue;
      }

      const ol = line.match(/^\d+\.\s+(.*)$/);
      if (ol) {
        flushParagraph(); flushBlockquote();
        if (listType && listType !== "ol") flushList();
        listType = "ol";
        listItems.push(ol[1]);
        i++;
        continue;
      }
      if (listType) flushList();

      if (line.trim() === "") { flushParagraph(); i++; continue; }

      paragraph.push(line.trim());
      i++;
    }
    flushAll();
    return out.join("");
  };
})();
"""


def GET(request):
    return {"content_type": "application/javascript; charset=utf-8", "body": _JS}
