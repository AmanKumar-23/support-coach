"""Personal details must never reach the model."""

import pytest

SECRETS = {
    "phone_intl":  ("my number is +91 98765 43210", "PHONE"),
    "phone_bare":  ("call me on 9876543210",        "PHONE"),
    "email":       ("mail aman.kumar@example.com",  "EMAIL"),
    "card":        ("card 4111 1111 1111 1111",     "CARD"),
    "order_id":    ("order OD-4471 please",         "ORDER"),
}

# Things that look numeric but are NOT personal. A false positive here would
# mangle a perfectly good customer message.
INNOCENT = [
    "mera recharge fail ho gaya but paise cut gaye",
    "refund of 499 within 5-7 working days",
    "expected by 2026-09-05",
    "I have 3 orders and 2 refunds",
]


@pytest.mark.parametrize("text,kind", SECRETS.values(), ids=list(SECRETS))
def test_detects_each_kind(core, text, kind):
    redactor = core.Redactor()
    cleaned = redactor.redact(text)
    assert [f["kind"] for f in redactor.found] == [kind]
    assert cleaned != text


@pytest.mark.parametrize("text", INNOCENT)
def test_leaves_innocent_text_alone(core, text):
    redactor = core.Redactor()
    assert redactor.redact(text) == text
    assert redactor.found == []


def test_round_trip_restores_the_original(core):
    redactor = core.Redactor()
    original = "order OD-4471, phone +91 98765 43210, mail a.b@example.com"
    cleaned = redactor.redact(original)

    assert "98765" not in cleaned
    assert "OD-4471" not in cleaned
    assert "a.b@example.com" not in cleaned

    assert redactor.restore(cleaned) == original


@pytest.mark.parametrize("written_as", [
    "[PHONE_1]",        # exactly as we gave it
    "PHONE_1",          # model dropped the brackets
    "[phone_1]",        # model lower-cased it
    "phone_1",          # both
])
def test_restores_however_the_model_writes_it(core, written_as):
    """Models do not echo placeholders back exactly. This bit us in practice:
    a reply reached a customer reading 'Refund for order ORDER_1'."""
    redactor = core.Redactor()
    redactor.redact("call 9876543210")
    assert redactor.restore(f"We will ring {written_as}") == "We will ring 9876543210"


def test_same_value_keeps_one_placeholder(core):
    """Otherwise the model thinks two turns are two different people."""
    redactor = core.Redactor()
    first = redactor.redact("call 9876543210")
    second = redactor.redact("still nothing on 9876543210")

    assert "[PHONE_1]" in first and "[PHONE_1]" in second
    assert len(redactor.found) == 1


def test_log_is_masked_not_the_original(core):
    """The audit log must not become a second copy of the thing we protect."""
    redactor = core.Redactor()
    redactor.redact("mail aman.kumar@example.com")
    entry = redactor.log()[0]

    assert "aman.kumar" not in entry["masked"]
    assert entry["masked"].endswith("@example.com")
    assert entry["kind"] == "EMAIL"


def test_empty_input_is_safe(core):
    redactor = core.Redactor()
    assert redactor.redact("") == ""
    assert redactor.restore("") == ""
    assert redactor.redact(None) is None
