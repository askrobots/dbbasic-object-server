"""site_privacy -- a privacy policy that is a FOLD over this server's
configuration, not a template somebody filled in once.

GET /privacy       the policy
GET /cookies       the cookie table on its own
GET /privacy.json  the same fold, machine-readable, for anyone who wants
                   to diff it between deploys

Almost every privacy policy on the web is a lie of omission, and not
because anyone set out to lie: the policy is written once, by a template,
and the software changes weekly. Nothing connects the two, so nothing
notices when they part company. This one is generated from the live
settings, so it cannot claim a 30-day retention while the box keeps 180,
cannot mention a payment processor that is not configured, and cannot
stay silent about a cookie the server sets.

The load-bearing property is that it is a fold, in the same family as the
nav registry (`action_nav_entries`) and the attention counts: install the
shop and the policy grows a payments paragraph with nobody remembering to
write one; switch analytics off and the traffic section disappears
entirely. A policy that CAN drift from the software is the normal case;
one that cannot is worth having.

## What this object refuses to do, and why it is a refusal rather than a gap

**It does not write legal text.** No lawful bases, no legitimate-interest
balancing, no Standard Contractual Clauses, no adequacy decisions, no
transfer mechanisms, no retention justifications, no claims about which
regimes apply to the operator's business. It states FACTS about this
system -- what is stored, where, for how long, which third parties are
configured, what a cookie does -- and stops.

That boundary is the same one this system holds everywhere else. A
generated paragraph asserting "we rely on legitimate interests under
Article 6(1)(f)" would be a confident sentence about somebody else's legal
position, produced by a program that cannot know their business, their
customers or their regulator, and pasted into a document a regulator
reads. Wrong legal text is worse than no legal text, because it is
evidence of a claim the operator never made and cannot support.

`privacy.extra_markdown` is where the operator's own reviewed wording
goes. It is rendered verbatim, under its own heading, so a reader can see
which half of the page is machine-generated fact and which half is a
human's legal statement. (`app_settings` caps a value at 500 characters,
so long text belongs in a document this links to rather than inline.)

**It refuses to render at all until it is signed.** A policy with no named
controller and no contact address is not a policy; it is a page shaped
like one, and it is worse than nothing because it looks like compliance
while giving a reader nobody to write to. Absent
`privacy.controller_name`, `privacy.contact_email` or
`privacy.jurisdiction`, this page says exactly which settings are missing
and renders no claims whatsoever about the system. The cookie table is
still served at /cookies, because the browser-facing facts are true
regardless of who signed them.

## The settings it folds

    privacy.controller_name    who is responsible for the data (required)
    privacy.contact_email      where a subject writes (required)
    privacy.jurisdiction       where the controller is established (required)
    privacy.extra_markdown     the operator's own reviewed legal text

Everything else is read from the running configuration: the analytics
env, the cookie switches, which packages are installed, and which third
parties actually have credentials.
"""

import html
import json
import os
from pathlib import Path

import object_ai
import object_analytics
import object_email
import object_identity
import object_package_baselines
import object_packages
import object_records
import object_service_keys
import object_stripe

DATA_DIR_ENV = "DBBASIC_DATA_DIR"
SETTINGS_COLLECTION = "app_settings"

CONTROLLER_NAME_KEY = "privacy.controller_name"
CONTACT_EMAIL_KEY = "privacy.contact_email"
JURISDICTION_KEY = "privacy.jurisdiction"
EXTRA_MARKDOWN_KEY = "privacy.extra_markdown"
CARRIER_PROVIDER_KEY = "carrier.provider"

REQUIRED_SETTINGS = (
    (CONTROLLER_NAME_KEY, "the name of the person or company responsible for this data"),
    (CONTACT_EMAIL_KEY, "an address a visitor can actually write to"),
    (JURISDICTION_KEY, "where that controller is established, e.g. 'England and Wales'"),
)

