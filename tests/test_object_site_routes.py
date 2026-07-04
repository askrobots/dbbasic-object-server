"""Tests for clean-URL site routing."""

import json

import object_site_routes
import object_server

from test_object_server import (
    ANONYMOUS_IDENTITY,
    auth_headers,
    enable_admin_token,
    raw_request,
    request,
    write_records,
    write_source,
)


def enable_site_routes(monkeypatch, tmp_path):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    monkeypatch.setenv(object_server.SITE_ROUTES_ENV, "true")
    return root, data_dir


def test_convention_object_id_mapping():
    assert object_site_routes.convention_object_id("/") == "site_home"
    assert object_site_routes.convention_object_id("/about") == "site_about"
    assert object_site_routes.convention_object_id("/docs/install") == "site_docs_install"
    assert object_site_routes.convention_object_id("/getting-started") == "site_getting_started"
    assert object_site_routes.convention_object_id("/has space") is None
    assert object_site_routes.convention_object_id("/../etc") is None
    assert object_site_routes.convention_object_id("/favicon.ico") is None


def test_pattern_matching_with_uuid_params():
    records = [
        {"pattern": "/articles/{article_id:uuid}", "object_id": "articles_view"},
        {"pattern": "/blog/{slug}", "object_id": "blog_post"},
        {"pattern": "/blog/archive", "object_id": "blog_archive", "priority": 1},
    ]

    uuid_value = "123e4567-e89b-42d3-a456-426614174000"
    assert object_site_routes.match_records(f"/articles/{uuid_value}", records) == (
        "articles_view",
        {"article_id": uuid_value},
    )
    assert object_site_routes.match_records("/articles/not-a-uuid", records) is None
    assert object_site_routes.match_records("/blog/hello-world", records) == (
        "blog_post",
        {"slug": "hello-world"},
    )
    assert object_site_routes.match_records("/blog/archive", records) == (
        "blog_archive",
        {},
    )
    assert object_site_routes.match_records("/nope", records) is None


