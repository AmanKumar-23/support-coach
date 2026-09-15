"""Web backend for the AI Support Coach.

The coaching logic is NOT reimplemented here. It is imported from
coach_core.py, which is generated straight from customer_support_coach.ipynb
by build_core.py. This file only does four things:

    1. hold the state of the conversation currently open,
    2. keep every conversation as a "case" on disk, with a status,
    3. expose all of that over a small JSON API,
    4. serve the two front ends (console + dashboard).

Run it with:

    python3 app/server.py
"""

import contextlib
import csv
import io
import json
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from flask import Flask, Response, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import coach_core
except ImportError as import_error:
    print(f"\n  Could not load the coaching engine: {import_error}")
    print("  The Python running this server does not have the Gemini SDK.")
    print("  Install it for this interpreter:\n")
    print(f"      {sys.executable} -m pip install google-genai\n")
    raise SystemExit(1) from import_error

# The SDK prints a long advisory about automatic function calling that does
# not apply to us. Quiet it so the server log stays readable.
logging.getLogger("google_genai.models").setLevel(logging.ERROR)

app = Flask(__name__, static_folder=os.path.join(HERE, "static"))

# analyze_customer_message() now returns a real 0-100 frustration score of its
# own, so the old low/medium/high -> number lookup is gone. This stays only as
# a fallback for a response that somehow arrives without one.
FALLBACK_SCORE = {"low": 25, "medium": 55, "high": 90}

# The canned questions offered in the console, so an agent (or you, during a
# demo) can pick a common issue instead of typing it out. The topics mirror
# the KNOWLEDGE_BASE categories in Part 1 of the notebook, and the keywords
# are what we count cases against for the dashboard's volume figures.
FAQS = [
    {
        "id": "recharge", "icon": "📱",
        "label": "Recharge failed, money deducted",
        "text": "mera recharge nahi hua but paise cut gaye",
        "keywords": ["recharge", "paise cut", "deducted", "prepaid"],
    },
    {
        "id": "refund", "icon": "💸",
        "label": "Refund still not received",
        "text": "bhai 2 din ho gaye, abhi tak refund nahi aaya",
        "keywords": ["refund", "money back", "reversal", "paise wapas"],
    },
    {
        "id": "delivery", "icon": "📦",
        "label": "Order has not arrived",
        "text": "bhai order abhi tak nahi aaya, bahut ganda service hai",
        "keywords": ["order", "delivery", "deliver", "parcel", "shipment"],
    },
    {
        "id": "network", "icon": "📶",
        "label": "Internet / network down",
        "text": "My internet has stopped working since this morning.",
        "keywords": ["internet", "network", "signal", "slow", "connection"],
    },
    {
        "id": "account", "icon": "🔑",
        "label": "Cannot log in",
        "text": "I cannot log in and the password reset email never arrives.",
        "keywords": ["log in", "login", "password", "otp", "account"],
    },
    {
        "id": "escalate", "icon": "⚠️",
        "label": "Threatening to cancel",
        "text": "This is the third time I am asking. I want to cancel and I will "
                "take this further.",
        "keywords": ["cancel", "legal", "complaint", "consumer court", "escalate"],
    },
]

def is_knowledge_gap(text):
    """True when we have nothing at all to answer this question with.

    Two independent misses are required:
      1. knowledge_gap_detector() from Part 1 of the notebook finds no help
         article (it also appends to the notebook's KNOWLEDGE_GAP_LOG), and
      2. none of the FAQ topics above matches on keywords either.

    A gap is not a bug. It is a real customer asking something the
    documentation cannot answer -- which is to say, a help article somebody
    still has to write.
    """
    lowered = (text or "").lower()

    matches_faq = any(
        word in lowered
        for faq in FAQS
        for word in faq["keywords"]
    )
    if matches_faq:
        return False

    try:
        return coach_core.knowledge_gap_detector(text)
    except Exception:
        # Part 1's knowledge base is optional; without it we cannot judge.
        return False


# How sure the knowledge base must be before we answer a customer without any
# agent involved. Deliberately higher than the 0.58 used merely to SHOW an
# article to an agent: putting an answer straight in front of a customer needs
# more confidence than putting one in front of a human who can overrule it.
# Measured matches ran 0.596-0.711, so 0.65 keeps only the strong half.
AUTO_RESOLVE_THRESHOLD = 0.65


# How long we have to give the customer a FIRST reply, by how risky the
# conversation looks. An angry customer waiting fifteen minutes is a different
# problem from a calm one waiting four hours, so one flat target would be
# either far too tight or meaningless.
SLA_TARGET_MINUTES = {"high": 15, "medium": 60, "low": 240}
DEFAULT_SLA_MINUTES = 60          # when we have no risk reading yet


# Cases live in SQLite now. The JSON file it grew out of is kept as the
# migration source and is still what /api/export.json produces, so nothing
# that read the old format has been orphaned.
CASES_DB = os.path.join(HERE, "cases.db")
CASES_FILE = os.path.join(HERE, "cases.json")     # legacy, and export shape


# ==========================================================================
# Case store
# ==========================================================================
#
# One row per case: the fields we actually filter and sort on get real
# columns, and everything nested -- messages, facts, ratings, redactions --
# rides along as JSON in `data`.
#
# Fully normalising this would mean five more tables and a join for every
# read, and the app always wants the whole case anyway. This way the queries
# that matter can use an index, without pretending a support transcript is
# relational when it is not.

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    id              TEXT UNIQUE NOT NULL,
    status          TEXT,
    escalation_risk TEXT,
    sentiment       TEXT,
    opened_at       TEXT,
    closed_at       TEXT,
    data            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cases_status ON cases(status);