# The shop's basket cookie lives in app-shop (objects/site/shop.py, COOKIE)
# and its lifetime is a literal in the header that object builds. Restated
# here because a package may not import another package's objects -- and
# pinned by tests/test_privacy.py, which reads the literal back out of
# app-shop's source and fails if the two ever disagree. A disclosure that
# can drift from the cookie it describes is the drift this whole page
# exists to prevent.
CART_COOKIE_NAME = "cart"
CART_COOKIE_MAX_AGE = 1209600          # 14 days
CART_COOKIE_PACKAGE = "app-shop"

# The apps whose presence changes what this policy says, and the
# collection each one owns. Nothing else is folded, and nothing else is
# reported: /privacy is public, and the full inventory of what an
# operator installed is not a visitor's business.
FOLDED_PACKAGES = {
    "app-shop": ("carts",),
    "app-orders": ("orders",),
    "app-invoices": ("invoices",),
    "app-payments": ("payments",),
    "app-shipping": ("shipments",),
}


# === the registry of cookies this server can set ============================
#
# THE list. tests/test_privacy.py sweeps every source file on the box for
# anything that sets a cookie and fails unless each name it finds appears
# here, naming the undisclosed cookie in the failure message. A cookie
# added anywhere in this repo therefore breaks the build until somebody
# writes down what it is for and whether it is strictly necessary.
#
# `strictly_necessary` is the ePrivacy question and it is the only one on
# this page with a consent consequence: a cookie needed for a service the
# user actively asked for (the session they signed in to, the basket they
# filled) needs no consent; anything else does. Two of three here are
# necessary, which is why this server's default posture needs no banner.
COOKIE_DISCLOSURES = (
    {
        "name": "dbbasic_session",
        "purpose": "Keeps you signed in after you enter your password.",
        "strictly_necessary": True,
        "necessary_because": "you asked to sign in, and there is no way to stay "
                             "signed in without it",
        "set_when": "You sign in.",
        "requires": "",
    },
    {
        "name": CART_COOKIE_NAME,
        "purpose": "Remembers which basket is yours, so what you put in it is "
                   "still there on the next page. It holds an opaque token, "
                   "not the contents.",
        "strictly_necessary": True,
        "necessary_because": "you asked for a basket; a shop cannot keep one "
                             "without a way to recognise it",
        "set_when": "You put something in the basket.",
        "requires": "package:" + CART_COOKIE_PACKAGE,
    },
    {
        "name": object_analytics.VISITOR_COOKIE_NAME,
        "purpose": "An opaque random token used to tell a returning visit from "
                   "a new one in this site's own traffic figures. It carries no "
                   "name, no email and no account, and it is never joined to a "
                   "signed-in identity.",
        "strictly_necessary": False,
        "necessary_because": "",
        "set_when": "You open a page — unless your browser sends Do Not Track "
                    "or Global Privacy Control, in which case it is never set.",
        "requires": "setting:visitor_cookie",
    },
)

# What a page_views row actually holds. Named field by field rather than
# summarised, because "usage data" is the phrasing that lets a policy mean
# anything.
PAGE_VIEW_FIELDS = (
    ("IP address", "the address the request came from"),
    ("Host", "which of this server's sites was asked"),
    ("Path and method", "the URL requested and how"),
    ("Status", "what the server answered"),
    ("User agent", "the browser or client string your software sent"),
    ("Referrer", "the page that linked you here, if your browser sent one"),
    ("Visitor token", "the dbbasic_visitor cookie value, when that cookie is on"),
    ("Timestamp", "when"),
)

_STYLE = """
.pp { max-width: 46rem; }
.pp h2 { font-size: 1.05rem; margin: 1.8rem 0 .4rem; }
.pp h3 { font-size: .95rem; margin: 1.2rem 0 .3rem; }
.pp p, .pp li { line-height: 1.55; }
.pp table { width: 100%; border-collapse: collapse; margin: .6rem 0 1.2rem; }
.pp th, .pp td { text-align: left; padding: .35rem .5rem; vertical-align: top;
                 border-bottom: 1px solid var(--line, #38384a); font-size: .9rem; }
.pp .yes { color: var(--accent, #b5713a); font-weight: 600; }
.pp .note { border-left: 3px solid var(--line, #55556a); padding: .1rem 0 .1rem .7rem;
            margin: .6rem 0 1.2rem; font-size: .88rem; opacity: .85; }
.pp .refusal { border: 1px solid var(--accent, #b5713a); border-radius: 8px;
               padding: .8rem 1rem; margin: 1rem 0; }
.pp code { font-size: .85em; }
"""


