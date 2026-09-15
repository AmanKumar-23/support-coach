# Architecture

How the pieces fit, and why they are arranged this way.

---

## The notebook is the source of truth

Most projects would treat a notebook as a scratchpad and the application as the real code. This
one is the other way round, on purpose.

```
customer_support_coach.ipynb        37 cells: the engine AND the written analysis
            │
            │   python3 app/build_core.py
            ▼
app/coach_core.py                   generated — never edited by hand
            │
            │   import
            ▼
app/server.py                       Flask: routes, SLA, analytics, storage
```

The notebook carries the reasoning as well as the code: the Hinglish comparison, the measured
similarity thresholds, the before/after runs that justify each design decision. Those outputs are
the evidence for the project's central claim, so they are committed with the notebook rather than
stripped.

If the engine lived only in `app/`, the notebook would drift into a stale copy within a week —
and a reader could no longer trust that the printed results came from the code that ships.

### What `build_core.py` extracts

| Slice | Notebook cell | Contents |
|---|---|---|
| Key helper | 1 | `get_gemini_api_key()`, `find_key_file()`, key-file search |
| Knowledge | 17 | `KNOWLEDGE_BASE`, embeddings, cosine similarity, `find_kb_article()` |
| Data models | 26 | `Message`, `CoachingFeedback`, `ConversationState` |
| Engine | 28 | Response schemas, `Redactor`, back-office tools, `AICoach` |

Imports are read out of the notebook rather than hardcoded, after a stale hardcoded banner once
dropped `import time` and produced a `NameError` that only appeared when a retry fired. A
`symtable` pass now warns about any global the generated module references but never defines.

**CI enforces this.** The `notebook-sync` job regenerates `coach_core.py` and fails if the result
differs from the committed file.

---

## Request flow

A customer message arriving at `POST /api/customer`:

```
  browser
     │  { "message": "mera recharge fail ho gaya" }
     ▼
  server.py
     │
     ├─► 1. try_auto_resolve()      KB match ≥ 0.65 and low risk?  ──► answer, close, done
     │
     ├─► 2. AICoach.analyze_customer_message(msg, history)
     │         redact ─► Gemini (structured output) ─► sentiment, urgency, risk, key issue
     │
     ├─► 3. AICoach.gather_facts(msg, history)
     │         function-calling loop, up to 3 rounds:
     │           model names a function ─► we run it ─► hand the result back ─► repeat
     │         writes (issue_refund, send_password_reset) are recorded, NOT run,
     │         unless allow_writes is on
     │
     ├─► 4. find_kb_article(msg)    embeddings + cosine, keyword fallback
     │
     ├─► 5. AICoach.suggest_reply(msg, history, analysis, facts)
     │         grounded in the article and the real lookup results
     │         ─► restore the redacted values back into the draft
     │
     └─► 6. save_case()             SQLite
              │
              ▼
          { analysis, facts, article, suggestion, sla }
```

Each of the numbered steps is one Gemini call except step 3, which is a loop.

### The redaction boundary

```
   customer text ──► Redactor.redact() ──► Gemini ──► Redactor.restore() ──► agent
                          │                                  ▲
                     placeholders                       real values
                   ORDER_1, PHONE_1 …               put back locally
```

A fresh `Redactor` is created per call, so one customer's placeholders can never resolve to
another's details and the map cannot grow without bound in a long-running server. The map lives
only in this process; the real values are never sent.

Function *arguments* come back from the model holding placeholders, because placeholders are all
it was ever given. They are restored before the function runs — the lookup happens inside our own
process, where the real values were never a secret — and the *result* is redacted again on the
way back to the model.

---

## Resilience

`_call_model` is the single door to Gemini. Every path goes through it — plain text, structured
JSON and function calling alike.

| Failure | Response |
|---|---|
| `503`, `429`, `UNAVAILABLE`, `RESOURCE_EXHAUSTED` | Retry with backoff (1s, 2s, 4s), then fall back to `gemini-3.5-flash-lite` |
| `400`, `401`, `403`, `INVALID_ARGUMENT` | Fail immediately — the request is malformed, so every other model rejects it identically |
| A lookup fails | Recorded as `(lookup unavailable)`; the customer still gets a reply |
| Gemini returns non-JSON | `_parse_json` fallback, though structured output makes this rare |

Function calling has a tighter free-tier quota than ordinary generation, which is why it needs
this more than the other paths do.

---

## Storage

One SQLite table. Scalar columns are indexed for the queries the dashboard actually runs; the
full case — transcript, timeline, redaction log, scores — is kept as a JSON blob alongside them.

```sql
CREATE TABLE cases (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    id              TEXT UNIQUE NOT NULL,
    status          TEXT,
    escalation_risk TEXT,
    sentiment       TEXT,
    opened_at       TEXT,
    closed_at       TEXT,
    data            TEXT NOT NULL      -- the whole case as JSON
);
CREATE INDEX cases_status ON cases(status);
CREATE INDEX cases_risk   ON cases(escalation_risk);
CREATE INDEX cases_opened ON cases(opened_at);
```

That split keeps the dashboard's filters and aggregates in SQL while leaving the shape of a case
free to change without a migration for every field. `migrate_from_json()` imports the older
`cases.json` on first run; JSON and CSV export still work.

---

## The front end

Vanilla JavaScript and inline SVG — no framework, no build step, no `node_modules`.

| File | What it is |
|---|---|
| `app/static/index.html` | The live console the agent works in |
| `app/static/dashboard.html` | Ten panels, work queue first, plus the case detail drawer |

Charts are drawn as inline SVG in the page's own style rather than pulled from a charting
library, which keeps the whole front end two files a contributor can read end to end.

---

## Deliberate limits

This is a coursework and demonstration project, and it is worth being explicit about what it is
not:

- **No authentication or authorisation.** `server.py` binds to `127.0.0.1` on purpose.
- **The back office is a mock.** `orders.json` is a file, not an order management system.
- **Thresholds come from a small sample.** `0.58` and `0.65` were measured, and the measurements
  are in the notebook, but the sample is small enough that they should be re-measured before
  anyone relies on them.
- **One language pair tested.** The Hinglish finding is real and reproduced; whether it repeats
  in Tamil, Bengali or Marathi is untested, and measuring it would be a genuine contribution.
