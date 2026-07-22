# Secrets, Credentials, and the Trust Boundary

This is the doctrine for **where secrets live** in DBBASIC and why. It matters
to anyone writing a package that touches a credential — a user's third-party
API key, an outbound-mail relay password, an email-hosting mailbox password, a
signing token. Get this wrong and a secret ends up in a record collection,
which means it ends up in an export, a backup, or an API read. Get it right and
secrets stay in one place, with one honest boundary, that every other part of
the system already trusts.

The one-line rule: **a secret is never a record field.** Records are readable,
listable, exportable, and included in portable backups by design. A secret must
be none of those things. So secrets go somewhere else, through a different door.

## Where secrets live

There are exactly two homes, by lifetime and ownership:

### 1. Deploy-time, server-wide secrets → the environment file

A credential the *whole deployment* shares — the SMTP relay password
(`DBBASIC_SMTP_PASSWORD`), a connector's API key (`DBBASIC_MAILCOW_API_KEY`),
the admin token (`DBBASIC_ADMIN_TOKEN`) — lives in
`/etc/dbbasic-object-server.env`, `640 root:dbbasic` (see
[`single-vm-deployment.md`](single-vm-deployment.md)). It is set by the operator, read by the server
process, and never appears in a schema field, a record, an API response, or a
backup archive. A package that needs one **declares the env var name in its
docs and reads it at runtime** — it never ships the value and never stores it.

### 2. Per-user recoverable secrets → the identity vault

A secret that belongs to *one user* and must be *recoverable to be used* (a BYO
API key the server calls a provider with; a mailbox password handed to the mail
server) lives in the **write-only identity vault**, `object_service_keys`
(`identity/service_keys.tsv`). Its contract:

- **Owner-only file**, `0600`, next to `credentials.tsv` — same filesystem
  trust boundary as the admin token.
- **Write-only**: a caller can *set* a key, *list which services have one*, and
  *delete* one. **No surface ever reads key material back** — not the HTTP API,
  not the shell, not an agent, not a package. The server uses a stored key
  internally (to call the provider) so it never travels to a browser.
- **Stored as-provided, not encrypted, not hashed.** Passwords are hashed
  because they only need to be *verified*; these must be *used*, so they must be
  recoverable. On-record encryption with a key derived from a server secret
  (the pattern many frameworks reach for) is *theater* here — the ciphertext and
  the key it's decrypted with live in the same blast radius, so they leak
  together (see the Trust Boundary section). The real protection is the `0600`
  file and the write-only contract, not a cipher.
- **Excluded from portable backups and source control** — so a leaked backup
  archive, the most common accidental exposure, contains no secret material.
  (See [`backup-restore.md`](backup-restore.md); a package must never undo this by copying a secret
  into a record it *does* back up.)

## How a secret gets written (it is not a record write)

The vault has its own door, deliberately separate from the generic record API:

```
GET    /identity/users/{user_id}/service-keys          → which services are set (status only)
PUT    /identity/users/{user_id}/service-keys          → { "service": "...", "key": "..." }
DELETE /identity/users/{user_id}/service-keys/{service} → clear one
```

- **Owner-scoped**: only the account owner's own session may write their vault
  (or the operator admin gate). Identity is resolved server-side from an opaque
  session token — it cannot be spoofed by a client-supplied user id.
- **CSRF-guarded**: cross-origin cookie writes are refused.
- **Never echoes material**: `PUT` returns metadata (`service`, timestamps)
  only; `GET` returns presence only.

`POST /collections/{c}/records` — the generic record write — **cannot reach the
vault at all.** That is the point: there is no record surface to misconfigure
into leaking a secret.

**In a generated UI**, a secret is a *write-only field*: it renders as "set /
not set" with an input that only ever POSTs to the vault endpoint above and
never displays a stored value. The form still looks uniform to the user — one
field just writes to a different door than the record.

## Reserved namespaces: user-writable vs platform-owned

A vault entry is keyed by a **service name**. Two kinds:

