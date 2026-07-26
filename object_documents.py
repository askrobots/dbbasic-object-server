"""The document layer: ONE renderer for every business document.

Seven printable pages already shipped before this module existed -- packing
slip, pick list, manifest, kitchen ticket, kitchen queue, receiving sheet,
return form -- plus the invoice portal and the order tracking page. Each one
invented its own header, its own `@media print` block, and its own answer to
"does this show money?". None of them shares a line with any other, and the
seven blocks between them contain zero occurrences of the one print rule that
actually matters:

    thead { display: table-header-group; }

Without it a three-page pick list comes out of the printer with column headings
on page one and two anonymous columns of numbers on pages two and three. Nobody
notices in review, because nobody reviews on paper. That single rule is most of
the argument for a shared layer.

WHAT IS HERE AND WHY IT IS PURE
-------------------------------
This module is a FOLD: facts in, render model out, HTML out, hash out. No file
reads, no settings lookups, no clock, no currency formatting (that needs a data
dir -- see object_money). Every decision a document makes lives here; every
piece of I/O a document needs lives in the calling object. That split is what
lets app-invoices and app-shipping share one renderer at all: package objects
cannot import each other, and the ONLY code two packages can both reach is a
root ``object_*`` module. So the renderer had to be here or be copied.

THE MONEY RULE IS A PARAMETER, NOT AN ARGUMENT
----------------------------------------------
``KINDS[kind]["show_money"]`` decides, once, for every document of that kind,
and there is deliberately no per-call override. The packing slip's no-prices
rule (see site_packing_slip: pricelessness by construction is what makes every
parcel gift-safe without a flag somebody has to remember to tick) is now a
table entry rather than a paragraph re-argued in each new printable. A caller
that could pass ``show_money=True`` to a packing slip would be a caller that
can staple the amount paid to a birthday present, so that argument is not
offered.

When ``show_money`` is false the money is not hidden -- it is NEVER PUT IN THE
MODEL. Line unit prices, line amounts, totals and the currency are dropped by
``build_model`` itself, so a snapshot of a packing slip contains no prices
either, and no future renderer, template, PDF engine or debug dump can leak
what was never carried.

THE ADDRESS RULE IS ALSO A PARAMETER
------------------------------------
A packing slip's ship-to is not an invoice's bill-to. ``KINDS[kind]["to_role"]``
names which one the document's "to" party is, and the renderer labels it from
that -- so a document cannot accidentally print the accounts department's
address on the box.

PRINT CSS: WHERE IT LIVES, AND WHY NOT IN /style
------------------------------------------------
``site_style`` (app-theme, served at ``/style``) is the SCREEN design system:
semantic tokens, chrome, and a theme an operator can swap live. Paper geometry
is a different thing wearing the same word "CSS":

  * it is not themeable -- installing the `terminal` theme must not change the
    margins of an invoice somebody is about to post;
  * it depends on a setting app-theme has no business knowing
    (``documents.page_size``: A4 or Letter is a fact about the operator's
    printer tray, not about their brand);
  * it must survive leaving the server. A document saved to disk, attached to
    a mail, or handed to a PDF engine still has to print correctly, and a page
    whose print rules live behind ``<link href="/style">`` prints wrong the
    moment it is read anywhere but this origin.

So the print CSS is defined ONCE, here, in ``document_css()``, and INLINED by
``render_page`` into every document page after the ``/style`` link. That keeps
the design-system property that matters (nothing downstream owns a copy --
change this function and every document on the box changes) while keeping the
property ``/style`` cannot give (the page is self-contained on paper). Pages
still link ``/style`` for tokens and chrome; the two do not overlap.

PDF
---
Browser Print -> Save as PDF already produces a good PDF and costs nothing, so
every page rendered here SAYS SO (``_print_hint_html``) rather than leaving it
to be discovered. Server-side PDF is a capability with an honest absent state
(docs/capability-objects.md) -- ``pdf_engine_status`` is the pure half of that
decision, and ``render_page(pdf=...)`` is why no surface offers a button that
would 409: the button is not drawn unless an engine is actually there.
"""

from __future__ import annotations

import hashlib
import html
import json
from typing import Any, Callable, Iterable, Mapping, Sequence

__all__ = [
    "DocumentError",
    "NamelessBusinessError",
    "UnknownKindError",
    "KINDS",
    "PAGE_SIZES",
    "DEFAULT_PAGE_SIZE",
    "PDF_ENGINE_SETTING",
    "PAGE_SIZE_SETTING",
    "kind_spec",
    "source_spec",
    "shows_money",
    "business_header",
    "build_model",
    "facts_from_records",
    "content_hash",
    "snapshot_json",
    "model_from_snapshot",
    "document_css",
    "render_html",
    "render_page",
    "pdf_engine_status",
]


