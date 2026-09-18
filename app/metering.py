"""What each conversation actually cost, and a ceiling on the bill.

The app already counted API calls. A call is a poor unit: analysing a
three-line message and drafting a reply grounded in two back-office lookups
are both "one call", and they differ by an order of magnitude in tokens. So
this records the token counts Gemini itself reports, attributes them to a case
and an agent, prices them, and refuses new work once a daily ceiling is hit.

Two things are deliberately kept apart:

    tokens  are MEASURED. They come from the API's own usage_metadata and are
            exact. They are the number to trust.
    cost    is DERIVED, from a rate table that is configuration. Google
            changes prices; the rates below are placeholders until somebody
            sets the real ones. The UI says so rather than pretending.

The engine knows none of this. coach_core.report_usage() announces a call and
this module decides what to do with it -- so metering policy never ends up
inside the coaching logic.
"""

import os
import threading
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

# --------------------------------------------------------------------------
# Rates
# --------------------------------------------------------------------------
# Rupees per MILLION tokens. These are PLACEHOLDERS -- they are the right
# shape and the wrong numbers, and every cost shown in the UI is flagged as
# estimated until they are replaced. Set the real ones from the current price
# list with, for example:
#
#     export PRICE_IN_INR_PER_MTOK=25.0
#     export PRICE_OUT_INR_PER_MTOK=75.0
#
# Output tokens cost several times what input tokens do on every provider, so
# the two are priced separately rather than averaged -- a drafting-heavy
# workload would otherwise be badly understated.
PLACEHOLDER_RATES = True

PRICE_IN = float(os.getenv("PRICE_IN_INR_PER_MTOK", "25.0"))
PRICE_OUT = float(os.getenv("PRICE_OUT_INR_PER_MTOK", "75.0"))
PRICE_EMBED = float(os.getenv("PRICE_EMBED_INR_PER_MTOK", "2.0"))

# Rates were configured explicitly, so stop calling the numbers estimates.
if any(os.getenv(name) for name in
       ("PRICE_IN_INR_PER_MTOK", "PRICE_OUT_INR_PER_MTOK",
        "PRICE_EMBED_INR_PER_MTOK")):
    PLACEHOLDER_RATES = False


# --------------------------------------------------------------------------
# Ceilings
# --------------------------------------------------------------------------
# A runaway loop, a stuck retry or somebody demonstrating the app to a hall
# full of people can all spend a month's quota in an afternoon. These are the
# brakes. Set any of them to 0 to turn that particular limit off.
DAILY_TOKEN_CAP = int(os.getenv("DAILY_TOKEN_CAP", "2000000"))
DAILY_COST_CAP_INR = float(os.getenv("DAILY_COST_CAP_INR", "200"))

