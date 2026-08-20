# APEXYX Mesh — a self-testing agent economy that runs on a phone

Pure Python 3 + bash. No cloud, no daemons you didn't start, no pip installs.
Each tier is one file, one sqlite db in `~/.mesh/`, one installer, and a
hermetic `--selftest` (150–300 checks) that runs in a throwaway tmpdir.

Built and battle-tested on a Google Pixel (Termux). Runs anywhere Python does.

## Install (Termux or any Linux)

```bash
cd mesh_router && bash router_up.sh   # repeat per tier, any order
python3 mesh_router.py --selftest      # prove it before you trust it
```

## Free tiers in this repo

| Tier | Node | What it does |
|---|---|---|
| t2 | router | LLM (or keyword-fallback) request routing onto the bus |
| t3 | exec | command execution node |
| t4 | sched | scheduler |
| t5 | learn | feedback / learning loop |
| t6 | reach | outbound reach |
| t7 | state | shared state |
| t8 | plan | planner |
| t9 | guard | policy guard — refusals are the product |
| t10 | mirror | self-mirroring / audit |

t1 (bus) ships with the Pro pack's installer or bring your own event bus —
every node degrades gracefully without it.

## Pro: tiers 11–33 (paid)

The full financial-grade stack: federation, sensing, voice, recall, reasoning,
safe-act, negotiation, self-repair, forecasting, budgeting, markets, coalitions,
staking, insurance, reinsurance, solvency, liquidity, clearing/CCP, recovery,
macroprudential control, supervision, accountability, and a fiscal backstop —
sealed commitments, escalation ladders, clawbacks, taxpayer-loss accounting.
**5,000+ hermetic self-test checks across the stack.**

→ Get it: https://slackstack-1a8ac6.viktor.page/apexyx-mesh

## Design rules the whole mesh obeys

- Every db write is evented; nodes never re-ingest their own events.
- Sibling dbs are opened read-only. Sealed rows break their hash if mutated.
- Supervisory states never self-clear — lifting a freeze is an explicit act.
- Selftests are hermetic: tmpdir MESH_DIR, simulated siblings, zero network.

MIT licensed. Issues and PRs welcome.
