# BUILD-REPORT — ApexYX-Sovereign vision build

**Date:** 2026-10-29 (runs captured 2026-10-29T00:29Z, estate VM)
**Base:** local copy at pushed HEAD `5233e40`. Nothing pushed. No test weakened,
edited, or deleted. Python 3 stdlib only; zero new dependencies; nothing deleted;
$0.

## The vision, per the repo's own docs

- **README.md:** ten pure-Python tiers (bus, router, exec, sched, learn, reach,
  plan, state, mirror, guard) giving agents a sealed task bus, routing, execution,
  scheduling and admission control — "a self-testing agent economy that runs on a
  phone", hermetic selftests as the proof culture ("simulated siblings, zero
  network"), and the admission that the tiers are only "one organism" if something
  runs them together.
- **docs/termux-tutorial.md:** the architectural verdict the repo argues for —
  *"daemons are an optimization, ticks are the contract: a single short-lived
  process that evaluates everything, posts work items onto the bus, and exits…
  a phantom-process kill of a daemon loses nothing, because the next tick
  reconstructs all state from SQLite."*
- **mesh_guard.py's own docstring:** tier 9 is "SURVIVAL: nothing runs unless the
  phone can afford it" — its lease/budget admission checks are the product, and
  they were the suite's only failures.

Two gaps stood between the stated repo and that vision: the guard's admission
checks failed in CI conditions (411/421), and no conductor existed — four
separate daemons (router, exec, sched, guard) had to be alive at once for one
task to flow end-to-end. Both are closed by this build.

## Finding: the Dockerfile / sch_omni_p1.py references do not exist here

Checked before building: `grep -rn "sovereign_core|Dockerfile|sch_omni"` over the
whole tree returns **nothing**, and the root listing has no Dockerfile and no
`sch_omni_p1.py`. Those references live in some other artefact, not in
ApexYX-Sovereign at HEAD `5233e40`. Per instructions, no references were
invented: `sovereign_core.py` was built as the daemon the brief describes, and
the Dockerfile note is in this report only (see "Still missing").

## A) mesh_guard fixed — 39/49 → 49/49, suite 411/421 → 421/421

**Root cause (proven by reproduction, not inferred).** The ten failing checks
were exactly the recorded set — *first model admitted, second model deferred,
release closes lease, model admitted after release, per-node cap, usage
recorded, over budget denied, shell budget counts calls, wrap runs allowed cmd,
wrap propagates rc*. They all flow through `admit()`/`wrap()` → `decide()` with
**no snapshot**, so they read live sensors. The storage sensor `_free_mb()`
measured free space on the volume holding `MESH_DIR` — which inside the
"hermetic" selftest is a tmpdir. On this VM `/tmp` is a 512 MB tmpfs; filling it
to 358 MB free reproduces the recorded failure exactly:

```
$ dd if=/dev/zero of=/tmp/fill bs=1M count=150   # /tmp free: 358 MB
$ python3 mesh_guard/mesh_guard.py --selftest
39 passed, 10 failed        # the same 10 names as the README's list
```

With `free_mb < free_mb_min (400)`, `decide()` DENYs every model/shell
admission and the lease/budget/wrap block cascades. The test was never testing
what it claims to test on any host whose scratch volume is small.

