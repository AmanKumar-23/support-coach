"""First-response targets vary by how risky the conversation looks."""

import pytest


def test_targets_come_from_risk(srv):
    assert srv.SLA_TARGET_MINUTES == {"high": 15, "medium": 60, "low": 240}


@pytest.mark.parametrize("risk,waited,expected", [
    ("high",    3, "on_track"),   # target 15
    ("high",   10, "at_risk"),
    ("high",   40, "breached"),
    ("medium", 40, "at_risk"),    # target 60
    ("low",    40, "on_track"),   # target 240
    ("low",   300, "breached"),
])
def test_status_while_still_waiting(srv, case, ago, risk, waited, expected):
    sla = srv.sla_for(case(escalation_risk=risk, opened_at=ago(minutes=waited)))
    assert sla["status"] == expected


def test_replied_inside_target_is_met(srv, case, ago):
    sla = srv.sla_for(case(
        escalation_risk="high",
        opened_at=ago(minutes=20),
        first_response_at=ago(minutes=15),   # 5 minutes to reply
        status="resolved",
    ))
    assert sla["status"] == "met"
    assert sla["first_response_seconds"] == 300


def test_replied_outside_target_is_breached(srv, case, ago):
    sla = srv.sla_for(case(
        escalation_risk="high",
        opened_at=ago(minutes=60),
        first_response_at=ago(minutes=20),   # 40 minutes, target is 15
        status="resolved",
    ))
    assert sla["status"] == "breached"


def test_old_case_without_a_recorded_reply_is_unknown(srv, case, ago):
    """Cases saved before response times were recorded. Saying 'unknown' is
    honest; inventing a duration would not be."""
    sla = srv.sla_for(case(opened_at=ago(days=1), status="resolved",
                           closed_at=ago(hours=1)))
    assert sla["status"] == "unknown"
    assert sla["first_response_seconds"] is None


def test_summary_only_queues_open_cases(srv, case, ago):
    summary = srv.sla_summary([
        case(id="OPEN", status="pending", escalation_risk="high",
             opened_at=ago(minutes=90)),
        case(id="CLOSED", status="resolved", escalation_risk="high",
             opened_at=ago(minutes=90)),
    ])
    assert [row["id"] for row in summary["breaching"]] == ["OPEN"]
