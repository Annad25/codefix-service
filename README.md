# codefix-service

An HTTP service that takes a repository snapshot and a task description, produces a code
change with an LLM, **verifies that change independently**, and returns it before the
deadline — or returns `null` rather than something it has not verified.

- `POST /solve` — the request contract from the brief; answers before `deadline_seconds`.
- `GET /health` — 200 when the service can accept work (readiness).
- `GET /livez` — 200 while the process is alive (liveness).

---

## Quick start

```bash
cp .env.example .env          # then put your key in it
docker compose up --build     # builds the sandbox image, then starts the service on :8000
curl localhost:8000/health
```

`docker compose up` builds `acceptance:latest` from `sandbox/Dockerfile` (a verbatim copy of
the challenge's `acceptance/Dockerfile`) and starts the service. The service runs repository
code only in **sibling containers** from that image, with networking disabled.

Run it against the public tasks with the challenge's own harness:

```bash
python harness/run_client.py --url http://localhost:8000 tasks/public/*
```

### Choosing the LLM back end

One environment variable; only that provider's key variable is ever read, so a key for a
different provider sitting in your environment can never be used or charged.

| `CODEFIX_LLM_PROVIDER` | Key variable | Endpoint | Default models |
|---|---|---|---|
| `openrouter` (default) | `OPENROUTER_API_KEY` | openrouter.ai/api/v1 | free models, with fallbacks |
| `openai` | `OPENAI_API_KEY` | api.openai.com/v1 | `gpt-5.2`, `gpt-5-mini` |
| `compatible` | `CODEFIX_LLM_API_KEY` | `CODEFIX_LLM_BASE_URL` | whatever you configure |

```bash
# the reviewer's own OpenAI key, nothing else to change
CODEFIX_LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
CODEFIX_MODEL_PRIMARY=gpt-5.2
```

The differences are handled inside the client: OpenAI needs `max_completion_tokens`
(`max_tokens` is invalid for its reasoning models) and top-level `reasoning_effort`, while
OpenRouter takes routing, cost and fallback options in `extra_body`. OpenRouter reports the
exact cost of each call; for OpenAI you can set `CODEFIX_PRICE_*_PER_M` to have cost computed,
and without them the response reports `cost_known: false` instead of a misleading `$0`.

**Which provider was used for the results, and why.** Everything in `results.md` was produced
on OpenRouter free models (`nex-agi/nex-n2.5-pro:free`), because it needs no spend, gives one
key for many models, reports exact per-call cost, and can route around a rate-limited model.
The trade-off is speed, and it shows in the numbers.

---

## Architecture

**One fixed pipeline, with the model used only where judgement is needed.** A request is
unpacked into a workspace, profiled (language, test command) and then handled by three steps
that overlap in time:

1. **Derived checks.** One call writes acceptance tests *from the task text alone*, before any
   fix exists. They never see a candidate, so they are independent evidence — the brief's
   "check derived from the task description", and the role reproduction tests play in
   Agentless and generated tests play in CodeT. They are an overlay: copied into test runs,
   never into the worktree or the delivered diff.
2. **The candidate.** One call writes the fix as SEARCH/REPLACE edit blocks. The blocks are
   applied through jailed tools, so the path jail, read-only test files and the syntax guard
   all apply. The candidate then runs the repository's own tests plus the derived checks in
   the sandbox, on a throwaway copy of its worktree.
3. **The delivery gate** (`app/gate/`), a separate code path that shares no state with the
   candidate. It re-extracts the **original request bytes**, applies the grader's static rules
   (a verbatim port of `static_check`, plus stricter rules of our own), runs `git apply
   --check` and `git apply` exactly as the grader does, then runs the checks again in the
   sandbox. **Nothing is delivered that this gate has not passed.** A failure becomes the
   feedback for the next repair round while time remains.

The gate returns a tier: **A** = repository tests and derived checks both pass; **B** =
repository tests pass but the derived checks failed or could not be produced; **C** = anything
earlier failed, never delivered. Tier A is always preferred; a tier B diff is delivered only
when no tier A exists by the deadline.

Everything is bounded by one `Deadline` object created when the request arrives: every model
call, container run and retry asks it how long it may take, a margin is held back for
transfer, and a watchdog returns the best gate-passed diff (or `null`) if anything overruns.

Why this shape rather than a free-roaming agent: the task repositories are small enough to fit
in one prompt, the deadlines are 180–300 s, and the Agentless result is that a structured
pipeline matches or beats an autonomous agent at a fraction of the calls. A ReAct-style loop
with its full jailed toolset is still in `app/tools/` and is what applies every edit.

```
POST /solve ─► workspace (safe extract) ─► profile + baseline tests
                 │
                 ├─ derived checks (LLM, independent)  ─┐
                 └─ candidate: edit ─► own checks ──────┴─► DELIVERY GATE ─► repair…
                                                              │
                                              best Tier A, else Tier B, else null
```

Read `docs/ARCHITECTURE.md` for the detailed design, the decision log, and what the live runs
changed.

---

## Trade-offs taken

- **Tier B delivery.** A diff that passed the static rules, applied cleanly and passed the
  repository's own tests is delivered when nothing better exists. Measured on the public
  tasks, an A-only policy would have turned 5 accepted tasks into 2: `null` scores zero for
  certain, while a repo-test-verified diff often passes the hidden suite. Tasks 03 and 11 were
  both accepted by the hidden tests via tier B.
- **Sequential candidates, not parallel.** The brief allows more than one candidate when time
  allows. With free-tier quotas (50 requests/day) a second parallel candidate doubles spend
  for a modest gain, so candidate 2 runs only if candidate 1 fails and time remains
  (`CODEFIX_MAX_CANDIDATES`).
- **Whole repo in the prompt** instead of letting the model explore with tools. Fewer calls,
  which matters on a quota, and small repositories fit comfortably. It would not scale to a
  large repository; that is what the tool loop is for.
- **SEARCH/REPLACE blocks rather than raw diffs.** Models get hunk headers and line counts
  wrong, and `git apply` is unforgiving. Git generates the diff; the model never writes one.
- **The Docker socket is mounted** so the service can start sibling sandbox containers. That
  is root-equivalent on the host and acceptable for a take-home; production alternatives are
  in `docs/PRODUCTION.md`.
- **Evaluation mode** (`CODEFIX_BUDGET_FLOOR_S`) can give a request more time than
  `deadline_seconds` to measure correctness separately from speed on slow free models. It is
  **0 by default**, so a graded run always honours the client's deadline.

---

## Verification

```bash
python -m pytest            # 168 passing, 2 skipped
```

The suite is built around agreeing with the real grader:

- **Static-rule parity** with `harness/grade.py`, including 3 000 fuzzed diffs.
- **Docker flag parity**, checked by capturing the argv `grade.py` itself builds.
- **Gate verdicts** cross-checked against `grade.grade()` on public tasks, including context
  drift, CRLF and restricted paths.
- **Sandbox probe** (`CODEFIX_DOCKER_TESTS=1`): no network, uid 65534, read-only root,
  writable `/work` and `/tmp`, and all six toolchains at the pinned versions.
- **Adversarial input**: path-jail escapes, malicious archives (symlinks, hardlinks, traversal),
  process-tree kills on timeout, diff round-trips.
- **Failure handling**: spend caps, daily-quota exhaustion, mid-stream connection drops,
  truncated answers, the watchdog, and idempotent retries of the same `request_id`.

Two skipped tests need a privilege or tool this host lacks (symlink creation on Windows; the
Docker probe unless enabled).

Scripts:

| Script | Purpose |
|---|---|
| `scripts/eval_tasks.py` | Solve tasks and grade them, reporting correctness and deadline compliance separately |
| `scripts/demo_tools.py` | Drive the tools with scripted calls, no LLM — proves the pipeline without spending anything |
| `scripts/check_openrouter.py` | Show an OpenRouter key's limit and remaining credit; `--ping` makes one free call |
| `scripts/run_client_local.py` | The challenge's `run_client.py` with two Windows-only fixes (see below) |

---

## Notes for running this on Windows

Both are host quirks, not service behaviour, and neither applies on a Linux grader:

1. `harness/run_client.py` saves the diff with `Path.write_text()`, which turns `\n` into
   `\r\n` on Windows and makes the grader reject it for CRLF. `scripts/run_client_local.py`
   forces LF.
2. Git for Windows sets `core.autocrlf=true` system-wide, so the grader's `git apply` writes
   CRLF files. Run the harness with `GIT_CONFIG_NOSYSTEM=1` (the scripts set it). The service
   is immune: its own git calls always pass `core.autocrlf=false`.

---

## Layout

```
app/
  api.py          routes, contract, idempotency, readiness
  solver.py       the pipeline: candidates, repair, selection, record, traces
  gate/           the independent delivery gate (static_rules.py is a verbatim port)
  llm.py          Chat Completions client: providers, streaming, retries, spend caps
  specchecks.py   derived checks: generation, lenient parsing, sanity run
  editformat.py   SEARCH/REPLACE parsing and application
  tools/          jailed agent tools (list, read, search, edit, write, run, submit)
  sandbox.py      docker/local backends with the grader's exact flags
  diffing.py      canonical diff builder
  profiler.py     test-command and language discovery
  deadline.py procs.py workspace.py config.py prompts.py textutil.py envfile.py
docs/ARCHITECTURE.md  docs/PRODUCTION.md  results.md
sandbox/Dockerfile    copy of the challenge's acceptance image
```