CREATE INDEX IF NOT EXISTS cases_risk   ON cases(escalation_risk);
CREATE INDEX IF NOT EXISTS cases_opened ON cases(opened_at);
"""


def connect():
    """A fresh connection per call -- Flask serves requests on many threads,
    and a SQLite connection must not be shared across them."""
    conn = sqlite3.connect(CASES_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)


def migrate_from_json():
    """Import cases.json the first time, then never again.

    Idempotent: it only runs when the table is empty, so restarting the
    server cannot duplicate anything.
    """
    init_db()

    with connect() as conn:
        already = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
    if already or not os.path.exists(CASES_FILE):
        return 0

    try:
        with open(CASES_FILE) as handle:
            cases = json.load(handle).get("cases", [])
    except (json.JSONDecodeError, OSError):
        return 0

    for case in cases:
        save_case(case)

    # Rename it once imported. Left in place it becomes a trap: delete the
    # database later and the migration would silently re-import a snapshot
    # that is now weeks out of date, quietly losing everything since.
    # Losing the rename is survivable -- the import already succeeded.
    with contextlib.suppress(OSError):
        os.replace(CASES_FILE, CASES_FILE + ".imported")

    return len(cases)


def _row_values(case):
    return (case.get("id"), case.get("status"), case.get("escalation_risk"),
            case.get("sentiment"), case.get("opened_at"), case.get("closed_at"),
            json.dumps(case))


def save_case(case):
    """Insert or update ONE case.

    The JSON store had to rewrite every case on every message. This touches
    a single row, which is the whole reason for moving.
    """
    if not case.get("id"):
        return
    with connect() as conn:
        conn.execute("""
            INSERT INTO cases (id, status, escalation_risk, sentiment,
                               opened_at, closed_at, data)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status,
                escalation_risk=excluded.escalation_risk,
                sentiment=excluded.sentiment,
                opened_at=excluded.opened_at,
                closed_at=excluded.closed_at,
                data=excluded.data
        """, _row_values(case))


def load_cases():
    """Every saved case, oldest first -- the order the JSON list had."""
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT data FROM cases ORDER BY seq").fetchall()
        return [json.loads(row["data"]) for row in rows]
    except (sqlite3.Error, json.JSONDecodeError):
        # A broken store must not take the whole server down.
        return []


def save_cases(cases):
    """Replace the whole store. Same signature as the JSON version had, so
    anything written against the old API still works."""
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.execute("DELETE FROM cases")
        conn.executemany("""
            INSERT INTO cases (id, status, escalation_risk, sentiment,
                               opened_at, closed_at, data)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [_row_values(c) for c in cases if c.get("id")])


def next_case_id(cases):
    """SC-1001, SC-1002, ... Readable, and stable across restarts."""
    numbers = []
    for case in cases:
        try:
            numbers.append(int(str(case.get("id", "")).split("-")[-1]))
        except ValueError:
            continue
    return f"SC-{(max(numbers) if numbers else 1000) + 1}"


def now_iso():
    return datetime.now(UTC).isoformat(timespec="seconds")


