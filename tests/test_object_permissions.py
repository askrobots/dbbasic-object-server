from datetime import datetime, timezone

import object_permissions as permissions


def test_public_access_allows_read_and_execute_only():
    policy = permissions.PermissionPolicy(access_mode="public")

    read = permissions.check_permission(None, permissions.READ, policy=policy, collection="articles")
    execute = permissions.check_permission(
        permissions.PermissionSubject.anonymous(),
        permissions.EXECUTE,
        policy=policy,
        object_id="site_home",
    )
    update = permissions.check_permission(None, permissions.UPDATE, policy=policy, collection="articles")

    assert read.allowed is True
    assert execute.allowed is True
    assert update.allowed is False
    assert update.reason == "public access is read-only"


def test_registered_access_requires_authenticated_subject():
    policy = permissions.PermissionPolicy(access_mode="registered")

    anonymous = permissions.check_permission(None, permissions.READ, policy=policy, collection="forum")
    signed_in = permissions.check_permission(
        permissions.PermissionSubject(user_id="42"),
        permissions.READ,
        policy=policy,
        collection="forum",
    )

    assert anonymous.allowed is False
    assert anonymous.reason == "registered user required"
    assert anonymous.code == "authentication_required"
    assert anonymous.http_status == 401
    assert signed_in.allowed is True
    assert signed_in.reason == "registered access"


def test_admin_role_allows_all_actions_from_subject_or_policy_assignment():
    policy = permissions.PermissionPolicy(
        access_mode="role_based",
        user_roles={"42": ("admin",)},
    )

    decision = permissions.check_permission(
        permissions.PermissionSubject(user_id="42"),
        permissions.DELETE,
        policy=policy,
        collection="invoices",
    )

    assert decision.allowed is True
    assert decision.reason == "admin role"


def test_old_user_object_owner_convention_still_works_as_fallback():
    owner = permissions.PermissionSubject(user_id="7")
    other_user = permissions.PermissionSubject(user_id="8")

    owner_decision = permissions.check_permission(owner, permissions.SOURCE, object_id="u_7_report")
    other_decision = permissions.check_permission(other_user, permissions.SOURCE, object_id="u_7_report")

    assert owner_decision.allowed is True
    assert owner_decision.reason == "object owner"
    assert other_decision.allowed is False


def test_system_object_fallback_allows_public_read_execute_but_not_source():
    assert permissions.check_permission(None, permissions.READ, object_id="site_home").allowed is True
    assert permissions.check_permission(None, permissions.EXECUTE, object_id="site_home").allowed is True

    source = permissions.check_permission(None, permissions.SOURCE, object_id="site_home")

    assert source.allowed is False


def test_role_rule_with_row_filter_returns_filter_when_no_record_is_supplied():
    policy = permissions.PermissionPolicy(
        access_mode="role_based",
        rules=(
            permissions.PermissionRule.allow(
                "role:sales",
                [permissions.READ],
                collection="contacts",
                row_filter={"owner_id": "$user_id"},
                reason="sales reps only see own contacts",
            ),
        ),
    )

    decision = permissions.check_permission(
        permissions.PermissionSubject(user_id="7", roles=("sales",)),
        permissions.READ,
        policy=policy,
        collection="contacts",
    )

    assert decision.allowed is True
    assert decision.row_filter == {"owner_id": "$user_id"}
    assert decision.reason == "sales reps only see own contacts"


def test_role_rule_with_row_filter_checks_record_when_supplied():
    policy = permissions.PermissionPolicy(
        access_mode="role_based",
        rules=(
            permissions.PermissionRule.allow(
                "role:sales",
                [permissions.READ],
                collection="contacts",
                row_filter={"owner_id": "$user_id"},
            ),
        ),
    )
    subject = permissions.PermissionSubject(user_id="7", roles=("sales",))

    own_contact = permissions.check_permission(
        subject,
        permissions.READ,
        policy=policy,
        collection="contacts",
        record={"owner_id": "7", "name": "Alice"},
    )
    other_contact = permissions.check_permission(
        subject,
        permissions.READ,
        policy=policy,
        collection="contacts",
        record={"owner_id": "8", "name": "Bob"},
    )

    assert own_contact.allowed is True
    assert other_contact.allowed is False