def _esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def _data_dir():
    return os.environ.get(DATA_DIR_ENV, object_records.DEFAULT_DATA_DIR)


# --- reading the box ---------------------------------------------------------

def _settings(base):
    """Every privacy.* setting as a plain dict, missing collection and all.

    Duplicated rather than shared, like every other package that reads
    app_settings -- see docs/logic-decisions.md #4."""
    values = {}
    try:
        rows = object_records.read_collection_records(
            SETTINGS_COLLECTION, base_dir=base)
    except Exception:
        return values
    for row in rows:
        key = str(row.get("key") or "").strip()
        value = str(row.get("value") or "").strip()
        if key and value:
            values[key] = value
    return values


def _schema_present(base, collection):
    """Whether this box knows a collection at all.

    Cheaper and more honest than reading it: the question is "does this
    server store shipments", not "has it stored one yet". An empty
    shipments collection is still a server that ships things.
    """
    return (Path(base) / "schemas" / f"{collection}.json").is_file()


def installed_packages(base, *, root=None):
    """Which of the apps this policy folds over are installed on this box.

    Only those apps, deliberately. The full inventory of what an operator
    has installed is not a visitor's business and /privacy is public; what
    a visitor is owed is the apps that touch their data, which is exactly
    the list below.

    Two signals, either sufficient, because either one alone is wrong
    somewhere real:

    * an install baseline (object_package_baselines) -- the crisp record
      that `install_package` ran here. Absent on a box whose packages
      predate baselines, which would drop a payments section from a
      server that plainly takes payments.
    * the package's own collection present in the data directory -- the
      schema exists, so this server stores that kind of record whether or
      not anybody stamped a baseline. Emptiness proves nothing either
      way: a shipments collection with no rows is still a server that
      ships things.

    The manifest merely being present under packages/ is NOT sufficient.
    This repo ships every app it has, and a policy that grew a fulfilment
    section on a box that never installed the shop would be inventing
    processing rather than reporting it. `list_packages` is still what
    decides the candidate set, so an app removed from the box drops out
    of the policy with it.
    """
    installed = set()
    try:
        summaries = (object_packages.list_packages(root=root) if root
                     else object_packages.list_packages())
    except Exception:
        return installed
    shipped = {summary.get("id") or "" for summary in summaries}

    for package_id, collections in FOLDED_PACKAGES.items():
        if package_id not in shipped:
            continue
        try:
            baseline = object_package_baselines.load_baseline(
                package_id, base_dir=base)
        except Exception:
            baseline = None
        if baseline is not None or any(_schema_present(base, name)
                                       for name in collections):
            installed.add(package_id)
    return installed


def _ai_services(base):
    """AI providers this box has a key for, by name.

    Read from the stored-service-key metadata, never the keys themselves.
    Restricted to the services object_ai can actually call, so a key
    somebody stored for something else is not reported as an AI
    sub-processor.
    """
    services = set()
    try:
        path = object_service_keys.service_keys_path(base)
    except Exception:
        return []
    try:
        import csv
        with open(path, newline="") as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                service = (row.get("service") or "").strip()
                if service in object_ai.SUPPORTED_SERVICES:
                    services.add(service)
    except (OSError, ValueError):
        return []
    return sorted(services)


# === the fold ================================================================

