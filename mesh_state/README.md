# Mesh tier 7 — STATE (shared world-model)

t1 bus (memory) → t2 router (brain) → t3 exec (hands) → t4 sched (clock) →
t5 learn (judgement) → t6 reach (outside) → **t7 state (what is true right now)**

## Install (Termux)
    bash state_up.sh          # selftests (38), seeds entities, installs daemon + boot hook

## Concepts
- **entity** — project / person / service / device / doc, with aliases.
- **fact** — `entity.key = value` with source, node, confidence, timestamp.
  Newer facts supersede older ones (history kept, `superseded=1`); a low-confidence
  claim cannot overwrite a fresh high-confidence one.
- **open loop** — anything unfinished; deduped by title+project signature, reopenable,
  with due dates, staleness and overdue detection.
- **brief** — the compact block every node reads before acting.

## Daily use
    state brief [--project ARAIKI] [--json]
    state set "Pixel 8 Pro" battery=62% --conf 0.9 --node n15
    state get ARAIKI
    state loop open "rotate webhook token" --project APEXYX
    state loop close 4 --note "done"
    state loops --all
    state ingest            # harvest tier3 results.jsonl + tier1 bus into facts/loops
    state import export.json
    state gc --days 30

## Wiring into models
    ollama run deepseek-r1-abliterated:8b "$(state-ctx 'what is my next move?' ARAIKI)"
Router/exec prompts get the same treatment — prepend `state brief` and nodes stop
re-asking things the mesh already knows.

## Autonomy
- daemon every 300s: ingest new results/bus rows, push `kind=digest` to the bus and a
  Termux notification when loops go overdue or stale (>3d).
- tier 4 schedules: nightly `gc`, 08:00 brief → digest node.

Files: `~/.mesh/state.db`, `~/.mesh/state.cursor`, `~/.mesh/state.log`. Nothing else touched.