# ==========================================================================
# The conversation currently open
# ==========================================================================
class LiveSession:
    """The one conversation an agent is working on right now.

    It is also a case: as soon as it has a message it is written to
    cases.json with status "pending", so the dashboard can count it.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.case_id = None          # assigned on the first message
        self.opened_at = None
        self.state = coach_core.ConversationState()
        self.last_customer_message = ""
        self.trajectory = []
        self.calls = 0
        self.last_feedback = None
        self.last_suggestion = ""
        self.last_latency_ms = 0
        self.unanswered = []         # questions nothing in our docs covers
        self.facts = []              # back-office lookups for this turn
        self.allow_writes = False    # actions stay gated until a person says so
        self.auto_reply = None       # set when we answered without an agent
        self.first_response_at = None  # when the customer first heard back
        self.redactions = []         # personal details kept from the model
        self.ratings = []            # thumbs on each suggestion we produced

    _coach = None

    @property
    def coach(self):
        if self._coach is None:
            self._coach = coach_core.AICoach()
        return self._coach

    # -- persistence ------------------------------------------------------
    def as_case(self, status="pending", closed_at=None):
        return {
            "id": self.case_id,
            "opened_at": self.opened_at,
            "first_response_at": self.first_response_at,
            "closed_at": closed_at,
            "status": status,
            "turns": len(self.state.history),
            "calls": self.calls,
            "sentiment": self.state.sentiment,
            "urgency": self.state.urgency,
            "escalation_risk": self.state.escalation_risk,
            "frustration": self.state.frustration,
            "trend": self.state.trend,
            "key_issue": self.state.key_issue,
            "trajectory": list(self.trajectory),
            # The detail view shows these, so they have to be saved with the
            # case -- previously they lived only in memory and were lost the
            # moment the conversation was closed.
            "feedback": self.last_feedback,
            "suggestion": self.last_suggestion,
            "unanswered": list(self.unanswered),
            "redactions": list(self.redactions),
            "ratings": list(self.ratings),
            "facts": list(self.facts),
            "auto_reply": self.auto_reply,
            "messages": [
                {"speaker": m.speaker, "text": m.text}
                for m in self.state.history
            ],
        }

    def persist(self, status="pending", closed_at=None, reopen=False):
        """Insert or update this conversation in cases.json.

        reopen=True is passed only when a CUSTOMER has just written. That is
        the one situation where an auto-resolved case should fall back to
        pending -- saving the case for any other reason (a reset, an agent
        reply) must not quietly undo a deflection.
        """
        cases = load_cases()

        if self.case_id is None:
            self.case_id = next_case_id(cases)
            self.opened_at = now_iso()

        record = self.as_case(status=status, closed_at=closed_at)

        for index, case in enumerate(cases):
            if case.get("id") == self.case_id:
                # Never downgrade a resolved case back to pending.
                # A case a person closed stays closed, always.
                was = case.get("status")
                keep_closed = (was == "resolved")

                # An auto-resolved case stays closed too -- UNLESS the customer
                # has just written again, which means the automatic answer did
                # not land. Deflection you have to follow up is not deflection.
                if was == "auto_resolved" and not reopen:
                    keep_closed = True

                if keep_closed and status == "pending":
                    record["status"] = was
                    record["closed_at"] = case.get("closed_at")
                cases[index] = record
                break
        else:
            cases.append(record)

        save_case(record)          # one row, not the whole store
        return record

    def load(self, case):
        """Pull a saved case back into the live session so it can continue.

        The console only ever holds ONE conversation, so opening a case from
        the work queue means rehydrating it here -- transcript, analysis,
        trajectory and all -- rather than starting something new.
        """
        self.reset()

        self.case_id = case.get("id")
        self.opened_at = case.get("opened_at")
        self.first_response_at = case.get("first_response_at")
        self.trajectory = list(case.get("trajectory") or [])
        self.calls = case.get("calls", 0)
        self.unanswered = list(case.get("unanswered") or [])
        self.redactions = list(case.get("redactions") or [])
        self.ratings = list(case.get("ratings") or [])
        self.last_feedback = case.get("feedback")
        self.last_suggestion = case.get("suggestion") or ""

        for message in case.get("messages", []):
            self.state.add_message(message.get("speaker", "customer"),
                                   message.get("text", ""))

        # The agent replies to the last thing the CUSTOMER said, which is not
        # necessarily the last line of the transcript.
        for message in reversed(case.get("messages", [])):
            if message.get("speaker") == "customer":
                self.last_customer_message = message.get("text", "")
                break

        self.state.sentiment = case.get("sentiment", "unknown")
        self.state.urgency = case.get("urgency", "unknown")
        self.state.escalation_risk = case.get("escalation_risk", "unknown")
        self.state.frustration = case.get("frustration", 0)
        self.state.trend = case.get("trend", "unknown")
        self.state.key_issue = case.get("key_issue", "")

    def as_dict(self):
        return {
            "case_id": self.case_id,
            "history": [
                {"speaker": m.speaker, "text": m.text}
                for m in self.state.history
            ],
            "sentiment": self.state.sentiment,
            "urgency": self.state.urgency,
            "escalation_risk": self.state.escalation_risk,
            "frustration": self.state.frustration,
            "trend": self.state.trend,
            "key_issue": self.state.key_issue,
            "trajectory": self.trajectory,
            "calls": self.calls,
            "model": self._coach.model if self._coach else None,
            "last_feedback": self.last_feedback,
            "last_suggestion": self.last_suggestion,
            "last_latency_ms": self.last_latency_ms,
            "facts": self.facts,
            "redactions": self.redactions,
            "ratings": self.ratings,
            "allow_writes": self.allow_writes,
            "auto_reply": self.auto_reply,
        }


session = LiveSession()


def parse_time(value):
    """Read one of our ISO timestamps back, or None if it is missing/broken."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def sla_for(case, now=None):
    """Work out where one case stands against its first-response target.

    Statuses:
      met       -- the customer got a reply inside the target
      breached  -- they did not, or are still waiting past it
      at_risk   -- still waiting, and more than half the target is gone
      on_track  -- still waiting, comfortably inside the target
      unknown   -- saved before we started recording response times
    """
    now = now or datetime.now(UTC)

    minutes = SLA_TARGET_MINUTES.get(case.get("escalation_risk"),
                                     DEFAULT_SLA_MINUTES)
    target = minutes * 60

    opened = parse_time(case.get("opened_at"))
    responded = parse_time(case.get("first_response_at"))
    closed = parse_time(case.get("closed_at"))
    is_closed = case.get("status") in ("resolved", "auto_resolved")

    result = {"target_minutes": minutes, "first_response_seconds": None,
              "resolution_seconds": None, "elapsed_seconds": None,
              "remaining_seconds": None, "status": "unknown"}

    if opened is None:
        return result

    if closed is not None:
        result["resolution_seconds"] = int((closed - opened).total_seconds())

    if responded is not None:
        taken = int((responded - opened).total_seconds())
        result["first_response_seconds"] = taken
        result["elapsed_seconds"] = taken
        result["status"] = "met" if taken <= target else "breached"
        result["remaining_seconds"] = target - taken
        return result

    if is_closed:
        # Closed without us knowing when the reply went out -- these are
        # cases saved before response times were recorded. Say so rather
        # than inventing a number.
        return result

    # Still waiting for a first reply: the clock is running.
    waited = int((now - opened).total_seconds())
    result["elapsed_seconds"] = waited
    result["remaining_seconds"] = target - waited

    if waited > target:
        result["status"] = "breached"
    elif waited > target / 2:
        result["status"] = "at_risk"
    else:
        result["status"] = "on_track"

    return result


def try_auto_resolve(text):
    """Answer the customer outright when we are confident enough to.

    Two conditions, both required:
      1. the conversation is calm (escalation risk low), and
      2. semantic search found a strongly matching help article.

    A keyword match never qualifies -- its score is a word count, not a
    confidence, and it is the weaker matcher.
    """
    if session.state.escalation_risk != "low":
        return None

    article = coach_core.find_kb_article(text)
    if not article or article.get("how") != "semantic":
        return None
    if article.get("score", 0) < AUTO_RESOLVE_THRESHOLD:
        return None

    reply = session.coach.suggest_reply(
        text, session.state.history,
        analysis={
            "sentiment": session.state.sentiment,
            "urgency": session.state.urgency,
            "key_issue": session.state.key_issue,
        },
        facts=session.facts,
    )
    return {"reply": reply, "topic": article["topic"],
            "confidence": article["score"]}


