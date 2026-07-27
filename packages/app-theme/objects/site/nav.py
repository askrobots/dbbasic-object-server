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

  // Fallback only. The switcher is a fold over the nav registry
  // (action_nav_entries -> the nav_entries collection every package
  // writes on install); this array is what gets drawn if that call
  // fails. It is deliberately NOT maintained -- a second maintained list
  // is the exact drift this registry was built to end -- but a nav that
  // vanishes on a fetch error is worse than one that is slightly stale,
  // so a stale door beats no door.
  const APPS = [
    ["/shell", "Shell"], ["/talk", "Talk"], ["/notes", "Notes"], ["/tasks", "Tasks"], ["/templates", "Templates"],
    ["/projects", "Projects"], ["/contacts", "Contacts"], ["/articles", "Articles"],
    ["/links", "Links"], ["/calendar", "Calendar"], ["/files", "Files"],
    ["/invoices", "Invoices"], ["/products", "Products"], ["/orders", "Orders"],
    ["/stock", "Stock"], ["/locations", "Locations"],
    ["/accounts", "Accounts"], ["/journals", "Journals"], ["/trial-balance", "Trial Balance"],
    ["/activity", "Activity"], ["/forum", "Forum"], ["/profile/edit", "Profile"], ["/inbox", "Inbox"],
    ["/dashboard", "Dashboard"], ["/appearance", "Appearance"],
  ];
  // The third hand-maintained list, still hand-maintained: a search hit
  // needs a URL for a RECORD, which the nav registry does not model (it
  // registers doors, not permalinks). Deliberately left alone rather
  // than half-folded into something that does not fit.
  const HIT_URL = {
    notes: (id) => "/notes/" + encodeURIComponent(id),
    articles: (id) => "/articles/" + encodeURIComponent(id),
    files: (id) => "/api/files/" + encodeURIComponent(id),
    views: (id) => "/views/" + encodeURIComponent(id),
    tasks: () => "/tasks", projects: () => "/projects", contacts: () => "/contacts",
    organizations: () => "/contacts", interactions: () => "/contacts",
    links: () => "/links", events: () => "/calendar",
    invoices: () => "/invoices", templates: () => "/templates", products: (id) => "/products/" + encodeURIComponent(id), orders: (id) => "/orders/" + encodeURIComponent(id),
    forum_categories: () => "/forum", forum_topics: (id) => "/forum/topics/" + encodeURIComponent(id), profiles: (id) => "/u/" + encodeURIComponent(id), message_threads: (id) => "/inbox/" + encodeURIComponent(id), fin_accounts: () => "/accounts", fin_journals: (id) => "/journals/" + encodeURIComponent(id), locations: () => "/locations",
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const api = (path) => fetch(path, { credentials: "same-origin", headers: { accept: "application/json" } });

  // 65 multi-entity: the "current entity" (set of books) is a client-held
  // value in localStorage, read here and by the list/form generators to
  // scope every entity-scoped collection. A convenience getter -- the
  // generators read the same localStorage key directly, so they never depend
  // on the nav having loaded first. Empty string = "All entities" (no scope).
  var ENTITY_KEY = "dbbasic_entity";
  window.dbbasicEntity = function () {
    try { return localStorage.getItem(ENTITY_KEY) || ""; } catch (e) { return ""; }
  };

  const bar = document.createElement("div");
  bar.className = "appbar";
  bar.id = "dbbasic-appbar";
  bar.innerHTML =
    '<a class="brand" href="/">DBBASIC</a>' +
    '<button class="navbtn" id="nav-apps">Apps ▾</button>' +
    '<button class="navbtn" id="nav-books" style="display:none"></button>' +
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

  // App switcher. Drawn from the fallback immediately so the menu is
  // never empty, then replaced by the registry as soon as it answers:
  // action_nav_entries returns the doors THIS caller may see, already
  // grouped and sorted, so the switcher restates no visibility rule of
  // its own.
  const appsBtn = document.getElementById("nav-apps");
  const appsMenu = menu("nav-apps-menu");
  // Two async loaders write this menu, so each owns a piece and a single
  // render joins them -- appending straight into innerHTML made whichever
  // one answered second wipe the other.
  let appsHtml = APPS.map(([u, n]) => '<a href="' + u + '">' + esc(n) + "</a>").join("");
  let pinnedHtml = "";
  const renderApps = () => { appsMenu.innerHTML = appsHtml + pinnedHtml; };
  renderApps();
  appsBtn.addEventListener("click", () => {
    const open = appsMenu.classList.contains("open"); closeAll();
    if (!open) { place(appsMenu, appsBtn); appsMenu.classList.add("open"); }
  });
  (async function loadApps() {
    try {
      const res = await api("/objects/action_nav_entries");
      if (!res.ok) return;                       // keep the fallback
      const groups = (await res.json()).groups || [];
      if (!groups.length) return;                // empty registry: keep the fallback
      appsHtml = groups.map((g) =>
        '<div class="head">' + esc(g.group) + "</div>" +
        // The count rides along on the entry when a package's attention
        // provider says that door has a queue. Rendered as text rather
        // than a styled pill deliberately: the switcher is drawn by this
        // script and styled by /style, and a class this stylesheet does
        // not know about would ship as an invisible promise. A zero never
        // arrives -- action_nav_entries drops empty queues -- so there is
        // no "0" case to suppress here.
        (g.entries || []).map((e) =>
          '<a href="' + esc(e.path) + '">' + esc(e.label) +
          (e.count ? " · " + esc(e.count) : "") + "</a>").join("")
      ).join("");
      renderApps();
    } catch (e) { /* registry unreachable -- the fallback list is already drawn */ }
  })();

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
      pinnedHtml = '<div class="head">pinned</div>' + pinned.map((v) =>
        '<a href="' + esc(v.route || ("/views/" + v.id)) + '">' + esc(v.title || "View") + "</a>").join("");
      renderApps();
    } catch (e) { /* app-views not installed -- the switcher still works without it */ }
  })();

  // Entity switcher (65 multi-entity): a "Books" picker listing the signed-in
  // user's entities. Selecting one stores its id in localStorage; the list/
  // form generators read that key and scope every entity-scoped collection to
  // it (filter on read, FK-lock on new writes). "All entities" clears the
  // scope. Hidden entirely when the user has no entities (nothing to switch),
  // so a fresh/unused install shows no extra chrome -- same silent-degrade
  // posture as the pinned-views loader above.
  const booksBtn = document.getElementById("nav-books");
  const booksMenu = menu("nav-books-menu");
  (async function loadEntities() {
    try {
      const res = await api("/collections/entities/records?limit=200");
      if (!res.ok) return;
      const entities = ((await res.json()).records) || [];
      if (!entities.length) return;  // no books -> no switcher
      const current = window.dbbasicEntity();
      const currentName = (entities.find((e) => e.id === current) || {}).name;
      booksBtn.textContent = "Books: " + (currentName || "All") + " ▾";
      booksBtn.style.display = "";
      const setEntity = (id) => {
        try { id ? localStorage.setItem(ENTITY_KEY, id) : localStorage.removeItem(ENTITY_KEY); } catch (e) {}
        location.reload();
      };
      booksMenu.innerHTML =
        '<div class="head">books</div>' +
        '<a href="#" data-eid="">All entities</a>' +
        entities.map((e) => '<a href="#" data-eid="' + esc(e.id) + '">' + esc(e.name || e.id) + "</a>").join("") +
        '<div class="head"><a href="/entities">Manage entities…</a></div>';
      booksMenu.querySelectorAll("a[data-eid]").forEach((a) =>
        a.addEventListener("click", (ev) => { ev.preventDefault(); setEntity(a.dataset.eid); }));
      booksBtn.addEventListener("click", () => {
        const open = booksMenu.classList.contains("open"); closeAll();
        if (!open) { place(booksMenu, booksBtn); booksMenu.classList.add("open"); }
      });
    } catch (e) { /* entities collection not installed -- no switcher, nav still works */ }
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

// === local time ============================================================
//
// Every timestamp this server STORES is UTC, which is correct and is not
// negotiable -- a stamp without a zone is a bug waiting for the clocks to
// change. Every timestamp a person READS should be in their own zone,
// which is a rendering question and belongs here rather than in each page.
//
// The generative renderer already got this right by accident of being
// client-side: `new Date(iso)` parses UTC and `toLocaleString` prints
// local. Server-rendered pages did not, because a Python f-string has no
// idea who is reading it. So the convention is one element:
//
//     <time datetime="2026-07-27T03:12:44Z">2026-07-27T03:12:44Z</time>
//
// and this converts the text while leaving the machine-readable attribute
// exactly as the server wrote it. No JavaScript, or an unparseable stamp,
// leaves the UTC text in place -- which is honest rather than blank.
//
// THE BROWSER IS THE DEFAULT, NOT A PREFERENCE, because it is right more
// often: it follows the reader across daylight saving and across a plane,
// and it needs nobody to have set anything. A stored preference OVERRIDES
// it (display.timezone in user_prefs) for the real case the browser gets
// wrong -- a business whose books are kept in one zone regardless of where
// the person reading them happens to be sitting.
//
// AND THE BROWSER'S ZONE IS NEVER SENT TO THE SERVER. It is available
// (Intl.DateTimeFormat().resolvedOptions().timeZone returns the IANA
// name) and it is deliberately used only in this process. A timezone is a
// real fingerprinting signal -- one of the higher-entropy bits in the
// standard browser fingerprint -- so silently posting it back to store as
// a preference would be collecting an identifying attribute nobody asked
// us to collect, and /privacy is a fold over exactly that. Reading it to
// format text costs nothing and discloses nothing. If a user wants a
// fixed zone, they set the pref themselves and it is their statement
// rather than our observation.
(function () {
  const ISO = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/;
  let zone = null;   // null = browser default

  function fmt(iso, opts) {
    if (!iso || !ISO.test(String(iso).trim())) return null;
    // A bare stamp with no zone marker is UTC by this server's contract.
    let text = String(iso).trim().replace(" ", "T");
    if (!/[Zz]|[+-]\d{2}:?\d{2}$/.test(text)) text += "Z";
    const d = new Date(text);
    if (isNaN(d)) return null;
    const o = Object.assign({dateStyle: "medium", timeStyle: "short"}, opts || {});
    if (o.timeStyle === undefined) delete o.timeStyle;   // date-only, cleanly
    if (zone) o.timeZone = zone;
    try { return d.toLocaleString(undefined, o); }
    catch (e) { return d.toLocaleString(); }   // a bad pref must not blank the page
  }

  function apply(root) {
    (root || document).querySelectorAll("time[datetime]").forEach((el) => {
      if (el.dataset.localized === "1") return;
      const out = fmt(el.getAttribute("datetime"),
                      el.dataset.timeStyle === "date" ? {timeStyle: undefined} : null);
      if (!out) return;                        // unparseable: leave UTC showing
      if (!el.title) el.title = el.getAttribute("datetime") + " (UTC)";
      el.textContent = out;
      el.dataset.localized = "1";
    });
  }

  window.dbbasicTime = {format: fmt, apply: apply,
                        zone: () => zone || Intl.DateTimeFormat().resolvedOptions().timeZone};

  function start() {
    apply(document);
    // Anything rendered after load -- the generative list, a form panel,
    // a realtime push -- gets converted as it arrives, so a page does not
    // have to remember to call apply().
    if (window.MutationObserver) {
      new MutationObserver((muts) => {
        for (const m of muts) if (m.addedNodes.length) { apply(document); break; }
      }).observe(document.body, {childList: true, subtree: true});
    }
  }

  fetch("/prefs", {credentials: "same-origin"})
    .then((r) => (r.ok ? r.json() : null))
    .then((p) => {
      const prefs = (p && (p.prefs || p.records || p)) || {};
      const tz = prefs["display.timezone"] || prefs.display_timezone;
      if (tz && String(tz).trim()) zone = String(tz).trim();
    })
    .catch(() => {})
    .finally(() => {
      if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start);
      } else { start(); }
    });
})();
"""


def GET(request):
    return {"content_type": "application/javascript; charset=utf-8", "body": _JS}
