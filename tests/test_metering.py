"""Token metering, pricing and the ceilings that stop a runaway bill.

No API key needed: the engine's usage hook is driven directly with the same
shape a real Gemini response produces, so every path here is exercised
without spending anything.
"""

import pytest


@pytest.fixture
def meter(srv):
    """The metering module, pointed at this test's temp database."""
    import metering as metering_module
    return metering_module


@pytest.fixture
def spend(meter):
    """Record one call, as the engine's USAGE_HOOK would."""
    def _spend(prompt=1000, output=500, operation="analyse",
               case_id="SC-1001", username="priya", model="gemini-3.5-flash"):
        with meter.attribute_to(case_id, username):
            meter.record({"prompt": prompt, "output": output, "cached": 0,
                          "operation": operation, "model": model})
    return _spend


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------
def test_a_call_is_recorded_against_its_case_and_agent(meter, spend):
    spend(prompt=1200, output=300, case_id="SC-1001", username="priya")

    billed = meter.for_case("SC-1001")
    assert billed["calls"] == 1
    assert billed["prompt_tokens"] == 1200
    assert billed["output_tokens"] == 300
    assert billed["tokens"] == 1500


def test_calls_accumulate_across_a_conversation(meter, spend):
    for operation in ("analyse", "lookup", "draft", "score"):
        spend(prompt=1000, output=200, operation=operation)

    billed = meter.for_case("SC-1001")
    assert billed["calls"] == 4
    assert billed["tokens"] == 4800
    assert {row["operation"] for row in billed["by_operation"]} == {
        "analyse", "lookup", "draft", "score"}


def test_usage_is_split_by_step_so_the_expensive_one_is_visible(meter, spend):
    spend(prompt=500, output=100, operation="analyse")
    spend(prompt=8000, output=2000, operation="draft")

    steps = {r["operation"]: r["tokens"]
             for r in meter.for_case("SC-1001")["by_operation"]}
    assert steps["draft"] > steps["analyse"]


def test_a_call_with_no_case_is_still_recorded(meter):
    """Usage outside a conversation must not vanish, or the daily total
    silently under-reports what was actually spent."""
    meter.begin("priya")                 # a request, but no case yet
    meter.record({"prompt": 900, "output": 0, "operation": "embed",
                  "model": "gemini-embedding-001"})

    assert meter.spent_today()["tokens"] == 900


def test_metering_never_breaks_a_conversation(meter, monkeypatch):
    """A broken metering store must cost an under-reported bill, never a
    customer's reply."""
    def explode():
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(meter, "connect", explode)
    meter.record({"prompt": 10, "output": 10, "operation": "analyse"})
    # no exception escaped


def test_a_malformed_usage_report_is_survivable(meter):
    meter.record({})
    meter.record({"prompt": None, "output": None})
    assert meter.spent_today()["tokens"] == 0


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------
def test_output_tokens_cost_more_than_input(meter):
    """Averaging the two would understate a drafting-heavy workload."""
    assert meter.price(0, 1000) > meter.price(1000, 0)


def test_embeddings_are_priced_on_their_own_much_lower_rate(meter):
    assert meter.price(1000, 0, "embed") < meter.price(1000, 0, "analyse")


def test_price_scales_with_tokens(meter):
    assert meter.price(2_000_000, 0) == pytest.approx(2 * meter.PRICE_IN)


def test_cost_is_flagged_as_estimated_while_rates_are_placeholders(meter, spend):
    spend()
    assert meter.for_case("SC-1001")["estimated"] is meter.PLACEHOLDER_RATES


# --------------------------------------------------------------------------
# Ceilings
# --------------------------------------------------------------------------
def test_work_is_allowed_while_under_budget(meter, spend):
    spend(prompt=10, output=10)
    ok, why = meter.check_limits("priya")
    assert ok and why is None


def test_the_daily_token_cap_stops_new_work(meter, spend, monkeypatch):
    monkeypatch.setattr(meter, "DAILY_TOKEN_CAP", 5000)
    monkeypatch.setattr(meter, "DAILY_COST_CAP_INR", 0)      # isolate this one
    spend(prompt=4000, output=2000)

    ok, why = meter.check_limits("priya")
    assert not ok
    assert why["limit"] == "daily_tokens"
    assert why["retry_after"] > 0


def test_the_daily_spend_cap_stops_new_work(meter, spend, monkeypatch):
    monkeypatch.setattr(meter, "DAILY_TOKEN_CAP", 0)
    monkeypatch.setattr(meter, "DAILY_COST_CAP_INR", 0.001)
    spend(prompt=100000, output=100000)

    ok, why = meter.check_limits("priya")
    assert not ok
    assert why["limit"] == "daily_cost"


