# APEXYX Mesh — a self-testing agent economy that runs on a phone

![selftests](https://img.shields.io/badge/selftests-421%2F421_passing-brightgreen) ![python](https://img.shields.io/badge/python-3.9%2B-blue) ![deps](https://img.shields.io/badge/dependencies-none-lightgrey) ![license](https://img.shields.io/badge/license-MIT-green) ![platform](https://img.shields.io/badge/runs_on-Termux%20%7C%20any%20Linux-orange)

Pure Python 3 + bash. No cloud, no daemons you didn't start, no pip installs.
Each tier is one file, one sqlite db in `~/.mesh/`, one installer, and a
hermetic `--selftest` (150–300 checks) that runs in a throwaway tmpdir.

Built and battle-tested on a Google Pixel (Termux). Runs anywhere Python does.

## Install and prove it — one command (Termux or any Linux)

```bash
git clone https://github.com/xhall-beep/ApexYX-Sovereign.git
cd ApexYX-Sovereign && bash up_all.sh
# installs all ten tiers, then runs every selftest — 421 checks, zero network
```

Or tier by tier:

```bash
cd mesh_bus && bash bus_up.sh       # tier 1 first: gives the mesh shared memory
cd ../mesh_router && bash router_up.sh   # repeat per tier, any order
python3 mesh_router.py --selftest      # prove it before you trust it
```

## Free tiers in this repo

| Tier | Node | What it does |
|---|---|---|
| t1 | bus | shared memory: sealed append-only sqlite bus + `mesh` CLI |
| t2 | router | LLM (or keyword-fallback) request routing onto the bus |
| t3 | exec | command execution node |
| t4 | sched | scheduler |
| t5 | learn | feedback / learning loop |
| t6 | reach | outbound reach |
| t7 | state | shared state |
| t8 | plan | planner |
| t9 | guard | policy guard — refusals are the product |
| t10 | mirror | self-mirroring / audit |

Every node degrades gracefully if the bus is offline — install t1 first anyway;
it's what turns nine scripts into one organism.

## Pro: tiers 11–34 (paid)

The full financial-grade stack: federation, sensing, voice, recall, reasoning,
safe-act, negotiation, self-repair, forecasting, budgeting, markets, coalitions,
staking, insurance, reinsurance, solvency, liquidity, clearing/CCP, recovery,
macroprudential control, supervision, accountability, a fiscal backstop, and a polity tier —
mandates, legislature votes, vetoes, sunset clocks and a debt brake deciding
who may aim that backstop —
sealed commitments, escalation ladders, clawbacks, taxpayer-loss accounting.
**5,000+ hermetic self-test checks across the stack.**

→ Buy it: https://stephenhall8.gumroad.com/l/apexyxpro ($49 CAD, instant download)

Next tier in development: **t35 constitutional layer** — amendments, franchise,
succession. Pro buyers get every new tier as it ships.

Full story + free download mirror: https://slackstack-1a8ac6.viktor.page/apexyx-mesh

## Design rules the whole mesh obeys

- Every db write is evented; nodes never re-ingest their own events.
- Sibling dbs are opened read-only. Sealed rows break their hash if mutated.
- Supervisory states never self-clear — lifting a freeze is an explicit act.
- Selftests are hermetic: tmpdir MESH_DIR, simulated siblings, zero network.

MIT licensed. Issues and PRs welcome.

## Related

- **APEXYX Mesh Console** — Android APK (Kivy) running a sealed-chain task bus
  in your pocket: https://github.com/xhall-beep/APEXYX_ORI_AI
