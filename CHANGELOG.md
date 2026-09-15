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

- Regression tests for the function-calling loop, including an assertion that every turn carries
  a role the API accepts.

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

[Unreleased]: https://github.com/YOUR-USERNAME/support-coach/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/YOUR-USERNAME/support-coach/releases/tag/v0.1.0
