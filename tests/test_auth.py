"""Accounts, roles and who can reach what.

These run without an API key like the rest of the suite: every route checked
here is refused before it would ever reach Gemini, and the two that are not
(the console and dashboard pages) are static files.
"""

import pytest


@pytest.fixture
def auth(srv):
    """The auth module, wired to this test's temp database."""
    import auth as auth_module
    # srv's import already pointed auth at the temp store via configure().
    return auth_module


@pytest.fixture
def client(srv, auth):
    """A Flask test client with cookies, and three accounts to sign in as."""
    auth.harden(srv.app, local_only=True)
    srv.app.config["TESTING"] = True

    auth.create_user("priya", "agent-password", "agent")
    auth.create_user("ravi", "lead-password", "lead")
    auth.create_user("root", "admin-password", "admin")

    return srv.app.test_client()


def sign_in(client, username, password):
    return client.post("/api/login",
                       json={"username": username, "password": password})


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------
def test_roles_are_a_hierarchy_not_a_set(auth):
    """Each role must imply every lesser one, or a check for "lead or better"
    would silently exclude admins."""
    assert auth.RANK["admin"] > auth.RANK["lead"] > auth.RANK["agent"]


def test_user_reports_what_its_role_can_reach(auth, client):
    row = auth.find_user("ravi")
    lead = auth.User(row)

    assert lead.at_least("agent") and lead.at_least("lead")
    assert not lead.at_least("admin")

    rights = lead.as_dict()
    assert rights["can_see_dashboard"] is True
    assert rights["can_allow_writes"] is True
    assert rights["can_export"] is False


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------
def test_password_is_hashed_never_stored(auth, client):
    row = auth.find_user("priya")
    assert "agent-password" not in row["password_hash"]
    assert len(row["password_hash"]) > 40


@pytest.mark.parametrize("username, password, role, why", [
    ("nobody", "short", "agent", "at least 8"),
    ("nobody", "long-enough", "wizard", "Role must be"),
    ("priya", "long-enough", "agent", "already exists"),
    ("", "long-enough", "agent", "username is required"),
])
def test_create_user_rejects_bad_input(auth, client, username, password, role, why):
    with pytest.raises(ValueError, match=why):
        auth.create_user(username, password, role)


def test_usernames_are_case_insensitive(auth, client):
    assert auth.find_user("PRIYA")["username"] == "priya"


# --------------------------------------------------------------------------
# Signing in
# --------------------------------------------------------------------------
def test_correct_password_signs_in(client):
    res = sign_in(client, "priya", "agent-password")
    assert res.status_code == 200
    assert res.get_json()["user"]["role"] == "agent"


def test_wrong_password_is_refused(client):
    res = sign_in(client, "priya", "not-the-password")
    assert res.status_code == 401


def test_unknown_user_is_indistinguishable_from_a_wrong_password(client):
    """Different messages here would confirm which usernames exist."""
    missing = sign_in(client, "ghost", "whatever-here").get_json()["error"]
    wrong = sign_in(client, "priya", "not-the-password").get_json()["error"]
    assert missing == wrong


def test_a_deactivated_account_cannot_sign_in(auth, client):
    auth.set_active("priya", False)
    assert sign_in(client, "priya", "agent-password").status_code == 401


def test_repeated_failures_lock_the_account(auth, client):
    for _ in range(auth.LOCKOUT_THRESHOLD):
        sign_in(client, "priya", "wrong-password")

    # Even the RIGHT password is refused while the lock holds.
    res = sign_in(client, "priya", "agent-password")
    assert res.status_code == 401
    assert "failed attempts" in res.get_json()["error"]


def test_changing_the_password_clears_a_lock(auth, client):
    for _ in range(auth.LOCKOUT_THRESHOLD):
        sign_in(client, "priya", "wrong-password")

    auth.set_password("priya", "a-brand-new-password")
    assert sign_in(client, "priya", "a-brand-new-password").status_code == 200


