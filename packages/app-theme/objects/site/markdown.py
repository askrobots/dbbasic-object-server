"""Shared markdown renderer, served as one object at /markdown.

Any page includes <script src="/markdown"></script> and gets
`window.dbbasicMarkdown(text)` — defined once, reused everywhere (the
shell today; notes, articles, and Scroll-adjacent surfaces next).
Rendering markdown is a shared UI utility, so it lives in the design
system alongside /style and /nav, not copied into each page.

It escapes the input FIRST, so untrusted text (an AI reply, a user note)
can never inject HTML, then applies a few transforms for the common
cases: inline code, bold, links, headings, bullets, line breaks. Full
GFM (tables, nested lists, fenced blocks) is a deliberate future add in
this one place — never a second implementation.
"""

_JS = r"""
(function () {
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  window.dbbasicMarkdown = function (text) {
    return esc(text)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(https?:\/\/[^ <]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>')
      .replace(/^#{1,6} (.+)$/gm, "<strong>$1</strong>")
      .replace(/^\s*[-*] (.+)$/gm, "• $1")
      .replace(/\n/g, "<br>");
  };
})();
"""


def GET(request):
    return {"content_type": "application/javascript; charset=utf-8", "body": _JS}
