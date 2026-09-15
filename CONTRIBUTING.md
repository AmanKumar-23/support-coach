# Contributing

Thanks for taking the time. This guide gets you from a clone to a merged pull request.

---

## The one rule that trips everybody up

**`app/coach_core.py` is generated from the notebook. Never edit it by hand.**

```
customer_support_coach.ipynb  ──  python3 app/build_core.py  ──►  app/coach_core.py
```

The notebook is the source of truth: it holds the coaching engine *and* the written analysis
that justifies it. `build_core.py` slices the engine cells out and writes the module the Flask
app imports.

So the loop for any engine change is:

1. Edit the **notebook** cell.
2. Run `python3 app/build_core.py`.
3. Commit **both** the notebook and the regenerated `app/coach_core.py`.

CI regenerates the file and fails the build if it differs from what you committed. A hand-edit
gets wiped the next time anyone rebuilds, and the app silently stops matching the notebook —
which is exactly the class of bug this check exists to prevent.

Changes to `app/server.py`, `app/static/*` and `tests/` are ordinary edits. Only the engine is
generated.

---

## Setting up

```bash
git clone https://github.com/AmanKumar-23/support-coach.git
cd support-coach

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

cp gemini_api_key.example.txt gemini_api_key.txt   # paste your key after the "="
cp orders.example.json orders.json

pytest tests/ -q          # should be green before you change anything
python3 app/server.py     # http://127.0.0.1:5001
```

A free key comes from [Google AI Studio](https://aistudio.google.com/apikey). You do **not**
need one to run the tests.

---

## Tests

The suite runs with **no API key**, on purpose: CI needs no secret, and a pull request from a
fork runs the whole thing.

```bash
pytest tests/ -v
```

If your change touches behaviour, it needs a test. The bar we hold ourselves to:

> **A regression test must fail against the broken code.** Write the test, reintroduce the bug,
> watch it go red, then put the fix back. A test that passes either way is documentation, not a
> test.

Anything that would call Gemini gets stubbed — see `tests/test_tool_calling.py` for the pattern,
and the fixtures in `tests/conftest.py` (`srv` gives you a temp database, `back_office` an
in-memory order system so the suite never writes to the real `orders.json`).

## Style

```bash
ruff check app/ tests/
```

Rules are pinned in `pyproject.toml`, so a new ruff release cannot fail your
build on its own. `app/coach_core.py` is excluded because it is generated.

There is deliberately **no formatter**. The codebase is hand-aligned and
`ruff format` would rewrite every file, so please match the surrounding style
rather than reformatting code you are not otherwise changing.

Comments here explain **why**, not what. If a number was measured — a similarity threshold, an
SLA target — say what it was measured against, the way the existing ones do.

---

## Commit messages

We use [Conventional Commits](https://www.conventionalcommits.org/). The format is:

```
<type>(<optional scope>): <summary in the imperative, lower case, no full stop>

<optional body: why, not what>

<optional footer: Fixes #12 / BREAKING CHANGE: ...>
```

| Type | Use it for |
|---|---|
| `feat` | A new capability a user can see |
| `fix` | A bug fix |
| `docs` | Documentation only |
| `test` | Adding or correcting tests |
| `refactor` | Behaviour unchanged |
| `perf` | Makes it faster or cheaper |
| `chore` | Tooling, dependencies, CI |
| `style` | Formatting only |

Useful scopes: `coach`, `server`, `console`, `dashboard`, `kb`, `notebook`, `ci`.

**Good:**

```
fix(coach): send function results back as role="user"

The API rejects role="tool" outright, so round 1 of the tool loop
worked and every round after it 400'd. The model could never chain
one lookup into the next, and the failure was swallowed into a
"(lookup unavailable)" note, so nothing reached the log.

Fixes #41
```

```
feat(dashboard): add a deflection-rate KPI
docs(readme): document the auto-resolve threshold
test(kb): cover paraphrases that keyword matching missed
```

**Not useful:** `update`, `fixes`, `final version`, `asdf`, `changes as discussed`.

One logical change per commit. If the summary needs an "and", it is probably two commits.

---

## Branching

`main` is always releasable. Work happens on short-lived branches off it.

```
main ─────●────────────●─────────────●──────►
           \          /             /
            ●────────●  feat/sla-panel
                        \           /
                         ●─────────●  fix/tool-loop-role
```

| Prefix | For |
|---|---|
| `feat/` | New capability — `feat/voice-input` |
| `fix/` | Bug fix — `fix/tool-loop-role` |
| `docs/` | Documentation — `docs/architecture` |
| `chore/` | Tooling — `chore/bump-google-genai` |

```bash
git switch main && git pull
git switch -c fix/tool-loop-role
# ... work, committing as you go ...
git push -u origin fix/tool-loop-role
```

Rebase onto `main` rather than merging it in, so history stays linear:

```bash
git fetch origin && git rebase origin/main
```

Pull requests are **squash-merged**, so the PR title becomes the commit on `main` — give it a
Conventional Commit title too.

---

## Pull requests

Before you open one:

- [ ] `pytest tests/ -q` is green
- [ ] `ruff check app/ tests/` is clean
- [ ] If you touched the notebook, you ran `python3 app/build_core.py` and committed the result
- [ ] No API key, database or real transcript in the diff
- [ ] Screenshots for anything visual

Then fill in the template. Small and focused beats big and thorough — a 40-line pull request
gets reviewed today, a 2,000-line one gets reviewed eventually.

---

## What is worth working on

- **Help articles.** The dashboard's "questions we cannot answer" panel is a live to-do list.
- **More languages.** The Hinglish finding almost certainly repeats in Tamil, Bengali and
  Marathi. Measuring it is a genuine contribution.
- **Threshold tuning** on a larger sample — the current numbers come from a small set and say so.
- **Accessibility** in the console and dashboard.

Opening an issue to discuss first is welcome, especially for anything large.

---

## Reporting security problems

Please do **not** open a public issue. See [SECURITY.md](SECURITY.md).