def open_rating_slot(session, grounded, topic=None):
    """Record that a suggestion was produced, ready for a thumbs up or down."""
    session.ratings.append({
        "rating": None,
        "grounded": bool(grounded),
        "topic": topic,
        "at": now_iso(),
    })


def note_redactions(session):
    """Fold the last call's redaction log into the case, without repeats."""
    seen = {(r["kind"], r["placeholder"], r["masked"])
            for r in session.redactions}
    for entry in getattr(session.coach, "last_redactions", []) or []:
        key = (entry["kind"], entry["placeholder"], entry["masked"])
        if key not in seen:
            seen.add(key)
            session.redactions.append(entry)


def failure(error, status=502):
    return jsonify({"ok": False, "error": str(error)}), status


# ==========================================================================
# Pages
# ==========================================================================
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/dashboard")
def dashboard():
    return send_from_directory(app.static_folder, "dashboard.html")


# ==========================================================================
# API -- the live conversation
# ==========================================================================
@app.get("/api/health")
def health():
    key_file = coach_core.find_key_file()
    has_key = bool(coach_core.get_gemini_api_key(prompt_if_missing=False))
    return jsonify({
        "ok": True,
        "has_key": has_key,
        "key_file": key_file or None,
        "model": os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
    })


@app.post("/api/customer")
def customer_message():
    text = (request.json or {}).get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Empty message."}), 400

    session.state.add_message("customer", text)
    session.last_customer_message = text

    # Log it before we call the model -- whether we can answer this has
    # nothing to do with whether the analysis succeeds.
    if is_knowledge_gap(text):
        session.unanswered.append({"text": text, "at": now_iso()})

    started = time.perf_counter()
    try:
        # Hand over the whole conversation so the model judges the trajectory,
        # not one isolated sentence.
        analysis = session.coach.analyze_customer_message(
            text, session.state.history
        )
    except Exception as error:
        session.persist()            # keep the case even if the model failed
        return failure(error)
    elapsed = int((time.perf_counter() - started) * 1000)
    session.calls += 1

    session.state.sentiment = analysis.get("sentiment", "unknown")
    session.state.urgency = analysis.get("urgency", "unknown")
    session.state.escalation_risk = analysis.get("escalation_risk", "unknown")
    session.state.key_issue = analysis.get("key_issue", "")
    session.state.frustration = analysis.get(
        "frustration", FALLBACK_SCORE.get(session.state.escalation_risk, 35)
    )
    session.state.trend = analysis.get("trend", "flat")
    note_redactions(session)
    session.trajectory.append(session.state.frustration)

    # Query the order system now rather than waiting for the agent to reply,
    # so the facts are on screen while they are still typing.
    try:
        session.facts = session.coach.gather_facts(
            text, session.state.history, allow_writes=session.allow_writes
        )
    except Exception:
        session.facts = []          # a lookup failing must not lose the turn

    # Can we just answer this, without an agent ever seeing it?
    session.auto_reply = None
    try:
        auto = try_auto_resolve(text)
    except Exception:
        auto = None                 # never let this break an ordinary turn

    if auto:
        session.auto_reply = auto
        session.calls += 1
        # The automatic answer is a real reply, so it goes in the transcript
        # and it stops the first-response clock. Deflection answers instantly,
        # which is rather the point of it.
        session.state.add_message("agent", auto["reply"])
        # An automatic answer is only ever sent when an article matched.
        open_rating_slot(session, True, auto.get("topic"))
        session.last_suggestion = auto["reply"]
        if session.first_response_at is None:
            session.first_response_at = now_iso()
        session.persist(status="auto_resolved", closed_at=now_iso())

    else:
        # Draft a reply NOW, on the customer's turn.
        #
        # This used to wait until the agent had already typed something, which
        # is backwards: by then they have done the work the draft was meant to
        # save. The agent should open the case and find a reply waiting, ready
        # to send, edit, or ignore.
        try:
            session.last_suggestion = session.coach.suggest_reply(
                text,
                session.state.history,
                analysis={
                    "sentiment": session.state.sentiment,
                    "urgency": session.state.urgency,
                    "key_issue": session.state.key_issue,
                },
                facts=session.facts,
            )
            session.calls += 1
            note_redactions(session)

            article = getattr(session.coach, "last_article", None)
            open_rating_slot(session, article is not None,
                             article.get("topic") if article else None)
        except Exception:
            # A draft failing must not cost the agent the analysis, the
            # lookups, or the turn itself.
            session.last_suggestion = ""

        session.persist(reopen=True)

    return jsonify({
        "ok": True,
        "analysis": analysis,
        "latency_ms": elapsed,
        "model_used": getattr(session.coach, "last_model_used", session.coach.model),
        "facts": session.facts,
        "auto_reply": session.auto_reply,
        "suggestion": session.last_suggestion,
        "state": session.as_dict(),
    })