class DocumentError(ValueError):
    """A document that could not honestly be built."""


class NamelessBusinessError(DocumentError):
    """No ``business.name`` configured.

    A document is a statement by a business. One with no name on it cannot be
    paid (there is nobody to pay), cannot be queried (there is nobody to ring)
    and cannot be filed. Fabricating a default -- "My Company", the server's
    hostname, the owner's user id -- would be worse than refusing, because it
    would be a plausible-looking lie printed on paperwork somebody keeps.

    Raised by ``business_header`` and, by default, ``build_model``. Callers
    that must render for an operator standing at a printer pass
    ``require_business=False`` and get an unbranded (but honest) document; the
    refusal belongs at SEND time, where nobody is waiting and the cost of
    stopping is one settings row.
    """


class UnknownKindError(DocumentError):
    """A document kind this layer has never heard of."""


# --- the kind registry -------------------------------------------------------
#
# Everything a document kind decides differently from every other kind, in one
# table. Adding a printable is adding a row here; it is not adding a file with
# its own opinions about money and addresses.
#
#   label        what the document calls itself at the top of the page
#   show_money   the no-prices rule as a parameter (see the module docstring)
#   to_role      which address the counterparty block carries
#   from_label / to_label   the two headings over the parties block
#   sendable     whether `source` can resolve facts for it today. A kind with
#                no `source` is printable but not yet sendable, which is an
#                honest state rather than a missing feature: the five
#                unmigrated printables are declared here so that migrating
#                them later is a data edit rather than a fresh argument.

_TO_LABELS = {
    "bill_to": "Bill to",
    "ship_to": "Ship to",
    "deliver_to": "Deliver to",
    "supplier": "Supplier",
    "customer": "Customer",
}

KINDS: dict[str, dict[str, Any]] = {
    # --- sendable: a source record resolves into facts ---------------------
    "invoice": {
        "label": "Invoice",
        "show_money": True,
        "to_role": "bill_to",
        "from_label": "From",
        "source": {"collection": "invoices",
                   "lines_collection": "invoice_lines",
                   "lines_fk": "invoice_id"},
    },
    "quote": {
        "label": "Quote",
        "show_money": True,
        "to_role": "bill_to",
        "from_label": "From",
        # A quote is an ORDER with doc_type=quote (plan/documents-print-pdf-spec
        # .md): same row, same history, before anybody committed. It resolves
        # against `orders` for exactly that reason -- a quote with its own
        # collection would be a quote nobody can find after the sale.
        "source": {"collection": "orders",
                   "lines_collection": "order_lines",
                   "lines_fk": "order_id"},
    },
    "order": {
        "label": "Order confirmation",
        "show_money": True,
        "to_role": "bill_to",
        "from_label": "From",
        "source": {"collection": "orders",
                   "lines_collection": "order_lines",
                   "lines_fk": "order_id"},
    },
    "purchase_order": {
        "label": "Purchase order",
        "show_money": True,
        "to_role": "supplier",
        "from_label": "Ordered by",
        "source": {"collection": "orders",
                   "lines_collection": "order_lines",
                   "lines_fk": "order_id"},
    },
    "packing_slip": {
        "label": "Packing slip",
        # NO PRICES. Not "hidden behind a gift flag", not "unless the buyer
        # ticked something": none, ever. See site_packing_slip's docstring --
        # the money conversation already happened with somebody who may not be
        # the person holding the box.
        "show_money": False,
        "to_role": "ship_to",
        "from_label": "From",
        "source": {"collection": "shipments",
                   "lines_collection": "shipment_lines",
                   "lines_fk": "shipment_id"},
    },

    # --- printable, not yet sendable ---------------------------------------
    "pick_list": {
        "label": "Pick list", "show_money": False, "to_role": "ship_to",
        "from_label": "From",
    },
    "manifest": {
        "label": "Carrier manifest", "show_money": False, "to_role": "ship_to",
        "from_label": "From",
    },
    "receiving_sheet": {
        "label": "Receiving sheet", "show_money": False, "to_role": "supplier",
        "from_label": "Received by",
    },
    "return_form": {
        "label": "Return form", "show_money": False, "to_role": "customer",
        "from_label": "Return to",
    },
    "kitchen_ticket": {
        "label": "Kitchen ticket", "show_money": False, "to_role": "customer",
        "from_label": "From",
    },
    "statement": {
        "label": "Statement", "show_money": True, "to_role": "bill_to",
        "from_label": "From",
    },
}

PAGE_SIZE_SETTING = "documents.page_size"
PDF_ENGINE_SETTING = "documents.pdf_engine"