def test_accessible_projects_filter_matches_by_membership():
    policy = permissions.PermissionPolicy(
        access_mode="role_based",
        rules=(
            permissions.PermissionRule.allow(
                "registered",
                [permissions.READ],
                collection="notes",
                row_filter={"project_id": "$accessible_projects"},
            ),
        ),
    )
    granted = permissions.PermissionSubject(user_id="8", project_ids=("p1", "p2"))
    ungranted = permissions.PermissionSubject(user_id="9")
    shared_note = {"id": "n1", "project_id": "p1", "owner_id": "7"}
    other_note = {"id": "n2", "project_id": "p9", "owner_id": "7"}
    unfiled_note = {"id": "n3", "project_id": "", "owner_id": "7"}

    assert permissions.check_permission(
        granted, permissions.READ, policy=policy, collection="notes", record=shared_note
    ).allowed is True
    assert permissions.check_permission(
        granted, permissions.READ, policy=policy, collection="notes", record=other_note
    ).allowed is False
    assert permissions.check_permission(
        granted, permissions.READ, policy=policy, collection="notes", record=unfiled_note
    ).allowed is False
    assert permissions.check_permission(
        ungranted, permissions.READ, policy=policy, collection="notes", record=shared_note
    ).allowed is False


def test_subject_from_dict_reads_project_ids():
    subject = permissions.subject_from_dict(
        {"user_id": "8", "project_ids": ["p1", "p2"], "owned_project_ids": ["p9"]}
    )
    assert subject.project_ids == ("p1", "p2")
    assert subject.owned_project_ids == ("p9",)
    enriched = subject.with_projects(["p3"], ["p4"])
    assert enriched.project_ids == ("p3",)
    assert enriched.owned_project_ids == ("p4",)


def test_writable_projects_filter_matches_write_grants_only():
    policy = permissions.PermissionPolicy(
        access_mode="role_based",
        rules=(
            permissions.PermissionRule.allow(
                "registered",
                [permissions.UPDATE],
                collection="notes",
                row_filter={"project_id": "$writable_projects"},
            ),
        ),
    )
    writer = permissions.PermissionSubject(user_id="8", writable_project_ids=("p1",))
    reader_only = permissions.PermissionSubject(user_id="9", project_ids=("p1",))
    note = {"id": "n1", "project_id": "p1"}

    assert permissions.check_permission(
        writer, permissions.UPDATE, policy=policy, collection="notes", record=note
    ).allowed is True
    assert permissions.check_permission(
        reader_only, permissions.UPDATE, policy=policy, collection="notes", record=note
    ).allowed is False


def test_subject_from_dict_reads_writable_project_ids():
    subject = permissions.subject_from_dict(
        {"user_id": "8", "writable_project_ids": ["p1"]}
    )
    assert subject.writable_project_ids == ("p1",)
    enriched = subject.with_projects(["p3"], ["p4"], ["p5"])
    assert enriched.project_ids == ("p3",)
    assert enriched.owned_project_ids == ("p4",)
    assert enriched.writable_project_ids == ("p5",)


def test_record_matches_filter_public_wrapper_shares_row_filter_semantics():
    """Transition guards reuse this for their ``when`` clauses (see
    object_records._validate_field_transitions), so it must match the same
    $-variable resolution and empty-string posture row filters use."""
    subject = permissions.PermissionSubject(user_id="7")

    assert permissions.record_matches_filter(
        {"owner_id": "7"}, {"owner_id": "$user_id"}, subject
    ) is True
    assert permissions.record_matches_filter(
        {"owner_id": "8"}, {"owner_id": "$user_id"}, subject
    ) is False
    assert permissions.record_matches_filter(
        {"owner_id": ""}, {"owner_id": "$user_id"}, subject
    ) is False
    assert permissions.record_matches_filter(
        {"status": "open"}, {"status": "open"}, subject
    ) is True