@app.post("/api/agent")
def agent_message():
    text = (request.json or {}).get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Empty message."}), 400

    if not session.last_customer_message:
        return jsonify({
            "ok": False,
            "error": "There is no customer message to score this against yet.",
        }), 400

    session.state.add_message("agent", text)

    # The moment the customer first hears back stops the SLA clock.
    if session.first_response_at is None:
        session.first_response_at = now_iso()

    started = time.perf_counter()
    try:
        feedback = session.coach.evaluate_agent_response(
            session.last_customer_message, text
        )
        # Hand over the analysis we already computed on the customer's turn,
        # so the draft is written for THIS customer's mood -- and let
        # suggest_reply quote the matching help article.
        suggestion = session.coach.suggest_reply(
            session.last_customer_message,
            session.state.history,
            analysis={
                "sentiment": session.state.sentiment,
                "urgency": session.state.urgency,
                "key_issue": session.state.key_issue,
            },
            facts=session.facts,
        )
    except Exception as error:
        session.persist()
        return failure(error)
    note_redactions(session)
    elapsed = int((time.perf_counter() - started) * 1000)
    session.calls += 2

    session.last_feedback = {
        "tone_score": feedback.tone_score,
        "empathy_score": feedback.empathy_score,
        "clarity_score": feedback.clarity_score,
        "coaching_tip": feedback.coaching_tip,
    }
    session.last_suggestion = suggestion
    session.last_latency_ms = elapsed

    article = getattr(session.coach, "last_article", None)
    open_rating_slot(session, article is not None,
                     article.get("topic") if article else None)
    session.persist()

    return jsonify({
        "ok": True,
        "feedback": session.last_feedback,
        "suggestion": suggestion,
        "latency_ms": elapsed,
        "model_used": getattr(session.coach, "last_model_used", session.coach.model),
        "state": session.as_dict(),
    })


@app.post("/api/allow-writes")
def set_allow_writes():
    """Turn human approval for data-changing actions on or off.

    Off by default. With it off, the model may still ASK for a refund -- the
    request is recorded and left uncarried out.
    """
    session.allow_writes = bool((request.json or {}).get("allow", False))
    return jsonify({"ok": True, "allow_writes": session.allow_writes})


@app.post("/api/rate")
def rate_suggestion():
    """Thumbs up or down on the suggestion currently on screen."""
    rating = (request.json or {}).get("rating")
    if rating not in ("up", "down", None):
        return jsonify({"ok": False, "error": "rating must be up or down."}), 400

    if not session.ratings:
        return jsonify({"ok": False, "error": "No suggestion to rate yet."}), 400

    # Rating the same suggestion again replaces the earlier verdict rather
    # than counting twice.
    session.ratings[-1]["rating"] = rating
    session.ratings[-1]["rated_at"] = now_iso()
    session.persist()

    return jsonify({"ok": True, "ratings": session.ratings,
                    "state": session.as_dict()})


@app.get("/api/state")
def get_state():
    return jsonify({"ok": True, "state": session.as_dict()})


@app.post("/api/reset")
def reset():
    """Start a new conversation. Anything already said stays as a pending case."""
    if session.case_id:
        session.persist()
    session.reset()
    return jsonify({"ok": True, "state": session.as_dict()})


@app.post("/api/open-case")
def open_case():
    """Load a saved case into the console so the agent can carry on with it."""
    case_id = (request.json or {}).get("id", "")

    # Whatever is open now must be saved before we swap it out.
    if session.case_id and session.case_id != case_id:
        session.persist()

    for case in load_cases():
        if case.get("id") == case_id:
            session.load(case)
            return jsonify({"ok": True, "state": session.as_dict()})

    return jsonify({"ok": False, "error": f"No case {case_id}."}), 404


@app.post("/api/resolve")
def resolve():
    """Mark the open case resolved, then start a fresh one."""
    if not session.case_id:
        return jsonify({"ok": False, "error": "Nothing to resolve yet."}), 400

    record = session.persist(status="resolved", closed_at=now_iso())
    session.reset()
    return jsonify({"ok": True, "resolved": record["id"], "state": session.as_dict()})


# ==========================================================================
# API -- the dashboard
# ==========================================================================
def daily_counts(cases, min_days=7, max_days=30):
    """Cases opened per day, split by their status now.

    Days with no cases are filled in with zeros -- a gap in the middle of a
    bar chart is information, and leaving those days out would quietly
    compress the time axis and make the volume look steadier than it was.
    """
    buckets = defaultdict(lambda: {"resolved": 0, "pending": 0})

    for case in cases:
        opened = case.get("opened_at")
        if not opened:
            continue
        try:
            day = datetime.fromisoformat(opened).astimezone(UTC).date()
        except ValueError:
            continue          # a timestamp we cannot read is not worth a crash

        side = "resolved" if case.get("status") == "resolved" else "pending"
        buckets[day.isoformat()][side] += 1

    if not buckets:
        return []

    today = datetime.now(UTC).date()
    first = min(date.fromisoformat(k) for k in buckets)

    # Show at least a week so the chart has shape, and never more than a
    # month so the bars stay readable.
    span = max(min_days, min((today - first).days + 1, max_days))
    start = today - timedelta(days=span - 1)

    out, cursor = [], start
    while cursor <= today:
        counts = buckets.get(cursor.isoformat(), {"resolved": 0, "pending": 0})
        out.append({
            "day": cursor.isoformat(),
            "resolved": counts["resolved"],
            "pending": counts["pending"],
            "total": counts["resolved"] + counts["pending"],
        })
        cursor += timedelta(days=1)

    return out


# Worst first: an angry customer waiting is more urgent than a calm one
# waiting the same length of time.
RISK_ORDER = {"high": 3, "medium": 2, "low": 1}


def work_queue(cases, limit=8):
    """Open cases, ranked by what an agent should pick up next.

    Sorted by escalation risk first, then by age -- so the oldest of the
    riskiest conversations sits at the top.
    """
    now = datetime.now(UTC)
    waiting = []

    for case in cases:
        if case.get("status") in ("resolved", "auto_resolved"):
            continue

        opened = parse_time(case.get("opened_at"))
        waiting.append({
            "id": case.get("id"),
            "escalation_risk": case.get("escalation_risk"),
            "frustration": case.get("frustration", 0),
            "turns": case.get("turns", 0),
            "opened_at": case.get("opened_at"),
            "age_seconds": int((now - opened).total_seconds()) if opened else 0,
            "key_issue": case.get("key_issue", ""),
            "first_message": next(
                (m["text"] for m in case.get("messages", [])
                 if m.get("speaker") == "customer"), ""),
            "sla": sla_for(case, now),
            "awaiting_reply": case.get("first_response_at") is None,
        })

    waiting.sort(key=lambda row: (
        -RISK_ORDER.get(row["escalation_risk"], 0),   # riskiest first
        -row["age_seconds"],                          # then oldest first
    ))

    return {"queue": waiting[:limit], "total": len(waiting)}


