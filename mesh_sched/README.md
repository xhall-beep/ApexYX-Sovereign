# mesh tier 4 — scheduler, triggers, digest node

| tier | file | gives the mesh |
|---|---|---|
| 1 | `mesh_bus.py` | shared memory |
| 2 | `mesh_router.py` | a brain (who does what) |
| 3 | `mesh_exec.py` | hands (work actually runs) |
| **4** | **`mesh_sched.py`** | **a clock, senses, and a daily brief** |

Until now the mesh only moved when *you* typed something. Tier 4 makes it move on its
own: recurring jobs, environment triggers, and a morning digest of everything it did.

## Install (Termux)
```bash
unzip mesh_tier4.zip -d ~/mesh_tier4 && bash ~/mesh_tier4/sched_up.sh
```
Runs 19 offline self-tests first, seeds default jobs, installs a boot hook
(`~/.termux/boot/40-mesh-sched`) and starts the daemon.

## Schedules
```bash
sched --add "every 30m"   --text "summarize new results" --for maestro
sched --add "daily 07:30" --digest --for maestro
sched --add "*/15 * * * *" --text "shell:df -h" --for local
sched --add "@boot"       --text "notify:mesh online" --for local
```
Specs: `every N s|m|h|d`, `daily HH:MM`, `@boot`, 5-field cron (`*`, `*/n`, `a-b`, `a,b`).

## Triggers (edge-detected — fires on *change*, never spams)
```bash
sched --trigger "battery:<20"     --text "notify:battery low" --for local
sched --trigger "battery:charging" --text "run heavy backlog" --for maestro
sched --trigger "net:up"          --text "flush queued work"  --for maestro
sched --trigger "file:~/notes.md" --text "re-index notes"     --for maestro
sched --trigger "sh:pgrep ollama | wc -l" --text "notify:ollama state changed" --for local
```

## Digest
```bash
sched --digest-now --since 24h --model --notify
```
Reads `~/.mesh/results.jsonl` (tier 3 output): task counts, per-node ok/fail table,
failures needing attention, output highlights, battery, plus an optional 4-bullet
local-model summary. Saved to `~/.mesh/digests/YYYY-MM-DD.md`, posted to the bus as
`kind=digest`, and pushed as a Termux notification.

## Manage
```bash
sched --list          # jobs, next fire time, fire count, last trigger state
sched --pause 3 / --resume 3 / --rm 3
sched --tick          # one evaluation pass (cron-safe, no daemon needed)
sched --selftest      # 19 checks, no bus / no ollama / no waiting
```

## Design notes
- State in `~/.mesh/sched.db` — tier 1/3 schemas untouched.
- If the bus is down, jobs are handed straight to `mesh_exec.py --submit`, so nothing is lost.
- Triggers store `last_state`; a probe that can't run (no termux-api, no network) is
  treated as "unknown" and stays quiet rather than firing false alarms.
- First observation of a boolean trigger only fires if the condition is already true.