# Per agent, per minute. One customer message costs roughly four calls
# (analyse, look up, draft, and the scorecard on the reply), so 30 leaves
# room for a fast typist and still stops a script.
RATE_LIMIT_CALLS_PER_MIN = int(os.getenv("RATE_LIMIT_CALLS_PER_MIN", "30"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,
    day           TEXT NOT NULL,
    case_id       TEXT,
    username      TEXT,
    operation     TEXT,
    model         TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cost_inr      REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS usage_day  ON usage(day);
CREATE INDEX IF NOT EXISTS usage_case ON usage(case_id);
CREATE INDEX IF NOT EXISTS usage_user ON usage(username, at);
"""


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------
_connect = None

# Who the calls happening right now belong to. A ContextVar rather than a
# global: Flask serves requests on many threads, and two agents mid
# conversation must not be billed to each other.
#
# It holds a mutable dict because the case id is not known when the request
# starts -- a brand new conversation is given its id part way through. So the
# request opens the account, and the case id is filled in as soon as there is
# one, without every call site having to pass it down.
_attribution = ContextVar("attribution", default=None)

# Writes are small and rare, but SQLite still dislikes concurrent ones.
_write_lock = threading.Lock()


def configure(connect):
    global _connect
    _connect = connect


def connect():
    if _connect is None:
        raise RuntimeError("metering.configure(connect) was never called")
    return _connect()


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)


def now():
    return datetime.now(UTC)


def today():
    return now().strftime("%Y-%m-%d")


def begin(username, case_id=None):
    """Start billing whatever happens next to this agent. Call once per
    request, before any model call."""
    _attribution.set({"username": username, "case_id": case_id})


def bill_to(case_id):
    """Name the case, once the request knows which one it is."""
    account = _attribution.get()
    if account is not None:
        account["case_id"] = case_id


def billing_to():
    account = _attribution.get()
    if account is None:
        return None, None
    return account.get("case_id"), account.get("username")


class attribute_to:
    """Bill one block explicitly. Used by the tests, and by anything that
    calls the coach outside a request.

        with metering.attribute_to("SC-1042", "priya"):
            coach.analyze_customer_message(...)
    """

    def __init__(self, case_id, username):
        self.account = {"case_id": case_id, "username": username}

    def __enter__(self):
        self.token = _attribution.set(self.account)
        return self

    def __exit__(self, *exc):
        _attribution.reset(self.token)
        return False


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------
def price(prompt_tokens, output_tokens, operation="generate"):
    """Rupees for one call. Embeddings have their own, much lower rate."""
    if operation == "embed":
        return (prompt_tokens / 1_000_000) * PRICE_EMBED
    return ((prompt_tokens / 1_000_000) * PRICE_IN
            + (output_tokens / 1_000_000) * PRICE_OUT)


def record(call):
    """The USAGE_HOOK the engine calls after every request to Gemini.

    Never raises. A metering failure must not cost a customer their reply,
    so anything that goes wrong here is swallowed -- the worst case is an
    under-reported bill, not a dropped conversation.
    """
    try:
        prompt = int(call.get("prompt") or 0)
        output = int(call.get("output") or 0)
        cached = int(call.get("cached") or 0)
        operation = call.get("operation") or "generate"
        case_id, username = billing_to()

        with _write_lock, connect() as conn:
            conn.execute("""
                INSERT INTO usage (at, day, case_id, username, operation,
                                   model, prompt_tokens, output_tokens,
                                   cached_tokens, cost_inr)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (now().isoformat(timespec="seconds"), today(), case_id,
                  username, operation, call.get("model"), prompt, output,
                  cached, price(prompt, output, operation)))
    except Exception:
        pass


# --------------------------------------------------------------------------
# Limits
# --------------------------------------------------------------------------
def spent_today():
    with connect() as conn:
        row = conn.execute("""
            SELECT COALESCE(SUM(prompt_tokens + output_tokens), 0) AS tokens,
                   COALESCE(SUM(cost_inr), 0)                     AS cost,
                   COUNT(*)                                       AS calls
            FROM usage WHERE day = ?
        """, (today(),)).fetchone()
    return {"tokens": row["tokens"], "cost_inr": row["cost"],
            "calls": row["calls"]}


def calls_in_last_minute(username):
    since = (now() - timedelta(seconds=60)).isoformat(timespec="seconds")
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM usage WHERE username = ? AND at >= ?",
            (username, since)).fetchone()[0]


def check_limits(username):
    """May this agent start work that will call the model?

    Returns (True, None) or (False, {"error": ..., "retry_after": seconds}).

    Checked at the START of a turn, not before each individual call: aborting
    half way through a tool-calling loop would leave the customer with a reply
    that references a lookup that never finished. One turn may overshoot the
    cap slightly; the next one is refused.
    """
    spent = spent_today()

    if DAILY_TOKEN_CAP and spent["tokens"] >= DAILY_TOKEN_CAP:
        return False, {
            "error": (f"Daily token budget reached "
                      f"({spent['tokens']:,} of {DAILY_TOKEN_CAP:,}). "
                      f"It resets at midnight UTC."),
            "limit": "daily_tokens", "retry_after": _seconds_to_midnight(),
        }

    if DAILY_COST_CAP_INR and spent["cost_inr"] >= DAILY_COST_CAP_INR:
        return False, {
            "error": (f"Daily spend limit reached "
                      f"(₹{spent['cost_inr']:.2f} of ₹{DAILY_COST_CAP_INR:.2f}). "
                      f"It resets at midnight UTC."),
            "limit": "daily_cost", "retry_after": _seconds_to_midnight(),
        }

    if RATE_LIMIT_CALLS_PER_MIN and username:
        recent = calls_in_last_minute(username)
        if recent >= RATE_LIMIT_CALLS_PER_MIN:
            return False, {
                "error": (f"That is {recent} model calls in a minute, which is "
                          f"over the limit of {RATE_LIMIT_CALLS_PER_MIN}. "
                          f"Wait a moment and try again."),
                "limit": "rate", "retry_after": 60,
            }

    return True, None


