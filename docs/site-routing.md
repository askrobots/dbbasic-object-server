# Site Routing

Site routing turns the object server into a webserver: clean public URLs
(`/about`, `/articles/{uuid}`) resolve to objects, so a whole website can be
built, edited, and rolled back through the normal object loop with no deploy
step and no per-page reverse-proxy rewrites.

It is off by default and enabled per deployment:

```text
DBBASIC_ENABLE_SITE_ROUTES=true
```

## Resolution Order

Site routing runs only after every built-in route family has declined the
path, so reserved surfaces (`/objects`, `/admin`, `/api`, `/identity`,
`/login`, `/health`, `/collections`, `/schemas`, `/events`, `/packages`,
`/daemon`, `/permissions`, `/logout`) can never be shadowed by a site page.

For everything else:

1. **Convention.** `/` maps to `site_home`, `/about` to `site_about`,
   `/docs/install` to `site_docs_install`. Hyphens become underscores
   (`/getting-started` → `site_getting_started`). Creating a page object
   creates its URL — the filesystem-routing feel of the original prototype,
   kept because it makes the common case zero-config.
2. **The `site_routes` collection.** Records with `pattern`, `object_id`, and
   optional `priority` handle what conventions cannot express:

   ```text
   id  pattern                        object_id      priority
   r1  /articles/{article_id:uuid}    articles_view  10
   r2  /blog/{slug}                   blog_post      20
   ```

   Pattern segments are literal text, `{name}` (any single segment), or
   `{name:uuid}` (segment must be UUID-shaped — the common case for record
   ids, which are UUIDv4 by default). Captured params are merged into the
   object's request payload, so `articles_view` reads
   `request["article_id"]`. Lower `priority` wins; ties go to the pattern
   with more literal segments.
3. **`site_404`.** If nothing resolves and a `site_404` object exists, it
   runs with the missed `path` in its payload and should return
   `status_code: 404`. Without it, the plain JSON 404 stays.

## Design Decisions

These choices came out of comparing the original prototype's pure filesystem
router, the route-table-in-object-state design from the early dbbasic.com
plan, and what the public server has grown since:

- **Routing only maps URLs; authorization stays in the permission policy.**
  The resolved object executes through the normal execution path, so
  enforcement, audit entries, execution logs, timeouts, concurrency limits,
  and correlation ids all apply. Publishing a page is two audited steps:
  create the object, grant `execute` to `public` (or whichever principal).
  A file appearing on disk never silently becomes public under enforcement.
- **The route table lives in records, not object state.** Records give the
  table schema validation, change history, rollback, Scroll editing, and MCP
  tools for free. Editing the sitemap is a data change with an audit trail.
- **Convention for the 90%, data for the 10%.** Plain pages need no table
  entry. The table exists for parameterized routes, renames, and pointing
  multiple URLs at one object.
- **TLS and static assets stay in the reverse proxy.** Caddy terminates TLS
  and should serve `/static/*` from disk with cache headers; site routing
  replaces per-page rewrites with one catch-all proxy block.

## Deployment

With site routing enabled, the Caddy shape becomes: explicit handles for the
reserved admin/API surfaces you want public, a `/static/*` file server, and a
catch-all `reverse_proxy` instead of a catch-all 404. Under permission
enforcement, unrouted or unauthorized paths still return controlled 403/404
responses from the server.

## Static Assets

Site CSS, JavaScript, images, and fonts are served by the reverse proxy from
a plain directory — never through object execution — so they get file-server
performance, gzip, and cache headers:

```caddyfile
handle_path /static/* {
    root * /var/lib/dbbasic-static
    file_server
    header Cache-Control "public, max-age=3600"
}
```

Pages reference them normally (`<link href="/static/site.css">`). The
directory is deploy-time content owned by the service user; treat it like
object source (versioned in the site package), not like runtime data.
User-uploaded files are different: keep those in the object file APIs so they
inherit permissions, audit, and backups.

## Writing Site Pages

Site pages are normal objects (see `object-authoring.md`): return a dict for
JSON, a `content_type`/`body` dict for HTML, use `request["_identity"]` for
per-user rendering, and handle `POST` for forms — browser form posts arrive
as parsed fields with the session cookie applied. Ship a site as a package
(`package-authoring.md`) so it installs with its schema, seed records, and
route table rows in one reviewed unit.