def test_owned_projects_filter_gates_grant_writes():
    policy = permissions.PermissionPolicy(
        access_mode="role_based",
        rules=(
            permissions.PermissionRule.allow(
                "registered",
                [permissions.CREATE, permissions.DELETE],
                collection="project_access",
                row_filter={"project_id": "$owned_projects"},
            ),
        ),
    )
    owner = permissions.PermissionSubject(user_id="7", owned_project_ids=("p1",))

    own_grant = {"id": "g1", "project_id": "p1", "user_id": "8"}
    foreign_grant = {"id": "g2", "project_id": "p9", "user_id": "7"}

    assert permissions.check_permission(
        owner, permissions.CREATE, policy=policy, collection="project_access", record=own_grant
    ).allowed is True
    assert permissions.check_permission(
        owner, permissions.CREATE, policy=policy, collection="project_access", record=foreign_grant
    ).allowed is False


def test_customer_employee_account_rule_models_tenant_shared_access():
    policy = permissions.PermissionPolicy(
        access_mode="role_based",
        rules=(
            permissions.PermissionRule.allow(
                "account:customer-acme",
                [permissions.READ],
                collection="invoices",
                row_filter={"customer_account_id": "$account_id"},
                fields=["invoice_id", "status", "total"],
                denied_fields=["internal_notes"],
            ),
        ),
    )
    subject = permissions.PermissionSubject(
        user_id="employee-1",
        account_id="customer-acme",
        roles=("customer_employee",),
    )

    decision = permissions.check_permission(
        subject,
        permissions.READ,
        policy=policy,
        collection="invoices",
        record={"customer_account_id": "customer-acme", "total": 120},
    )

    assert decision.allowed is True
    assert decision.fields == frozenset({"invoice_id", "status", "total"})
    assert decision.denied_fields == frozenset({"internal_notes"})


def test_user_share_rule_allows_specific_user_without_owner_role():
    policy = permissions.PermissionPolicy(
        access_mode="role_based",
        rules=(
            permissions.PermissionRule.allow(
                "user:99",
                [permissions.READ, permissions.UPDATE],
                object_id="u_7_shared_calculator",
                reason="shared by owner",
            ),
        ),
    )

    decision = permissions.check_permission(
        permissions.PermissionSubject(user_id="99"),
        permissions.UPDATE,
        policy=policy,
        object_id="u_7_shared_calculator",
    )

    assert decision.allowed is True
    assert decision.reason == "shared by owner"


def test_explicit_deny_overrides_role_allow():
    policy = permissions.PermissionPolicy(
        access_mode="role_based",
        rules=(
            permissions.PermissionRule.deny(
                "role:support",
                [permissions.DELETE],
                collection="tickets",
                reason="support cannot delete tickets",
            ),
            permissions.PermissionRule.allow(
                "role:support",
                [permissions.READ, permissions.UPDATE, permissions.DELETE],
                collection="tickets",
            ),
        ),
    )

    decision = permissions.check_permission(
        permissions.PermissionSubject(user_id="3", roles=("support",)),
        permissions.DELETE,
        policy=policy,
        collection="tickets",
    )

    assert decision.allowed is False
    assert decision.reason == "support cannot delete tickets"


def test_subscription_mode_allows_subscribed_users_to_read():
    policy = permissions.PermissionPolicy(access_mode="subscription")
    subscriber = permissions.PermissionSubject(user_id="42", subscriptions=("pro",))

    read = permissions.check_permission(subscriber, permissions.READ, policy=policy, collection="reports")
    write = permissions.check_permission(
        subscriber,
        permissions.UPDATE,
        policy=policy,
        collection="reports",
    )

    assert read.allowed is True
    assert write.allowed is False
    assert write.reason == "subscription access is read-only"
    assert write.http_status == 403


def test_subscription_mode_returns_payment_required_for_missing_entitlement():
    policy = permissions.PermissionPolicy(access_mode="subscription")

    decision = permissions.check_permission(
        permissions.PermissionSubject(user_id="42"),
        permissions.READ,
        policy=policy,
        collection="reports",
    )

    assert decision.allowed is False
    assert decision.code == "payment_required"
    assert decision.http_status == 402


