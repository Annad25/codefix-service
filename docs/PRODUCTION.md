# What would change in production

This is the take-home service with its edges named. Where something is already built, it says
so; where it is deliberately a take-home shortcut, it says that too.

---

## 1. Scaling

**Today.** One process, one host. Requests run concurrently on an asyncio event loop; sandbox
containers are capped by a semaphore (`CODEFIX_SANDBOX_CONCURRENCY`, default half the cores)
so parallel requests cannot oversubscribe the machine. Duplicate `request_id`s attach to the
in-flight solve instead of starting a second one. The whole service is stateless apart from
that in-memory map.

**In production.**

- **Split the API from the workers.** The API validates, admits and enqueues; workers pull
  jobs. Each job carries its **absolute deadline**, not a duration, so queue time is spent
  from the same budget the client is waiting on and a job nobody can still use is dropped
  rather than started (SRE: do not do work no one is waiting for).
- **Move sandboxes to a runner pool.** Mounting the Docker socket is root-equivalent on the
  host — fine for a take-home, not for a multi-tenant service. Replace it with per-tenant
  runners: gVisor or Firecracker microVMs, or Kubernetes Jobs with a restricted pod security
  policy. The sandbox interface (`app/sandbox.py`) is one small class with a `run` method, so
  a new backend is a drop-in.
- **Warm pools for the agent's inner loop only.** Pre-pulled images and warm language caches
  cut tens of seconds off compiled-language tasks. The **gate must stay cold**, because its
  job is to reproduce the grader's environment, not to be fast.
- **Autoscale on queue depth and deadline slack**, not CPU: the meaningful signal is "how much
  of each request's budget is being spent waiting".
- **Idempotency and retries.** The `request_id` map becomes Redis with a TTL, so a client
  retry after a worker dies attaches to the same result instead of paying twice.

## 2. Monitoring

**Today.** Structured logs with `request_id` and phase timings, a per-request trace under
`runs/` (records, gate results, diff, usage), and `usage` in every response.

**In production.**

- **SLOs worth paging on:** on-time response rate (the service failing to answer is scored as
  a failure of every request it missed) and gate-pass rate. Correctness is graded elsewhere, so
  these two are what the service itself controls.
- **Dashboards:** tier distribution (A/B/C), deadline slack at response (p5 is the number that
  predicts missed deadlines), repair rounds per request, null rate, calls and cost per request,
  model error and rate-limit rates, sandbox queue wait.
- **Alerts:** deadline slack p5 below a threshold, null rate spiking, provider error rate,
  daily quota exhausted, cost per request drifting.
- **Traces are the debugger.** Every failure in this project was diagnosed from a trace:
  truncated answers, checks that never reached the gate, a mid-stream connection drop. Keep
  them, sample them in high volume, and redact repository content by default.
- **Replay.** A trace plus the original archive reproduces a request exactly, which makes
  prompt or model changes measurable rather than anecdotal.

## 3. Cost control

**Today.** A per-request and a process-wide `CostLedger`, checked before every call; an output
cap on every call, so the worst-case overshoot is one bounded call; exact cost from OpenRouter
or computed from configured prices; provider-side caps underneath (OpenRouter's per-key credit
limit, HTTP 402) treated as a hard stop, never retried. Daily quota exhaustion is detected and
stops work immediately instead of burning retries.

**In production.**

- **Per-tenant budgets** on the same ledger mechanism, with a circuit breaker per provider.
- **Stop early.** The pipeline already stops at the first tier A; the remaining lever is
  skipping candidate 2 when the first one is close (its derived checks nearly pass).
- **A model ladder.** Cheap model first, escalate only on repair. The provider abstraction
  makes this configuration, not code.
- **Prompt caching.** The system prompt and tool schemas are a stable prefix already; with a
  paid model this is a 4–10× saving on input tokens within a request. Keep the prefix
  byte-identical when editing prompts.
- **Reasoning effort is the biggest single lever** with reasoning models: output tokens were
  roughly 80% of the bill in our estimates, and `low` cut them by 5–10× on free models.

## 4. Absorbing a breaking change to the request contract

**Today.** The request model is a tolerant reader: unknown fields are ignored, so additive
changes cannot break the service. The response is built in one place (`SolveResult.response`).

**For a breaking change.**

1. **Version the endpoint** (`/v1/solve`, `/v2/solve`) or accept a `contract_version` field.
   Both map onto one internal `SolveRequest`; the pipeline never sees the wire format.
2. **Adapters, not branches.** One adapter per version converts to the internal model and back.
   The gate, solver and tools stay untouched, which is the point of keeping them free of
   transport concerns.
3. **Shadow first.** Replay recorded traffic through the v2 adapter and compare verdicts before
   any client is switched.
4. **Run both during a deprecation window,** with per-version metrics so a rollback is a
   routing change rather than a deploy.
5. **If the change is forced on us with no warning** — a field renamed, a new required field —
   the tolerant reader keeps v1 working, and the service keeps answering `null` on time rather
   than failing hard. That is the difference between a bad day and a scored-zero day: an
   unreachable service is treated as having failed every request it did not answer.

## 5. Known gaps, honestly

- **The Docker socket** (above) is the main one.
- **Free models cannot meet the deadlines** on most tasks. Correctness is 7/7 on the public
  tasks, but only the two fastest finish inside the real limits (see `results.md`). In
  production this is a model choice, not a code change.
- **Derived checks inherit the model's blind spots.** They caught real hidden failures here,
  but on one task they were weaker than the hidden suite. More than one independent check
  generation, and cross-checking candidates against each other's checks (CodeT-style consensus),
  is the obvious next step when quota is not the binding constraint.
- **No persistence.** Traces are local files and the idempotency map is in memory; both belong
  in shared storage once there is more than one worker.
