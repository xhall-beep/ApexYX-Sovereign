# Mesh tier 3 — executor loop

Tier 1 = shared memory (mesh_bus). Tier 2 = router (decides who). Tier 3 = **hands**:
per-node workers that claim tasks off the bus, execute them, retry with backoff
(3 attempts: 0s / 20s / 90s), park failures as dead letters, and write every
result back to the bus (`kind=result`) plus `~/.mesh/results.jsonl`.

Install (Termux, Pixel 8 Pro):
    tar xzf mesh_exec.tar.gz && cd mesh_exec && bash exec_up.sh

Standalone-safe: state lives in `~/.mesh/exec.db`, tier 1's schema is never
modified; if the `mesh` CLI or ollama is missing, tasks queue locally instead
of vanishing. `exec --selftest` proves the loop offline (6 checks).

Handlers: plain text -> node model via ollama (maestro = deepseek-r1 8B, others
1.5B) | `shell:<cmd>` allowlisted | `notify:<msg>` termux-notification | `http:<url>`.
Env: MESH_MAX_ATTEMPTS, MESH_MODEL_MAESTRO, MESH_MODEL_TIMEOUT, MESH_HOOK_<NODE>, OLLAMA_HOST.