def sla_summary(cases, limit=6):
    """Counts by SLA status, plus the cases that need attention first."""
    now = datetime.now(UTC)
    counts = {"met": 0, "breached": 0, "at_risk": 0,
              "on_track": 0, "unknown": 0}
    waiting = []

    for case in cases:
        sla = sla_for(case, now)
        counts[sla["status"]] = counts.get(sla["status"], 0) + 1

        # Only OPEN cases can still be saved, so they are the ones worth
        # putting in front of somebody.
        if (case.get("status") not in ("resolved", "auto_resolved")
                and sla["status"] in ("breached", "at_risk")):
            waiting.append({
                "id": case.get("id"),
                "escalation_risk": case.get("escalation_risk"),
                "key_issue": case.get("key_issue", ""),
                "first_message": next(
                    (m["text"] for m in case.get("messages", [])
                     if m.get("speaker") == "customer"), ""),
                "sla": sla,
            })

    # Worst first: most overdue at the top.
    waiting.sort(key=lambda row: row["sla"]["remaining_seconds"] or 0)

    answered = counts["met"] + counts["breached"]
    return {
        "counts": counts,
        "on_time_rate": round(100 * counts["met"] / answered) if answered else 0,
        "breaching": waiting[:limit],
        "breaching_total": len(waiting),
    }


# Coaching tips are free text, so we group them the same honest way the
# knowledge base does: keyword overlap. A tip can land in more than one theme,
# which is correct -- "add a greeting and give a timeline" really is both.
TIP_THEMES = [
    ("Open with a greeting",
     ["greeting", "greet", "hello", "opening line", "open with"]),
    ("Acknowledge the problem first",
     ["acknowledge", "empathy", "empathetic", "understand", "frustration",
      "stress", "feelings", "validate", "reassure"]),
    ("Give a specific timeline",
     ["timeline", "timeframe", "time frame", "how long", "specific date",
      "deadline", "when the", "within"]),
    ("Apologise for the delay",
     ["apolog", "sorry", "delay", "wait time", "kept waiting"]),
    ("Be specific, not generic",
     ["specific", "generic", "actual issue", "personalise", "personalize",
      "vague", "tailor", "detail"]),
    ("Do not ask for details too soon",
     ["before asking", "straight to", "jumping", "instead of asking",
      "rather than asking", "immediately asking"]),
]


def suggestion_quality(cases):
    """How often the agent actually accepted what we suggested.

    Split by whether a help article was found, because that is the thing we
    most want to know: does grounding a suggestion in real documentation make
    an agent more likely to use it?
    """
    groups = {"grounded": {"up": 0, "down": 0},
              "ungrounded": {"up": 0, "down": 0}}
    produced = rated = 0

    for case in cases:
        for entry in case.get("ratings") or []:
            produced += 1
            verdict = entry.get("rating")
            if verdict not in ("up", "down"):
                continue
            rated += 1
            bucket = "grounded" if entry.get("grounded") else "ungrounded"
            groups[bucket][verdict] += 1

    def rate(counts):
        total = counts["up"] + counts["down"]
        return {"up": counts["up"], "down": counts["down"], "total": total,
                "rate": round(100 * counts["up"] / total) if total else None}

    overall = {"up": sum(g["up"] for g in groups.values()),
               "down": sum(g["down"] for g in groups.values())}

    return {
        "produced": produced,
        "rated": rated,
        "overall": rate(overall),
        "grounded": rate(groups["grounded"]),
        "ungrounded": rate(groups["ungrounded"]),
    }


def performance(cases):
    """How the agent is doing, and which way it is going.

    Every case carries at most one scorecard, so a "point" here is one scored
    conversation rather than one reply. With a handful of cases that is the
    honest unit -- pretending to per-reply resolution would be inventing
    detail we do not have.
    """
    scored = [c for c in cases if c.get("feedback")]
    scored.sort(key=lambda c: c.get("opened_at") or "")

    fields = ("tone_score", "empathy_score", "clarity_score")
    short = {"tone_score": "tone", "empathy_score": "empathy",
             "clarity_score": "clarity"}

    if not scored:
        return {"scored": 0, "averages": {}, "by_day": [],
                "trend": {}, "themes": [], "enough_for_trend": False}

    def mean(rows, field):
        values = [r["feedback"].get(field) for r in rows
                  if isinstance(r["feedback"].get(field), (int, float))]
        return round(sum(values) / len(values), 2) if values else None

    # ---- averaged per day, skipping days with no scores ----
    buckets = {}
    for case in scored:
        day = (case.get("opened_at") or "")[:10]
        buckets.setdefault(day, []).append(case)

    by_day = []
    for day in sorted(buckets):
        rows = buckets[day]
        by_day.append({
            "day": day, "n": len(rows),
            **{short[f]: mean(rows, f) for f in fields},
        })

    # ---- which way is it going? ----
    # Compare the older half against the newer half. With an odd count the
    # middle case is left out of both, so it cannot skew either side.
    trend = {}
    enough = len(scored) >= 4
    if enough:
        half = len(scored) // 2
        older, newer = scored[:half], scored[-half:]
        for field in fields:
            before, after = mean(older, field), mean(newer, field)
            if before is None or after is None:
                continue
            delta = round(after - before, 2)
            trend[short[field]] = {
                "before": before, "after": after, "delta": delta,
                "direction": "improving" if delta >= 0.5
                             else "declining" if delta <= -0.5
                             else "steady",
            }

    # ---- what the coach keeps saying ----
    themes = []
    for label, keywords in TIP_THEMES:
        hits = [c for c in scored
                if any(word in (c["feedback"].get("coaching_tip") or "").lower()
                       for word in keywords)]
        if hits:
            themes.append({
                "label": label, "count": len(hits),
                "example": hits[-1]["feedback"].get("coaching_tip", ""),
                "cases": [h["id"] for h in hits][:4],
            })
    themes.sort(key=lambda t: -t["count"])

    return {
        "scored": len(scored),
        "averages": {short[f]: mean(scored, f) for f in fields},
        "by_day": by_day,
        "trend": trend,
        "enough_for_trend": enough,
        "themes": themes,
        "tips_total": len(scored),
    }