# `size` values CSS actually understands. An operator who types something else
# gets A4 rather than a stylesheet with a syntax error in it, because a broken
# @page rule fails silently and prints at whatever the browser felt like.
PAGE_SIZES = {"a4": "A4", "letter": "Letter", "legal": "Legal",
              "a5": "A5"}
DEFAULT_PAGE_SIZE = "A4"

# The engines a server-side PDF could plausibly use, and what installing each
# one actually costs. Named in the refusal so an operator gets a decision
# rather than an error (see docs/capability-objects.md, and app-shipping's
# carrier connector for the same absent-by-default posture).
PDF_ENGINES = {
    "weasyprint": "pip install weasyprint, plus the pango/cairo system "
                  "libraries (heavy on a 1GB box)",
    "chrome": "a headless Chrome/Chromium binary on PATH (heavier still: a "
              "whole browser to render a page this server already rendered)",
    "wkhtmltopdf": "the wkhtmltopdf binary (unmaintained upstream)",
}


def kind_spec(kind: str) -> dict[str, Any]:
    """The one row of the registry that decides everything about `kind`."""
    spec = KINDS.get(str(kind or "").strip())
    if spec is None:
        raise UnknownKindError(
            f"Unknown document kind {kind!r}. Known kinds: "
            + ", ".join(sorted(KINDS)))
    return spec


def source_spec(kind: str) -> dict[str, str] | None:
    """Which collection a kind's facts come from, or None when the kind is
    printable but not yet sendable. Data, so the sending action and the public
    document page read the same collections without either owning the
    knowledge."""
    spec = kind_spec(kind).get("source")
    return dict(spec) if spec else None


def shows_money(kind: str) -> bool:
    """The no-prices rule, asked as a question instead of re-argued."""
    return bool(kind_spec(kind)["show_money"])


# --- the header --------------------------------------------------------------

_BUSINESS_FIELDS = (
    ("name", "business.name"),
    ("address", "business.address"),
    ("email", "business.email"),
    ("phone", "business.phone"),
    ("website", "business.website"),
    ("tax_id", "business.tax_id"),
)


def business_header(settings: Mapping[str, Any]) -> dict[str, str]:
    """The business's own identity, from ``business.*``.

    Refuses a nameless business: see NamelessBusinessError. Every other field
    is optional and simply absent when unset -- an operator who has not typed
    a VAT number has a document without one, not a document with a blank
    labelled row.
    """
    values = {key: _text(settings.get(setting_key))
              for key, setting_key in _BUSINESS_FIELDS}
    if not values["name"]:
        raise NamelessBusinessError(
            "business.name is not set: a document with no business on it "
            "cannot be paid, queried or filed. Set business.name in Settings.")
    return {key: value for key, value in values.items() if value}


# --- the fold ----------------------------------------------------------------

def build_model(
    kind: str,
    facts: Mapping[str, Any],
    settings: Mapping[str, Any],
    *,
    require_business: bool = True,
) -> dict[str, Any]:
    """Fold one document's facts into the render model every surface uses.

    `facts` is a plain dict of already-resolved, already-formatted values --
    money arrives as strings a human can read ("$24.00"), never as minor units,
    because formatting currency needs a data dir and this module does no I/O.
    ``facts_from_records`` builds one from a stored record for the sendable
    kinds; a hand-written page can build one itself.

    Recognised keys: number, date, state, reference, currency, to, from,
    lines, totals, terms, notes, footer.

    The model is JSON-safe by construction: it is what gets hashed
    (``content_hash``) and snapshotted (``snapshot_json``) when a document is
    sent, and a snapshot that cannot round-trip through JSON is not evidence
    of anything.
    """
    spec = kind_spec(kind)
    money = bool(spec["show_money"])

    try:
        business = business_header(settings)
    except NamelessBusinessError:
        if require_business:
            raise
        # An operator standing at a printer gets their paperwork. The document
        # is honest about being unbranded rather than inventing a name, and
        # the page shows a no-print nudge (see render_page) so the missing
        # setting is visible to the one person who can fix it.
        business = {}

    model: dict[str, Any] = {
        "kind": str(kind).strip(),
        "kind_label": spec["label"],
        "show_money": money,
        "business": business,
        "number": _text(facts.get("number")),
        "date": _text(facts.get("date")),
        "state": _text(facts.get("state")),
        "reference": _text(facts.get("reference")),
        "parties": _parties(spec, facts, business),
        "lines": _lines(facts.get("lines") or (), money),
        "totals": _totals(facts.get("totals") or (), money),
        "terms": _text(facts.get("terms")),
        "notes": _notes(facts.get("notes") or ()),
        "footer": _text(facts.get("footer")),
    }
    if money:
        currency = _text(facts.get("currency"))
        if currency:
            model["currency"] = currency
    return model


