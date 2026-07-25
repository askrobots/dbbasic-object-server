"""site_flow -- the workflow, viewable: /flow and /flow/{collection}.

Dan's observation, which most web software never answered: apps forgot
help, and frameworks forgot docs. The reason was structural -- their
workflow existed only as the union of whatever every callback did, so any
documentation was hand-written and rotting from the moment it was saved.
Here the workflow is COMPILED (object_governance.py): transitions and
guards from the schema, the gate the schema names, reactions from HANDLES
declarations, notify rules and schedules from data. This page renders that
compilation live, so the docs cannot disagree with the system -- they are
the system, read back.

Per collection: the state machine drawn as SVG (server-side, zero JS
dependencies -- a diagram that needs a CDN is a diagram that breaks),
every transition with its guard, what gates writes, what reacts, what gets
composed, who gets notified, when the clock touches it, and where it
renders. Plus the Mermaid source in a copy block, because the same text
pastes into GitHub, docs, or a whiteboard tool.

Signed-in users only: the picture of how money moves through a system is
itself information about the system. No per-collection secrets are shown
beyond what the schema API already exposes, but the assembled map deserves
the same courtesy as the data it describes.
"""

import html
import math
import os

import object_governance
import object_schemas

_PAGE_ROOTS = None  # objects run from the server's roots; governance reads the same


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _esc(value):
    return html.escape(str(value or ""))