@app.get("/api/performance")
def performance_view():
    cases = load_cases()
    return jsonify({"ok": True, **performance(cases),
                    "suggestions": suggestion_quality(cases)})


@app.get("/api/stats")
def stats():
    """Everything the dashboard needs, counted server-side."""
    cases = load_cases()

    resolved = [c for c in cases if c.get("status") == "resolved"]
    deflected = [c for c in cases if c.get("status") == "auto_resolved"]
    pending = [c for c in cases
               if c.get("status") not in ("resolved", "auto_resolved")]

    def count_by(field, values, source):
        return {v: sum(1 for c in source if c.get(field) == v) for v in values}

    total_turns = sum(c.get("turns", 0) for c in cases)
    total_calls = sum(c.get("calls", 0) for c in cases)

    return jsonify({
        "ok": True,
        "total": len(cases),
        "resolved": len(resolved),
        "pending": len(pending),
        "deflected": len(deflected),
        "deflection_rate": round(100 * len(deflected) / len(cases)) if cases else 0,
        "resolution_rate": round(
            100 * (len(resolved) + len(deflected)) / len(cases)) if cases else 0,
        "high_risk_pending": sum(
            1 for c in pending if c.get("escalation_risk") == "high"
        ),
        "by_risk": count_by("escalation_risk", ["low", "medium", "high"], cases),
        "by_sentiment": count_by(
            "sentiment", ["positive", "neutral", "negative"], cases
        ),
        "pending_by_risk": count_by(
            "escalation_risk", ["low", "medium", "high"], pending
        ),
        "sla": sla_summary(cases),
        "work_queue": work_queue(cases),
        "by_day": daily_counts(cases),
        "avg_turns": round(total_turns / len(cases), 1) if cases else 0,
        "total_calls": total_calls,
        "open_case": session.case_id,
    })


def filter_cases(cases, query="", status="all", risk="all", sentiment="all"):
    """Narrow the case list. Every filter defaults to "all" = no filtering.

    The search runs over the case id, the key issue AND every message, which
    is why it lives here rather than in the browser: /api/cases deliberately
    strips the transcript out of its response, so the front end never has the
    full text to search.
    """
    query = (query or "").strip().lower()
    kept = []

    for case in cases:
        if status != "all" and case.get("status") != status:
            continue
        if risk != "all" and case.get("escalation_risk") != risk:
            continue
        if sentiment != "all" and case.get("sentiment") != sentiment:
            continue

        if query:
            haystack = " ".join([
                str(case.get("id", "")),
                str(case.get("key_issue", "")),
                *(m.get("text", "") for m in case.get("messages", [])),
            ]).lower()
            if query not in haystack:
                continue

        kept.append(case)

    return kept


def read_filters():
    """Pull the four filter values off the query string."""
    return {
        "query": request.args.get("q", ""),
        "status": request.args.get("status", "all"),
        "risk": request.args.get("risk", "all"),
        "sentiment": request.args.get("sentiment", "all"),
    }


@app.get("/api/faqs")
def faqs():
    """The canned questions, each with how many saved cases look like it.

    Matching is deliberately simple keyword overlap -- the same honest
    approach the notebook's knowledge base uses.
    """
    cases = load_cases()

    def haystack(case):
        parts = [case.get("key_issue", "")]
        parts += [m.get("text", "") for m in case.get("messages", [])]
        return " ".join(parts).lower()

    blobs = [haystack(c) for c in cases]

    rows = []
    for faq in FAQS:
        count = sum(
            1 for blob in blobs
            if any(word in blob for word in faq["keywords"])
        )
        rows.append({
            "id": faq["id"], "icon": faq["icon"], "label": faq["label"],
            "text": faq["text"], "count": count,
        })

    # Busiest topic first on the dashboard; the console keeps its own order.
    return jsonify({
        "ok": True,
        "faqs": rows,
        "ranked": sorted(rows, key=lambda r: -r["count"]),
        "matched": sum(1 for b in blobs if any(
            w in b for f in FAQS for w in f["keywords"])),
        "total_cases": len(cases),
    })


@app.get("/api/cases")
def list_cases():
    """Recent cases, newest first, without the full message transcript."""
    limit = int(request.args.get("limit", 25))
    all_cases = load_cases()
    cases = filter_cases(all_cases, **read_filters())

    slim = []
    for case in reversed(cases):
        row = {k: v for k, v in case.items() if k != "messages"}
        first = next(
            (m["text"] for m in case.get("messages", [])
             if m["speaker"] == "customer"),
            "",
        )
        row["first_message"] = first
        row["is_open"] = case.get("id") == session.case_id
        row["sla"] = sla_for(case)
        slim.append(row)

    return jsonify({
        "ok": True,
        "cases": slim[:limit],
        "shown": min(limit, len(slim)),
        "matched": len(slim),        # after filtering
        "total": len(all_cases),     # before filtering
    })


