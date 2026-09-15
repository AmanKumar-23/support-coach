"""The Gemini function-calling loop.

These run with no API key: the model is replaced by a stub, because what is
being tested is the CONVERSATION WE BUILD, not what Gemini answers.

The bug that prompted this file: function results were appended with
role="tool", which the API rejects outright --

    Role 'tool' is not supported. Please use a valid role:
    SYSTEM, SYSTEM_1, USER, ASSISTANT, DEVELOPER, CONTEXT, USER_CONTEXT,
    MODEL, USER.

Round 1 worked, so the feature looked fine in the UI; every round after it
400'd, and the model could never chain one lookup into the next. The failure
was swallowed into a "(lookup unavailable)" note, so nothing reached the log.
"""

import pytest

# Exactly what the API told us it accepts.
VALID_ROLES = {
    "system", "system_1", "user", "assistant",
    "developer", "context", "user_context", "model",
}


class FakeCall:
    def __init__(self, name, args=None):
        self.name = name
        self.args = args or {}


class FakeContent:
    """Stands in for response.candidates[0].content."""
    def __init__(self, role="model"):
        self.role = role
        self.parts = []


class FakeResponse:
    def __init__(self, calls):
        self.function_calls = calls
        self.candidates = [type("C", (), {"content": FakeContent()})()]


@pytest.fixture
def coach(core):
    """An AICoach with no client -- __init__ would demand an API key."""
    obj = object.__new__(core.AICoach)
    obj.last_redactions = []
    obj.last_article = None
    obj.model = "gemini-3.5-flash"
    obj.client = None
    return obj


def test_function_results_go_back_with_a_role_the_api_accepts(
    core, coach, back_office, monkeypatch
):
    """Every turn we build must carry a role the API will accept."""
    seen = []

    def fake_call(contents, config=None, max_attempts=3):
        # Record the roles of the conversation as it stands on each round.
        seen.append([getattr(c, "role", None) for c in contents])
        # Round 1 asks for a lookup; round 2 is satisfied and asks for nothing.
        if len(seen) == 1:
            return FakeResponse([FakeCall("check_order_status")])
        return FakeResponse([])

    monkeypatch.setattr(coach, "_call_model", fake_call)

    facts = coach.gather_facts(
        "mera recharge fail ho gaya",
        conversation_history=[core.Message(speaker="customer",
                                           text="mera recharge fail ho gaya")],
    )

    assert len(seen) == 2, "the loop should have come back for a second round"

    roles = [r for round_roles in seen for r in round_roles]
    assert "tool" not in roles, "role='tool' is rejected by the Gemini API"
    for role in roles:
        assert role.lower() in VALID_ROLES, f"{role!r} is not a valid role"

    # The lookup really ran, rather than being recorded as unavailable.
    assert [f["name"] for f in facts] == ["check_order_status"]
    assert facts[0]["ran"] is True


def test_the_loop_can_chain_two_lookups(core, coach, back_office, monkeypatch):
    """Round 2 must survive -- that is the whole point of a multi-round loop."""
    rounds = []

    def fake_call(contents, config=None, max_attempts=3):
        rounds.append(contents)
        if len(rounds) == 1:
            return FakeResponse([FakeCall("check_order_status")])
        if len(rounds) == 2:
            return FakeResponse([FakeCall("check_refund_status",
                                          {"order_id": "OD-4471"})])
        return FakeResponse([])

    monkeypatch.setattr(coach, "_call_model", fake_call)

    facts = coach.gather_facts("refund kab aayega")

    assert [f["name"] for f in facts] == ["check_order_status",
                                          "check_refund_status"]
    assert all(f["ran"] for f in facts)
    # The second lookup returned the real refund record, not an error.
    assert facts[1]["result"]["refund_id"] == "RF-9012"


def test_a_malformed_request_is_not_retried_against_every_model(core, coach):
    """A 400 is our fault, so the fallback model rejects it identically.

    Trying anyway only doubles the latency and the quota spent before we
    give up.
    """
    attempts = []

    class FakeModels:
        def generate_content(self, model, contents, config=None):
            attempts.append(model)
            raise RuntimeError(
                "ClientError: 400 INVALID_ARGUMENT. "
                "{'error': {'code': 400, 'status': 'INVALID_ARGUMENT'}}"
            )

    coach.client = type("C", (), {"models": FakeModels()})()

    with pytest.raises(ValueError, match="rejected the request"):
        coach._call_model("anything")

    assert attempts == ["gemini-3.5-flash"], (
        f"a malformed request should stop after one attempt, got {attempts}"
    )


def test_a_rate_limit_still_falls_back_to_the_other_model(core, coach,
                                                          monkeypatch):
    """The fail-fast path must not have broken the retry path."""
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    attempts = []

    class FakeModels:
        def generate_content(self, model, contents, config=None):
            attempts.append(model)
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

    coach.client = type("C", (), {"models": FakeModels()})()

    with pytest.raises(ValueError, match="could not be reached"):
        coach._call_model("anything")

    # Three attempts on each of the two models.
    assert attempts.count("gemini-3.5-flash") == 3
    assert "gemini-3.5-flash-lite" in attempts