def _seconds_to_midnight():
    right_now = now()
    midnight = (right_now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return int((midnight - right_now).total_seconds())


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def for_case(case_id):
    """Tokens and rupees for one conversation."""
    with connect() as conn:
        row = conn.execute("""
            SELECT COALESCE(SUM(prompt_tokens), 0)  AS prompt_tokens,
                   COALESCE(SUM(output_tokens), 0)  AS output_tokens,
                   COALESCE(SUM(cost_inr), 0)       AS cost_inr,
                   COUNT(*)                         AS calls
            FROM usage WHERE case_id = ?
        """, (case_id,)).fetchone()
        steps = conn.execute("""
            SELECT operation,
                   COUNT(*)                                       AS calls,
                   COALESCE(SUM(prompt_tokens + output_tokens), 0) AS tokens,
                   COALESCE(SUM(cost_inr), 0)                      AS cost_inr
            FROM usage WHERE case_id = ?
            GROUP BY operation ORDER BY tokens DESC
        """, (case_id,)).fetchall()

    return {
        "calls": row["calls"],
        "prompt_tokens": row["prompt_tokens"],
        "output_tokens": row["output_tokens"],
        "tokens": row["prompt_tokens"] + row["output_tokens"],
        "cost_inr": round(row["cost_inr"], 4),
        "by_operation": [dict(s) for s in steps],
        "estimated": PLACEHOLDER_RATES,
    }


def costs_for_cases(case_ids):
    """One query for many cases, so a case list does not fire N of them."""
    if not case_ids:
        return {}
    marks = ",".join("?" * len(case_ids))
    with connect() as conn:
        rows = conn.execute(f"""
            SELECT case_id,
                   COALESCE(SUM(prompt_tokens + output_tokens), 0) AS tokens,
                   COALESCE(SUM(cost_inr), 0)                      AS cost_inr
            FROM usage WHERE case_id IN ({marks})
            GROUP BY case_id
        """, list(case_ids)).fetchall()
    return {r["case_id"]: {"tokens": r["tokens"],
                           "cost_inr": round(r["cost_inr"], 4)} for r in rows}


def summary(days=14, top=8):
    """Everything the dashboard's cost view needs, in one call."""
    spent = spent_today()
    since = (now() - timedelta(days=days)).strftime("%Y-%m-%d")

    with connect() as conn:
        by_day = conn.execute("""
            SELECT day,
                   COALESCE(SUM(prompt_tokens + output_tokens), 0) AS tokens,
                   COALESCE(SUM(cost_inr), 0)                      AS cost_inr,
                   COUNT(*)                                        AS calls
            FROM usage WHERE day >= ?
            GROUP BY day ORDER BY day
        """, (since,)).fetchall()

        by_operation = conn.execute("""
            SELECT operation,
                   COUNT(*)                                        AS calls,
                   COALESCE(SUM(prompt_tokens + output_tokens), 0) AS tokens,
                   COALESCE(SUM(cost_inr), 0)                      AS cost_inr
            FROM usage GROUP BY operation ORDER BY tokens DESC
        """).fetchall()

        priciest = conn.execute("""
            SELECT case_id,
                   COALESCE(SUM(prompt_tokens + output_tokens), 0) AS tokens,
                   COALESCE(SUM(cost_inr), 0)                      AS cost_inr,
                   COUNT(*)                                        AS calls
            FROM usage WHERE case_id IS NOT NULL
            GROUP BY case_id ORDER BY cost_inr DESC LIMIT ?
        """, (top,)).fetchall()

        overall = conn.execute("""
            SELECT COALESCE(SUM(prompt_tokens + output_tokens), 0) AS tokens,
                   COALESCE(SUM(cost_inr), 0)                      AS cost_inr,
                   COUNT(*)                                        AS calls,
                   COUNT(DISTINCT case_id)                         AS cases
            FROM usage
        """).fetchone()

    cases = overall["cases"] or 0
    return {
        "today": {
            **spent,
            "token_cap": DAILY_TOKEN_CAP,
            "cost_cap_inr": DAILY_COST_CAP_INR,
            "token_pct": _pct(spent["tokens"], DAILY_TOKEN_CAP),
            "cost_pct": _pct(spent["cost_inr"], DAILY_COST_CAP_INR),
        },
        "total": {
            "tokens": overall["tokens"], "calls": overall["calls"],
            "cost_inr": round(overall["cost_inr"], 4), "cases": cases,
        },
        "per_case": {
            "tokens": round(overall["tokens"] / cases) if cases else 0,
            "cost_inr": round(overall["cost_inr"] / cases, 4) if cases else 0,
        },
        "by_day": [dict(r) for r in by_day],
        "by_operation": [dict(r) for r in by_operation],
        "priciest_cases": [dict(r) for r in priciest],
        "rates": {
            "in_inr_per_mtok": PRICE_IN,
            "out_inr_per_mtok": PRICE_OUT,
            "embed_inr_per_mtok": PRICE_EMBED,
            "estimated": PLACEHOLDER_RATES,
        },
        "limits": {
            "daily_tokens": DAILY_TOKEN_CAP,
            "daily_cost_inr": DAILY_COST_CAP_INR,
            "calls_per_minute": RATE_LIMIT_CALLS_PER_MIN,
        },
    }


def _pct(used, cap):
    if not cap:
        return None          # the limit is switched off
    return round(min(100.0, (used / cap) * 100), 1)
