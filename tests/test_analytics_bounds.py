"""Analytics that measures visitors, and a log that stays bounded.

Both of these were found the same way: a 961MB demo box went to 675MB
resident and started swapping, and the "Top paths (visitors)" report
turned out to be 45% one-hit rows made of vulnerability scanners and the
operator's own deploy calls. Neither failure was a bug in the code that
was written -- both were things nobody had told the system.
"""

import object_analytics


# --- whose traffic is this ---------------------------------------------------

def test_the_operators_own_automation_is_not_a_visitor():
    """A deploy call and a package install are the observer, not the
    observed; counting them made the top-paths report describe us."""
    record = object_analytics.build_page_view(
        path="/packages/app-shop/install", method="POST", status=200,
        ip="203.0.113.9", headers={}, owners=frozenset(), is_operator=True)
    assert record["is_owner"] == "true"


def test_an_owner_ip_still_counts_as_the_owner():
    record = object_analytics.build_page_view(
        path="/shop", method="GET", status=200,
        ip="203.0.113.9", headers={}, owners=frozenset({"203.0.113.9"}))
    assert record["is_owner"] == "true"


def test_an_ordinary_visitor_is_a_visitor():
    record = object_analytics.build_page_view(
        path="/shop", method="GET", status=200,
        ip="198.51.100.4", headers={}, owners=frozenset())
    assert record["is_owner"] == "false"


def test_a_signed_in_member_is_still_real_traffic():
    """The line is between somebody USING the site and somebody OPERATING
    it. Which pages members actually use is among the most useful things
    this collection knows, so a member is never flagged away."""
    record = object_analytics.build_page_view(
        path="/invoices", method="GET", status=200, ip="198.51.100.4",
        headers={"cookie": "dbbasic_session=abc"}, owners=frozenset(),
        user_id="dana")
    assert record["is_owner"] == "false"
    assert record["user_id"] == "dana"


# --- how much of it we keep ----------------------------------------------------

def test_the_row_cap_reads_from_the_environment():
    assert object_analytics.max_rows({"DBBASIC_ANALYTICS_MAX_ROWS": "5000"}) == 5000
    assert object_analytics.max_rows({}) == object_analytics.DEFAULT_MAX_ROWS
    # 0 is a real answer meaning "no cap", not a missing setting.
    assert object_analytics.max_rows({"DBBASIC_ANALYTICS_MAX_ROWS": "0"}) == 0
    assert object_analytics.max_rows(
        {"DBBASIC_ANALYTICS_MAX_ROWS": "not a number"}) == object_analytics.DEFAULT_MAX_ROWS


def test_retention_days_still_defaults_sanely():
    assert object_analytics.retention_days({"DBBASIC_ANALYTICS_RETENTION_DAYS": "7"}) == 7
    assert object_analytics.retention_days({}) == object_analytics.DEFAULT_RETENTION_DAYS
