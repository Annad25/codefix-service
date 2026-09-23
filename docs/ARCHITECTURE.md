# codefix-service: Architecture

Status: built and running end to end; 168 tests pass; all 7 public tasks accepted. See §17–18 for build status and
what the live runs on free models showed.
Provider: chosen by `CODEFIX_LLM_PROVIDER` — `openrouter` (default), `openai`, or
`compatible` for any other OpenAI-compatible endpoint. All of them speak Chat
Completions; model IDs are configuration, not code, so the same service runs free
models, GPT-5.2 on a reviewer's own key, or a local endpoint unchanged.
Sections 1–14 are the original design; where the live runs changed it, §15–18 say so.

---

## 0. What we are building, in one paragraph

An HTTP service with `POST /solve` and `GET /health`. A request brings a git snapshot (tar.gz), a task in prose, and a deadline. The service works out how to build and test the repository, runs **two LLM agents in parallel**, each editing an isolated working copy through a small, jailed toolset. Meanwhile an **independent spec-to-checks step** writes tests from the task text alone. Each candidate diff goes through a **delivery gate**: a separate code path that re-applies the diff to a pristine copy of the original archive, repeats the grader's static rules, and runs the repository tests plus the derived checks inside the acceptance image with networking off. Failures go back to the agent for repair while time remains. The service returns the best gate-passed diff, or `null`, always before the deadline.

---

## 1. Grading facts this design is built on (from `harness/grade.py`)

| Fact | Design consequence |
|---|---|
| Static rules: starts with `diff --git `, UTF-8, no CRLF, no `GIT binary patch`, no symlink mode, ≤200 000 bytes, non-empty, no abs/`..` paths, nothing under `.git/`, `acceptance/`, `acceptance_tests/` | Port `static_check` **verbatim**. A unit test asserts parity with the original function. |
| `git apply --check` then `git apply` on a fresh snapshot | The gate re-extracts the **original request bytes** and runs the same two commands. |
| Tests run with `docker run --rm --network none --memory 1g --cpus 2 --pids-limit 512 --read-only --tmpfs /tmp:exec -u 65534` in `acceptance:latest`, 300 s timeout | The sandbox runner uses **exactly** these flags, plus hardening flags that don't change behavior. |
| Hidden suite is copied in as `acceptance_tests/`. Rust copies to `tests/zz_acceptance.rs`. Java compiles only `src/main` + acceptance. C compiles only `src/strutil.c` + acceptance | The diff should touch **source only**. Our checks live outside the diff. Reserve the names `acceptance*`, `zz_acceptance*`. |
| Client counts `elapsed > deadline_seconds`, measured before upload | The deadline clock has a safety margin for transfer time. |
| Client reads `usage.estimated_cost_usd` | The response includes a `usage` object. |
| Toolchains are fixed: Python 3.12, Node 18, Go 1.22, Rust 1.75, Java 21, GCC 13, git 2.43 | Versions go into the system prompt. All execution happens in the image, never on the host. |

**The request does not carry `language` or `test_command`.** The service discovers them from the snapshot (§4.3).

---

## 2. Design principles (and where each one comes from)

| # | Principle | Pattern / source | How it shows up here |
|---|---|---|---|
| P1 | **Verify, don't trust** | Agentless: generate candidates, validate with regression + reproduction tests, then select | The gate is the only path to delivery. Derived checks act as reproduction tests. |
| P2 | **Structure over free-form autonomy** | Agentless shows simple fixed pipelines match or beat complex agents | A fixed outer pipeline (profile → derive → generate → gate → repair). The agent loop is only the inner "repair" step. |
| P3 | **Design the tool interface for the model (ACI)** | SWE-agent: windowed file viewer, compact search, edits guarded by a linter, concise feedback | Six small jailed tools, a line-numbered windowed reader, exact-match edits, a syntax guard, truncated outputs. |
| P4 | **Select by agreement with independent tests** | CodeT dual execution agreement | Candidates are scored against checks written *without seeing any candidate*. Agreement breaks ties. |
| P5 | **Use execution feedback for repair** | Reflexion / self-debugging | Gate failures go back as compact, structured feedback into the same conversation. |
| P6 | **Propagate the deadline and drop work nobody is waiting for** | Google SRE, *Addressing Cascading Failures* | One `Deadline` object is passed everywhere. Every LLM call, container and retry is capped by the time remaining. Work that can't finish is cancelled. |
| P7 | **Bulkheads and graceful degradation** | Google SRE, *Handling Overload* | Separate semaphores for containers and LLM calls. Under load, drop to one candidate instead of failing. |
| P8 | **Least privilege sandbox** | OWASP Docker Security Cheat Sheet | `--network none`, `--cap-drop ALL`, `--security-opt no-new-privileges`, non-root, read-only root, resource limits. |
| P9 | **Treat input as hostile** | PEP 706 plus the 2025–26 tarfile CVEs (filter bypass via links) | Pre-scan tar members and reject every link or special file, *then* extract with `filter="data"`. Resolve and jail every agent path. |
| P10 | **Retry with backoff, bounded by the budget** | OpenAI rate-limit guide and cookbook | Exponential backoff with jitter, honor `retry-after`, never retry past the deadline. |
| P11 | **Liveness ≠ readiness** | Kubernetes probe guidance | `/health` = ready (Docker reachable, image present, key configured; cached). `/livez` = process alive. |
| P12 | **Stable prompt prefix for caching** | OpenAI prompt-caching guide (≥1 024-token prefix, cached input billed at 10%) | System prompt + tool schemas are byte-identical across requests and come first. Per-request content comes after. |