def test_subscription_rule_can_target_a_specific_plan():
    policy = permissions.PermissionPolicy(
        access_mode="role_based",
        rules=(
            permissions.PermissionRule.allow(
                "subscription:pro",
                [permissions.READ],
                collection="premium_reports",
                reason="active pro subscription",
            ),
        ),
    )

    free_user = permissions.PermissionSubject(user_id="1", subscriptions=("free",))
    pro_user = permissions.PermissionSubject(user_id="2", subscriptions=("pro",))

    assert (
        permissions.check_permission(
            free_user,
            permissions.READ,
            policy=policy,
            collection="premium_reports",
        ).allowed
        is False
    )

    decision = permissions.check_permission(
        pro_user,
        permissions.READ,
        policy=policy,
        collection="premium_reports",
    )

    assert decision.allowed is True
    assert decision.reason == "active pro subscription"


def test_time_boxed_rule_models_temporary_pay_per_view_access():
    policy = permissions.PermissionPolicy(
        access_mode="role_based",
        rules=(
            permissions.PermissionRule.allow(
                "user:42",
                [permissions.READ],
                object_id="reports_market_snapshot",
                valid_from="2026-06-01T00:00:00Z",
                expires_at="2026-07-01T00:00:00Z",
                reason="temporary paid access",
            ),
        ),
    )
    subject = permissions.PermissionSubject(user_id="42")

    active = permissions.check_permission(
        subject,
        permissions.READ,
        policy=policy,
        object_id="reports_market_snapshot",
        now=datetime(2026, 6, 15, tzinfo=timezone.utc),
    )
    expired = permissions.check_permission(
        subject,
        permissions.READ,
        policy=policy,
        object_id="reports_market_snapshot",
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )

    assert active.allowed is True
    assert active.reason == "temporary paid access"
    assert expired.allowed is False


def test_policy_serialization_round_trips_json_shape():
    payload = {
        "access_mode": "role_based",
        "roles": {"sales": {"label": "Sales"}},
        "user_roles": {"7": ["sales"]},
        "rules": [
            {
                "effect": "allow",
                "principal": "role:sales",
                "actions": ["read"],
                "collection": "contacts",
                "row_filter": {"owner_id": "$user_id"},
                "fields": ["email", "name"],
                "denied_fields": ["internal_notes"],
                "reason": "sales reps only see own contacts",
            }
        ],
        "admin_roles": ["admin"],
    }

    policy = permissions.policy_from_dict(payload)
    serialized = permissions.policy_to_dict(policy)

    assert serialized == {
        "access_mode": "role_based",
        "roles": {"sales": {"label": "Sales"}},
        "user_roles": {"7": ["sales"]},
        "rules": [
            {
                "effect": "allow",
                "actions": ["read"],
                "principal": "role:sales",
                "collection": "contacts",
                "row_filter": {"owner_id": "$user_id"},
                "fields": ["email", "name"],
                "denied_fields": ["internal_notes"],
                "reason": "sales reps only see own contacts",
            }
        ],
        "admin_roles": ["admin"],
    }


def test_subject_and_decision_serialization_match_scroll_shape():
    subject = permissions.subject_from_dict(
        {
            "user_id": 42,
            "account_id": "customer-acme",
            "roles": ["customer_employee"],
            "subscriptions": ["pro"],
        }
    )
    decision = permissions.PermissionDecision.allow(
        "active pro subscription",
        row_filter={"customer_account_id": "$account_id"},
        fields=["invoice_id", "total"],
        denied_fields=["internal_notes"],
    )

    assert subject.user_id == "42"
    assert subject.account_id == "customer-acme"
    assert subject.roles == ("customer_employee",)
    assert permissions.decision_to_dict(decision) == {
        "allowed": True,
        "reason": "active pro subscription",
        "code": "allowed",
        "http_status": 200,
        "row_filter": {"customer_account_id": "$account_id"},
        "fields": ["invoice_id", "total"],
        "denied_fields": ["internal_notes"],
    }


def test_policy_from_dict_rejects_unknown_access_mode():
    try:
        permissions.policy_from_dict({"access_mode": "unknown"})
    except ValueError as exc:
        assert "Permission access_mode must be one of:" in str(exc)
    else:
        raise AssertionError("Expected unknown access mode to fail")
