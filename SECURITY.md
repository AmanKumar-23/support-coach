# Security Policy

## Reporting a vulnerability

**Please do not open a public issue.**

Use GitHub's private reporting — **Security → Report a vulnerability** on this repository — or
email the maintainer listed on the GitHub profile.

Please include what the problem is, how to reproduce it, and what an attacker could do with it.
You can expect an acknowledgement within a few days. This is a student-maintained project, so
please be patient with the timeline; credit is given in the release notes unless you would
rather stay anonymous.

## If you have committed an API key

It happens. Speed matters more than tidiness:

1. **Revoke it first**, at <https://aistudio.google.com/apikey>. A key in git history is public
   the moment the repository is, and scrapers find them in minutes. Revoking is the only step
   that actually stops the damage.
2. Issue a new key and put it in `gemini_api_key.txt` (which is git-ignored).
3. Only then worry about scrubbing history — with
   [`git filter-repo`](https://github.com/newren/git-filter-repo) or the
   [BFG](https://rtyley.github.io/bfg-repo-cleaner/). Rewriting history does **not** un-leak a
   key that was already pushed. Step 1 is the one that counts.

## What this project protects

| Asset | How |
|---|---|
| `gemini_api_key.txt` | Git-ignored. The repo ships `gemini_api_key.example.txt` with an empty value. |
| `app/cases.db` | Git-ignored — it holds real conversation transcripts. |
| `orders.json` | Git-ignored. `orders.example.json` is committed instead. |
| Customer PII | Phone numbers, emails, order ids and card numbers are replaced with placeholders **before** any text reaches Gemini, on both the chat and the embeddings path, then restored in the drafted reply. |
| Refunds and password resets | `issue_refund` and `send_password_reset` are recorded as *requested* and never executed until a person turns on **allow actions**. Off by default. |
| Accidental commits | A CI job greps every commit for Google API-key patterns (`AIza…`, `AQ.…`) and fails the build, and checks that the key file, database and order file are untracked. |

## Scope

This is a coursework and demonstration project. It is **not hardened for production** and has no
authentication, authorisation, rate limiting or CSRF protection — `app/server.py` binds to
`127.0.0.1` deliberately. Do not expose it to the internet or point it at real customer data
without adding those first.

Reports about the lack of auth on a localhost demo are out of scope. Reports about **key
leakage, PII reaching the model, or a write-capable tool firing without approval** are very much
in scope — those are the properties this project actually claims.