def test_site_routes_disabled_by_default(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "site" / "about.py", "def GET(request):\n    return {'page': 'about'}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.delenv(object_server.SITE_ROUTES_ENV, raising=False)

    status, _, payload = request("/about")

    assert status == 404
    assert payload == {"status": "error", "error": "Not found"}


def test_convention_routes_serve_objects(tmp_path, monkeypatch):
    root, _ = enable_site_routes(monkeypatch, tmp_path)
    write_source(root / "site" / "home.py", "def GET(request):\n    return {'page': 'home'}\n")
    write_source(root / "site" / "about.py", "def GET(request):\n    return {'page': 'about'}\n")
    write_source(
        root / "site" / "docs" / "install.py",
        "def GET(request):\n    return {'page': 'install', 'v': request.get('v')}\n",
    )

    home_status, _, home = request("/")
    about_status, _, about = request("/about")
    nested_status, _, nested = request("/docs/install", query_string="v=2")

    assert home_status == 200 and home == {"page": "home"}
    assert about_status == 200 and about == {"page": "about"}
    assert nested_status == 200 and nested == {"page": "install", "v": "2"}


def test_route_table_patterns_execute_with_params(tmp_path, monkeypatch):
    root, data_dir = enable_site_routes(monkeypatch, tmp_path)
    write_source(
        root / "articles" / "view.py",
        "def GET(request):\n    return {'article_id': request['article_id']}\n",
    )
    uuid_value = "123e4567-e89b-42d3-a456-426614174000"
    write_records(
        data_dir,
        "site_routes",
        "id\tpattern\tobject_id\tpriority\n"
        "r1\t/articles/{article_id:uuid}\tarticles_view\t10\n",
    )

    status, _, payload = request(f"/articles/{uuid_value}")
    miss_status, _, _ = request("/articles/not-a-uuid")

    assert status == 200
    assert payload == {"article_id": uuid_value}
    assert miss_status == 404


def test_site_404_object_handles_misses(tmp_path, monkeypatch):
    root, _ = enable_site_routes(monkeypatch, tmp_path)
    write_source(
        root / "site" / "404.py",
        "def GET(request):\n"
        "    return {'content_type': 'text/html', 'status_code': 404,\n"
        "            'body': 'missing: ' + request.get('path', '')}\n",
    )

    status, headers, body = raw_request("/no/such/page")

    assert status == 404
    assert b"missing: /no/such/page" in body


def test_form_post_to_routed_page(tmp_path, monkeypatch):
    root, _ = enable_site_routes(monkeypatch, tmp_path)
    write_source(
        root / "site" / "contact.py",
        "def POST(request):\n"
        "    return {'received': request.get('email'), 'user': request['_identity']['user_id']}\n",
    )

    status, _, payload = request(
        "/contact",
        method="POST",
        body=b"email=dan%40example.com",
        headers=[("content-type", "application/x-www-form-urlencoded")],
    )

    assert status == 200
    assert payload == {"received": "dan@example.com", "user": None}


def test_reserved_routes_are_never_shadowed(tmp_path, monkeypatch):
    root, _ = enable_site_routes(monkeypatch, tmp_path)
    write_source(root / "site" / "health.py", "def GET(request):\n    return {'fake': True}\n")
    write_source(root / "site" / "login.py", "def GET(request):\n    return {'fake': True}\n")

    health_status, _, health = request("/health")
    login_status, _, login_body = raw_request("/login")

    assert health_status == 200
    assert health == {"status": "ok"}
    assert login_status == 403
    assert b"Password login is disabled" in login_body


def test_normalize_host_and_resolve_host():
    records = [
        {"id": "h1", "host": "DBBasic.com:443", "prefix": "db", "home_object": "db_start"},
        {"id": "h2", "host": "q9.is", "prefix": "q9"},
        {"id": "bad", "host": "evil.example", "prefix": "Bad-Prefix!"},
    ]

    assert object_site_routes.normalize_host("DBBasic.com:8443") == "dbbasic.com"
    db = object_site_routes.resolve_host("dbbasic.com", records)
    q9 = object_site_routes.resolve_host("Q9.IS:443", records)
    unknown = object_site_routes.resolve_host("other.example", records)
    invalid = object_site_routes.resolve_host("evil.example", records)

    assert db == {"host": "dbbasic.com", "prefix": "db", "home": "db_start", "not_found": "db_404"}
    assert q9["prefix"] == "q9" and q9["home"] == "q9_home" and q9["not_found"] == "q9_404"
    assert unknown["prefix"] == "site" and unknown["home"] == "site_home"
    assert invalid["prefix"] == "site"  # bad prefix records are ignored


def test_multi_domain_conventions_serve_separate_sites(tmp_path, monkeypatch):
    root, data_dir = enable_site_routes(monkeypatch, tmp_path)
    write_source(root / "site" / "home.py", "def GET(request):\n    return {'site': 'default'}\n")
    write_source(root / "db" / "home.py", "def GET(request):\n    return {'site': 'dbbasic'}\n")
    write_source(root / "db" / "pricing.py", "def GET(request):\n    return {'page': 'pricing'}\n")
    write_source(root / "q9" / "home.py", "def GET(request):\n    return {'site': 'q9'}\n")
    write_records(
        data_dir,
        "site_hosts",
        "id\thost\tprefix\thome_object\tnot_found_object\n"
        "h1\tdbbasic.com\tdb\t\t\n"
        "h2\tq9.is\tq9\t\t\n",
    )

    default_status, _, default_home = request("/")
    db_status, _, db_home = request("/", headers=[("host", "dbbasic.com")])
    db_page_status, _, db_page = request("/pricing", headers=[("host", "DBBASIC.COM:443")])
    q9_status, _, q9_home = request("/", headers=[("host", "q9.is")])
    cross_status, _, _ = request("/pricing", headers=[("host", "q9.is")])

    assert default_status == 200 and default_home == {"site": "default"}
    assert db_status == 200 and db_home == {"site": "dbbasic"}
    assert db_page_status == 200 and db_page == {"page": "pricing"}
    assert q9_status == 200 and q9_home == {"site": "q9"}
    assert cross_status == 404  # q9.is has no pricing page; sites do not leak


def test_host_scoped_route_records(tmp_path, monkeypatch):
    root, data_dir = enable_site_routes(monkeypatch, tmp_path)
    write_source(root / "db" / "article.py", "def GET(request):\n    return {'site': 'db', 'slug': request['slug']}\n")
    write_source(root / "q9" / "article.py", "def GET(request):\n    return {'site': 'q9', 'slug': request['slug']}\n")
    write_records(
        data_dir,
        "site_hosts",
        "id\thost\tprefix\nh1\tdbbasic.com\tdb\nh2\tq9.is\tq9\n",
    )
    write_records(
        data_dir,
        "site_routes",
        "id\tpattern\tobject_id\thost\tpriority\n"
        "r1\t/blog/{slug}\tdb_article\tdbbasic.com\t10\n"
        "r2\t/blog/{slug}\tq9_article\tq9.is\t10\n",
    )

    db_status, _, db_payload = request("/blog/hello", headers=[("host", "dbbasic.com")])
    q9_status, _, q9_payload = request("/blog/hello", headers=[("host", "q9.is")])

    assert db_status == 200 and db_payload == {"site": "db", "slug": "hello"}
    assert q9_status == 200 and q9_payload == {"site": "q9", "slug": "hello"}


def test_host_specific_404_with_generic_fallback(tmp_path, monkeypatch):
    root, data_dir = enable_site_routes(monkeypatch, tmp_path)
    write_source(
        root / "db" / "404.py",
        "def GET(request):\n"
        "    return {'content_type': 'text/html', 'status_code': 404, 'body': 'db missing'}\n",
    )
    write_source(
        root / "site" / "404.py",
        "def GET(request):\n"
        "    return {'content_type': 'text/html', 'status_code': 404, 'body': 'generic missing'}\n",
    )
    write_records(
        data_dir,
        "site_hosts",
        "id\thost\tprefix\nh1\tdbbasic.com\tdb\nh2\tq9.is\tq9\n",
    )

    db_status, _, db_body = raw_request("/nope", headers=[("host", "dbbasic.com")])
    q9_status, _, q9_body = raw_request("/nope", headers=[("host", "q9.is")])

    assert db_status == 404 and b"db missing" in db_body
    assert q9_status == 404 and b"generic missing" in q9_body  # q9 has no q9_404; generic serves


def test_site_routes_respect_permission_enforcement(tmp_path, monkeypatch):
    from test_object_server import save_permission_policy

    root, data_dir = enable_site_routes(monkeypatch, tmp_path)
    write_source(root / "site" / "public.py", "def GET(request):\n    return {'ok': True}\n")
    write_source(root / "site" / "secret.py", "def GET(request):\n    return {'ok': True}\n")
    save_permission_policy(
        data_dir,
        {
            "access_mode": "role_based",
            "rules": [
                {
                    "effect": "allow",
                    "principal": "public",
                    "actions": ["execute"],
                    "object_id": "site_public",
                }
            ],
        },
    )
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    monkeypatch.setenv(object_server.PERMISSION_TRUST_HEADERS_ENV, "true")
    enable_admin_token(monkeypatch)

    public_status, _, public_payload = request("/public")
    secret_status, _, secret_payload = request("/secret")

    assert public_status == 200
    assert public_payload == {"ok": True}
    assert secret_status == 403
    assert secret_payload["code"] == "forbidden"