def policy(*, base=None, env=None, packages_root=None):
    """Everything the page and the JSON are rendered from. One fold, three
    surfaces -- /privacy, /cookies and /privacy.json cannot disagree
    because there is nothing for them to disagree about.

    No timestamps in here, deliberately: /privacy.json is meant to be
    diffed between deploys, and a generated-at stamp would make every
    fetch differ from the last one and hide the changes that matter.
    """
    base = _data_dir() if base is None else base
    env = os.environ if env is None else env
    settings = _settings(base)
    installed = installed_packages(base, root=packages_root)

    missing = [key for key, _why in REQUIRED_SETTINGS if not settings.get(key)]
    controller = {
        "name": settings.get(CONTROLLER_NAME_KEY, ""),
        "contact_email": settings.get(CONTACT_EMAIL_KEY, ""),
        "jurisdiction": settings.get(JURISDICTION_KEY, ""),
        "missing_settings": missing,
        "signed": not missing,
    }

    analytics_on = object_analytics.analytics_enabled(env)
    cookie_on = object_analytics.visitor_cookie_enabled(env)
    analytics = {
        "enabled": analytics_on,
        "visitor_cookie": cookie_on,
        "visitor_cookie_days": object_analytics.visitor_days(env) if cookie_on else 0,
        "retention_days": object_analytics.retention_days(env) if analytics_on else 0,
        "max_rows": object_analytics.max_rows(env) if analytics_on else 0,
        "fields": [{"field": name, "meaning": meaning}
                   for name, meaning in PAGE_VIEW_FIELDS
                   if analytics_on and (cookie_on or name != "Visitor token")],
        "respects_dnt": True,
    }

    return {
        "controller": controller,
        "analytics": analytics,
        "cookies": cookie_table(base=base, env=env, installed=installed),
        "processing": _processing(base, env, installed),
        "subprocessors": subprocessors(base=base, env=env, installed=installed),
        "rights": _rights(controller, analytics),
        "extra_markdown": settings.get(EXTRA_MARKDOWN_KEY, ""),
        "installed_packages": sorted(installed),
    }


def disclosed_cookie_names():
    """Every cookie name this server is disclosed as being able to set.

    The set the codebase sweep in tests/test_privacy.py checks itself
    against. Deliberately NOT filtered by configuration: the sweep is
    asking "is this cookie written down anywhere", and a box with no shop
    installed must still fail if somebody adds an undisclosed cookie to
    the shop.
    """
    return {entry["name"] for entry in COOKIE_DISCLOSURES}


def cookie_max_age_seconds(name, *, env=None):
    """The real Max-Age this server puts on one cookie, in seconds.

    Read from the same places the cookie itself is built from, so the
    published number cannot be a number somebody typed.
    """
    env = os.environ if env is None else env
    if name == object_analytics.VISITOR_COOKIE_NAME:
        return object_analytics.visitor_days(env) * 86400
    if name == CART_COOKIE_NAME:
        return CART_COOKIE_MAX_AGE
    if name == "dbbasic_session":
        return object_identity.DEFAULT_SESSION_TTL_SECONDS
    return 0


def cookie_table(*, base=None, env=None, installed=None):
    """The cookies THIS server, as configured, can put in a browser.

    Filtered by configuration rather than listing the catalogue: telling a
    visitor about a basket cookie on a box with no shop is noise, and
    listing an analytics cookie that is switched off would be the page
    lying in the direction that flatters nobody.
    """
    base = _data_dir() if base is None else base
    env = os.environ if env is None else env
    if installed is None:
        installed = installed_packages(base)

    rows = []
    for entry in COOKIE_DISCLOSURES:
        requires = entry["requires"]
        if requires.startswith("package:"):
            if requires.split(":", 1)[1] not in installed:
                continue
        elif requires == "setting:visitor_cookie":
            if not (object_analytics.visitor_cookie_enabled(env)
                    and object_analytics.analytics_enabled(env)):
                continue
        seconds = cookie_max_age_seconds(entry["name"], env=env)
        rows.append({
            "name": entry["name"],
            "purpose": entry["purpose"],
            "strictly_necessary": entry["strictly_necessary"],
            "necessary_because": entry["necessary_because"],
            "set_when": entry["set_when"],
            "max_age_seconds": seconds,
            "max_age_days": round(seconds / 86400, 2) if seconds else 0,
            "first_party": True,
            "http_only": True,
            "same_site": "Lax",
        })
    return rows


