# Tier 8 — plan (goal decomposition + orchestration)

`bash plan_up.sh` — installs `plan` + `goal`, runs 32 offline selftests, starts the
orchestrator daemon, adds a tier-4 `plan-tick` job and a boot hook.

## What it does
1. **Decompose** — `goal "objective"` asks the 8B planner for a JSON step graph
   (`text/kind/deps`). If the model is down or returns junk it falls back to a
   heuristic decomposer (numbered lists, "then/next/;", or a 4-phase template),
   so a plan is *always* produced.
2. **Dispatch** — every ready step (all deps `done`) goes through tier 5 `sroute`
   (tier 2 `route` fallback) to pick a node, then to tier 3 `exec --submit`.
3. **Collect** — tails `~/.mesh/results.jsonl` from a byte cursor, matching results
   to steps by task id. Bad lines are skipped, never fatal.
4. **Self-repair** — failed step → retry with an escalated, more explicit prompt
   (`MESH_PLAN_ATTEMPTS`, default 2). Permanently dead branch → **replan**: the
   unfinished tail is dropped and re-decomposed with "already done / avoid these"
   context (budget `MESH_PLAN_REPLANS`, default 1). Out of budget → goal `blocked`.
5. **Close the loop** — goal complete → tier-7 `state loop close` + a `state set`
   fact, so the world-model knows.

## Commands
    goal "ship X" ARAIKI          plan + start immediately
    plan ls [--all]               progress per goal
    plan show 3 / plan graph 3    steps, deps, results / ascii DAG
    plan tick                     one dispatch+collect cycle
    plan replan 3 --why "..."     force a rethink
    plan step add 3 "df -h" --kind shell --after 7
    plan step done 9 / plan step fail 9 --why "..."
    plan cancel 3
    plan --daemon --interval 30

Env: `MESH_PLANNER_MODEL`, `MESH_PLAN_NODE`, `MESH_PLAN_ATTEMPTS`, `MESH_PLAN_REPLANS`,
`MESH_PLAN_OFFLINE=1` (no model/router/state calls — used by tests).
State: `~/.mesh/plan.db` + `plan.cursor` only.