**Fix (guard code only, sensor layer).** `_free_mb()` now asks `_fs_type()`
(new helper, reads `/proc/self/mounts`) which filesystem holds the path. On
tmpfs/ramfs — whose "free space" is shared RAM, not storage — it measures the
home volume instead: the persistent storage the `free_mb_min` floor exists to
protect. On real storage (the phone's internal flash, any normal disk) behavior
is byte-for-byte what it was. This is the same class of correction the file
already makes for deci-degree battery temperatures. `git diff` confirms the
change is confined to the sensor section; the selftest section is untouched.

**Result:** 49/49 normally **and** 49/49 with `/tmp` filled to the recorded
failure condition.

## B) sovereign_core.py — the missing conductor

New file at repo root (13,997 B, stdlib only). One process that runs the whole
loop the tiers were designed for, per the tutorial's tick contract:

1. **sched** — `mesh_sched.tick()` fires due schedules/triggers onto the bus.
   Tier 4 submits through the `mesh` CLI, so the core installs the same wrapper
   `bus_up.sh` installs (`exec python3 …/mesh_bus.py "$@"`), kept inside
   `MESH_DIR/bin` and prepended to PATH — the core is self-contained and a
   hermetic run never touches a real install.
2. **route** — unrouted `kind=inbox` rows go through `mesh_router.route()`
   (model engine, keyword fallback; `--offline` forces keywords) and are
   re-posted as addressed `kind=task` rows; the inbox row is claimed then
   closed, so nothing routes twice.
3. **admit** — each new task is pre-flighted with `mesh_guard.decide()`:
   DEFER leaves it queued for a later tick (no stranded claims), DENY claims
   and closes it with a `guard denied: …` result instead of letting it spin,
   ALLOW claims it (tier-1 exactly-once) and opens a real guard lease via
   `admit()`. Task→guard-class mapping follows tier 3's prefixes
   (`shell:`/`http:`/`notify:`/model), model tasks get a ~4 chars/token
   pre-admission estimate.
4. **exec** — the task runs through `mesh_exec.execute()` (shell allowlist,
   http, notify, local-model handlers), executed as its target persona
   (router nodes as themselves; `exec`/untargeted as `maestro`, tier 3's
   default worker).
5. **report** — the lease is released (usage lands in the guard budget
   ledger), the result is appended to `results.jsonl` via tier 3's
   `log_result()` (so tier-4 digests see core-executed work) and posted to
   the bus as `kind=result` in tier 3's shape, and the task is marked done.

Modes: `--once` (one tick, JSON summary), `--daemon --interval S`, `--status`
(bus status + guard flag + open leases), `--selftest`. All state under
`MESH_DIR` (default `~/.mesh`, `--mesh-dir` to override); every tier module's
globals are pinned to that one directory so core and tiers can never desync.

**Its test** (`--selftest`, 9 checks, hermetic tmpdir): boots with tier dbs
created; the `mesh` shim is installed; one addressed `shell:` task is claimed,
executed, closed, with its result on the bus; the guard lease opens and closes
with shell usage recorded and the audit chain verifying; one inbox item is
routed → dispatched → executed end-to-end; a guard budget denial closes a task
with a denial result. **9/9.**

**Out-of-process smoke** (evidence/sovereign-core-smoke.txt): seeded a bus via
the tier-1 CLI, ran `sovereign_core.py --once --offline` as a separate process —
routed 1, done 2, failed 0; bus seal chain intact; 0 open guard leases.
A due scheduler job was also proven in-process: fired through `sched.tick()` →
shim → bus → guard → exec → result (`sched_fired: 1, done: 1`).

## Files

| File | Change |
|---|---|
| `mesh_guard/mesh_guard.py` | `_free_mb()` tmpfs/ramfs correction + new `_fs_type()` helper (sensor section only) |
| `sovereign_core.py` | **new** — the conductor daemon + `--selftest` |
| `README.md` | badge 411/421 → 421/421; new "The conductor" section; Selftest-status rewritten to the evidenced numbers incl. the guard root cause |
| `evidence/*.txt` | captured runs (below) |
| `BUILD-REPORT.md` | this file |

Untouched: every selftest in every tier, `.github/workflows/`, all other tiers.

## Test evidence (commands + counts)

Full suite = the CI commands, in-tree, run twice (captures in `evidence/`):

```
for t in mesh_bus mesh_exec mesh_guard mesh_learn mesh_mirror \
         mesh_plan mesh_reach mesh_router mesh_sched mesh_state; do
  python3 "$t/$t.py" --selftest
done
```

| Tier | Normal (`suite-normal.txt`) | Hostile: /tmp at 358 MB free (`suite-hostile.txt`) |
|---|---|---|
| mesh_bus | 125/125 | 125/125 |
| mesh_exec | 6/6 | 6/6 |
| mesh_guard | **49/49** | **49/49** |
| mesh_learn | 23/23 | 23/23 |
| mesh_mirror | 94/94 | 94/94 |
| mesh_plan | 32/32 | 32/32 |
| mesh_reach | 30/30 | 30/30 |
| mesh_router | 5/5 | 5/5 |
| mesh_sched | 19/19 | 19/19 |
| mesh_state | 38/38 | 38/38 |
| **Total** | **421/421** | **421/421** |
| sovereign_core (new, outside the 421) | 9/9 | 9/9 |

Plus: `python3 mesh_guard/mesh_guard.py --selftest` → `49 passed, 0 failed`
(`evidence/guard-selftest.txt`); the pre-fix reproduction above (39/49, same
ten names) run before the fix was applied.

## Still missing / honest limits

- **No Dockerfile exists in this repo** (see Finding). If the owner wants one,
  the shape the core implies is: `python:3-slim`, copy the tree, `MESH_DIR=/mesh`,
  `CMD ["python3", "sovereign_core.py", "--daemon"]` — report-only note, not
  created, because no in-repo reference asks for it.
- **CI doesn't compile `sovereign_core.py`**: `.github/workflows/ci.yml` only
  compiles `mesh_*/*.py`. The workflow was not touched (standing rule), so the
  core's selftest runs locally but not in GitHub CI until the workflow glob is
  widened by whoever owns that change.
- **No `core_up.sh` installer / boot hook**: the tiers each ship one; the core
  currently runs in-tree (`python3 sovereign_core.py …`). Wiring it into
  `up_all.sh` and Termux:Boot is a small follow-up, deliberately not bundled
  here so this build stays reviewable.
- **Failed tasks close with an error result** in the core loop; tier 3's
  retry/backoff ledger (`exec.db`) is its daemon mode's business and is not
  replicated. Re-posting a failed task is the operator's move.
- **Model token accounting is estimate-based** (~4 chars/token, admitted and
  released at the estimate) because tier 3's model handler doesn't surface
  provider token counts. Budgets still bind; they're just not exact.
- **The guard's loadavg check remains a live host reading** in the admit path
  (`load_max` 6.0). It did **not** cause the recorded failures (a load DEFER
  contradicts the recorded failure pattern; the storage DENY explains all ten),
  so its semantics were left alone — but a selftest run on a host under heavy
  sustained load (>6.0 loadavg) can still defer admissions. That's the guard
  working as designed, noted here rather than engineered around.
- **Not pushed.** Local HEAD is still `5233e40`; the working tree carries this
  build for review.
