# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **The function-calling loop could never chain two lookups.** Function results were sent back
  with `role="tool"`, which the Gemini API rejects outright (`Role 'tool' is not supported`).
  Round 1 worked, so the feature looked healthy in the UI, but every round after it returned
  400 — and the failure was swallowed into a `(lookup unavailable)` note, so nothing reached the
  log. Results now go back as `role="user"`, and the coach can read a failed order and then look
  up that order's refund.
- **A malformed request was retried against every model.** A `400 INVALID_ARGUMENT` means the
  request is wrong, not the model, so the fallback was guaranteed to fail identically — it only
  doubled the latency and quota spent before giving up. These now fail fast.

### Added

- **Accounts and roles.** Every page and every API route now sits behind a login and a role —
  `agent` (console, own cases), `lead` (+ dashboard, + the write-action gate), `admin`
  (+ bulk exports). Until now anyone who could reach the port could read every saved customer
  transcript, which sat badly next to a product that masks personal data before it reaches the
  model. See [`app/auth.py`](app/auth.py).
- Passwords stored as scrypt hashes, a fifteen-minute lockout after five failed attempts, and
  `HttpOnly` / `SameSite=Lax` session cookies. The cookie signing key is generated once and kept
  in the database, so a restart no longer signs everyone out.
- Account management from the command line — `--list-users`, `--add-user`, `--passwd`,
  `--disable-user`, `--enable-user`. Accounts cannot be created through the web app on purpose.
- Cases record the agent who opened them (`owner`), and an agent can only open their own or an
  unclaimed one. Cases from before this change are unowned and claimed by whoever opens them.
- A sign-in page, a "signed in but wrong role" page, and a user chip with sign-out in both front
  ends. Controls a role cannot use are hidden rather than left to fail with a 403.
- 47 tests covering role boundaries, lockout, ownership and the post-login redirect.
- Regression tests for the function-calling loop, including an assertion that every turn carries
  a role the API accepts.

- **Token metering and cost per conversation.** Every Gemini call now reports its own token
  counts, which are attributed to a case, an agent and a step (analyse, look up, draft, score,
  embed) and priced. The dashboard gained a **Cost** section, and each case shows its own bill.
  Counting API calls was the wrong unit: analysing one short message and drafting a grounded
  reply are both "one call" and differ by an order of magnitude. See [`app/metering.py`](app/metering.py).
- **Daily caps and a per-agent rate limit.** `DAILY_TOKEN_CAP`, `DAILY_COST_CAP_INR` and
  `RATE_LIMIT_CALLS_PER_MIN` refuse new work with a 429 and a `Retry-After` *before* the model
  is called. Any of them set to `0` is switched off.
- 26 tests for metering, pricing, the ceilings and the engine's usage hook.

### Changed

- The engine gained a `USAGE_HOOK` (notebook cells 1, 17 and 28). It announces the token cost
  of each call and knows nothing about storage, pricing or budgets — metering policy stays in
  the app, not in the coaching logic.
- A new case is given its id *before* the first model call, so the opening turn of every
  conversation is billed to a case instead of to nothing.
- `/api/health` stays public so a container probe can reach it, but now tells an anonymous
  caller only that the process is up — the key path and model name need a login.
- The `cases` table gained an `owner` column, applied to an existing database by a guarded
  migration on startup.

---

## [0.1.0] — 2026-09-15

First public version.

### Added

- **Coaching engine** (`AICoach`, generated from the notebook) — conversation-aware sentiment,
  urgency and escalation-risk analysis; agent scoring against a published 1–10 rubric for tone,
  empathy and clarity; drafted replies grounded in the knowledge base.
- **Semantic knowledge base** using Gemini embeddings and cosine similarity, with the keyword
  matcher kept as a fallback. Threshold `0.58`, measured rather than guessed.
- **Back-office tools** via Gemini function calling — `check_order_status`,
  `check_refund_status`, `issue_refund`, `send_password_reset` against a mock `orders.json`.
  Write-capable tools are recorded as requested and wait for human approval.
- **PII redaction** of phone numbers, emails, order ids and card numbers before any text reaches
  Gemini, on both the chat and embeddings paths, restored in the drafted reply.
- **Live console** — quick-pick chips, voice input in English and Hindi, escalation timeline,
  agent scorecard, suggested reply with 👍/👎.
- **Dashboard** — work queue, deflection rate, SLA breaches, issues over time, agent performance,
  suggestion acceptance, FAQ coverage, and a "questions we cannot answer" panel.
- **Auto-resolution** of confident, low-risk knowledge-base matches.
- **SLA tracking** with targets by escalation risk (15 min / 1 h / 4 h).
- **SQLite case store** with a migration from the previous `cases.json` and JSON/CSV export.
- **Test suite** that runs with no API key, and CI covering tests, lint, notebook/engine sync and
  a secret scan.

### The finding

Documented in the notebook: a standard multilingual sentiment model reads
`"bahut ganda service hai"` — plainly negative Hinglish — as **POSITIVE, 49.8% confidence**,
while the Gemini-backed analyser reads it as `negative` with frustration 85/100. Step 13
(English) escalates `35 → 35 → 43 → 94` and fires red; Step 17 (Hinglish) stays green throughout.
The escalation tracker was correct. The signal feeding it was not.

[Unreleased]: https://github.com/AmanKumar-23/support-coach/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/AmanKumar-23/support-coach/releases/tag/v0.1.0
