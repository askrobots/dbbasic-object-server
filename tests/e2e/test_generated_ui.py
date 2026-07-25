"""Browser end-to-end smoke over the generated-UI pipeline: schema ->
seeded `views` record -> site_view_render -> window.dbbasicList /
window.dbbasicForm. See tests/e2e/conftest.py for the server + user
fixtures every test here drives.

Kept to four tests, each proving something the sixteen bespoke pages this
pipeline replaced used to require writing separately:

  1. the real /login form (not a shortcut) grants a browser session.
  2. /notes: the Add button opens a form built FROM THE SCHEMA, and
     saving it updates the list live (no reload) -- the full generated
     create path, end to end.
  3. /tasks: the same create-through-generated-form path on a second,
     differently-shaped collection (tasks render as a Kanban board, not
     a row list) -- proof the pipeline is shared, not notes-specific.
  4. permissions: owner_scoped + row-filtered reads hold up when driven
     from a real signed-in browser, not just asserted at the API layer.

Every wait below is an `expect(...)` (Playwright's own auto-retrying
assertion) or a `get_by_role`/`get_by_text` locator that itself waits for
the element to appear -- no bare sleep(), since the thing actually being
proven (the websocket-pushed live list refresh) is exactly the kind of
timing a sleep would either flake on or silently paper over.
"""

from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import BrowserContext, Page, expect

pytestmark = pytest.mark.e2e

# The live-list-refresh assertions cross a real POST -> websocket broadcast
# -> dbbasicSubscribe -> re-fetch round trip; a little more headroom than
# Playwright's 5s default keeps this from flaking under CI/CPU contention
# without hiding a genuinely broken push (a real failure still times out).
LIVE_UPDATE_TIMEOUT_MS = 10_000


def _login(page: Page, base_url: str, email: str, password: str, next_path: str = "/") -> None:
    """Drive the real /login page (object_server._handle_login) exactly
    like a person would: GET the form, fill it in, submit, follow the
    redirect. The hidden `next` field (pre-filled from the query string
    on GET, see _send_login_page) is how the handler decides where to
    land -- http_api_contract.LOGIN_PATH POSTs back to itself and 302s to
    `next` on success.
    """
    page.goto(f"{base_url}/login?next={next_path}")
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Sign in").click()
    expect(page).to_have_url(f"{base_url}{next_path}")


def test_login_through_the_browser_lands_on_next_and_sets_session_cookie(
    e2e_server: dict, page: Page
) -> None:
    base_url = e2e_server["base_url"]
    alice = e2e_server["users"]["alice"]

    _login(page, base_url, alice["email"], alice["password"], next_path="/notes")

    cookie_names = {cookie["name"] for cookie in page.context.cookies()}
    assert "dbbasic_session" in cookie_names

    # Landed on a real generated page, not an error page or a login
    # bounce -- the seeded `views` record's title lands in #viewtitle
    # once the page's own script fetches it (see view_render.py's GET()
    # and _SCRIPT).
    expect(page.locator("#viewtitle")).to_have_text("Notes")


def test_notes_add_button_renders_schema_form_and_new_note_appears_live(
    e2e_server: dict, page: Page
) -> None:
    base_url = e2e_server["base_url"]
    alice = e2e_server["users"]["alice"]
    _login(page, base_url, alice["email"], alice["password"], next_path="/notes")

    add_button = page.get_by_role("button", name="+ New Note")
    expect(add_button).to_be_visible()
    add_button.click()

    # The field that renders is FROM THE SCHEMA
    # (packages/app-notes/schemas/notes.json): `content` is a required
    # textarea, not hand-authored markup -- see form.py's control().
    content_field = page.locator('textarea[name="content"]')
    expect(content_field).to_be_visible()

    note_text = f"e2e note {uuid.uuid4()}"
    content_field.fill(note_text)
    page.get_by_role("button", name="Save", exact=True).click()

    # No manual reload: window.dbbasicList re-renders over the websocket
    # push (dbbasicSubscribe, wired in app-theme's nav.py) once the POST
    # that created the record lands.
    expect(page.get_by_text(note_text)).to_be_visible(timeout=LIVE_UPDATE_TIMEOUT_MS)


def test_tasks_add_button_renders_schema_form_and_new_task_appears_live(
    e2e_server: dict, page: Page
) -> None:
    base_url = e2e_server["base_url"]
    alice = e2e_server["users"]["alice"]
    _login(page, base_url, alice["email"], alice["password"], next_path="/tasks")

    add_button = page.get_by_role("button", name="+ New Task")
    expect(add_button).to_be_visible()
    add_button.click()

    # tasks.json's schema-driven form -- `title` is a required text
    # input. Same /form generator as notes' textarea, and the same
    # Add-button/inline-panel chrome from view_render.py's `list` block
    # -- proof one pipeline draws both collections with zero extra page
    # code, even though tasks render as a Kanban board (views.list_mode
    # == "board") rather than notes' plain row list.
    title_field = page.locator('input[name="title"]')
    expect(title_field).to_be_visible()

    task_title = f"e2e task {uuid.uuid4()}"
    title_field.fill(task_title)
    page.get_by_role("button", name="Save", exact=True).click()

    expect(page.get_by_text(task_title)).to_be_visible(timeout=LIVE_UPDATE_TIMEOUT_MS)


def test_owner_scoped_notes_are_invisible_to_a_second_signed_in_user(
    e2e_server: dict, new_context
) -> None:
    base_url = e2e_server["base_url"]
    alice = e2e_server["users"]["alice"]
    bob = e2e_server["users"]["bob"]

    # Two independent browser contexts (separate cookie jars) rather than
    # two sequential logins in one context/page -- this is really two
    # different people, and reusing one page could hide a cookie-scoping
    # bug behind app-level state cleanup.
    alice_context: BrowserContext = new_context()
    alice_page = alice_context.new_page()
    _login(alice_page, base_url, alice["email"], alice["password"], next_path="/notes")

    private_note = f"e2e private note {uuid.uuid4()}"
    alice_page.get_by_role("button", name="+ New Note").click()
    alice_page.locator('textarea[name="content"]').fill(private_note)
    alice_page.get_by_role("button", name="Save", exact=True).click()
    # Confirm it actually saved (and is visible to its owner) before
    # checking who else can see it.
    expect(alice_page.get_by_text(private_note)).to_be_visible(timeout=LIVE_UPDATE_TIMEOUT_MS)
    alice_context.close()

    bob_context: BrowserContext = new_context()
    bob_page = bob_context.new_page()
    _login(bob_page, base_url, bob["email"], bob["password"], next_path="/notes")

    # Bob is a real, freshly signed-in user with zero notes of his own.
    # The `list` block asked for VIEWER_ID-scoped rows (owner_scoped:
    # true), but what actually enforces it is the server-side row_filter
    # {"owner_id": "$user_id"} in packages/app-notes/permissions/
    # rules.json -- this is checking that gate, not the client's ask.
    expect(bob_page.get_by_text("Nothing yet.")).to_be_visible()
    expect(bob_page.get_by_text(private_note)).to_have_count(0)
    bob_context.close()
