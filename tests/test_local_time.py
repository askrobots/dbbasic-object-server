"""Timestamps: stored in UTC, read in the viewer's own zone.

Every timestamp this server STORES is UTC, which is correct and is not
negotiable — a stamp without a zone is a bug waiting for the clocks to
change. Every timestamp a person READS should be in their own zone, which
is a rendering question.

The generative renderer already got this right by accident of being
client-side (`new Date(iso)` parses UTC, `toLocaleString` prints local).
Server-rendered pages did not, because a Python f-string has no idea who
is reading it. So the convention is one element — `<time datetime="...">`
— converted by the shared `/nav` script that every page already loads.

THREE THINGS THIS FILE PINS, each of which is a decision rather than an
implementation detail:

1. The browser's zone is the DEFAULT and is NEVER TRANSMITTED. It is
   available and deliberately used only in the browser: a timezone is a
   real fingerprinting signal, so posting it back to store would be
   collecting an identifying attribute nobody asked us to collect.
2. A DATE is not a DATETIME. Converting a date across zones actively
   breaks it — an invoice dated the 1st is dated the 1st in Tokyo.
3. Evidence keeps its canonical UTC form on screen. A notary attestation
   that changed shape with who was reading it would be a worse
   attestation.
"""

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"

NAV = PACKAGES / "app-theme" / "objects" / "site" / "nav.py"


def nav_js():
    return NAV.read_text()


def time_block():
    """Just the local-time IIFE. The privacy claim is about THIS block --
    scanning the whole script would catch the unrelated logout POST and
    turn a real assertion into a false one."""
    source = NAV.read_text()
    start = source.index("// === local time")
    return source[start:]


# --- the shared helper ----------------------------------------------------------

def test_the_shared_script_localises_time_elements():
    """One convention, applied by the script every page already loads,
    rather than 34 pages each remembering to convert."""
    js = nav_js()
    assert "window.dbbasicTime" in js
    assert 'querySelectorAll("time[datetime]")' in js
    assert "toLocaleString" in js


def test_the_browser_zone_is_never_sent_to_the_server():
    """THE privacy decision. A timezone is one of the higher-entropy bits
    in a browser fingerprint, so reading it to format text is free and
    posting it back to store would be collecting an identifying attribute
    nobody asked us to collect -- which /privacy is a fold over."""
    js = time_block()

    # It is read only inside the reporting helper, never assembled into a
    # request body or query string.
    assert "resolvedOptions().timeZone" in js
    for smell in ('body: JSON.stringify({timezone',
                  '"timezone="', "timezone=' +", 'tz=" +',
                  'method: "PUT"', 'method: "POST"'):
        assert smell not in js, smell

    # The only network call this block makes is a READ of the caller's
    # own prefs.
    prefs_calls = js.count('fetch("/prefs"')
    assert prefs_calls == 1
    assert 'fetch("/prefs", {credentials: "same-origin"})' in js


def test_a_stored_preference_overrides_the_browser():
    """The real case the browser gets wrong: a business whose books are
    kept in one zone regardless of where the person reading them sits."""
    js = nav_js()
    assert '"display.timezone"' in js
    assert "o.timeZone = zone" in js


def test_an_unparseable_stamp_is_left_showing_utc_rather_than_blanked():
    """Honest beats empty. A page that blanks a timestamp it could not
    parse has destroyed the only information it had."""
    js = nav_js()
    assert "if (!out) return;" in js
    assert "leave UTC showing" in js


def test_a_bad_timezone_preference_cannot_blank_the_page():
    """A typo in a pref must degrade to the browser's zone, not throw
    inside a render loop and leave every timestamp unconverted."""
    js = nav_js()
    assert "catch (e) { return d.toLocaleString(); }" in js


def test_a_bare_stamp_with_no_zone_marker_is_treated_as_utc():
    """This server's own contract: it writes UTC. A stamp that reached a
    page without a Z would otherwise be parsed as LOCAL by the browser,
    which silently shifts every value by the reader's offset."""
    js = nav_js()
    assert 'if (!/[Zz]|[+-]\\d{2}:?\\d{2}$/.test(text)) text += "Z";' in js


def test_content_rendered_after_load_is_converted_too():
    """The generative list, a form panel and a realtime push all arrive
    after DOMContentLoaded. Without this a page would convert only what
    the server sent and silently miss everything the client added."""
    assert "MutationObserver" in nav_js()


# --- the pages ------------------------------------------------------------------

def test_server_rendered_datetimes_use_the_time_element():
    """The sweep, pinned. A page that prints a bare ISO stamp is showing
    UTC to somebody who does not live there."""
    for path, field in (
        (PACKAGES / "app-notary" / "objects" / "site" / "notary.py",
         "first_seen_at"),
        (PACKAGES / "app-integrity" / "objects" / "site" / "ledger_integrity.py",
         "taken_at"),
    ):
        source = path.read_text()
        assert f'<time datetime="{{_esc(' in source, path.name
        assert field in source


def test_a_date_is_not_converted_because_a_date_is_not_an_instant():
    """An invoice dated the 1st is dated the 1st in Tokyo. Shifting a
    date by a timezone offset turns it into the 31st for half the world,
    which is a data corruption dressed as a courtesy."""
    sheet = (PACKAGES / "app-receiving" / "objects" / "site"
             / "receiving_sheet.py").read_text()
    assert "received_on" in sheet
    assert "<time datetime" not in sheet