---

## 3. Component view

```
                        ┌──────────────────────── codefix-service container ────────────────────────┐
 client ──POST /solve──►│ API layer (FastAPI)                                                        │
                        │  ├─ contract models (pydantic, versioned, extra=ignore)                     │
                        │  ├─ idempotency map  request_id → in-flight Future / result                 │
                        │  └─ Deadline(t0, D, margin) ──────────────────────────────┐                 │
                        │                                                           ▼                 │
                        │ Orchestrator (one asyncio task tree per request)                            │
                        │  1 Workspace   safe-extract → verify git HEAD → keep ORIGINAL bytes         │
                        │  2 Profiler    test cmd, language, file tree, baseline test run             │
                        │  3 SpecChecks  task → clause list → derived test files (LLM, structured)    │
                        │  4 Candidates  2 × AgentLoop (parallel), each in its own worktree           │
                        │  5 Gate        independent module (see §4.7)                                │
                        │  6 Repair      gate feedback → same AgentLoop → Gate again                  │
                        │  7 Selector    tiered + agreement + smallest diff                           │
                        │  8 Responder   diff | null, record, usage — always before deadline          │
                        │                                                                             │
                        │ Shared infra: LLMClient (OpenAI, retries, cost), SandboxRunner (docker CLI),│
                        │ Semaphores (sandbox, llm), structured logs + per-request trace dir          │
                        └───────────────┬─────────────────────────────────────────────────────────────┘
                                        │ /var/run/docker.sock (sibling containers)
                                        ▼
                        acceptance:latest  --network none --read-only -u 65534 --cap-drop ALL ...
```

### Repository layout

```
codefix-service/
  app/
    api.py            FastAPI routes, contract models, idempotency, top-level watchdog
    config.py         env-driven settings (models, K, margins, limits, prices)
    deadline.py       Deadline object: remaining(), sub-budget(), cancel scope
    workspace.py      safe tar extraction, worktree copies, cleanup
    profiler.py       test-command/language detection, repo map, baseline run
    sandbox.py        docker run wrapper (exact grading flags), kill on timeout, output truncation
    llm.py            OpenAI Responses client, retries, usage→cost, prompt-cache-friendly layout
    agent/
      loop.py         tool-calling loop, stop conditions, record building
      tools.py        jailed ACI tools + JSON schemas (strict)
      prompts.py      system prompt (static), task prompt (dynamic), repair prompt
    specchecks.py     clause extraction + derived-check generation + sanity run
    diffing.py        canonical `git diff` builder + normalization
    gate/             *** independent: imports only stdlib + sandbox.py ***
      static_rules.py verbatim port of grade.static_check
      policy.py       our extra rules (no test edits, reserved names, trailing \n, no mode lines)
      gate.py         fresh extract → apply → run tests → run derived checks → GateResult
    select.py         candidate scoring and tiering
    orchestrator.py   pipeline wiring and budget allocation
  sandbox/Dockerfile  copy of the acceptance Dockerfile (build as acceptance:latest)
  Dockerfile          service image: python:3.12-slim + git + docker CLI
  docker-compose.yml  service + socket mount + env
  tests/              unit (parity, jail, tar, diff), integration (gate vs grade.py)
  README.md  results.md  docs/ARCHITECTURE.md  docs/PRODUCTION.md
```

---

## 4. Components in detail

