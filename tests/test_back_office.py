"""The mock order system the coach is allowed to query."""

import pytest


def test_looks_up_a_known_order(core, back_office):
    order = core.check_order_status("OD-4468")
    assert order["status"] == "in transit"
    assert order["courier"] == "BlueDart"


def test_unknown_order_returns_an_error_not_an_exception(core, back_office):
    assert "error" in core.check_order_status("OD-0000")


def test_no_id_lists_recent_orders(core, back_office):
    """The customer rarely quotes an order number, so the model is told to call
    this with no arguments first."""
    result = core.check_order_status()
    assert "recent_orders" in result
    assert len(result["recent_orders"]) >= 1


def test_refund_lookup_by_order(core, back_office):
    assert core.check_refund_status(order_id="OD-4471")["refund_id"] == "RF-9012"


def test_refund_lookup_for_an_order_without_one(core, back_office):
    assert "error" in core.check_refund_status(order_id="OD-4468")


def test_duplicate_refund_is_refused(core, back_office):
    result = core.issue_refund("OD-4471")
    assert "error" in result
    assert "already exists" in result["error"]


def test_password_reset_rejects_the_wrong_email(core, back_office):
    assert "error" in core.send_password_reset("stranger@example.com")


@pytest.mark.parametrize("name", [
    "check_order_status", "check_refund_status",
    "issue_refund", "send_password_reset",
])
def test_every_tool_is_declared_to_the_model(core, name):
    assert name in core.BACK_OFFICE
    assert core.BACK_OFFICE[name]["declaration"].name == name


def test_only_data_changing_tools_are_marked_as_writes(core):
    writes = {k for k, v in core.BACK_OFFICE.items() if v["writes"]}
    assert writes == {"issue_refund", "send_password_reset"}


def test_issuing_a_refund_creates_one(core, back_office):
    """A write tool really does change data -- which is why gather_facts()
    refuses to run these without a human saying so."""
    before = len(back_office["refunds"])
    refund = core.issue_refund("OD-4468", reason="never arrived")

    assert refund["order_id"] == "OD-4468"
    assert refund["amount"] == 349
    assert refund["status"] == "processing"
    assert len(back_office["refunds"]) == before + 1


def test_password_reset_stamps_the_account(core, back_office):
    result = core.send_password_reset("test@example.com")
    assert result["sent_to"] == "test@example.com"
    assert back_office["account"]["password_reset_sent_at"] is not None