def test_the_notary_keeps_its_canonical_utc_on_screen():
    """An attestation is evidence, and evidence should not change shape
    with who is reading it. Local time for the human, UTC for the record,
    both visible."""
    notary = (PACKAGES / "app-notary" / "objects" / "site" / "notary.py").read_text()
    assert "UTC" in notary
    assert "canonical" in notary
    assert "evidence should not" in notary


# --- the generative form: display, load AND save --------------------------------
#
# Found by creating a note and reading its Created field: "2026-07-27
# 09:39" for something made at 4:39am. The list renderer had been right
# all along (relDate is client-side), so an earlier claim that "the
# generative layer already handles this" was true of lists and false of
# detail and edit -- which is why it needed checking rather than asserting.

FORM = PACKAGES / "app-theme" / "objects" / "site" / "form.py"


def test_a_read_only_datetime_is_shown_in_the_readers_zone():
    """It sliced the ISO string, which shows UTC digits to somebody who
    does not live there."""
    js = FORM.read_text()
    assert "window.dbbasicTime && window.dbbasicTime.format(v)" in js
    # and degrades to the old behaviour rather than blanking
    assert 'local || v.slice(0, 16).replace("T", " ")' in js


def test_a_datetime_input_is_fed_local_and_answers_in_utc():
    """THE data-corruption fix. <input type="datetime-local"> is local by
    definition: it was being fed a UTC slice, so it displayed the wrong
    hour AND handed that wall-clock back to be stored as UTC. Every edit
    shifted the value by the reader's offset, compounding each round trip.
    Nine editable datetime fields across seven collections were reachable
    through this one control."""
    js = FORM.read_text()
    assert "function utcToLocalInput" in js
    assert "function localInputToUtc" in js
    assert "esc(utcToLocalInput(v))" in js          # load: UTC -> local
    assert "localInputToUtc(el.value)" in js        # save: local -> UTC
    assert "d.toISOString()" in js
    # The old raw slice must not survive on either path.
    assert "value=\"' + esc(v.slice(0, 16))" not in js


def test_the_round_trip_is_lossless_in_principle():
    """utcToLocalInput and localInputToUtc are inverses: the first drops
    the offset for the control, the second puts it back. Pinned as a
    property because a one-way conversion is exactly how the original bug
    happened."""
    js = FORM.read_text()
    load = js.split("function utcToLocalInput")[1].split("function localInputToUtc")[0]
    save = js.split("function localInputToUtc")[1].split("\n  }")[0]
    # Load builds a zone-less wall-clock string from local getters...
    assert "getHours()" in load and "getMinutes()" in load
    assert "toISOString" not in load
    # ...and save parses that back as local and emits UTC.
    assert "new Date(String(v))" in save
    assert "toISOString" in save


def test_editable_datetime_fields_really_exist_so_this_matters():
    """Not hypothetical: these are in default forms today, reachable
    through the one generic control."""
    import json as _json
    import glob as _glob
    found = []
    for path in _glob.glob(str(PACKAGES / "*" / "schemas" / "*.json")):
        schema = _json.loads(pathlib.Path(path).read_text())
        in_forms = set()
        for spec in (schema.get("forms") or {}).values():
            in_forms.update(spec.get("fields") or [])
        for field in schema["fields"]:
            if (field.get("type") in ("datetime", "timestamp")
                    and not field.get("read_only")
                    and field["name"] in in_forms):
                found.append((schema["name"], field["name"]))
    assert len(found) >= 5, found
    assert ("events", "starts_at") in found


# --- the picker -----------------------------------------------------------------
#
# display.timezone was writable over PUT /prefs/... from the moment the
# formatter shipped, and had nowhere a person would ever find it -- which
# is the same as not existing.

APPEARANCE = PACKAGES / "app-theme" / "objects" / "site" / "appearance.py"


def test_the_timezone_pref_has_a_place_a_person_can_find_it():
    page = APPEARANCE.read_text()
    assert 'id="tz"' in page
    assert "/prefs/display.timezone" in page
    assert 'method: "PUT"' in page


def test_the_default_option_is_the_browser_not_a_named_zone():
    """Blank means 'use the browser', which stays the default because it
    is right more often and requires nobody to decide anything."""
    page = APPEARANCE.read_text()
    assert '<option value="">Whatever my browser says (recommended)</option>' in page


def test_the_page_says_the_zone_is_not_transmitted():
    """The privacy property is only worth having if a user is told about
    it -- an undisclosed protection is indistinguishable from none."""
    page = APPEARANCE.read_text()
    assert "Nothing is sent to the server to work that out" in page
    assert "unless you choose one below" in page


def test_the_picker_writes_the_documented_pref_contract():
    """PUT /prefs/{key} takes {"value": "<string>"} -- a mismatch here
    would fail silently in the browser and look like the pref not
    sticking."""
    page = APPEARANCE.read_text()
    assert 'body: JSON.stringify({value: sel.value})' in page
    assert 'credentials: "same-origin"' in page


def test_the_zone_list_degrades_rather_than_bundling_a_database():
    """Intl.supportedValuesOf where it exists, a short explicit list where
    it does not. Shipping 400 names to render a dropdown nobody scrolls is
    not worth the bytes, and an exotic zone can still be PUT directly."""
    page = APPEARANCE.read_text()
    assert 'Intl.supportedValuesOf("timeZone")' in page
    assert "catch (e)" in page
    assert '"UTC"' in page


def test_the_public_invoice_portal_shows_a_customers_own_time():
    """The portal is public and a customer may be on another continent;
    paid_at is a datetime, so a raw stamp put UTC on somebody's receipt."""
    portal = (PACKAGES / "app-invoices" / "objects" / "site"
              / "invoice_portal.py").read_text()
    assert '<time datetime="{_esc(paid_at)}">' in portal
