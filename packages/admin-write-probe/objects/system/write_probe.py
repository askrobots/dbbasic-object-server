from datetime import datetime, timezone


def GET(request):
    served = int(_state_manager.get("served", 0) or 0) + 1
    now = datetime.now(timezone.utc).isoformat()
    _state_manager.set("served", served)
    _state_manager.set("last_seen", now)
    _logger.info("system_write_probe served", count=served)

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DBBASIC Write Probe</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0b0b10;
      --panel: #17171f;
      --panel-2: #101018;
      --text: #f4f4f7;
      --muted: #a2a2ad;
      --line: #38384a;
      --green: #52d273;
      --amber: #f1b747;
      --blue: #54a7ff;
      --red: #ff6b6b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      display: grid;
      place-items: center;
      padding: 48px 24px;
    }}
    main {{
      width: min(1120px, 100%);
      display: grid;
      gap: 20px;
    }}
    header {{
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: flex-start;
      border-bottom: 1px solid var(--line);
      padding-bottom: 24px;
    }}
    h1 {{
      margin: 0;
      font-size: clamp(42px, 6vw, 76px);
      line-height: .95;
      letter-spacing: 0;
    }}
    p {{
      margin: 12px 0 0;
      color: var(--muted);
      max-width: 740px;
    }}
    code {{
      font: 14px/1.4 SFMono-Regular, Menlo, Consolas, monospace;
      color: var(--text);
      background: var(--panel-2);
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 2px 6px;
    }}
    .badge-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 20px;
    }}
    .badge {{
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 6px 10px;
      color: var(--muted);
      background: var(--panel);
      font-size: 13px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .ok {{ color: var(--green); }}
    .warn {{ color: var(--amber); }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 16px;
    }}
    .card {{
      min-height: 160px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 20px;
    }}
    h2 {{
      margin: 0 0 12px;
      font-size: 18px;
      letter-spacing: 0;
    }}
    .row {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      border-top: 1px solid var(--line);
      padding: 10px 0;
      color: var(--muted);
    }}
    .row:first-of-type {{ border-top: 0; }}
    .row span:last-child {{
      color: var(--text);
      text-align: right;
      font-weight: 700;
    }}
    .terminal {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #050506;
      overflow-x: auto;
      padding: 16px;
      color: #d9ecff;
      font: 13px/1.55 SFMono-Regular, Menlo, Consolas, monospace;
    }}
    .terminal .comment {{ color: var(--muted); }}
    .terminal .method {{ color: var(--green); }}
    .terminal .url {{ color: var(--blue); }}
    footer {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      color: #787887;
      font-size: 13px;
    }}
    @media (max-width: 900px) {{
      header {{ display: grid; }}
      .grid {{ grid-template-columns: 1fr; }}
      footer {{ display: grid; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>DBBASIC Write Probe</h1>
        <p>
          This page is served by one installed object. It writes only its own object state.
          Collection record writes stay behind the server admin-token gate.
        </p>
        <div class="badge-row">
          <span class="badge ok">object state write: on</span>
          <span class="badge warn">collection writes: admin-token gated</span>
          <span class="badge">source writes: disabled by default</span>
        </div>
      </div>
      <div class="card">
        <h2>Probe Object</h2>
        <div class="row"><span>object</span><span>system_write_probe</span></div>
        <div class="row"><span>served</span><span>{served} times</span></div>
        <div class="row"><span>last seen</span><span>{now}</span></div>
      </div>
    </header>

    <section class="grid" aria-label="write surfaces">
      <article class="card">
        <h2>Object State</h2>
        <div class="row"><span>store</span><span>state TSV</span></div>
        <div class="row"><span>key</span><span>served</span></div>
        <div class="row"><span>route</span><span>/admin/write-probe</span></div>
      </article>
      <article class="card">
        <h2>Collection Records</h2>
        <div class="row"><span>collection</span><span>dbbasic_probe</span></div>
        <div class="row"><span>schema</span><span>installed</span></div>
        <div class="row"><span>auth</span><span>admin token</span></div>
      </article>
      <article class="card">
        <h2>Public Staging</h2>
        <div class="row"><span>listing</span><span>blocked</span></div>
        <div class="row"><span>source writes</span><span>off</span></div>
        <div class="row"><span>route policy</span><span>allowlist</span></div>
      </article>
    </section>

    <section class="terminal" aria-label="admin write probe example">
      <div><span class="comment"># narrow public route, still server-gated by Authorization</span></div>
      <div><span class="method">POST</span> <span class="url">/collections/dbbasic_probe/records</span></div>
      <div>{{"id":"probe_001","status":"created","note":"admin write test"}}</div>
      <br>
      <div><span class="method">PUT</span> <span class="url">/collections/dbbasic_probe/records/probe_001</span></div>
      <div>{{"status":"updated","note":"update path works"}}</div>
      <br>
      <div><span class="method">DELETE</span> <span class="url">/collections/dbbasic_probe/records/probe_001</span></div>
    </section>

    <footer>
      <span>Installed from <code>packages/admin-write-probe</code>.</span>
      <span>Designed to prove write paths before broader admin APIs are exposed.</span>
    </footer>
  </main>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