def _processing(base, env, installed):
    """The sections that exist only because the thing they describe does.

    Each entry is a fact about a collection this box actually has, so a
    server that does not sell anything has no payments paragraph to be
    wrong about.
    """
    sections = []

    if object_analytics.analytics_enabled(env):
        sections.append({
            "id": "traffic",
            "title": "Traffic to this site",
            "why": "To see how many people visit, which pages they read, and "
                   "to spot attacks and broken links.",
            "kept": f"{object_analytics.retention_days(env)} days",
            "shared_with": [],
        })
    else:
        sections.append({
            "id": "traffic",
            "title": "Traffic to this site",
            "why": "Not collected. This server keeps no record of visits: no "
                   "page log, no visitor counter, no third-party analytics "
                   "script of any kind.",
            "kept": "nothing is stored",
            "shared_with": [],
        })

    if "app-orders" in installed or "app-invoices" in installed:
        sections.append({
            "id": "orders",
            "title": "Orders and invoices",
            "why": "To take, fulfil and account for what you bought. These are "
                   "business records.",
            "kept": "for as long as the law requires business records to be "
                    "kept, which is longer than you might expect",
            "shared_with": [],
        })

    if "app-payments" in installed:
        stripe_configured = object_stripe.stripe_config_from_env(env).configured
        sections.append({
            "id": "payments",
            "title": "Payments",
            "why": "To take payment and to reconcile it against your invoice.",
            "kept": "with the invoice it pays",
            "shared_with": (["Stripe"] if stripe_configured else []),
            "note": ("Card details are entered on Stripe's own pages and never "
                     "reach this server; what it stores is the amount, the "
                     "date and Stripe's reference."
                     if stripe_configured else
                     "No payment provider is configured on this server; "
                     "payments are recorded by hand."),
        })

    if "app-shipping" in installed:
        carrier = _settings(base).get(CARRIER_PROVIDER_KEY, "").strip().lower()
        integrated = carrier not in ("", "none", "manual")
        sections.append({
            "id": "fulfilment",
            "title": "Delivery",
            "why": "To get the parcel to the address you gave.",
            "kept": "with the order it belongs to",
            "shared_with": ([carrier] if integrated else []),
            "note": ("Your delivery address and name are sent to this carrier "
                     "so a label can be produced."
                     if integrated else
                     "No carrier account is connected to this server, so no "
                     "address is sent to one from here."),
        })

    smtp = object_email.smtp_config_from_env(env)
    if smtp.mode != "disabled":
        sections.append({
            "id": "email",
            "title": "Email we send you",
            "why": "Order confirmations, invoices and replies to things you "
                   "asked about.",
            "kept": "with the record that caused it",
            "shared_with": ([smtp.host] if smtp.mode == "live" and smtp.host
                            else []),
            "note": ("Outgoing mail is handed to this mail server for "
                     "delivery." if smtp.mode == "live" and smtp.host else
                     "Mail is written to a local log on this server rather "
                     "than sent."),
        })

    ai_services = _ai_services(base)
    if ai_services:
        sections.append({
            "id": "ai",
            "title": "AI features",
            "why": "Text you type into an AI feature on this server is sent to "
                   "the provider below to answer it.",
            "kept": "the request is not stored by this server beyond its usage "
                    "record (which service, how many tokens, when)",
            "shared_with": list(ai_services),
            "note": "AI features are only reachable by a signed-in operator of "
                    "this server, not by visitors.",
        })

    return sections


def subprocessors(*, base=None, env=None, installed=None):
    """Third parties that actually have credentials on this box.

    Derived, so it is a list nobody has to remember to update: a key
    removed from the environment removes the row, and a provider nobody
    configured never appears. It is a list of who this server CAN send
    data to, which is the question a reader is asking.
    """
    base = _data_dir() if base is None else base
    env = os.environ if env is None else env
    if installed is None:
        installed = installed_packages(base)

    rows = []
    if "app-payments" in installed and object_stripe.stripe_config_from_env(env).configured:
        rows.append({
            "name": "Stripe",
            "role": "Payment processing",
            "data": "your name, email address, and the amount and reference of "
                    "the payment",
            "because": "DBBASIC_STRIPE_SECRET_KEY and "
                       "DBBASIC_STRIPE_WEBHOOK_SECRET are configured",
        })

    carrier = _settings(base).get(CARRIER_PROVIDER_KEY, "").strip().lower()
    if "app-shipping" in installed and carrier not in ("", "none", "manual"):
        rows.append({
            "name": carrier,
            "role": "Delivery",
            "data": "the delivery name and address on your order",
            "because": f"app_settings {CARRIER_PROVIDER_KEY} is set to "
                       f"{carrier!r}",
        })

    smtp = object_email.smtp_config_from_env(env)
    if smtp.mode == "live" and smtp.host:
        rows.append({
            "name": smtp.host,
            "role": "Email delivery",
            "data": "your email address and the contents of mail sent to you",
            "because": "DBBASIC_SMTP_MODE is live and DBBASIC_SMTP_HOST is set",
        })

    for service in _ai_services(base):
        rows.append({
            "name": service,
            "role": "AI features",
            "data": "the text an operator of this server types into an AI "
                    "feature, and whatever server records that feature reads "
                    "to answer it",
            "because": f"an API key for {service} is stored on this server",
        })

    return rows


