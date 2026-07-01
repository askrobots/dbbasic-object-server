def GET(request):
    count = int(_state_manager.get("served", 0) or 0) + 1
    _state_manager.set("served", count)
    _logger.info("system_dashboard served", count=count, response_type="html")

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DBBASIC Object Dashboard</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0b0b10;
      --panel: #17171f;
      --panel-2: #101018;
      --line: #2b2b37;
      --text: #f4f4f7;
      --muted: #a2a2ad;
      --green: #52d273;
      --blue: #5aa7ff;
      --amber: #f1b747;
      --red: #ff6b6b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .shell {{
      display: grid;
      grid-template-columns: 72px 1fr;
      min-height: 100vh;
    }}
    .rail {{
      border-right: 1px solid var(--line);
      background: #0f0f16;
      padding: 16px 12px;
    }}
    .mark {{
      display: grid;
      place-items: center;
      width: 42px;
      height: 42px;
      border: 1px solid #38384a;
      border-radius: 8px;
      background: #191928;
      color: #c7bfff;
      font-weight: 800;
      letter-spacing: 0;
    }}
    main {{
      padding: 28px clamp(20px, 5vw, 72px) 40px;
    }}
    .topbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 56px;
    }}
    .eyebrow {{
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
      letter-spacing: .08em;
      text-transform: uppercase;
    }}
    h1 {{
      max-width: 780px;
      margin: 0 0 18px;
      font-size: clamp(42px, 7vw, 88px);
      line-height: .98;
      letter-spacing: 0;
    }}
    .lead {{
      max-width: 760px;
      margin: 0;
      color: #d3d3dc;
      font-size: clamp(18px, 2.2vw, 24px);
    }}
    .badges {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 24px;
    }}
    .badge {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      color: #dfdff0;
      padding: 9px 12px;
      font-weight: 700;
    }}
    .badge.ok {{ color: var(--green); }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 16px;
      margin-top: 64px;
    }}
    .card {{
      min-height: 170px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 20px;
    }}
    .card h2 {{
      margin: 0 0 12px;
      font-size: 18px;
      letter-spacing: 0;
    }}
    .card p {{
      margin: 0;
      color: var(--muted);
    }}
    .metric {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      border-top: 1px solid var(--line);
      padding-top: 12px;
      margin-top: 18px;
      color: #dfdff0;
      font-weight: 700;
    }}
    .status-list {{
      display: grid;
      gap: 10px;
      margin-top: 14px;
    }}
    .row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      color: var(--muted);
    }}
    .pill {{
      border-radius: 999px;
      border: 1px solid var(--line);
      padding: 4px 9px;
      color: #dfdff0;
      background: var(--panel-2);
      font-size: 13px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .pill.ok {{ color: var(--green); }}
    .pill.warn {{ color: var(--amber); }}
    .pill.err {{ color: var(--red); }}
    code {{
      color: #d9f06b;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: .95em;
    }}
    footer {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      margin-top: 44px;
      color: #7e7e89;
      font-size: 14px;
    }}
    @media (max-width: 900px) {{
      .shell {{ grid-template-columns: 1fr; }}
      .rail {{ display: none; }}
      .topbar {{ margin-bottom: 36px; }}
      .grid {{ grid-template-columns: 1fr; margin-top: 42px; }}
      footer {{ flex-direction: column; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <aside class="rail" aria-hidden="true">
      <div class="mark">DB</div>
    </aside>
    <main>
      <div class="topbar">
        <div>
          <div class="eyebrow">object.dbbasic.com</div>
          <strong>DBBASIC Object Dashboard</strong>
        </div>
        <span class="pill ok" id="health-pill">checking health</span>
      </div>

      <section>
        <h1>Live object runtime, guarded staging surface.</h1>
        <p class="lead">
          This dashboard is installed as a DBBASIC package and served by one Python object.
          It is meant to prove the open-source object loop before broader write routes are exposed.
        </p>
        <div class="badges">
          <span class="badge ok">ASGI runtime</span>
          <span class="badge">package-installed object</span>
          <span class="badge">source writes blocked publicly</span>
          <span class="badge">admin APIs allowlisted later</span>
        </div>
      </section>

      <section class="grid" aria-label="Runtime summary">
        <article class="card">
          <h2>Object</h2>
          <p><code>system_dashboard</code> keeps its own state and logs while rendering this page.</p>
          <div class="metric">
            <span>served</span>
            <span>{count} times</span>
          </div>
        </article>

        <article class="card">
          <h2>Public Routes</h2>
          <div class="status-list">
            <div class="row"><span><code>/</code></span><span class="pill ok">site_home</span></div>
            <div class="row"><span><code>/dashboard</code></span><span class="pill ok">this object</span></div>
            <div class="row"><span><code>/health</code></span><span class="pill ok" id="health-row">checking</span></div>
            <div class="row"><span><code>/identity/session</code></span><span class="pill warn" id="session-row">checking</span></div>
          </div>
        </article>

        <article class="card">
          <h2>Write Surface</h2>
          <p>Collection records and object source writes exist, but public staging keeps them behind admin/session policy.</p>
          <div class="status-list">
            <div class="row"><span>collection writes</span><span class="pill warn">admin gated</span></div>
            <div class="row"><span>object source writes</span><span class="pill warn">disabled publicly</span></div>
            <div class="row"><span>package installs</span><span class="pill warn">reviewed only</span></div>
          </div>
        </article>
      </section>

      <footer>
        <span>Installed from <code>packages/system-dashboard</code>.</span>
        <span>Next: login/session minting, permission-on-by-default, then Scroll writes.</span>
      </footer>
    </main>
  </div>
  <script>
    async function checkJson(path, expectedStatus) {{
      try {{
        const response = await fetch(path, {{ cache: "no-store" }});
        const data = await response.json().catch(() => ({{}}));
        return {{ ok: response.status === expectedStatus, status: response.status, data }};
      }} catch (error) {{
        return {{ ok: false, status: 0, data: {{ error: String(error) }} }};
      }}
    }}

    checkJson("/health", 200).then((result) => {{
      const top = document.getElementById("health-pill");
      const row = document.getElementById("health-row");
      const text = result.ok ? "healthy" : "error " + result.status;
      top.textContent = text;
      row.textContent = text;
      top.className = result.ok ? "pill ok" : "pill err";
      row.className = result.ok ? "pill ok" : "pill err";
    }});

    checkJson("/identity/session", 401).then((result) => {{
      const row = document.getElementById("session-row");
      row.textContent = result.ok ? "401 without token" : "check failed";
      row.className = result.ok ? "pill warn" : "pill err";
    }});
  </script>
</body>
</html>"""
    return html, "text/html; charset=utf-8"