@app.get("/api/gaps")
def knowledge_gaps():
    """Every question our documentation could not answer, most asked first."""
    grouped = {}

    for case in load_cases():
        for entry in case.get("unanswered", []):
            text = (entry.get("text") or "").strip()
            if not text:
                continue

            # Group on a squashed version so the same question asked twice
            # counts twice, but we still display it as it was actually typed.
            key = " ".join(text.lower().split())
            row = grouped.setdefault(key, {
                "text": text, "count": 0, "cases": [], "last_seen": None,
            })
            row["count"] += 1
            if case.get("id") not in row["cases"]:
                row["cases"].append(case.get("id"))
            seen = entry.get("at") or case.get("opened_at")
            if seen and (row["last_seen"] is None or seen > row["last_seen"]):
                row["last_seen"] = seen

    rows = sorted(
        grouped.values(),
        key=lambda r: (-r["count"], r["last_seen"] or ""),
    )

    total_questions = sum(
        1 for c in load_cases()
        for m in c.get("messages", []) if m.get("speaker") == "customer"
    )

    return jsonify({
        "ok": True,
        "gaps": rows,
        "distinct": len(rows),
        "logged": sum(r["count"] for r in rows),
        "customer_messages": total_questions,
    })


@app.get("/api/export.json")
def export_json():
    """Everything, in the exact shape cases.json always had.

    The store is SQLite now, but this format is still what the migration
    reads and what a backup looks like, so it stays supported.
    """
    cases = filter_cases(load_cases(), **read_filters())
    stamp = datetime.now().strftime("%Y-%m-%d")
    return Response(
        json.dumps({"cases": cases}, indent=1),
        mimetype="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="support-coach-cases-{stamp}.json"'},
    )


@app.get("/api/cases.csv")
def export_csv():
    """Download the cases as a spreadsheet.

    With no filters this is every case, which is the normal use. If filters
    are active it exports exactly what you are looking at, so what you see is
    what you get.
    """
    cases = filter_cases(load_cases(), **read_filters())

    columns = [
        "id", "status", "opened_at", "closed_at", "escalation_risk",
        "frustration", "trend", "sentiment", "urgency", "turns", "calls",
        "key_issue", "first_customer_message",
        "tone_score", "empathy_score", "clarity_score", "coaching_tip",
        "suggested_reply",
    ]

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()

    for case in cases:
        feedback = case.get("feedback") or {}
        first = next(
            (m["text"] for m in case.get("messages", [])
             if m.get("speaker") == "customer"),
            "",
        )
        writer.writerow({
            **{k: case.get(k, "") for k in columns},
            "first_customer_message": first,
            "tone_score": feedback.get("tone_score", ""),
            "empathy_score": feedback.get("empathy_score", ""),
            "clarity_score": feedback.get("clarity_score", ""),
            "coaching_tip": feedback.get("coaching_tip", ""),
            "suggested_reply": case.get("suggestion", "") or "",
        })

    stamp = datetime.now().strftime("%Y-%m-%d")
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition":
                f'attachment; filename="support-coach-cases-{stamp}.csv"'
        },
    )


@app.get("/api/cases/<case_id>")
def one_case(case_id):
    for case in load_cases():
        if case.get("id") == case_id:
            return jsonify({"ok": True, "case": case})
    return jsonify({"ok": False, "error": "No such case."}), 404


# ==========================================================================
# Start-up
# ==========================================================================
def find_free_port(preferred, attempts=12):
    """Return the first free port at or after `preferred`, or None."""
    for offset in range(attempts):
        port = preferred + offset
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            probe.close()
    return None


def who_has_the_port(port):
    """Best-effort: name the process holding a port, for the error message."""
    try:
        output = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=4,
            check=False,          # lsof exits non-zero when nothing matches

        ).stdout.strip().splitlines()
        if len(output) > 1:
            columns = output[1].split()
            return f"{columns[0]} (pid {columns[1]})"
    except Exception:
        pass
    return "another program"


if __name__ == "__main__":

    # Port 5000 is taken by AirPlay Receiver on macOS, so we start at 5001.
    # Override with:  PORT=8080 python3 app/server.py
    preferred = int(os.getenv("PORT", "5001"))
    port = find_free_port(preferred)

    if port is None:
        print(f"\n  Could not find a free port between {preferred} and "
              f"{preferred + 11}.")
        print("  Free one up, or choose your own:  PORT=8080 python3 app/server.py\n")
        raise SystemExit(1)

    imported = migrate_from_json()
    key_file = coach_core.find_key_file()
    saved = load_cases()

    print()
    print("  AI Support Coach")
    print("  " + "-" * 46)
    print(f"  key file : {key_file or 'NOT FOUND - see gemini_api_key.txt'}")
    print(f"  model    : {os.getenv('GEMINI_MODEL', 'gemini-3.5-flash')}")
    print(f"  store    : sqlite  {os.path.basename(CASES_DB)}")
    if imported:
        print(f"  migrated : {imported} case(s) imported from cases.json")
    print(f"  cases    : {len(saved)} saved "
          f"({sum(1 for c in saved if c.get('status') == 'resolved')} resolved)")

    if port != preferred:
        print(f"  note     : port {preferred} is busy "
              f"({who_has_the_port(preferred)}), using {port} instead")
        print("             to reclaim it:  pkill -f app/server.py")

    print(f"  console  : http://127.0.0.1:{port}")
    print(f"  dashboard: http://127.0.0.1:{port}/dashboard")
    print()

    app.run(host="127.0.0.1", port=port, debug=False)