def test_signing_out_ends_the_session(client):
    sign_in(client, "ravi", "lead-password")
    assert client.get("/api/stats").status_code == 200

    client.post("/api/logout")
    assert client.get("/api/stats").status_code == 401


# --------------------------------------------------------------------------
# What each role can reach
# --------------------------------------------------------------------------
ANONYMOUS_MUST_NOT_REACH = [
    "/api/stats", "/api/cases", "/api/gaps", "/api/state",
    "/api/faqs", "/api/performance", "/api/export.json", "/api/cases.csv",
]


@pytest.mark.parametrize("route", ANONYMOUS_MUST_NOT_REACH)
def test_signed_out_reaches_nothing(client, route):
    res = client.get(route)
    assert res.status_code == 401, f"{route} was readable signed out"
    assert res.get_json()["auth"] == "required"


def test_health_stays_public_for_probes(client):
    """A container probe cannot sign in, so this one must answer."""
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.get_json()["ok"] is True


def test_health_hides_configuration_from_anonymous_callers(client):
    assert "key_file" not in client.get("/api/health").get_json()

    sign_in(client, "priya", "agent-password")
    assert "key_file" in client.get("/api/health").get_json()


def test_agent_cannot_reach_the_dashboard_data(client):
    sign_in(client, "priya", "agent-password")

    assert client.get("/api/state").status_code == 200      # its own console
    assert client.get("/api/stats").status_code == 403      # not the dashboard
    assert client.get("/api/cases").status_code == 403


def test_agent_cannot_arm_the_write_tools(srv, client):
    """Deciding a refund may actually be issued is a lead's call."""
    sign_in(client, "priya", "agent-password")

    assert client.post("/api/allow-writes", json={"allow": True}).status_code == 403
    # Refused before the handler ran -- the gate is still shut.
    assert srv.session.allow_writes is False


def test_lead_reaches_the_dashboard_but_not_the_exports(client):
    sign_in(client, "ravi", "lead-password")

    assert client.get("/api/stats").status_code == 200
    assert client.post("/api/allow-writes", json={"allow": True}).status_code == 200
    assert client.get("/api/export.json").status_code == 403
    assert client.get("/api/cases.csv").status_code == 403


def test_admin_reaches_the_exports(client):
    sign_in(client, "root", "admin-password")

    assert client.get("/api/export.json").status_code == 200
    assert client.get("/api/cases.csv").status_code == 200


def test_pages_redirect_a_signed_out_visitor_to_the_login(client):
    res = client.get("/dashboard")
    assert res.status_code == 302
    assert "/login" in res.headers["Location"]


def test_agent_opening_the_dashboard_page_is_sent_to_denied_not_login(client):
    """Bouncing them to a login they are already past would be a loop."""
    sign_in(client, "priya", "agent-password")
    res = client.get("/dashboard")
    assert res.status_code == 302
    assert res.headers["Location"].endswith("/denied")


# --------------------------------------------------------------------------
# Case ownership
# --------------------------------------------------------------------------
def test_an_agent_cannot_read_another_agents_case(srv, auth, client, case):
    srv.save_case(case(id="SC-2001", owner="someone-else"))

    sign_in(client, "priya", "agent-password")
    res = client.get("/api/cases/SC-2001")

    # 404 rather than 403: "exists but not yours" is still a disclosure.
    assert res.status_code == 404


def test_an_agent_can_read_their_own_case(srv, client, case):
    srv.save_case(case(id="SC-2002", owner="priya"))

    sign_in(client, "priya", "agent-password")
    assert client.get("/api/cases/SC-2002").status_code == 200


def test_an_unowned_case_is_claimable(srv, client, case):
    """Cases saved before accounts existed belong to nobody. If an agent
    could not touch them they would be stranded forever."""
    srv.save_case(case(id="SC-2003", owner=None))

    sign_in(client, "priya", "agent-password")
    assert client.get("/api/cases/SC-2003").status_code == 200


def test_a_lead_reads_every_case(srv, client, case):
    srv.save_case(case(id="SC-2004", owner="someone-else"))

    sign_in(client, "ravi", "lead-password")
    assert client.get("/api/cases/SC-2004").status_code == 200