def _rights(controller, analytics):
    """What a subject can actually be given here, stated as capability
    rather than as law. `export` is real -- action_export_subject_data
    folds every collection keyed to an email. Erasure is deliberately
    absent rather than promised: see that object's docstring."""
    return {
        "contact_email": controller["contact_email"],
        "export": "Ask, and everything this server holds against your email "
                  "address can be exported and sent to you.",
        "correction": "Ask, and a wrong name, address or email will be "
                      "corrected.",
        "erasure": "Ask. Orders, invoices and payments are business records "
                   "with a statutory retention period and usually cannot be "
                   "deleted; contact details that are not part of such a "
                   "record can be.",
        "traffic": ("Traffic records are keyed to an address and a token with "
                    "no link to a person, so they cannot be searched for you "
                    "in either direction; they age out on the retention above."
                    if analytics["enabled"] else
                    "No traffic records exist, so there are none to ask about."),
    }


# === rendering ===============================================================

def _markdown_ish(text):
    """Paragraphs and line breaks, escaped. Deliberately not a Markdown
    engine: this is an operator's legal text and the failure mode of a
    half-implemented renderer is a mangled legal sentence."""
    paragraphs = [block.strip() for block in str(text or "").split("\n\n")
                  if block.strip()]
    return "".join(f"<p>{_esc(block).replace(chr(10), '<br>')}</p>"
                   for block in paragraphs)


def _days(value):
    """14, not 14.0. A lifetime rendered with a spurious decimal reads as
    a computed estimate rather than the exact number of seconds this
    server writes."""
    return int(value) if float(value) == int(value) else value


def _cookie_row_html(row):
    necessary = ('<span class="yes">Yes</span>' if row["strictly_necessary"]
                 else "No")
    because = (f'<br><span class="muted">{_esc(row["necessary_because"])}</span>'
               if row["necessary_because"] else "")
    return (f'<tr><td><code>{_esc(row["name"])}</code></td>'
            f'<td>{_esc(row["purpose"])}'
            f'<br><span class="muted">{_esc(row["set_when"])}</span></td>'
            f'<td>{_days(row["max_age_days"])} days'
            f'<br><span class="muted">Max-Age={row["max_age_seconds"]}</span>'
            f'</td>'
            f'<td>{necessary}{because}</td></tr>')


def _cookie_table_html(rows):
    if not rows:
        return ('<p>This server sets <strong>no cookies at all</strong> in the '
                'configuration it is running.</p>')
    body = "".join(_cookie_row_html(row) for row in rows)
    return f"""
<table>
<thead><tr><th>Cookie</th><th>What it is for</th><th>How long</th>
<th>Strictly necessary?</th></tr></thead>
<tbody>{body}</tbody></table>
<p class="note">Every cookie above is <strong>first-party</strong> (set by
this server, sent only back to it), <strong>HttpOnly</strong> (no script on
the page can read it) and <strong>SameSite=Lax</strong>. None of them is an
advertising or cross-site identifier, and there is no third-party script on
any page of this site to set one.</p>
"""


