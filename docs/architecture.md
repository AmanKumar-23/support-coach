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
            │
            │   every route guarded by
            ▼
app/auth.py                         accounts, roles, sessions
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

## Access control

Every page and every API route is behind a login and a role. `app/auth.py` holds it; `server.py`
only decorates.

### Roles are ranked, not enumerated

```
agent  (1)  ──►  lead  (2)  ──►  admin  (3)
```

Each role is a superset of the one below, so a guard asks "at least `lead`?" rather than "in this
set of roles?". A set would let someone hold `admin` without holding `lead`, and every check
would have to remember to list both. Ranking makes that gap unrepresentable.

The whole policy is one dict in `server.py` — sixteen routes, each with the role it needs —
rather than a decorator argument scattered down nine hundred lines. It can be read, and audited,
in one screen.

### Two decisions worth explaining

**A case an agent does not own answers `404`, not `403`.** Telling somebody a case exists but is
not theirs still tells them it exists, along with how many there are and roughly when they were
opened. The dashboard roles see everything, so nothing is hidden from the people who need it.

**Accounts cannot be created through the web app.** They are a command-line operation. An
application that can mint its own admin is one request-handling bug away from having no roles at
all, and nothing in this product needs self-service sign-up.

### What the login does not cover

Session cookies are `HttpOnly` and `SameSite=Lax`. Lax is what stops a cross-site POST carrying
the cookie, which is the practical CSRF defence here — but it is not the same as per-request CSRF
tokens, and those are the next thing to add.

---

## Metering

Every Gemini call funnels through `AICoach._call_model()`. That is not a
coincidence -- it is the reason the retry and fallback logic works -- and it
makes it the one place token usage can be read from with no way for a new kind
of call to be added later and quietly escape it.

```
AICoach._call_model()  ──►  coach_core.report_usage()  ──►  USAGE_HOOK
                                                              │
                                              app/metering.py │  price it,
                                                              ▼  store it
                                                        usage table
```

The engine **announces**; it does not decide. `report_usage()` reads the token
counts off the response and hands them to whatever `USAGE_HOOK` is set to. The
notebook leaves it `None` and nothing happens; the app points it at
`metering.record`. So pricing, budgets and storage never end up inside the
coaching logic, and the notebook keeps running unchanged.

### Measured versus derived

Tokens come from the API's own `usage_metadata`. They are exact, and they are
the number to trust. Rupees are those tokens multiplied by a rate that is
**configuration** -- prices change, and the shipped defaults are placeholders.
The two are kept visibly separate: the dashboard labels every rupee figure
"estimated rates" until the rates are set explicitly.

### Attribution without threading a parameter

A request opens a billing account in a `ContextVar` before doing anything, and
names the case as soon as it knows which one it is. `record()` reads that
account at the moment a call completes. A `ContextVar` rather than a global
because Flask serves requests on many threads and two agents mid-conversation
must not be billed to each other -- and a mutable account rather than a fixed
pair because a brand new conversation has no id until part way through.

### Where the ceilings sit

The caps are checked at the **start of a turn**, in the route, not before each
individual call. Aborting half way through a tool-calling loop would leave the
customer with a reply quoting a lookup that never finished. One turn may
overshoot slightly; the next is refused with a 429.

---

## Deliberate limits

This is a coursework and demonstration project, and it is worth being explicit about what it is
not:

- **One live conversation for the whole server.** `LiveSession` is a module-level singleton, so
  two signed-in agents share a console. Roles decide what each may *reach*; they do not yet give
  each agent their own session. That is the next structural change, and the `owner` column on
  `cases` exists to meet it.
- **No per-request CSRF tokens.** `SameSite=Lax` cookies cover the realistic attack; tokens
  cover the rest.
- **The shipped token prices are placeholders.** Token counts are measured and correct; the
  rupee figures are only as good as the rates in `app/metering.py`, and the UI says so until
  they are set.
- **The caps are per process.** They read a shared SQLite table, so several workers on one
  machine stay consistent, but nothing coordinates two machines.
- **The back office is a mock.** `orders.json` is a file, not an order management system.
- **Thresholds come from a small sample.** `0.58` and `0.65` were measured, and the measurements
  are in the notebook, but the sample is small enough that they should be re-measured before
  anyone relies on them.
- **One language pair tested.** The Hinglish finding is real and reproduced; whether it repeats
  in Tamil, Bengali or Marathi is untested, and measuring it would be a genuine contribution.
