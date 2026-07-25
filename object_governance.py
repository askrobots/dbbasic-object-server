"""What governs a collection -- and the workflow diagram, compiled.

There is deliberately no workflow engine here (docs/write-pipeline.md): a
business process is transitions on records plus reactions that write the
next record. The objection to that design is legibility -- "without an
engine, how does anyone SEE the workflow?" -- and this module is the
answer: **because every kind of rule has exactly one declared home, the
diagram is compilable.** Transitions and their guards live in the schema;
gates are named by `hooks.before_write`; reactions declare `HANDLES`
lists; notifications are `notify_rules` rows; the clock's work is
scheduler task entries. None of it is arbitrary code hiding in a
callback, which is precisely why Rails and Django could never draw this
picture: their workflow existed only as the union of whatever every
callback happened to do.

Two outputs, one truth:

- ``governs(collection)`` -- the placement table for one collection, live:
  every declaration, gate, reaction, notification and schedule that will
  touch a write, with where each lives. "Why did my record change?" as a
  data structure.
- ``workflow_mermaid(collection)`` -- the state machine plus the
  cross-collection reaction edges, as Mermaid source. Text on purpose:
  it renders in the docs, in GitHub, in the shell, and in any tool that
  speaks Mermaid, and it diffs in the change log like everything else.

Read-only folds; never raises for a missing layer -- an app with no hooks
simply has no gate rows, which is itself the honest report.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

import object_namespace
import object_records
import object_schemas
from object_versions import DEFAULT_DATA_DIR

_HANDLES_RE = re.compile(r"^HANDLES\s*=\s*(\[.*?\])", re.MULTILINE | re.DOTALL)
_EVENT_RE = re.compile(r"^([a-z0-9_]+)\.record\.(created|updated|deleted)$")


def _safe_records(collection: str, base_dir) -> list[dict]:
    try:
        return object_records.read_collection_records(collection, base_dir=base_dir)
    except Exception:
        return []


def _schema(collection: str, base_dir) -> dict | None:
    try:
        return object_schemas.get_schema(collection, base_dir=base_dir)
    except Exception:
        return None


def _handler_index(roots: Iterable[Path] | None = None) -> list[dict]:
    """Every HANDLES declaration across the object roots.

    Parsed from source with a regex rather than imported: an index must
    never execute the objects it is indexing, and a module-level HANDLES
    list is by convention a literal (the same convention the real
    dispatcher relies on).
    """
    out = []
    for root in (list(roots) if roots is not None else object_namespace.get_object_roots()):
        root = Path(root)
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            match = _HANDLES_RE.search(source)
            if not match:
                continue
            raw = match.group(1).replace("'", '"')
            raw = re.sub(r",\s*\]", "]", raw)   # Python allows a trailing comma; JSON does not
            try:
                events = json.loads(raw)
            except (ValueError, TypeError):
                continue
            try:
                object_id = object_namespace.object_id_from_path(path, root)
            except ValueError:
                continue
            parsed = []
            for event in events:
                m = _EVENT_RE.match(str(event))
                if m:
                    parsed.append({"collection": m.group(1), "action": m.group(2)})
            if parsed:
                out.append({"object_id": object_id, "events": parsed, "path": str(path)})
    return out


def governs(collection: str, *, base_dir=DEFAULT_DATA_DIR,
            roots: Iterable[Path] | None = None) -> dict[str, Any]:
    """The live placement table for one collection.

    Returns {"collection", "declarations", "gates", "reactions",
    "notifications", "schedules", "views"} -- each entry naming WHAT the
    rule is and WHERE it lives, which is the whole point: the seven layers
    that may act on a write, answered in one call instead of seven files.
    """
    schema = _schema(collection, base_dir) or {}
    fields = schema.get("fields", [])

    declarations: dict[str, Any] = {
        "required": [f["name"] for f in fields if f.get("required")],
        "enums": {f["name"]: f["enum"] for f in fields if f.get("enum")},
        "relations": {f["name"]: f["relation"]["collection"]
                      for f in fields if isinstance(f.get("relation"), dict)},
        "formulas": {f["name"]: f["formula"] for f in fields if f.get("formula")},
        "rollups": {f["name"]: f["rollup"] for f in fields if f.get("rollup")},
        "transitions": {},
    }
    for field in fields:
        if not field.get("transitions"):
            continue
        moves = {}
        for state, targets in field["transitions"].items():
            normalized = []
            for target in targets:
                if isinstance(target, dict):
                    normalized.append({"to": target.get("to"),
                                       "when": target.get("when") or None})
                else:
                    normalized.append({"to": target, "when": None})
            moves[state] = normalized
        declarations["transitions"][field["name"]] = moves

    hook_id = (schema.get("hooks") or {}).get("before_write")
    gates = [{"kind": "before_write_hook", "object_id": hook_id,
              "note": "fails closed; runs before validation on every create/update"}] if hook_id else []

    reactions, feeds = [], []
    for handler in _handler_index(roots):
        for event in handler["events"]:
            if event["collection"] == collection:
                reactions.append({"object_id": handler["object_id"],
                                  "on": event["action"]})
    # the other direction: which collections' handlers THIS collection's
    # composers feed is visible in live data via generated_from provenance
    for row in _safe_records("fin_journals", base_dir):
        src = str(row.get("generated_from") or "")
        if src.startswith(f"{collection}/"):
            feeds.append("fin_journals")
            break

    notifications = [
        {"rule_id": r.get("id"), "event": r.get("event_pattern"),
         "recipients": r.get("recipients")}
        for r in _safe_records("notify_rules", base_dir)
        if str(r.get("event_pattern") or "").startswith(f"{collection}.")
    ]

    schedules = []
    try:
        state = __import__("object_state").get_object_state("scheduler", base_dir)
        for key, value in state.items():
            if not key.startswith("task_"):
                continue
            try:
                task = json.loads(value)
            except (ValueError, TypeError):
                continue
            schedules.append({"task": task.get("id"), "runs": task.get("object_id"),
                              "schedule": task.get("schedule"),
                              "status": task.get("status")})
    except Exception:
        pass

    views = [{"view": r.get("id"), "route": r.get("route")}
             for r in _safe_records("views", base_dir)
             if collection in str(r.get("blocks") or "")]

    return {
        "collection": collection,
        "declarations": declarations,
        "gates": gates,
        "reactions": sorted(reactions, key=lambda r: (r["object_id"], r["on"])),
        "feeds": sorted(set(feeds)),
        "notifications": notifications,
        "schedules": schedules,
        "views": views,
    }


def workflow_mermaid(collection: str, *, base_dir=DEFAULT_DATA_DIR,
                     roots: Iterable[Path] | None = None) -> str:
    """The collection's workflow as Mermaid source: the declared state
    machine, guard labels, the gate, and every reaction edge.

    Compiled, not drawn: change a transition or install a handler and the
    next render shows it. A diagram that is generated from the same
    declarations the server enforces cannot drift from the truth -- which
    is the failing of every hand-maintained workflow picture.
    """
    info = governs(collection, base_dir=base_dir, roots=roots)
    lines = ["flowchart LR"]

    transitions = info["declarations"]["transitions"]
    if transitions:
        field, moves = next(iter(transitions.items()))
        states = sorted({s for s in moves} |
                        {t["to"] for targets in moves.values() for t in targets if t["to"]})
        lines.append(f'  subgraph {collection}["{collection}.{field}"]')
        for state in states:
            lines.append(f'    {collection}_{state}(["{state}"])')
        for state, targets in moves.items():
            for target in targets:
                if not target["to"]:
                    continue
                label = ""
                if target["when"]:
                    conds = ", ".join(f"{k}={v}" for k, v in target["when"].items())
                    label = f'|"{conds}"|'
                lines.append(f"    {collection}_{state} -->{label} {collection}_{target['to']}")
        lines.append("  end")
    else:
        lines.append(f'  {collection}[("{collection}")]')

    # Edges attach to the subgraph id (the collection) so the picture stays
    # readable: gates guard the whole write surface, reactions fire on the
    # whole collection, not on one state.
    anchor = collection

    for gate in info["gates"]:
        lines.append(f'  {gate["object_id"]}{{{{"gate: {gate["object_id"]}"}}}}')
        lines.append(f'  {gate["object_id"]} -.->|"before every write"| {anchor}')
    declared = set()
    for reaction in info["reactions"]:
        node = reaction["object_id"]
        if node not in declared:
            lines.append(f'  {node}[/"{node}"/]')
            declared.add(node)
        lines.append(f'  {anchor} -->|"on {reaction["on"]}"| {node}')
    for fed in info["feeds"]:
        lines.append(f'  {fed}[("{fed}")]')
        lines.append(f'  {anchor} ==>|"composes (generated_from)"| {fed}')
    for note in info["notifications"]:
        rid = re.sub(r"\W", "_", str(note["rule_id"] or "notify"))
        lines.append(f'  n_{rid}>"notify: {note["event"]}"]')
        lines.append(f'  {anchor} -.-> n_{rid}')
    # Scheduled runners that react to this collection get a clock edge, so
    # "time does this" is visible in the same picture as "writes do this".
    reaction_ids = {r["object_id"] for r in info["reactions"]}
    for task in info["schedules"]:
        if task.get("runs") in reaction_ids:
            lines.append(f'  clock_{task["task"]}(("{task["schedule"]}"))')
            lines.append(f'  clock_{task["task"]} -.-> {task["runs"]}')
    return "\n".join(lines)
