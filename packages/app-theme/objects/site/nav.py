"""The app shell / navigation bar, served as one script at /nav.

Every page includes <script src="/nav"></script> and gets a persistent
top bar: brand + app switcher, global search (Cmd/Ctrl-K) over
/api/search, an Ask-AI link to the shell, a notification bell reading
the notifications collection, and a user menu (appearance, sign out).

The bell polls today; it is written so a websocket message can call the
same renderNotes() to make it live the moment realtime push lands —
auto-update on events is the thing the old stack could not do cleanly.
"""

_JS = r"""
(function () {
  if (document.getElementById("dbbasic-appbar")) return;

  const APPS = [
    ["/shell", "Shell"], ["/talk", "Talk"], ["/notes", "Notes"], ["/tasks", "Tasks"],
    ["/projects", "Projects"], ["/contacts", "Contacts"], ["/articles", "Articles"],
    ["/links", "Links"], ["/calendar", "Calendar"], ["/files", "Files"],
    ["/invoices", "Invoices"], ["/products", "Products"], ["/orders", "Orders"],
    ["/stock", "Stock"], ["/locations", "Locations"],
    ["/accounts", "Accounts"], ["/journals", "Journals"], ["/trial-balance", "Trial Balance"],
    ["/activity", "Activity"], ["/forum", "Forum"],
    ["/dashboard", "Dashboard"], ["/appearance", "Appearance"],
  ];
  const HIT_URL = {
    notes: (id) => "/notes/" + encodeURIComponent(id),
    articles: (id) => "/articles/" + encodeURIComponent(id),
    files: (id) => "/api/files/" + encodeURIComponent(id),
    views: (id) => "/views/" + encodeURIComponent(id),
    tasks: () => "/tasks", projects: () => "/projects", contacts: () => "/contacts",
    organizations: () => "/contacts", interactions: () => "/contacts",
    links: () => "/links", events: () => "/calendar",
    invoices: () => "/invoices", templates: () => "/templates", products: (id) => "/products/" + encodeURIComponent(id), orders: (id) => "/orders/" + encodeURIComponent(id),
    forum_categories: () => "/forum", forum_topics: (id) => "/forum/topics/" + encodeURIComponent(id), fin_accounts: () => "/accounts", fin_journals: (id) => "/journals/" + encodeURIComponent(id), locations: () => "/locations",
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const api = (path) => fetch(path, { credentials: "same-origin", headers: { accept: "application/json" } });

  const bar = document.createElement("div");
  bar.className = "appbar";
  bar.id = "dbbasic-appbar";
  bar.innerHTML =
    '<a class="brand" href="/">DBBASIC</a>' +
    '<button class="navbtn" id="nav-apps">Apps ▾</button>' +
    '<div class="search"><input id="nav-search" placeholder="Search everything…" autocomplete="off">' +
    '<span class="kbd">⌘K</span></div>' +
    '<span class="spacer"></span>' +
    '<a class="navbtn accent" href="/shell">Ask AI</a>' +
    '<button class="navbtn" id="nav-bell" title="Notifications">◉<span class="count" id="nav-count" style="display:none">0</span></button>' +
    '<button class="navbtn" id="nav-user">…</button>';
  document.body.insertBefore(bar, document.body.firstChild);
  document.body.classList.add("has-appbar");

  const menus = {};
  function menu(id) {
    if (menus[id]) return menus[id];
    const m = document.createElement("div");
    m.className = "navmenu";
    m.id = id;
    document.body.appendChild(m);
    menus[id] = m;
    return m;
  }
  function place(m, anchor, right) {
    const r = anchor.getBoundingClientRect();
    m.style.left = right ? "auto" : r.left + "px";
    m.style.right = right ? (window.innerWidth - r.right) + "px" : "auto";
  }
  function closeAll(except) {
    Object.values(menus).forEach((m) => { if (m !== except) m.classList.remove("open"); });
  }
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".navmenu") && !e.target.closest(".appbar .navbtn")) closeAll();
  });

  // App switcher
  const appsBtn = document.getElementById("nav-apps");
  const appsMenu = menu("nav-apps-menu");
  appsMenu.innerHTML = APPS.map(([u, n]) => '<a href="' + u + '">' + esc(n) + "</a>").join("");
  appsBtn.addEventListener("click", () => {
    const open = appsMenu.classList.contains("open"); closeAll();
    if (!open) { place(appsMenu, appsBtn); appsMenu.classList.add("open"); }
  });

  // Pinned views (app-views, optional): append any pinned view record to
  // the Apps switcher. The nav ships in every install, app-views does not,
  // so a missing collection or a failed fetch must stay silent -- the
  // switcher already works without it.
  (async function loadPinnedViews() {
    try {
      const res = await api("/collections/views/records?limit=200");
      if (!res.ok) return;
      const body = await res.json();
      const pinned = (body.records || []).filter((v) => v.pinned === "true");
      if (!pinned.length) return;
      appsMenu.innerHTML += pinned.map((v) =>
        '<a href="' + esc(v.route || ("/views/" + v.id)) + '">' + esc(v.title || "View") + "</a>").join("");
    } catch (e) { /* app-views not installed -- the switcher still works without it */ }
  })();

  // Global search
  const search = document.getElementById("nav-search");
  const resMenu = menu("nav-results"); resMenu.classList.add("results");
  let timer = null;
  async function runSearch() {
    const q = search.value.trim();
    if (!q) { resMenu.classList.remove("open"); return; }
    const res = await api("/api/search?q=" + encodeURIComponent(q) + "&limit=6");
    if (!res.ok) return;
    const body = await res.json();
    const groups = Object.entries(body.results || {}).filter(([, hits]) => hits.length);
    if (!groups.length) { resMenu.innerHTML = '<div class="head">no matches</div>'; }
    else {
      resMenu.innerHTML = groups.map(([col, hits]) =>
        '<div class="head">' + esc(col) + "</div>" +
        hits.map((h) => {
          const label = esc(h.title || h.name || h.number || h.subject || h.body
            || h.content || h.comment || h.description || h.first_name || "(untitled)").slice(0, 80);
          const url = (HIT_URL[col] ? HIT_URL[col](h.id) : null);
          return url ? '<a class="hit item" href="' + url + '">' + label + "</a>"
                     : '<div class="hit item">' + label + "</div>";
        }).join("")
      ).join("");
    }
    place(resMenu, search); resMenu.classList.add("open");
  }
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(runSearch, 200); });
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); search.focus(); }
    if (e.key === "Escape") closeAll();
  });

  // Notifications (poll now; a websocket can call renderNotes() later)
  const bell = document.getElementById("nav-bell");
  const count = document.getElementById("nav-count");
  const notesMenu = menu("nav-notes");
  function renderNotes(records) {
    const unread = records.filter((n) => n.is_read !== "true");
    if (unread.length) { count.textContent = unread.length > 9 ? "9+" : unread.length; count.style.display = ""; }
    else count.style.display = "none";
    notesMenu.innerHTML = '<div class="head">notifications</div>' +
      (records.length ? records.slice(-8).reverse().map((n) =>
        '<div class="item">' + esc(n.body || "") + "</div>").join("")
        : '<div class="item" style="color:var(--muted)">nothing yet</div>');
  }
  async function refreshNotes() {
    const res = await api("/collections/notifications/records?limit=50");
    if (res.ok) { const b = await res.json(); renderNotes(b.records || []); }
  }
  window.dbbasicRenderNotes = renderNotes;

  // Realtime: live push over a websocket, with the 20s poll as fallback.
  const subs = {};            // collection -> [handlers]
  let ws = null, retry = 1000;
  function subscribe(collection, handler) {
    (subs[collection] = subs[collection] || []).push(handler);
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ action: "subscribe", collections: [collection] }));
  }
  window.dbbasicSubscribe = subscribe;   // pages can follow their own collection
  function connectRealtime() {
    try {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(proto + "://" + location.host + "/ws");
    } catch (e) { return; }
    ws.onopen = () => {
      retry = 1000;
      const cols = Object.keys(subs);
      if (cols.length) ws.send(JSON.stringify({ action: "subscribe", collections: cols }));
    };
    ws.onmessage = (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.type === "record" && subs[m.collection]) subs[m.collection].forEach((h) => h(m));
    };
    ws.onclose = () => { ws = null; setTimeout(connectRealtime, retry); retry = Math.min(retry * 2, 30000); };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }

  bell.addEventListener("click", () => {
    const open = notesMenu.classList.contains("open"); closeAll();
    if (!open) { place(notesMenu, bell, true); notesMenu.classList.add("open"); }
  });

  // User menu
  const userBtn = document.getElementById("nav-user");
  const userMenu = menu("nav-user-menu");
  async function loadUser() {
    let name = null;
    try {
      const res = await api("/identity/session");
      if (res.ok) { const b = await res.json(); name = (b.session || b).user_id || b.user_id; }
    } catch (e) {}
    if (name) {
      userBtn.textContent = name + " ▾";
      userMenu.innerHTML =
        '<div class="head">' + esc(name) + "</div>" +
        '<a href="/appearance">Appearance</a>' +
        '<button class="item" id="nav-signout">Sign out</button>';
      userMenu.querySelector("#nav-signout").addEventListener("click", async () => {
        await fetch("/logout", { method: "POST", credentials: "same-origin" });
        location.href = "/";
      });
      refreshNotes();
      setInterval(refreshNotes, 20000);        // fallback poll
      subscribe("notifications", refreshNotes); // live push updates the bell instantly
      connectRealtime();
    } else {
      userBtn.textContent = "Sign in";
      userBtn.onclick = () => { location.href = "/login?next=" + encodeURIComponent(location.pathname); };
      bell.style.display = "none";
    }
  }
  userBtn.addEventListener("click", () => {
    if (userBtn.textContent === "Sign in") return;
    const open = userMenu.classList.contains("open"); closeAll();
    if (!open) { place(userMenu, userBtn, true); userMenu.classList.add("open"); }
  });
  loadUser();
})();
"""


def GET(request):
    return {"content_type": "application/javascript; charset=utf-8", "body": _JS}