def test_a_burst_of_calls_from_one_agent_is_rate_limited(meter, spend, monkeypatch):
    monkeypatch.setattr(meter, "RATE_LIMIT_CALLS_PER_MIN", 5)
    monkeypatch.setattr(meter, "DAILY_TOKEN_CAP", 0)
    monkeypatch.setattr(meter, "DAILY_COST_CAP_INR", 0)

    for _ in range(5):
        spend(prompt=10, output=10, username="priya")

    blocked, why = meter.check_limits("priya")
    assert not blocked
    assert why["limit"] == "rate"
    assert why["retry_after"] == 60

    # and it is PER AGENT -- one busy agent must not lock out the team
    allowed, _ = meter.check_limits("ravi")
    assert allowed


def test_a_cap_set_to_zero_is_switched_off(meter, spend, monkeypatch):
    monkeypatch.setattr(meter, "DAILY_TOKEN_CAP", 0)
    monkeypatch.setattr(meter, "DAILY_COST_CAP_INR", 0)
    monkeypatch.setattr(meter, "RATE_LIMIT_CALLS_PER_MIN", 0)

    spend(prompt=10_000_000, output=10_000_000)
    ok, _ = meter.check_limits("priya")
    assert ok


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def test_summary_reports_cost_per_conversation(meter, spend):
    spend(prompt=1000, output=1000, case_id="SC-1001")
    spend(prompt=3000, output=3000, case_id="SC-1002")

    report = meter.summary()
    assert report["total"]["cases"] == 2
    assert report["total"]["tokens"] == 8000
    assert report["per_case"]["tokens"] == 4000


def test_summary_ranks_the_priciest_conversations(meter, spend):
    spend(prompt=100, output=100, case_id="SC-1001")
    spend(prompt=9000, output=9000, case_id="SC-1002")

    priciest = meter.summary()["priciest_cases"]
    assert priciest[0]["case_id"] == "SC-1002"


def test_summary_shows_todays_spend_against_the_cap(meter, spend, monkeypatch):
    monkeypatch.setattr(meter, "DAILY_TOKEN_CAP", 10000)
    spend(prompt=4000, output=1000)

    today = meter.summary()["today"]
    assert today["tokens"] == 5000
    assert today["token_pct"] == 50.0


def test_a_switched_off_cap_reports_no_percentage(meter, spend, monkeypatch):
    monkeypatch.setattr(meter, "DAILY_TOKEN_CAP", 0)
    spend()
    assert meter.summary()["today"]["token_pct"] is None


def test_costs_for_many_cases_come_back_in_one_call(meter, spend):
    spend(case_id="SC-1001")
    spend(case_id="SC-1002")

    costs = meter.costs_for_cases(["SC-1001", "SC-1002", "SC-9999"])
    assert set(costs) == {"SC-1001", "SC-1002"}     # unknown case simply absent
    assert meter.costs_for_cases([]) == {}


# --------------------------------------------------------------------------
# Through the engine's own hook
# --------------------------------------------------------------------------
def test_the_engine_reports_usage_through_the_hook(core, meter, monkeypatch):
    """coach_core.report_usage() is what every model call funnels into. Drive
    it with the shape a real response has."""
    monkeypatch.setattr(core, "USAGE_HOOK", meter.record)

    class FakeResponse:
        class usage_metadata:
            prompt_token_count = 2500
            candidates_token_count = 700
            cached_content_token_count = 0

    with meter.attribute_to("SC-1001", "priya"):
        core.report_usage("analyse", "gemini-3.5-flash", FakeResponse())

    billed = meter.for_case("SC-1001")
    assert billed["prompt_tokens"] == 2500
    assert billed["output_tokens"] == 700


def test_a_response_without_usage_metadata_does_not_crash(core, meter, monkeypatch):
    monkeypatch.setattr(core, "USAGE_HOOK", meter.record)

    with meter.attribute_to("SC-1001", "priya"):
        core.report_usage("analyse", "gemini-3.5-flash", object())

    assert meter.for_case("SC-1001")["tokens"] == 0


def test_nothing_is_recorded_when_no_hook_is_set(core, meter, monkeypatch):
    """The notebook runs with USAGE_HOOK unset and must be unaffected."""
    monkeypatch.setattr(core, "USAGE_HOOK", None)
    core.report_usage("analyse", "gemini-3.5-flash", object())
    assert meter.spent_today()["calls"] == 0
