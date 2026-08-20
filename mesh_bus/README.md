# mesh_bus — tier 1: the bus

Shared memory for the whole mesh. One sqlite file, one table, an append-only
sha256 seal chain (edit one byte of history and `mesh verify` names the row),
exactly-once `claim`/`done` handoff, and the `mesh` CLI every other tier
already speaks (`post`, `task`, `export`).

```bash
bash bus_up.sh                 # install + selftest (125 hermetic checks)
mesh post --node me --kind inbox --text "summarize today"
mesh task --node me --text "shell:uptime" --for exec
mesh export | jq .            # what the router and executor poll
mesh verify                   # tamper-evident history
```

No daemon: the bus is passive storage; nodes poll it. Rows from siblings that
write the table directly (tier 7's best-effort `push_bus`) are tolerated as
unsealed and don't break the chain.