- **User-writable** (a person's own BYO credential): a plain name — `openai`,
  `anthropic`, `stripe`. The self-service endpoint above writes these.
- **Platform-owned** (a secret the *platform* provisions on the user's behalf —
  a generated mailbox password): a **reserved name**, prefixed `sys-`
  (e.g. `sys-mailbox-<mailbox_id>`). **The self-service HTTP endpoint refuses to
  write or delete a reserved name.** Only in-process server code (a connector, a
  provisioning verb) may set one.

This is the control that stops a user from **overwriting a secret the platform
owns**. Without it, a user could `PUT` their own `sys-mailbox-<id>` and clobber
the password the mail server actually uses. `object_service_keys.is_reserved_service()`
is the single source of truth for the rule; the HTTP handler enforces it.

Two more rules for platform-owned secrets a package must follow:

- **Scope every read by the record's owner**, never by caller input. Resolve a
  mailbox's secret from *the mailbox record's `owner_id`* — never from a
  `mailbox_id` a request supplied — or a user could plant an entry in their own
  vault and hope a global lookup reads it.
- **Generate, don't collect, when you can.** A provisioned password should be
  generated server-side (`secrets.token_urlsafe`), stored in the vault, and
  revealed to the user *exactly once* in the provisioning response — never read
  back afterward.

## The trust boundary (threat model)

Be honest about what actually protects the vault, because it changes what's
worth building. A secret is reachable only by one of these, and **every one of
them also compromises the entire system** — so the vault is not a weaker link,
it inherits the system's security:

| Vector | What it takes | Does encryption-at-rest help? |
|---|---|---|
| **A logged-in user** reaching another user's secret | Impossible via the API: owner-scoped, server-derived identity, reserved-namespace guard | N/A — blocked at the app layer |
| **Code execution as the service user** | An RCE in the server, or an installed package/connector (both run as the service user and legitimately read the vault) | **No** — the running process holds the key too |
| **Root on the host** | Kernel/sudo/other-service exploit; `0600` is nothing to root | **No** — same blast radius |
| **The raw disk** | A VM snapshot, an attached volume, a stolen *unencrypted* backup | **Only here** — and only if the key lives off-box (a KMS) |
| **The deploy path** | A dev-box SSH key or a push-to-prod pipeline → arbitrary code as the service user | **No** — it's code execution with a login |

Conclusions that follow:

- **A plain user cannot reach it.** They are boxed into their own namespace by a
  server-resolved identity and the reserved-namespace guard.
- **On-box encryption of the vault is theater** against every vector except a
  raw-disk/snapshot theft — because for all the others the decryption key rides
  in the same boundary. The genuinely stronger option is **envelope encryption
  with a KMS-held root key**, and only when you have a KMS (not on a single VM).
  Until then, the higher-leverage move is to **encrypt backups/snapshots with an
  off-box key and lock the hosting account with 2FA** — the deploy credential
  and the hosting account are, in effect, vault keys.
- **"Installed package = code execution as the service user"** is a stated trust
  fact, which is why package installs (and any dynamically loaded connector
  code) are admin-gated. Fine while packages are first-party; it needs a
  stronger answer before any third-party marketplace.

## Checklist for package authors

- [ ] **Never** put a secret in a schema field or a record.
- [ ] Server-wide credential → read from a documented `DBBASIC_*` env var; never ship or store the value.
- [ ] Per-user recoverable secret → the identity vault (`object_service_keys`), write-only.
- [ ] Platform-provisioned secret → a `sys-`-prefixed reserved service name; write it only from server-side code.
- [ ] Resolve a secret by the owning record's `owner_id`, never by caller-supplied input.
- [ ] Prefer generating a secret and revealing it once over collecting and storing it.
- [ ] In a UI, a secret field is write-only (set/not-set), posting to the vault endpoint, never rendering the value.
- [ ] Never copy a secret into anything that lands in a backup or an export.

## See also

- [`single-vm-deployment.md`](single-vm-deployment.md) — the env-file trust boundary and admin-route posture.
- [`permissions-model.md`](permissions-model.md) — owner-scoping and the admin bypass.
- [`backup-restore.md`](backup-restore.md) — what is and isn't in a portable backup.
- [`package-authoring.md`](package-authoring.md) — how a package ships schemas, permissions, and (soon) connectors.
