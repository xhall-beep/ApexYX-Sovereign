# Running a 10-tier SQLite Agent Mesh on Termux without Thermal Throttling

Phones are hostile hardware for agent systems. A passively-cooled SoC starts
throttling within minutes of sustained load, Android kills background
processes on a whim, and every polling loop you leave running converts
battery into heat into slower clocks into longer loops — a feedback spiral
that ends with a warm brick.

This is a walkthrough of the engineering decisions that let a 10-tier agent
mesh (message bus, router, executor, scheduler, learner, egress, state, plan,
guard, mirror) run indefinitely on a stock Pixel under Termux, with no root,
no cloud, and no cooling problems. Case study: ApexYX-Sovereign, which ships
these tiers with 421 hermetic self-tests. Everything below is in the shipped
code, not aspiration.

## 1. The enemy list

Three things kill long-running agents on Android:

1. **Thermal throttling.** Sustained CPU on a passively-cooled SoC raises
   die temperature until the kernel governor cuts clocks. Work takes longer,
   which holds temperature up, which keeps clocks down.
2. **The phantom process killer.** Since Android 12, apps (including Termux)
   get a budget of 32 phantom child processes; exceed it or trip excessive-CPU
   detection and the kernel starts SIGKILLing your daemons silently.
3. **Doze and app standby.** Long sleeps in a background process are not
   guaranteed to wake on time; timers drift, sockets die.

The common thread: **you cannot afford long-lived busy processes.** Any
architecture built on "N daemons, each polling every few seconds" loses on
all three fronts at once.

## 2. Architecture: ticks, not daemons

Every tier in the mesh is a standalone CLI, and every recurring behaviour is
driven by *ticks* — a single short-lived process that evaluates everything
due, does the work, and exits:

    mesh_sched.py --tick    # evaluate all schedules/triggers once, cron-safe

The scheduler tier stores jobs in SQLite (`every 30m`, `daily 07:30`,
`@boot`, five-field cron specs, and sensor triggers like `battery:<20`).
The tick computes what is due, posts work items onto the bus, and exits in
well under a second. Between ticks, *nothing is running*. CPU duty cycle for
the orchestration layer is effectively the tick frequency times tick cost —
tens of milliseconds per minute.

Daemon mode exists for the executor (`--interval 10`) when you want low
latency, but it is optional. The design rule: **daemons are an optimization,
ticks are the contract.** A phantom-process kill of a daemon loses nothing,
because the next tick reconstructs all state from SQLite.

## 3. SQLite as the message bus (and why WAL matters on a phone)

The bus is one SQLite file (`~/.mesh/bus.db`), one table, and a CLI:

    mesh post --node me --kind inbox --text "..."   # append
    mesh task --node me --text "..." --for exec     # addressed work item
    mesh claim --id 7 --node exec                   # exactly-once handoff
    mesh done  --id 7 --node exec                   # close out

Two pragmas do the heavy lifting:

    PRAGMA journal_mode=WAL
    PRAGMA busy_timeout=10000

WAL lets ten uncoordinated short-lived processes hammer the same file from
cron without readers blocking the writer; `busy_timeout` turns lock
collisions into brief waits instead of errors. On flash storage this also
concentrates writes into the WAL file instead of scattering page rewrites —
kinder to both latency and flash wear.

Rows are append-only and sealed with a sha256 hash chain; `mesh verify`
recomputes the chain and screams if any byte of history was edited. Exactly-
once execution is a single `UPDATE ... WHERE state='new'` compare-and-swap —
no distributed-lock machinery, because SQLite *is* the lock.

## 4. Thermal admission control: nothing runs unless the phone can afford it

The guard tier is the part most agent frameworks are missing. Before any
expensive work runs, it must pass admission:

    guard check model --node brain --tokens 4000
    # exit 0 = allow, 10 = defer, 20 = deny  — shell-scriptable

The sensor layer reads battery percentage, charging state, and temperature
via `termux-battery-status` when present, falling back to
`/sys/class/power_supply/battery/*` and a scan of every
`/sys/class/thermal/thermal_zone*/temp`. Readings are cached with a ~20s TTL
so the sensors themselves never become a polling load.

Default policy (all tunable at runtime with `guard policy set`):

- above **44.0°C**: deny all model work
- above **40.0°C**: defer model work with a suggested retry window
- below **35%** battery: no local LLM inference
- below **20%** battery: only notifications survive
- plus per-node concurrency caps and rolling token/wall-clock budgets

The key insight: **backpressure must live below the agent layer.** Agents
are terrible at self-restraint; a deny/defer verdict enforced at admission
time works no matter how enthusiastic the plan tier is. The easy path wraps
any command:

    guard wrap -- python3 heavy_inference.py

admit → run → audit → release, one line. Every decision lands in an
append-only, hash-chained audit log (`guard verify` proves it untampered),
and a kill switch gives three depths: pause, drain, halt.

## 5. Termux-specific landmines (learned the hard way)

- **Shebangs.** Termux rewrites `#!/usr/bin/env` via termux-exec, but
  hardcoded `#!/data/data/com.termux/files/usr/bin/bash` shebangs make your
  scripts Termux-only. Worse: if your installers *generate* wrapper scripts
  via heredocs, the generated files inherit whatever shebang you typed. Our
  selftests passed everywhere while the generated `mesh` wrapper died on
  plain Linux. Use `#!/usr/bin/env bash` everywhere, including inside
  heredocs, and test on a non-Termux box.
- **`$PREFIX`.** Termux sets it; Linux doesn't. Any `$PREFIX/bin` write with
  an unset variable becomes `/bin` — instant permission error (or worse,
  with sudo). Guard every use: `[ -n "${PREFIX:-}" ] && ...`.
- **Wake lock.** For tick-driven designs you mostly don't need one, but if
  you run the exec daemon, `termux-wake-lock` prevents Doze from freezing it.
- **Process budget.** Count your daemons. Tick-driven tiers spend their
  phantom-process budget only for milliseconds at a time.

## 6. Trust through hermetic self-tests

Every tier ships a `--selftest` that runs in a tmpdir with zero network and
zero shared state: 421 checks across the ten tiers, executed by the
installer itself. The install command *is* the proof:

    pkg install python git       # Termux (Linux: python3 + git)
    git clone https://github.com/xhall-beep/ApexYX-Sovereign.git
    cd ApexYX-Sovereign && bash up_all.sh

If a tier's selftest fails, it does not install. On a platform where the OS
actively sabotages you, "it worked on my machine" is worthless; "it proves
itself on *your* machine, offline, in under a minute" is the only claim that
survives contact with Android.

## 7. What this buys you

A mesh that idles at zero CPU, admits work only when battery and temperature
allow, survives arbitrary process kills because all state lives in WAL-mode
SQLite, and can prove both its message history and its audit trail are
untampered. No root, no cloud, no Python dependencies beyond the standard
library.

Source, all ten tiers + selftests: https://github.com/xhall-beep/ApexYX-Sovereign