def test_a_new_case_records_who_opened_it(srv, client, case):
    """A case has to carry an owner, or ownership checks have nothing to
    check and every case is readable by every agent."""
    srv.save_case(case(id="SC-2005", owner="priya"))

    stored = next(c for c in srv.load_cases() if c["id"] == "SC-2005")
    assert stored["owner"] == "priya"


def test_persist_outside_a_request_leaves_the_owner_unset(srv):
    """The CLI and the tests call persist() with no signed-in user. That has
    to be survivable rather than a crash."""
    srv.session.reset()
    srv.session.state.add_message("customer", "my recharge failed")

    record = srv.session.persist()
    assert record["owner"] is None


# --------------------------------------------------------------------------
# Session cookie
# --------------------------------------------------------------------------
def test_the_signing_key_survives_a_restart(auth, client):
    """Regenerated per boot, it would sign everybody out on every restart."""
    assert auth.secret_key() == auth.secret_key()


def test_session_cookie_is_locked_down(srv, auth, client):
    config = srv.app.config
    assert config["SESSION_COOKIE_HTTPONLY"] is True
    # Lax is what stops a cross-site POST carrying the cookie.
    assert config["SESSION_COOKIE_SAMESITE"] == "Lax"


def test_seeding_only_happens_when_there_are_no_accounts(auth, client):
    assert auth.user_count() == 3
    assert auth.ensure_seed_admin() is None


# --------------------------------------------------------------------------
# Where signing in lands you
# --------------------------------------------------------------------------
def test_agent_bounced_from_the_dashboard_lands_on_the_console(srv, auth, client):
    """Honouring ?next=/dashboard for an agent would sign them in and drop
    them straight on "not allowed", which reads as a failed login."""
    agent = auth.User(auth.find_user("priya"))
    assert srv.landing_for(agent, "/dashboard") == "/"


def test_a_lead_keeps_the_page_they_asked_for(srv, auth, client):
    lead = auth.User(auth.find_user("ravi"))
    assert srv.landing_for(lead, "/dashboard") == "/dashboard"


@pytest.mark.parametrize("hostile", [
    "//evil.example/phish",          # protocol-relative: a path to a browser
    "https://evil.example",
    "javascript:alert(1)",
    "",
])
def test_login_will_not_redirect_off_site(srv, auth, client, hostile):
    lead = auth.User(auth.find_user("ravi"))
    assert srv.landing_for(lead, hostile) == "/dashboard"


def test_signing_in_returns_a_landing_the_role_can_open(client):
    res = client.post("/api/login", json={"username": "priya",
                                          "password": "agent-password",
                                          "next": "/dashboard"})
    assert res.get_json()["next"] == "/"


# --------------------------------------------------------------------------
# Budget ceilings on the routes that call the model
# --------------------------------------------------------------------------
def test_usage_is_a_dashboard_endpoint(client):
    sign_in(client, "priya", "agent-password")
    assert client.get("/api/usage").status_code == 403

    client.post("/api/logout")
    sign_in(client, "ravi", "lead-password")
    assert client.get("/api/usage").status_code == 200


def test_a_spent_out_day_refuses_new_work_with_429(srv, client, monkeypatch):
    """The refusal has to arrive BEFORE the model is called, or the cap
    protects nothing."""
    import metering

    monkeypatch.setattr(metering, "DAILY_TOKEN_CAP", 100)
    with metering.attribute_to("SC-1001", "priya"):
        metering.record({"prompt": 500, "output": 0, "operation": "analyse"})

    sign_in(client, "priya", "agent-password")
    res = client.post("/api/customer", json={"text": "my recharge failed"})

    assert res.status_code == 429
    assert res.headers.get("Retry-After")
    assert "budget" in res.get_json()["error"]


def test_an_empty_message_is_still_rejected_before_the_budget_check(client):
    sign_in(client, "priya", "agent-password")
    res = client.post("/api/customer", json={"text": "   "})
    assert res.status_code == 400