def _parties(spec, facts, business) -> list[dict[str, str]]:
    """From/to, with the address the KIND says this document carries.

    The "from" party defaults to the business header rather than being typed
    again: a document whose letterhead and whose "from" block can disagree is
    a document that will eventually disagree.
    """
    parties = []
    origin = facts.get("from")
    if origin:
        parties.append(_party("from", spec.get("from_label", "From"), origin))
    elif business:
        parties.append(_party("from", spec.get("from_label", "From"), {
            "name": business.get("name", ""),
            "address": business.get("address", ""),
            "email": business.get("email", ""),
        }))
    recipient = facts.get("to")
    if recipient:
        role = spec["to_role"]
        parties.append(_party(role, _TO_LABELS.get(role, "To"), recipient))
    return parties


def _party(role: str, label: str, values: Mapping[str, Any]) -> dict[str, str]:
    party = {"role": role, "label": label}
    for key in ("name", "address", "email", "phone"):
        value = _text(values.get(key))
        if value:
            party[key] = value
    return party


# Line keys that carry money. Dropped -- not blanked, not hidden -- on a
# document whose kind says no prices.
_MONEY_LINE_KEYS = ("unit_price", "amount", "tax", "discount")
_PLAIN_LINE_KEYS = ("description", "sku", "quantity", "unit", "note")


def _lines(raw: Iterable[Mapping[str, Any]], money: bool) -> list[dict[str, str]]:
    lines = []
    for entry in raw:
        line = {}
        for key in _PLAIN_LINE_KEYS:
            value = _text(entry.get(key))
            if value:
                line[key] = value
        if money:
            for key in _MONEY_LINE_KEYS:
                value = _text(entry.get(key))
                if value:
                    line[key] = value
        if line:
            lines.append(line)
    return lines


def _totals(raw: Iterable[Mapping[str, Any]], money: bool) -> list[dict[str, Any]]:
    """Empty on a no-prices document, always. A totals block is money by
    definition; there is no such thing as a priceless total."""
    if not money:
        return []
    totals = []
    for entry in raw:
        label = _text(entry.get("label"))
        value = _text(entry.get("value"))
        if not label and not value:
            continue
        total = {"label": label, "value": value}
        if entry.get("emphasis"):
            total["emphasis"] = True
        totals.append(total)
    return totals


