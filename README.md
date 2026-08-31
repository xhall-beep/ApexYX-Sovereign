# APEXYX Mesh — a self-testing agent economy that runs on a phone

![selftests](https://img.shields.io/badge/selftests-421%2F421_passing-brightgreen) ![python](https://img.shields.io/badge/python-3.9%2B-blue) ![deps](https://img.shields.io/badge/dependencies-none-lightgrey) ![license](https://img.shields.io/badge/license-MIT-green) ![platform](https://img.shields.io/badge/runs_on-Termux%20%7C%20any%20Linux-orange)

`termux` · `android` · `local-ai` · `agentic` · `offline-first` · `self-hosted` · `sqlite`

<p align="center"><img src="assets/mesh-demo.svg" alt="mesh CLI demo: post, task, tail, verify, status" width="720"></p>

Pure Python 3 + bash. No cloud, no daemons you didn't start, no pip installs.
Each tier is one file, one sqlite db in `~/.mesh/`, one installer, and a
hermetic `--selftest` (150–300 checks) that runs in a throwaway tmpdir.

Built and battle-tested on a Google Pixel (Termux). Runs anywhere Python does.

## Install and prove it — one command (Termux or any Linux)

```bash
# Termux only: pkg install python git   (Linux: python3 + git, already there)
git clone https://github.com/xhall-beep/ApexYX-Sovereign.git
cd ApexYX-Sovereign && bash up_all.sh
# installs all ten tiers, then runs every selftest — 421 checks, zero network
```

## Your first 60 seconds on the bus

```bash
mesh post --node me --kind inbox --text "hello mesh"   # feed the router
mesh task --node me --text "echo hi" --for exec        # address a worker
mesh tail                                              # watch the event stream
mesh verify                                            # prove the sealed chain is intact
mesh status                                            # who's alive, what's pending
```

If `mesh` isn't found, open a new shell (the installer adds `~/bin` to PATH).

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

## Pro: tiers 11–36 (paid)

The full financial-grade stack: federation, sensing, voice, recall, reasoning,
safe-act, negotiation, self-repair, forecasting, budgeting, markets, coalitions,
staking, insurance, reinsurance, solvency, liquidity, clearing/CCP, recovery,
macroprudential control, supervision, accountability, a fiscal backstop, a polity tier —
mandates, legislature votes, vetoes, sunset clocks and a debt brake deciding
who may aim that backstop — and now a **constitutional layer**:
sealed commitments, escalation ladders, clawbacks, taxpayer-loss accounting.
**5,000+ hermetic self-test checks across the stack.**

→ Buy it: https://stephenhall8.gumroad.com/l/apexyxpro ($49 CAD, instant download)

**NEW — t37 treaties just shipped:** the mesh now has a foreign policy that
cannot outrank its constitution. A citizen proposes a compact against a named
foreign mesh; ratification takes a majority of the WHOLE t35 franchise (one
node one vote) AND the counterparty's countersign — a hash bound to the exact
sealed treaty text, so text tampering or seal-swapping voids accession. The
entrenched core (art.seals, art.sunset, art.onevote) is non-derogable: a
compact touching it cannot even be proposed. Entry into force grants mutual
recognition and comity — foreign parties gain standing to file breach claims
via a pubseal-derived token. Breach arbitration is adversarial (claim AND
reply sealed before a deterministic 3-node tribunal): majority finds the
breach founded, awards are enforceable on a 14-day clock — reparations,
suspension-that-marks, or a denunciation referral (arbitrators cannot kill a
treaty; termination goes back to the franchise). Exit is lawful but never
silent: a sealed 14-day notice window, or sunset after the term. Treaties per
counterparty and awards per treaty hash-chain into tamper-evident lineages.
69 hermetic selftest checks. t36 judiciary (52) and t35 constitution (43)
shipped previously. Pro buyers get every new tier as it ships — re-open your
download link for the updated bundle.

Next tier in development: **t38 succession-of-state** — what happens to
treaties, case law, and the franchise when a mesh forks, merges, or migrates
hardware.

Full story + free download mirror: https://slackstack-1a8ac6.viktor.page/apexyx-mesh

## Design rules the whole mesh obeys

- Every db write is evented; nodes never re-ingest their own events.
- Sibling dbs are opened read-only. Sealed rows break their hash if mutated.
- Supervisory states never self-clear — lifting a freeze is an explicit act.
- Selftests are hermetic: tmpdir MESH_DIR, simulated siblings, zero network.

MIT licensed. Issues and PRs welcome.

## Deep dive

- [Running a 10-tier SQLite Agent Mesh on Termux without Thermal Throttling](docs/termux-tutorial.md) — the engineering: tick-driven architecture vs daemons, WAL as the bus, thermal admission control, phantom-process-killer survival.

## Discussion

- Hacker News: https://news.ycombinator.com/item?id=49398962

## Related

- **APEXYX Mesh Console** — Android APK (Kivy) running a sealed-chain task bus
  in your pocket: https://github.com/xhall-beep/APEXYX_ORI_AI