def _state_svg(transitions):
    """The state machine as a self-contained SVG.

    Hand-laid layout: states ring-positioned, edges as straight lines with
    small labels. Deliberately simple -- a six-state machine reads fine on
    a ring, and a machine too tangled for this picture is feedback about
    the machine, not the renderer.
    """
    if not transitions:
        return ""
    field, moves = next(iter(transitions.items()))
    states = sorted({s for s in moves} |
                    {t["to"] for targets in moves.values() for t in targets if t["to"]})
    n = len(states)
    if not n:
        return ""
    width, height = 640, max(300, 90 * n // 2 + 160)
    cx, cy = width / 2, height / 2
    radius = min(cx, cy) - 70
    pos = {}
    for i, state in enumerate(states):
        angle = (2 * math.pi * i / n) - math.pi / 2
        pos[state] = (cx + radius * math.cos(angle), cy + radius * math.sin(angle))

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" '
             f'style="max-width:100%;height:auto;background:var(--panel,#1a1a22);'
             f'border:1px solid var(--line,#333);border-radius:8px">']
    parts.append('<defs><marker id="arr" markerWidth="8" markerHeight="8" refX="7" refY="3" '
                 'orient="auto"><path d="M0,0 L7,3 L0,6 z" fill="currentColor"/></marker></defs>')
    for state, targets in moves.items():
        x1, y1 = pos[state]
        for target in targets:
            if not target["to"] or target["to"] not in pos:
                continue
            x2, y2 = pos[target["to"]]
            dx, dy = x2 - x1, y2 - y1
            dist = math.hypot(dx, dy) or 1
            # stop the line at the node's edge so the arrowhead is visible
            sx, sy = x1 + dx / dist * 34, y1 + dy / dist * 34
            ex, ey = x2 - dx / dist * 38, y2 - dy / dist * 38
            parts.append(f'<line x1="{sx:.0f}" y1="{sy:.0f}" x2="{ex:.0f}" y2="{ey:.0f}" '
                         f'stroke="currentColor" stroke-opacity="0.55" marker-end="url(#arr)"/>')
    for state in states:
        x, y = pos[state]
        parts.append(f'<g><circle cx="{x:.0f}" cy="{y:.0f}" r="32" fill="none" '
                     f'stroke="currentColor" stroke-opacity="0.9"/>'
                     f'<text x="{x:.0f}" y="{y:.0f}" text-anchor="middle" dominant-baseline="middle" '
                     f'fill="currentColor" font-size="12">{_esc(state)}</text></g>')
    parts.append(f'<text x="{width - 10}" y="{height - 10}" text-anchor="end" fill="currentColor" '
                 f'fill-opacity="0.5" font-size="11">{_esc(field)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _rows(items, render):
    return "".join(render(item) for item in items) or \
        '<tr><td colspan="9" class="muted">none</td></tr>'


def _collection_page(collection, base):
    info = object_governance.governs(collection, base_dir=base)
    mermaid = object_governance.workflow_mermaid(collection, base_dir=base)
    decl = info["declarations"]

    transitions_rows = []
    for field, moves in decl["transitions"].items():
        for state, targets in moves.items():
            for target in targets:
                guard = ", ".join(f"{k} = {v}" for k, v in (target["when"] or {}).items()) or "anyone with write access"
                transitions_rows.append(
                    f"<tr><td><code>{_esc(state)}</code> &rarr; <code>{_esc(target['to'])}</code></td>"
                    f"<td>{_esc(guard)}</td></tr>")
    body = f"""
<div class="breadcrumb"><a href="/">Home</a> / <a href="/flow">Flow</a> / {_esc(collection)}</div>
<h1>How <code>{_esc(collection)}</code> works</h1>
<p class="muted">Compiled from the schema, hooks, handlers and rules the server actually
enforces &mdash; this page cannot disagree with the system, because it is the system, read back.</p>
{_state_svg(decl["transitions"])}
<h2>Transitions &amp; who may make them</h2>
<table><thead><tr><th>Move</th><th>Guard</th></tr></thead>
<tbody>{''.join(transitions_rows) or '<tr><td colspan="2" class="muted">no state machine — plain records</td></tr>'}</tbody></table>
<h2>Gates (before the write, fail closed)</h2>
<table><tbody>{_rows(info["gates"], lambda g: f"<tr><td><code>{_esc(g['object_id'])}</code></td><td>{_esc(g['note'])}</td></tr>")}</tbody></table>
<h2>Reactions (after the write, never block it)</h2>
<table><tbody>{_rows(info["reactions"], lambda r: f"<tr><td><code>{_esc(r['object_id'])}</code></td><td>on {_esc(r['on'])}</td></tr>")}</tbody></table>
<h2>Composes into</h2>
<table><tbody>{_rows(info["feeds"], lambda f: f"<tr><td><code>{_esc(f)}</code> <span class='muted'>(stamped generated_from)</span></td></tr>")}</tbody></table>
<h2>Notifications</h2>
<table><tbody>{_rows(info["notifications"], lambda n: f"<tr><td>{_esc(n['event'])}</td><td>{_esc(n['recipients'])}</td></tr>")}</tbody></table>
<h2>Derived fields</h2>
<table><tbody>{_rows(list(decl["formulas"].items()) + list(decl["rollups"].items()), lambda kv: f"<tr><td><code>{_esc(kv[0])}</code></td><td><code>{_esc(kv[1])}</code></td></tr>")}</tbody></table>
<h2>Rendered at</h2>
<table><tbody>{_rows(info["views"], lambda v: f"<tr><td><a href='{_esc(v['route'] or '')}'>{_esc(v['route'] or v['view'])}</a></td></tr>")}</tbody></table>
<h2>Mermaid source</h2>
<p class="muted">The same diagram as text &mdash; paste it into GitHub, docs, or any tool that speaks Mermaid.</p>
<pre style="overflow-x:auto">{_esc(mermaid)}</pre>
"""
    return body


def _index_page(base):
    items = []
    for summary in object_schemas.list_schemas(base_dir=base):
        name = summary.get("name") or summary.get("collection")
        if not name:
            continue
        items.append(f'<li><a href="/flow/{_esc(name)}"><code>{_esc(name)}</code></a></li>')
    return f"""
<div class="breadcrumb"><a href="/">Home</a> / Flow</div>
<h1>How things work</h1>
<p class="muted">One page per collection: its state machine, who may move it, what gates
writes, what reacts, what gets composed, and where it renders &mdash; compiled live from
the declarations the server enforces. The docs most systems forgot, generated so they
cannot rot.</p>
<ul class="accountlist">{''.join(sorted(items))}</ul>
"""


def GET(request):
    identity = request.get("_identity") or {}
    if not identity.get("user_id"):
        body = ('<p class="hint"><a href="/login?next=/flow">Sign in</a> to see how the '
                "system's collections work &mdash; their state machines, gates, and reactions.</p>")
    else:
        base = _base_dir()
        collection = str(request.get("collection") or "").strip()
        if collection and object_schemas.validate_schema_name(collection):
            body = _collection_page(collection, base)
        else:
            body = _index_page(base)

    return {"content_type": "text/html; charset=utf-8", "body": f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flow</title>
<link rel="stylesheet" href="/style">
<style>
.wrap {{ max-width: 900px; margin: 0 auto; padding: 1.25rem; }}
table {{ width: 100%; border-collapse: collapse; margin: 0.25rem 0 1rem; }}
td, th {{ text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--line, #333); }}
h2 {{ font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted, #999); margin: 1.25rem 0 0.25rem; }}
.muted {{ color: var(--muted, #999); }}
svg {{ margin: 0.75rem 0; color: var(--text, #eee); }}
</style>
</head>
<body>
<script src="/nav"></script>
<div class="wrap">{body}</div>
</body>
</html>"""}
