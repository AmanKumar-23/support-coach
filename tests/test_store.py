"""The SQLite case store, and the one-way migration that produced it."""

import json


def test_upsert_does_not_duplicate(srv, case):
    srv.save_case(case(id="SC-1", status="pending"))
    srv.save_case(case(id="SC-1", status="resolved"))

    rows = srv.load_cases()
    assert len(rows) == 1
    assert rows[0]["status"] == "resolved"


def test_indexed_columns_track_the_json_blob(srv, case):
    srv.save_case(case(id="SC-1", status="resolved", escalation_risk="high"))
    with srv.connect() as conn:
        row = conn.execute(
            "SELECT status, escalation_risk FROM cases WHERE id='SC-1'").fetchone()
    assert row["status"] == "resolved"
    assert row["escalation_risk"] == "high"


def test_order_is_oldest_first(srv, case):
    for n in range(1, 4):
        srv.save_case(case(id=f"SC-100{n}"))
    assert [c["id"] for c in srv.load_cases()] == ["SC-1001", "SC-1002", "SC-1003"]


def test_next_case_id_continues_the_sequence(srv, case):
    for n in (1, 2, 10):
        srv.save_case(case(id=f"SC-{1000 + n}"))
    assert srv.next_case_id(srv.load_cases()) == "SC-1011"


def test_bulk_replace_keeps_the_old_api(srv, case):
    """save_cases(list) predates SQLite. Anything written against it still works."""
    srv.save_cases([case(id="X1"), case(id="X2", status="resolved")])
    assert [c["id"] for c in srv.load_cases()] == ["X1", "X2"]


def test_corrupt_store_degrades_instead_of_crashing(srv):
    with open(srv.CASES_DB, "wb") as handle:
        handle.write(b"this is not a database")
    assert srv.load_cases() == []


def test_migration_imports_then_retires_the_json(srv, tmp_path, case):
    """Left in place, cases.json becomes a trap: delete the database later and
    a stale snapshot silently comes back, losing everything since."""
    payload = {"cases": [case(id="SC-1"), case(id="SC-2")]}
    with open(srv.CASES_FILE, "w") as handle:
        json.dump(payload, handle)

    assert srv.migrate_from_json() == 2
    assert len(srv.load_cases()) == 2

    import os
    assert not os.path.exists(srv.CASES_FILE)
    assert os.path.exists(srv.CASES_FILE + ".imported")


def test_migration_is_idempotent(srv, case):
    srv.save_case(case(id="SC-1"))
    with open(srv.CASES_FILE, "w") as handle:
        json.dump({"cases": [case(id="SC-99")]}, handle)

    assert srv.migrate_from_json() == 0     # table not empty, so do nothing
    assert [c["id"] for c in srv.load_cases()] == ["SC-1"]


def test_migration_preserves_nested_structures(srv, case):
    rich = case(id="SC-1", messages=[{"speaker": "customer", "text": "hi"}],
                ratings=[{"rating": "up", "grounded": True}],
                redactions=[{"kind": "PHONE", "placeholder": "[PHONE_1]",
                             "masked": "+9***10"}])
    with open(srv.CASES_FILE, "w") as handle:
        json.dump({"cases": [rich]}, handle)

    srv.migrate_from_json()
    assert srv.load_cases()[0] == rich