def _notes(raw: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    notes = []
    for entry in raw:
        body = _text(entry.get("body"))
        if not body:
            continue
        notes.append({"title": _text(entry.get("title")), "body": body})
    return notes


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


# --- records -> facts (still pure: the caller does the reading) --------------

# How each sendable kind's source record spells the things every document
# needs. Tried in order; the first field present on the record wins, so one
# mapping serves collections that named the same fact differently.
_FIELD_ALIASES = {
    "number": ("number", "reference", "id"),
    "date": ("issue_date", "order_date", "shipped_on", "created_at"),
    "state": ("status",),
    "currency": ("currency",),
    "to_name": ("ship_to_name", "customer_name", "supplier_name", "contact_name"),
    "to_address": ("ship_to_address", "customer_address", "billing_address",
                   "address"),
    "to_email": ("customer_email", "supplier_email", "email"),
}

# Per-kind overrides where the generic order would pick the wrong fact. A
# packing slip must prefer ship_to_*; an invoice must prefer the billing
# address even on a record that also carries a shipping one.
_KIND_ALIASES = {
    "packing_slip": {"to_name": ("ship_to_name", "customer_name"),
                     "to_address": ("ship_to_address",),
                     "date": ("shipped_on", "created_at")},
    "invoice": {"to_name": ("customer_name",),
                "to_address": ("customer_address", "billing_address", "address"),
                "date": ("issue_date", "created_at")},
}


def facts_from_records(
    kind: str,
    record: Mapping[str, Any],
    lines: Sequence[Mapping[str, Any]] = (),
    *,
    money: Callable[[Any], str] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Map a stored record + its line rows onto the facts ``build_model`` wants.

    Pure: the caller reads the rows and passes a `money` callable (cents ->
    display string, usually a closure over object_money.format_amount and a
    base_dir). On a no-prices kind `money` is never called at all, so a
    packing slip cannot be made to format a price by accident.

    `extra` overlays anything the caller knows that the record does not --
    a gift message read off a joined order, a state the page derived, the
    reference of the shipment a slip belongs to.
    """
    spec = kind_spec(kind)
    show_money = bool(spec["show_money"])
    aliases = dict(_FIELD_ALIASES)
    aliases.update(_KIND_ALIASES.get(kind, {}))

    def pick(name: str) -> str:
        for field in aliases.get(name, ()):  # first present wins
            value = _text(record.get(field))
            if value:
                return value
        return ""

    facts: dict[str, Any] = {
        "number": pick("number"),
        "date": pick("date")[:10],
        "state": pick("state"),
        "currency": pick("currency") if show_money else "",
        "to": {"name": pick("to_name"), "address": pick("to_address"),
               "email": pick("to_email")},
        "lines": [_line_from_record(row, show_money, money) for row in lines],
    }
    if show_money:
        facts["totals"] = _totals_from_record(record, money)
    for key, value in (extra or {}).items():
        facts[key] = value
    return facts


def _line_from_record(row, show_money, money):
    line = {
        "description": (_text(row.get("description"))
                        or _text(row.get("name"))
                        or _text(row.get("product_id"))),
        "sku": _text(row.get("sku")),
        "quantity": _text(row.get("quantity")) or "1",
    }
    if show_money and money is not None:
        if _text(row.get("unit_price_cents")):
            line["unit_price"] = money(row.get("unit_price_cents"))
        if _text(row.get("line_total_cents")):
            line["amount"] = money(row.get("line_total_cents"))
    return line


_TOTAL_FIELDS = (
    ("subtotal_cents", "Subtotal", False),
    ("tax_cents", "Tax", False),
    ("shipping_cents", "Shipping", False),
    ("total_cents", "Total", True),
    ("amount_paid_cents", "Paid", False),
    ("balance_due_cents", "Balance due", True),
)


def _totals_from_record(record, money):
    if money is None:
        return []
    totals = []
    for field, label, emphasis in _TOTAL_FIELDS:
        if not _text(record.get(field)):
            continue
        entry = {"label": label, "value": money(record.get(field))}
        if emphasis:
            entry["emphasis"] = True
        totals.append(entry)
    return totals


# --- the snapshot rule's arithmetic ------------------------------------------

def content_hash(model: Mapping[str, Any]) -> str:
    """A stable fingerprint of what a document SAYS.

    Canonical JSON (sorted keys, no incidental whitespace) so two renders of
    the same facts hash the same on any machine, in any Python, in any order
    the dict happened to be built. Stored on the sent document beside the
    snapshot: the snapshot is the evidence and this is the seal on it.
    """
    payload = json.dumps(model, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def snapshot_json(model: Mapping[str, Any]) -> str:
    """The model as one storable line. Compact and escaped, so it is safe in a
    TSV cell (json.dumps escapes tabs and newlines by construction)."""
    return json.dumps(model, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def model_from_snapshot(text: str) -> dict[str, Any]:
    """Read a stored snapshot back, or raise DocumentError.

    Refuses rather than repairs: a document page that silently rendered a
    half-parsed snapshot would be showing something nobody ever sent, which is
    the exact failure this whole mechanism exists to prevent.
    """
    try:
        model = json.loads(text or "")
    except (TypeError, ValueError) as exc:
        raise DocumentError(f"Unreadable document snapshot: {exc}") from exc
    if not isinstance(model, dict):
        raise DocumentError("Document snapshot is not an object")
    return model


# --- PDF: the capability boundary --------------------------------------------

def pdf_engine_status(setting_value: Any) -> dict[str, Any]:
    """What ``documents.pdf_engine`` means today, as a plain answer.

    Absent by default and absent in practice: no PDF engine module ships with
    this server, exactly as no carrier adapter ships with app-shipping. The
    honest states are

        available=False, engine="none"   nothing configured -- Print -> Save
                                         as PDF is the answer, and it works
        available=False, engine="weasyprint"  a name with nothing behind it on
                                         this box

    and the second one is reported rather than swallowed, because an operator
    who pasted an engine name in and heard nothing would reasonably conclude
    PDFs were being generated.

    ``available`` is what every surface consults before drawing a PDF button.
    A button that 409s tells the person pressing it that something is broken
    on your end, which is the same reason site_invoice_portal only draws "Pay
    by card" when Stripe is actually configured.
    """
    engine = _text(setting_value).lower() or "none"
    if engine in ("none", "off", "disabled"):
        return {
            "available": False, "engine": "none",
            "reason": (f"No PDF engine is configured ({PDF_ENGINE_SETTING} is "
                       "'none'). Print this page and choose Save as PDF -- "
                       "that already produces a good PDF and costs nothing."),
            "install": dict(PDF_ENGINES),
        }
    if engine in PDF_ENGINES:
        return {
            "available": False, "engine": engine,
            "reason": (f"{PDF_ENGINE_SETTING} names '{engine}', but no PDF "
                       "engine module is installed on this server. Install "
                       f"it: {PDF_ENGINES[engine]}."),
            "install": {engine: PDF_ENGINES[engine]},
        }
    return {
        "available": False, "engine": engine,
        "reason": (f"{PDF_ENGINE_SETTING} names '{engine}', which is not a "
                   "PDF engine this server knows. Set it to one of "
                   + ", ".join(sorted(PDF_ENGINES))
                   + ", or to 'none' and use Print -> Save as PDF."),
        "install": dict(PDF_ENGINES),
    }


# --- print CSS, defined once --------------------------------------------------

def page_size(setting_value: Any) -> str:
    """The `@page size` value, from ``documents.page_size``. Unknown values
    fall back to A4 rather than emitting CSS the browser will discard."""
    return PAGE_SIZES.get(_text(setting_value).lower(), DEFAULT_PAGE_SIZE)


def document_css(size: Any = DEFAULT_PAGE_SIZE) -> str:
    """Every print rule this server has, in one string.

    The rule that justifies the module: ``thead { display: table-header-group }``
    tells the printer to repeat the heading row at the top of every page a
    table spills onto. Seven hand-rolled print blocks shipped without it, and
    a three-page pick list printed its column headings once.
    """
    size = page_size(size)
    return """
.doc { max-width: 46rem; margin: 0 auto; padding: 2rem 1.25rem; }
.doc-head { display: flex; justify-content: space-between; gap: 1.5rem;
  flex-wrap: wrap; align-items: flex-start; margin-bottom: 1.5rem; }
.doc-business { font-size: 0.9rem; line-height: 1.45; }
.doc-business .name { font-size: 1.05rem; font-weight: 700; display: block; }
.doc-identity { text-align: right; }
.doc-identity h1 { margin: 0 0 0.2rem; font-size: 1.5rem; }
.doc-identity .meta { color: var(--muted, #999); font-size: 0.9rem;
  line-height: 1.5; }
.doc-state { display: inline-block; padding: 0.1rem 0.55rem; border-radius: 999px;
  border: 1px solid var(--line, #444); font-size: 0.75rem; text-transform: uppercase;
  letter-spacing: 0.05em; }
.doc-parties { display: flex; gap: 1rem; flex-wrap: wrap; margin: 0 0 1.25rem; }
.doc-party { border: 1px solid var(--line, #333); border-radius: 8px;
  padding: 0.7rem 0.95rem; min-width: 15rem; flex: 1 1 15rem; line-height: 1.45; }
.doc-party .role { font-size: 0.7rem; text-transform: uppercase;
  letter-spacing: 0.05em; color: var(--muted, #999); display: block;
  margin-bottom: 0.2rem; }
table.doc-lines { width: 100%; border-collapse: collapse; margin: 0 0 1.25rem; }
table.doc-lines th, table.doc-lines td { text-align: left; padding: 0.4rem 0.6rem;
  border-bottom: 1px solid var(--line, #38384a); vertical-align: top; }
table.doc-lines th { font-size: 0.75rem; text-transform: uppercase;
  letter-spacing: 0.05em; color: var(--muted, #999); }
table.doc-lines td.num, table.doc-lines th.num { text-align: right;
  white-space: nowrap; }
.doc-totals { width: auto; margin: 0 0 1.25rem auto; border-collapse: collapse; }
.doc-totals td { padding: 0.25rem 0.6rem; text-align: right; }
.doc-totals tr.emphasis td { font-weight: 700; border-top: 1px solid var(--line, #444); }
.doc-note { border: 1px solid var(--line, #333); border-radius: 8px;
  padding: 0.7rem 0.95rem; margin: 0 0 1rem; white-space: pre-wrap; }
.doc-terms { color: var(--muted, #999); font-size: 0.9rem; margin: 0 0 1rem;
  white-space: pre-wrap; }
.doc-footer { color: var(--muted, #999); font-size: 0.85rem;
  border-top: 1px solid var(--line, #333); padding-top: 0.75rem; margin-top: 1.5rem; }
.doc-banner { border: 1px solid var(--warning, #f1b747); border-radius: 8px;
  padding: 0.7rem 0.95rem; margin: 0 0 1.25rem; line-height: 1.5; }
.doc-banner strong { display: block; }
.doc-hint { color: var(--muted, #999); font-size: 0.85rem; margin: 1rem 0 0; }
.doc-setup { border: 1px dashed var(--warning, #f1b747); border-radius: 8px;
  padding: 0.6rem 0.9rem; margin: 0 0 1.25rem; font-size: 0.9rem; }

@page { size: __SIZE__; margin: 14mm; }

@media print {
  /* Chrome is not part of the document. */
  nav, header.app, .noprint, .btn, button, form { display: none !important; }
  html, body { background: #fff !important; color: #000 !important; }
  .doc { max-width: none; padding: 0; }
  a { color: #000; text-decoration: none; }

  /* THE RULE. A table that spills onto page two repeats its heading row
     there. Every hand-rolled print stylesheet in this repo forgot it, and
     nobody notices until a three-page pick list prints headerless. */
  thead { display: table-header-group; }
  tfoot { display: table-footer-group; }
  tr, img { page-break-inside: avoid; break-inside: avoid; }

  /* Keep blocks whole across a break, and never orphan a heading. */
  .doc-party, .doc-note, .doc-totals, .doc-banner {
    page-break-inside: avoid; break-inside: avoid; }
  h1, h2, h3 { page-break-after: avoid; break-after: avoid; }
  p { orphans: 3; widows: 3; }

  /* Borders that survive a printer with no colour and no background
     graphics -- the default in every browser's print dialog. */
  .doc-party, .doc-note, .doc-banner, table.doc-lines th,
  table.doc-lines td, .doc-totals tr.emphasis td, .doc-footer,
  .doc-state { border-color: #999 !important; }
  .doc-identity .meta, .doc-party .role, .doc-terms, .doc-footer,
  table.doc-lines th { color: #333 !important; }
}
""".replace("__SIZE__", size)


# --- rendering ----------------------------------------------------------------

def _esc(value: Any) -> str:
    return html.escape(_text(value))


def _esc_multiline(value: Any) -> str:
    return _esc(value).replace("\n", "<br>")


def _business_html(model) -> str:
    business = model.get("business") or {}
    if not business:
        return '<div class="doc-business"></div>'
    parts = [f'<span class="name">{_esc(business["name"])}</span>']
    if business.get("address"):
        parts.append(f'<div>{_esc_multiline(business["address"])}</div>')
    contact = " &middot; ".join(
        _esc(business[key]) for key in ("email", "phone", "website")
        if business.get(key))
    if contact:
        parts.append(f"<div>{contact}</div>")
    if business.get("tax_id"):
        parts.append(f'<div>{_esc(business["tax_id"])}</div>')
    return f'<div class="doc-business">{"".join(parts)}</div>'


def _identity_html(model) -> str:
    meta = []
    if model.get("number"):
        meta.append(f'No. {_esc(model["number"])}')
    if model.get("date"):
        meta.append(_esc(model["date"]))
    if model.get("reference"):
        meta.append(_esc(model["reference"]))
    state = (f'<div><span class="doc-state">{_esc(model["state"])}</span></div>'
             if model.get("state") else "")
    return (f'<div class="doc-identity"><h1>{_esc(model.get("kind_label"))}</h1>'
            f'<div class="meta">{" &middot; ".join(meta)}</div>{state}</div>')


def _parties_html(model) -> str:
    parties = model.get("parties") or []
    if not parties:
        return ""
    blocks = []
    for party in parties:
        rows = [f'<span class="role">{_esc(party.get("label"))}</span>']
        if party.get("name"):
            rows.append(f'<strong>{_esc(party["name"])}</strong>')
        for key in ("address", "email", "phone"):
            if party.get(key):
                rows.append(f'<div>{_esc_multiline(party[key])}</div>')
        blocks.append(f'<div class="doc-party">{"".join(rows)}</div>')
    return f'<div class="doc-parties">{"".join(blocks)}</div>'


def _lines_html(model) -> str:
    lines = model.get("lines") or []
    if not lines:
        # Nothing at all, not an empty table and not a stock apology. What a
        # document with no lines should SAY depends entirely on which document
        # it is ("nothing has been picked into this shipment" is useful; "no
        # lines" on a cancelled invoice is noise), so the page says it.
        return ""
    money = bool(model.get("show_money"))
    has_sku = any(line.get("sku") for line in lines)

    head = ["<th>Item</th>"]
    if has_sku:
        head.append("<th>Code</th>")
    head.append('<th class="num">Qty</th>')
    if money:
        head.append('<th class="num">Unit price</th>')
        head.append('<th class="num">Amount</th>')

    rows = []
    for line in lines:
        cells = [f'<td>{_esc(line.get("description"))}'
                 + (f'<div class="doc-hint">{_esc(line["note"])}</div>'
                    if line.get("note") else "")
                 + "</td>"]
        if has_sku:
            cells.append(f'<td>{_esc(line.get("sku"))}</td>')
        quantity = _esc(line.get("quantity"))
        unit = _esc(line.get("unit"))
        cells.append(f'<td class="num">{quantity}{(" " + unit) if unit else ""}</td>')
        if money:
            cells.append(f'<td class="num">{_esc(line.get("unit_price"))}</td>')
            cells.append(f'<td class="num">{_esc(line.get("amount"))}</td>')
        rows.append(f"<tr>{''.join(cells)}</tr>")

    # <thead> is not decoration here: document_css's table-header-group rule
    # has nothing to repeat without it.
    return (f'<table class="doc-lines"><thead><tr>{"".join(head)}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def _totals_html(model) -> str:
    totals = model.get("totals") or []
    if not totals:
        return ""
    rows = "".join(
        f'<tr class="emphasis"><td>{_esc(t.get("label"))}</td>'
        f'<td>{_esc(t.get("value"))}</td></tr>' if t.get("emphasis") else
        f'<tr><td>{_esc(t.get("label"))}</td><td>{_esc(t.get("value"))}</td></tr>'
        for t in totals)
    return f'<table class="doc-totals"><tbody>{rows}</tbody></table>'


def _notes_html(model) -> str:
    blocks = []
    for note in model.get("notes") or []:
        title = (f'<strong>{_esc(note["title"])}</strong><br>'
                 if note.get("title") else "")
        blocks.append(f'<div class="doc-note">{title}'
                      f'{_esc_multiline(note.get("body"))}</div>')
    return "".join(blocks)


def render_html(model: Mapping[str, Any]) -> str:
    """The document itself: header, identity, parties, lines, totals, terms,
    notes, footer. One template, used by the print view, by anything that
    emails a document, and by a PDF engine if one is ever installed -- so a
    PDF cannot say something the link does not."""
    parts = [
        '<div class="doc-head">',
        _business_html(model),
        _identity_html(model),
        "</div>",
        _parties_html(model),
        _lines_html(model),
        _totals_html(model),
        _notes_html(model),
    ]
    if model.get("terms"):
        parts.append(f'<div class="doc-terms">{_esc_multiline(model["terms"])}</div>')
    if model.get("footer"):
        parts.append(f'<div class="doc-footer">{_esc_multiline(model["footer"])}</div>')
    return "".join(part for part in parts if part)


def _print_hint_html(pdf: bool) -> str:
    """Print -> Save as PDF already works, so say so.

    It costs nothing, it needs no dependency, and leaving it to be discovered
    is how somebody ends up asking for a PDF feature they already have.
    """
    hint = ('<p class="doc-hint noprint">Print this page (Ctrl/Cmd+P) and '
            'choose <strong>Save as PDF</strong> to get a PDF file &mdash; no '
            'setup needed.</p>')
    if not pdf:
        return hint
    return hint + ('<p class="noprint"><button class="btn" id="docpdf">'
                   "Download PDF</button></p>")


_SETUP_NUDGE = (
    '<div class="doc-setup noprint">This document has no business identity on '
    "it. Set <strong>business.name</strong> (and address) in Settings so your "
    "paperwork says who sent it.</div>")


def render_page(
    model: Mapping[str, Any],
    *,
    title: str = "",
    banner: str = "",
    before: str = "",
    after: str = "",
    chrome: bool = True,
    pdf: bool = False,
    setup_nudge: bool | None = None,
    size: Any = DEFAULT_PAGE_SIZE,
    status: int = 200,
) -> dict[str, Any]:
    """One whole HTML page around one document, as an object response.

    `chrome=False` drops the ``/nav`` script -- the posture site_invoice_portal
    established for a page handed to a stranger's inbox: never one click from
    somebody else's records or from a sign-in wall for a system they have no
    account on.

    `pdf` draws the download button, and it must only ever be true when
    ``pdf_engine_status(...)["available"]`` is. A button that 409s is a lie
    with a shadow on it.

    `setup_nudge` defaults to "whenever an OPERATOR is looking at an unbranded
    document" -- which is what ``chrome`` means. A customer who followed an
    emailed link must never be told to go and configure the business that sent
    it: they cannot, and it would read as the sender's software talking over
    the sender.
    """
    document_title = title or " ".join(
        part for part in (model.get("kind_label"), model.get("number")) if part)
    if setup_nudge is None:
        setup_nudge = chrome and not (model.get("business") or {})
    nudge = _SETUP_NUDGE if setup_nudge else ""
    body = "".join([
        banner, nudge, before,
        render_html(model),
        after,
        _print_hint_html(pdf),
    ])
    page = {
        "content_type": "text/html; charset=utf-8",
        "body": f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{'<meta name="referrer" content="no-referrer">' if not chrome else ''}
<title>{_esc(document_title)}</title>
<link rel="stylesheet" href="/style">
<style>{document_css(size)}</style>
</head>
<body>
<div class="doc">
{body}
</div>
{'<script src="/nav"></script>' if chrome else ''}
</body>
</html>""",
    }
    if status != 200:
        page["status"] = status
    return page