def _analytics_html(analytics):
    if not analytics["enabled"]:
        return """
<h2>What is recorded about your visit</h2>
<p><strong>Nothing.</strong> This server keeps no log of page views: no
record of which pages were read, no visitor counter, and no third-party
analytics script. Requests may appear briefly in ordinary web-server
operational logs, which is not something this application stores or
reads.</p>
"""
    fields = "".join(
        f"<tr><td>{_esc(row['field'])}</td><td>{_esc(row['meaning'])}</td></tr>"
        for row in analytics["fields"])
    cookie_line = (
        f"A <code>{_esc(object_analytics.VISITOR_COOKIE_NAME)}</code> cookie is "
        f"set so a returning visit can be told from a new one; it is described "
        f"in the table below."
        if analytics["visitor_cookie"] else
        "<strong>No cookie is involved.</strong> Visits are counted by network "
        "address, which means a returning visitor is indistinguishable from a "
        "new one — that is the trade this server makes rather than storing an "
        "identifier on your device.")
    return f"""
<h2>What is recorded about your visit</h2>
<p>Every request this server answers appends one row to its own log, on its
own disk. No third party is involved and no script is served to your browser
to do it. {cookie_line}</p>
<table>
<thead><tr><th>Field</th><th>What it is</th></tr></thead>
<tbody>{fields}</tbody></table>
<p>Those rows are kept for <strong>{analytics["retention_days"]} days</strong>
and then deleted, and the log is additionally capped at
{analytics["max_rows"]:,} rows so the oldest are dropped if it fills sooner.
Both figures are read from this server's own configuration by the page you
are reading, so they are the numbers actually in force.</p>
<p class="note"><strong>Do Not Track and Global Privacy Control are
honoured.</strong> If your browser sends either signal, no cookie is set for
you at all. You are still counted by address, because asking not to be
remembered is not the same as asking not to be counted.</p>
"""


def _processing_html(sections):
    parts = []
    for section in sections:
        shared = section.get("shared_with") or []
        shared_line = (
            f"<p>Shared with: {_esc(', '.join(shared))}.</p>" if shared else "")
        note = (f"<p>{_esc(section['note'])}</p>" if section.get("note") else "")
        parts.append(
            f"<h3>{_esc(section['title'])}</h3>"
            f"<p>{_esc(section['why'])}</p>{note}{shared_line}"
            f"<p class=\"muted\">Kept: {_esc(section['kept'])}.</p>")
    return "".join(parts)


def _subprocessors_html(rows):
    if not rows:
        return ("<p>None. No third party is configured on this server, so "
                "nothing you give it is sent anywhere else.</p>")
    body = "".join(
        f"<tr><td>{_esc(row['name'])}</td><td>{_esc(row['role'])}</td>"
        f"<td>{_esc(row['data'])}</td></tr>" for row in rows)
    return f"""
<table>
<thead><tr><th>Who</th><th>What for</th><th>What they receive</th></tr></thead>
<tbody>{body}</tbody></table>
<p class="note">This list is generated from the credentials actually
configured on this server, not maintained by hand. A provider that is not
set up cannot appear on it, and one that is set up cannot be left off.</p>
"""


def _unsigned_html(fold):
    """The refusal. It names the missing settings and states no policy.

    Rendering a policy body under a heading with nobody's name on it would
    be the worst of both: a reader would take it for a commitment and have
    no one to hold to it.
    """
    missing = set(fold["controller"]["missing_settings"])
    rows = "".join(
        f"<tr><td><code>{_esc(key)}</code></td><td>{_esc(why)}</td>"
        f"<td>{'<strong>missing</strong>' if key in missing else 'set'}</td></tr>"
        for key, why in REQUIRED_SETTINGS)
    return f"""
<div class="breadcrumb"><a href="/">Home</a> / Privacy</div>
<h1>No privacy policy has been published for this server</h1>
<div class="refusal">
<p>This page generates a privacy policy from the server's own
configuration, and it will not publish one that nobody has signed. A policy
with no named controller and no address to write to looks like compliance
while giving a reader nobody to contact, which is worse than saying
nothing.</p>
<p>The operator of this server needs to set the following in
<code>app_settings</code>:</p>
<table>
<thead><tr><th>Setting</th><th>What it is</th><th></th></tr></thead>
<tbody>{rows}</tbody></table>
<p>Optionally also <code>{_esc(EXTRA_MARKDOWN_KEY)}</code>, for reviewed
legal wording of your own. This page never writes legal text itself — it
states facts about the software and stops.</p>
</div>
<p>The cookie facts are true regardless of who signed them, so they are
published anyway: <a href="/cookies">what this server stores in your
browser</a>.</p>
"""


