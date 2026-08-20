# Mesh tier 5 — the feedback loop

Install (Termux, Pixel 8 Pro):

    unzip mesh_tier5.zip -d ~/mesh_tier5 && bash ~/mesh_tier5/learn_up.sh

What it adds on top of tiers 1-4:
- **Scoring** — every row of `~/.mesh/results.jsonl` gets a 0..1 quality score
  (errors, empty/short output, hedging, retry count, latency, shell exit code).
- **Memory of who is good at what** — observations grouped by task *class*
  (code / reason / shell / notify / http / summary / chat) x node x model,
  ranked by a Wilson lower bound so 2 lucky wins never beat 30 real ones.
- **Learned routing** — `sroute "task"` asks the policy instead of keywords.
- **Auto-escalation** — class confidence below `MESH_ESCALATE_AT` (0.55) or
  fewer than 3 scored samples => the task goes to the 8B on maestro.
- **Human feedback** — `learn --rate <task_id> up|down`, weight 3x.
- **Model judge** — `learn --judge` has the 1.5B second-guess borderline rows;
  scheduled nightly at 03:10 via tier 4.

Files: `~/.mesh/learn.db`, `~/.mesh/policy.json`, `~/.mesh/learn.log`.
23 offline selftests: `mesh_learn.py --selftest`.
Env: MESH_ESCALATE_AT, MESH_JUDGE_MODEL, MESH_BIG_NODE, MESH_MODEL_MAESTRO.