### 4.1 API layer
- **FastAPI + uvicorn, single worker, fully async.** Nothing CPU-heavy runs on the event loop: all subprocesses use `asyncio.create_subprocess_exec`, so `/health` always answers.
- **Contract models** use pydantic with `extra="ignore"`. `deadline_seconds` is validated to be at least 10 s. On a malformed archive the service still returns 200 with `diff: null` and a record entry explaining why. **Never a 5xx for bad input.**
- **Idempotency.** `request_id` maps to the in-flight `asyncio.Future`. A duplicate POST (client retry) attaches to the same future instead of starting new work.
- **Top-level watchdog.** `asyncio.wait_for(orchestrator.run(), deadline.hard_remaining())`. On timeout the orchestrator's `best_so_far` (a gate-passed diff or `None`) is returned. The watchdog also cancels the task tree, which kills any containers.

### 4.2 Workspace
1. `base64` decode, then open the tar.
2. **Pre-scan every member.** Reject symlinks, hardlinks, devices, FIFOs, absolute names and `..` components. The snapshot is plain source, so a link is never legitimate.
3. `extractall(filter="data")` into `runs/<request_id>/pristine/`, then verify that `repo/.git` exists and `git rev-parse HEAD` succeeds.
4. Keep the **original bytes** in memory. The gate re-extracts from them and never reuses a directory the agent touched.
5. Each candidate gets `worktree-k/`, a `cp -a` of pristine. Tests never run in a worktree. They run in a throwaway copy made per test run, so build artifacts never reach the diff.

### 4.3 Profiler (deterministic, no LLM)
- **Test command.** Use `bash run_tests.sh` if it exists (every task repo ships one). Otherwise, in order: `Makefile` with a `test:` target → `make -s test`; `go.mod` → `go test ./...`; `Cargo.toml` → `cargo test --offline`; `package.json` → `node --test <tests glob>`; `src/**/*.java` → the javac/java recipe; Python `tests/` → `PYTHONPATH=src python3 -m unittest discover -s tests -t . -v`; `tests/run.sh` → `bash tests/run.sh`.
- **Language** comes from manifests and extension counts. It picks the toolchain notes for the prompt and the file names for derived checks.
- **Repo map.** A tree of up to about 200 entries, with sizes. Small repos (all task repos are small) also get their source files inlined into the first user message, which saves agent turns.
- **Baseline run.** Run the test command once on pristine. Record which tests fail before the change; these are the signal for fix-style tasks. Record the measured cold wall time, which calibrates the gate reserve.

### 4.4 SpecChecks (independent reproduction tests: Agentless + CodeT)
One structured-output LLM call (strict JSON schema) that sees **the task text, the repo map and the public API surface, but no candidate**. It returns:
```json
{ "clauses": ["empty cart totals 0.0", "..."],
  "files":   [{"path": "tests/zz_derived_test.py", "content": "..."}],
  "command": "PYTHONPATH=src python3 -m unittest tests.zz_derived_test -v" }
```
- One test per atomic clause of the spec: edge cases, error types, boundaries, ordering, immutability.
- The files go in an **overlay** that is copied into sandbox runs only and never into the diff. Names use the `zz_derived` prefix. Paths are jailed and cannot touch `acceptance*`.
- **Sanity run on pristine.** For implement/fix tasks, most derived checks should *fail* before the change. A check that passes on pristine proves nothing and gets weight 0. A check that crashes at collection (syntax error, bad import) is regenerated once, then dropped.
- It runs **in parallel** with the candidates, so it costs no wall time on the critical path.

### 4.5 AgentLoop (the ACI)
Our own loop on the Responses API. We don't use the hosted `apply_patch` tool or an agent SDK, for three reasons:
- We need exact control over deadlines, cancellation and the `record` format.
- The diff is produced by git, not by the model.
- OpenAI's apply_patch documentation lists support up to GPT-5.5, so its availability on 5.6 isn't confirmed. Custom tools keep us provider-agnostic.

**Tools** (strict schemas, all paths resolved with `realpath` and required to stay inside the worktree):