def _policy_html(fold):
    controller = fold["controller"]
    extra = fold["extra_markdown"]
    extra_html = (f"<h2>Additional terms from {_esc(controller['name'])}</h2>"
                  f"{_markdown_ish(extra)}"
                  f"<p class=\"note\">The section above is the operator's own "
                  f"text. Everything else on this page is generated from this "
                  f"server's configuration.</p>") if extra else ""
    rights = fold["rights"]
    return f"""
<div class="breadcrumb"><a href="/">Home</a> / Privacy</div>
<h1>Privacy</h1>
<p class="note">This page is <strong>generated from this server's live
configuration</strong> every time it is loaded — the retention periods, the
cookie lifetimes and the list of third parties below are read from the
settings actually in force, not from a template. It describes what the
software does. It contains no legal interpretation, because the software
is not in a position to offer any.</p>

<h2>Who is responsible</h2>
<p><strong>{_esc(controller['name'])}</strong>, established in
{_esc(controller['jurisdiction'])}, is responsible for the information
described here. Write to
<a href="mailto:{_esc(controller['contact_email'])}">
{_esc(controller['contact_email'])}</a> about anything on this page.</p>

{_analytics_html(fold["analytics"])}

<h2>Cookies</h2>
{_cookie_table_html(fold["cookies"])}

<h2>What else this server holds, and why</h2>
{_processing_html(fold["processing"])}

<h2>Who else receives it</h2>
{_subprocessors_html(fold["subprocessors"])}

<h2>What you can ask for</h2>
<ul>
<li><strong>A copy of your data.</strong> {_esc(rights['export'])}</li>
<li><strong>A correction.</strong> {_esc(rights['correction'])}</li>
<li><strong>Deletion.</strong> {_esc(rights['erasure'])}</li>
</ul>
<p>{_esc(rights['traffic'])}</p>
<p>Write to <a href="mailto:{_esc(controller['contact_email'])}">
{_esc(controller['contact_email'])}</a>.</p>

{extra_html}

<p class="note">A machine-readable version of everything above is at
<a href="/privacy.json">/privacy.json</a>, and the cookie table on its own is
at <a href="/cookies">/cookies</a>. They are the same fold over the same
settings, so they cannot disagree with this page.</p>
"""


def _cookies_page_html(fold):
    return f"""
<div class="breadcrumb"><a href="/">Home</a> / <a href="/privacy">Privacy</a>
 / Cookies</div>
<h1>Cookies</h1>
<p>Everything this server can store in your browser, with the lifetime it
actually sets. Read from the running configuration, so it is the current
list rather than a list somebody maintained.</p>
{_cookie_table_html(fold["cookies"])}
<p><a href="/privacy">The full privacy page</a> &middot;
<a href="/privacy.json">machine-readable</a></p>
"""


def _page(title, body):
    return {
        "status": 200,
        "content_type": "text/html; charset=utf-8",
        "body": f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap pp">
<header class="app"><h1><a href="/">DBBASIC</a></h1></header>
{body}
</div>
<script src="/nav"></script>
</body>
</html>""",
    }


def GET(request):
    path = str((request or {}).get("_path") or "/privacy").strip().lower()
    fold = policy()

    if path.endswith(".json"):
        # The JSON surface is served whether or not the policy is signed:
        # an unsigned fold is still an honest answer, and it says so in
        # `controller.signed` and `controller.missing_settings` rather
        # than by 404ing and leaving a monitor guessing.
        return {
            "status": 200,
            "content_type": "application/json; charset=utf-8",
            "body": json.dumps(fold, indent=2, sort_keys=True),
        }

    if path.rstrip("/").endswith("/cookies"):
        return _page("Cookies", _cookies_page_html(fold))

    if not fold["controller"]["signed"]:
        return _page("Privacy", _unsigned_html(fold))
    return _page("Privacy", _policy_html(fold))


def POST(request):
    # Reading a policy is a read either way.
    return GET(request)