| Tool | Behavior |
|---|---|
| `list_dir(path)` | Entries plus sizes, capped. |
| `read_file(path, start_line, end_line)` | Line-numbered window, ≤250 lines (SWE-agent's windowed viewer). |
| `search(pattern, path)` | Regex over repo text files in Python, ≤50 hits with line numbers. No shell. |
| `str_replace(path, old, new)` | `old` must match **exactly once**. Blocked for `.git/`, `acceptance*`, `zz_acceptance*`, and pre-existing test files. **Syntax guard:** for `.py`, a failed `compile()` rejects the edit with the error (SWE-agent's linter guardrail). |
| `write_file(path, content)` | New files only, or full rewrite of a non-test source file. Same blocks. |
| `run_tests()` | Repo test command + derived checks (if ready) in the sandbox, on a fresh copy. Output truncated head+tail to about 8k tokens. |
| `run_command(cmd)` | Any command in the sandbox (network off, 60 s cap), e.g. compile only or a quick REPL check. |
| `submit(summary)` | Ends the loop. |

**Prompting.** Per OpenAI's reasoning and coding-agent guides: give a clear goal, hard constraints and a definition of done ("done = repo tests and derived checks pass, signatures unchanged, standard library only, toolchain versions X"). Tell the agent to persist until done and to batch parallel reads. Reasoning effort is `medium` by default and `high` for repair.

**Stop conditions:** `submit`, 40 turns, a token cap, or `deadline.agent_remaining() <= 0`.

**Record.** Every assistant text, every tool call (`name`, parsed `input`) and every tool result (`output` truncated, `is_error`), in order.

### 4.6 Diff builder (canonical form)
```
git -c core.autocrlf=false -c core.filemode=false -c core.quotepath=false add -A
git diff --cached --no-color --no-ext-diff --no-renames --src-prefix=a/ --dst-prefix=b/
```
Then normalize:
- Strip `old mode`/`new mode` lines.
- Assert no `Binary files differ`.
- Ensure a trailing `\n`.
- Drop any path matching the artifact denylist (`build/ out/ target/ Cargo.lock __pycache__/ node_modules/ *.class *.o`) by unstaging it before diffing.

### 4.7 Delivery gate (the independent path)
`gate/` imports **only the standard library and `sandbox.py`**. It gets `(original_archive_bytes, diff_text, test_command, derived_overlay)` and has no access to agent state.

1. **Static:** `static_rules.static_check(diff)`, a verbatim port of `grade.py`.
2. **Policy:** no edits to pre-existing test files, no reserved names, no mode lines, trailing newline.
3. **Apply:** a fresh extraction from the original bytes, then `git apply --check` and `git apply`, the same commands as the grader.
4. **Repo tests:** copy the tree, `chmod -R a+rwX` (the container runs as uid 65534), then run the test command in the sandbox with the exact grading flags and a **cold** `/tmp`.
5. **Derived checks:** copy the overlay in and run its command.
6. Return `GateResult{tier, static_ok, apply_ok, tests_ok, derived_passed/total, logs, duration}`.

The **tiers** implement "deliver only what the gate has seen pass" without throwing away a verified diff:

| Tier | Meaning | Delivered? |
|---|---|---|
| A | static + apply + repo tests + all weighted derived checks pass | yes, preferred |
| B | static + apply + repo tests pass, some derived checks fail | only if no A exists at the deadline |
| C | anything earlier fails | never. It goes to repair. |

Rationale: a derived check can itself be wrong. Tier B is still verified in parity conditions against the repo's own tests, while `null` is a guaranteed zero. This is a stated trade-off in the README.

### 4.8 Repair (Reflexion-style)
On Tier C, or Tier B while time remains, the gate result is condensed into a failure summary: stage, failing test names, the last 40 lines of output, and the failing derived clauses. It is appended to the **same** agent conversation as a user turn: "The delivery gate rejected your change: … Fix it." The agent continues with its existing context. At most 2 repair rounds per candidate, fewer if the budget says so.

### 4.9 Selector
Among gate results, order by:
1. Tier.
2. Weighted derived checks passed.
3. **Agreement:** the number of other candidates with an identical pass/fail vector on the derived checks (CodeT consensus sets).
4. Smaller diff.

Return the winner's diff and its record. The gate entries for the winner are appended to the record as `{"role": "tool", "name": "delivery_gate", ...}`.

---

## 5. Time budget (deadline propagation)

```
t0 = request received
hard_deadline  = t0 + D − margin              margin = max(10 s, 0.07·D)   # upload + response transfer
gate_reserve   = 1.5 × measured cold gate time (baseline run), floor per language
                 (defaults: py 6 s, js 6 s, bash 4 s, c 8 s, java 25 s, go 35 s, rust 60 s)
agent_deadline = hard_deadline − gate_reserve − 3 s
```
- Every LLM call gets `timeout = min(90 s, agent_remaining)`. Every retry checks `remaining` first.
- Every container gets `timeout = min(its cap, remaining)`, and on expiry `docker kill <name>`, so no orphaned containers.
- **When a candidate reaches Tier A**, cancel the other candidate unless there are more than 40 % of the budget left and fewer than 2 Tier-A results (this saves cost).
- **Queue pressure:** if the sandbox semaphore wait is more than 20 % of D, drop to K = 1.

Example for D = 180 s, Python: margin 12.6 s → hard 167 s. Gate reserve about 9 s, so agent_deadline is about 155 s from receipt.

---

## 6. Concurrency and availability
- **Bulkheads:** a `sandbox_sem` sized to `cpu_count // 2` (each container gets 2 CPUs, 1 GB) and an `llm_sem` (e.g. 8). They are separate so a slow LLM can't starve test runs, and the reverse.
- **Load shedding** means lowering K, never refusing. Refusing is scored as a failure.
- **`/health`:** 200 when Docker answers, `acceptance:latest` exists and the API key is set. The result is cached for 10 s so the probe is cheap. `/livez` is plain process liveness.
- **Crash safety:** each request's work is wrapped. Any exception returns `diff: best_so_far or null` with the error in the record.

---

## 7. LLM client and model choice

**The back end is selected by `CODEFIX_LLM_PROVIDER`** so a reviewer can run the service on
their own key. Only the selected provider's key variable is read, so a key for another
provider that happens to be in the environment can never be used or charged.

| Provider | Key variable | Endpoint | Request dialect | Cost reporting |
|---|---|---|---|---|
| `openrouter` (default) | `OPENROUTER_API_KEY` | openrouter.ai/api/v1 | `max_tokens`; routing, usage and model fallbacks in `extra_body`; `reasoning.effort` | exact, from `usage.cost` |
| `openai` | `OPENAI_API_KEY` | api.openai.com/v1 | `max_completion_tokens` (`max_tokens` is invalid for reasoning models); top-level `reasoning_effort`; none of OpenRouter's fields | computed from the configured per-1M prices, else reported as unknown |
| `compatible` | `CODEFIX_LLM_API_KEY` | `CODEFIX_LLM_BASE_URL` | the portable subset only | as above |

Everything else — streaming with stall detection, retries, spend caps, the record, the gate —
is identical across providers.

### OpenRouter specifics

**Provider: OpenRouter**, through its OpenAI-compatible Chat Completions API (`app/llm.py`). Reasons:
- **Prepaid credits and a per-key credit limit.** Once a key hits its limit, OpenRouter rejects requests *before* they reach a provider, so spend is capped.
- **Exact per-call cost** in `usage.cost`, so no price table goes stale.
- **One key for every model family.** The same code runs cheap models in development and GPT-5.6 for the graded run.
- **Privacy routing** with `provider.data_collection = "deny"`.

Model IDs and prices were verified from `openrouter.ai/api/v1/models` on 2026-09-22. All IDs are environment-configurable.

| Role | Graded run | Development | Price in/out per 1M (graded / dev) |
|---|---|---|---|
| Candidate 1, SpecChecks | `openai/gpt-5.6-terra` | `deepseek/deepseek-v4-flash` | $2 / $12 · $0.05 / $0.10 |
| Candidate 2, repair escalation | `openai/gpt-5.6-sol` | `qwen/qwen3.7-flash` | $2 / $10 · $0.03 / $0.13 |
| Key check `--ping` | n/a | `qwen/qwen3.8-27b:free` | free |

**Spend control (defence in depth):**
1. **OpenRouter per-key credit limit.** A hard stop outside our code: requests are refused with HTTP 402, which we treat as `BudgetExceeded` and never retry.
2. **A process-wide `CostLedger`** (`CODEFIX_MAX_COST_TOTAL_USD`, default $5), checked before every call.
3. **A per-request `CostLedger`** (`CODEFIX_MAX_COST_PER_REQUEST_USD`, default $1), checked before every call. When exhausted, the orchestrator returns the best gate-passed diff or `null`.
4. **`max_tokens` on every call**, so the worst-case overshoot of a cap is one bounded call.
5. **Only `OPENROUTER_API_KEY` is read.** `OPENAI_API_KEY` is ignored. A key that doesn't start with `sk-or-` is refused. The SDK always gets an explicit key and base URL, so it can't fall back to the environment.

**Other client behaviour:**
- **Routing:** `provider = {data_collection: "deny", require_parameters: true, allow_fallbacks: true}`. `require_parameters` sends requests only to endpoints that support tool calling.
- **Reasoning:** `reasoning.effort` is configurable (default medium). `reasoning_details` returned by the model are passed back on later turns, as OpenRouter recommends for tool calling.
- **Retries:** timeouts, connection errors, empty responses and 408/409/425/429/5xx get exponential backoff with jitter, honouring `Retry-After`. At most 3 retries, and **never past the deadline**. 400/401/403/404 and 402 are not retried.
- **Cost:** `usage.cost` from every call is summed into `usage.estimated_cost_usd` on the response (losing candidates included).
- **Caching:** automatic for OpenAI models on OpenRouter (≥1 024-token prefix), with sticky provider routing. The static system prompt and tool schemas come first. Cached tokens are reported from `prompt_tokens_details.cached_tokens`.
- **Expected cost per request** (to be replaced with measured numbers in results.md): about $0.3–0.9 on the graded models, about $0.01 on the development models.

---

## 8. Security and "never reach outside the snapshot"
- The **sandbox** runs with `--network none` plus the grading flags, `--cap-drop ALL` and `--security-opt no-new-privileges`, uid 65534, a read-only root and tmpfs `/tmp`.
- **Agent file tools** are jailed by `realpath` + prefix check, never follow symlinks, and block `.git/` and `acceptance*`. The system prompt forbids looking for acceptance tests. `run_command` only executes inside the container, which sees nothing but the copy of the repo.
- **The API key** lives only in the service env. It is never passed to the sandbox and never logged.
- **Known trade-off:** mounting `docker.sock` is root-equivalent on the host (OWASP). That's acceptable for a take-home. For production see §10.
- **Parity note found while reading `grade.py`:** it mounts a host temp dir as `/work` and runs as uid 65534. On native Linux Docker, uid 65534 can't write there, which would break `mkdir build`, `javac -d out` and `cargo`. Docker Desktop's file sharing hides this. We `chmod -R a+rwX` our copies so our results reflect the tests, not a permissions accident. We'll mention it to the reviewer.

---

## 9. Observability
- **Structured JSON logs**, one line per event, with `request_id`, `phase`, `candidate`, `elapsed_ms` and `remaining_ms`.
- **Per-request trace dir** `runs/<request_id>/`: the request metadata (not the archive), each candidate's record, the gate results, the chosen diff, and a timings/cost summary. This is where `results.md` numbers come from.
- **Counters:** gate tier distribution, repair rounds, deadline slack at response, null rate, and cost per request.

---

## 10. Production notes (draft of docs/PRODUCTION.md)
- **Scaling:** a stateless API behind a queue. Workers pull jobs carrying their absolute deadline. Sandboxes move to a dedicated runner pool (gVisor or Firecracker microVMs, or rootless Docker/Kubernetes Jobs) instead of `docker.sock`. Warm pools with prebuilt language caches serve the agent's inner loop, while the gate stays cold for parity.
- **Monitoring:** SLOs on on-time rate (≥99.9 %) and gate-pass rate. Alert on deadline slack p5, null-rate spikes, LLM error rate and cost per request. Replay failed requests from traces.
- **Cost control:** adaptive K (stop early on the first Tier A), a model ladder (Terra → Sol only on repair), prompt caching, per-tenant budgets, and a circuit breaker on the provider.
- **Contract changes:** versioned schemas (`/v1/solve`, `/v2/solve`, or a `contract_version` field) with adapters to one internal `SolveRequest`. Tolerant reader (`extra="ignore"`). Run both versions side by side through a deprecation window, and shadow-test v2 against recorded v1 traffic.

---

## 11. Test strategy for the service itself
- **Unit:**
  - `static_rules` parity: import `harness/grade.py` and fuzz both functions with the same inputs.
  - Path jail: `..`, absolute paths, symlinks.
  - Tar pre-scan: malicious archives.
  - Diff builder: artifacts, mode bits, trailing newline.
  - Deadline arithmetic.
- **Integration:** hand-written fixes for the 7 public tasks go through our gate and through `harness/grade.py`. The verdicts must agree. This also proves the sandbox flags.
- **End to end:** `harness/run_client.py --concurrency 3` on the public tasks, repeated 3× to measure variance, graded in Docker (never `--local`).
- **Chaos:** LLM timeout or 429 injection, a container that hangs, a malformed archive, a 30 s deadline. Each must return on time.

---

## 12. Assumptions (reviewer not yet confirmed)
1. We develop and report on the 7 README-listed public tasks (01–03, 06, 07, 08, 11). `reference/` and the other tasks' `acceptance/` folders are not opened.
2. The test command is discovered from the snapshot (`run_tests.sh` first).
3. Mounting the Docker socket into the service container is acceptable.
4. The deadline is measured client-side, including upload.

## 13. Build order (3 days)
- **Day 1:** `sandbox`, `workspace`, `profiler`, `diffing`, `gate` + parity tests. Hand-written fixes agree with `grade.py`. API skeleton with `/health` and a `/solve` that returns null on time.
- **Day 2:** `llm`, agent tools and loop, SpecChecks, orchestrator with K=2, repair, selector, cost. First `run_client` pass.
- **Day 3:** Hardening (chaos cases, concurrency 3), Dockerfile/compose, README, results.md, PRODUCTION.md.

## 14. Risk register
| Risk | Mitigation |
|---|---|
| Derived checks are wrong and reject a correct fix | Tier B delivery, weight-0 for checks that pass on pristine, independent generation |
| Cold Rust/Go builds eat the budget | Gate reserve is measured from the baseline run. Warm cache for the agent's inner loop only. |
| Model uses APIs newer than the pinned toolchain | Versions in the prompt. Every run happens in the pinned image. |
| Agent edits tests to go green | Test-file edits are blocked, and the gate uses pristine tests |
| Docker Desktop on Windows has path or permission quirks | Develop and run from WSL2. Copies get `chmod a+rwX`. |
| Provider outage | Retries within the budget, then `null` on time (never a 5xx or a timeout) |

---

## 15. Decision log
| Decision | Choice | Why |
|---|---|---|
| Agent style | ReAct tool loop inside a fixed pipeline, not a "deep agent" | Small repos and 3–5 minute deadlines: planners and self-spawned sub-agents add turns, variance and record complexity without improving fixes. The deep-agent features we want (plan, sub-agents, self-critique) are supplied deterministically by the pipeline, parallel candidates and the gate. |
| Tool transport | In-process function tools, not MCP | OpenAI's remote MCP requires a publicly reachable server (unacceptable for tools that run client code), and a local MCP sidecar adds latency, a second process and cross-process cancellation for no graded benefit. Tools are provider-neutral `ToolSpec`s, so an MCP adapter is a thin later addition. |
| Edit format | Our own `str_replace` / `write_file` tools, not OpenAI's hosted `apply_patch` | Exact-once matching is easy to verify, the diff always comes from git, and it keeps the provider swappable. |
| Sandbox backends | `docker` (grading parity, required for real runs) and `local` (development without Docker) | Selected by `CODEFIX_SANDBOX`. The local backend must never be used for results. |
| Delivery tiers | Tier A (repo tests + derived checks) preferred; Tier B (repo tests only) delivered as a last resort; Tier C never | Measured on the public tasks: an A-only policy would have turned 5 accepted tasks into 2, because null is a certain zero while a repo-test-verified diff still passes the hidden suite often. |
| Gate imports | stdlib + `sandbox` + `procs.git` + the pure `workspace.safe_extract` | Independence means no agent or tool state, not duplicated extraction code. |
| LLM provider | OpenRouter (Chat Completions), not OpenAI direct | Prepaid credits with a hard per-key limit, exact cost per call, and the same OpenAI models plus cheap ones for development. The only key available directly for OpenAI is a company key, which must not be used or charged. |
| API shape | Chat Completions, not the Responses API | It's the interface OpenRouter supports across all models, so the provider stays swappable. Tool definitions come from the same `ToolSpec`s (`to_chat_tools`). |

## 16. Findings from building and testing (2026-09-22)
1. **The grader's static symlink check never runs.** In `grade.static_check` the `new file mode 120000` test sits after a `continue` that skips every non-header line. Headers with spaces in paths also skip its path checks. Our `gate/policy.py` enforces both. Test: `tests/test_policy.py`.
2. **Git for Windows sets `core.autocrlf=true` system-wide.** The grader's plain `git apply` then writes CRLF files on a Windows host. Our git calls force `core.autocrlf=false` and `GIT_CONFIG_NOSYSTEM=1`. Run `harness/grade.py` from WSL/Linux, not Windows.
3. **uid 65534 and bind-mount permissions** (see §8). Copies are made world-writable before any sandbox run.
4. **Windows path length.** Deep `.git/objects` paths under long temp directories exceed 260 characters, so request directory names are kept short.

## 17. Build status
The whole pipeline is built and runs end to end on OpenRouter free models with the
Docker sandbox: config, deadline, procs, workspace, sandbox, profiler, diff builder,
gate, tools, LLM client (streamed, with stall detection), edit format, spec checks,
solver and API, plus the Dockerfile and compose file. 154 tests pass (`python -m pytest`),
covering static-rule parity with `grade.py` (including 3 000 fuzzed diffs), Docker flag
parity captured from the grader's own argv, gate tier boundaries, jail attacks, malicious
archives, timeout tree-kill, diff round-trips, spend caps, quota exhaustion and the watchdog.

Pending: README, results.md and the production note.

## 18. Findings from the live runs on free models (2026-09-22/23)
1. **Free models are slow and verbose.** Roughly 30 tokens/s, most of it hidden reasoning.
   One fix answer takes 80–250 s, which is the whole budget of a 180 s task. Answers are
   often cut off at the output limit before reaching the edit blocks; that now triggers an
   immediate "shorter format" retry which does not consume a repair round, and the limit is
   configurable (`CODEFIX_FIX_MAX_OUTPUT_TOKENS`).
2. **Evaluation mode.** `CODEFIX_BUDGET_FLOOR_S` gives a request more time than
   `deadline_seconds` so correctness can be measured separately from speed. It is 0 by
   default, so graded runs always honour the client's deadline. `scripts/eval_tasks.py`
   reports correctness and deadline compliance as separate numbers.
3. **Tier B earns its place.** In the first full run, three of the five accepted tasks had no
   usable derived checks; under an A-only policy all three would have been null, which scores
   zero for certain. Tier A is preferred, B is a last resort, and both are gate-verified.
4. **Derived checks change outcomes.** On the Go task the repo tests passed but the hidden
   "Items must return a copy" test failed; with derived checks in place the next run produced
   a fix that was accepted. On the token bucket the derived check reproduced the hidden
   "time going backwards" failure exactly.
5. **Generated checks inherit the model's blind spots.** The JS task passed its own checks but
   failed the hidden "whitespace and case" test, because neither the fix nor the checks
   considered padded input. Both prompts now call for allowed input variations (case,
   whitespace, optional prefixes) to be handled and tested wherever they can occur.
6. **Deadline tuning matters as much as model choice.** Under real deadlines the per-call
   timeout must let the request deadline govern (a fixed cap cuts off calls that would have
   finished, leaving no time to retry), and a candidate must not block waiting for the derived
   checks — it verifies with the repository's tests and attaches the checks when they arrive.
   `CODEFIX_LLM_CALL_TIMEOUT`, `CODEFIX_SPEC_WAIT_S` and `CODEFIX_MAX_REPAIRS` are the knobs.
7. **Free-tier limits are operational facts, not bugs.** 50 requests/day and 20/minute, shared
   upstream pools (Gemma 4 was rate-limited on every attempt, so a fallback served every call),
   and OpenRouter idle timeouts. The client detects daily-quota exhaustion and stops instead of
   retrying, rotates models on failure, and treats a silent stream as a failure while letting a
   slow-but-producing one continue.

## Sources (verified 2026-09-22)
- Agentless (Xia et al.), localize → repair → validate with reproduction and regression tests: https://github.com/OpenAutoCoder/Agentless · https://dl.acm.org/doi/full/10.1145/3715754
- SWE-agent, Agent-Computer Interface: https://arxiv.org/pdf/2405.15793 · https://swe-agent.com/latest/background/aci/
- CodeT, dual execution agreement: https://arxiv.org/abs/2207.10397
- Reflexion: https://arxiv.org/html/2303.11366
- OpenAI models / pricing: https://developers.openai.com/api/docs/models · https://developers.openai.com/api/docs/pricing
- OpenAI function calling (strict mode, Responses loop): https://developers.openai.com/api/docs/guides/function-calling
- OpenAI apply_patch tool: https://developers.openai.com/api/docs/guides/tools-apply-patch
- OpenAI coding-agent prompting guide: https://developers.openai.com/cookbook/examples/gpt-5/codex_prompting_guide
- OpenAI reasoning best practices: https://developers.openai.com/api/docs/guides/reasoning-best-practices
- OpenAI prompt caching: https://developers.openai.com/api/docs/guides/prompt-caching
- OpenAI rate limits / backoff: https://platform.openai.com/docs/guides/rate-limits · https://cookbook.openai.com/examples/how_to_handle_rate_limits
- OWASP Docker Security Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html
- PEP 706, tarfile extraction filters: https://peps.python.org/pep-0706/ · CVE-2026-11940: https://www.sentinelone.com/vulnerability-database/cve-2026-11940/
- Google SRE, Addressing Cascading Failures / Handling Overload: https://sre.google/sre-book/addressing-cascading-failures/ · https://sre.google/sre-book/handling-overload/
- Kubernetes probes: https://kubernetes.io/docs/concepts/workloads/pods/probes/
