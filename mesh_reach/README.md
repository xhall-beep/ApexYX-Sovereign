# Mesh tier 6 — REACH (inbox + outbox)

Tier 1 memory → 2 routing → 3 hands → 4 clock → 5 learning → **6 reach**.
Until now the mesh could only be fed by you, from the phone. Tier 6 gives it an
**inbox** (outside world → bus tasks) and an **outbox** (results/digests → out).

## Install (Termux, Pixel 8 Pro)
```bash
unzip mesh_tier6.zip -d ~/mesh_tier6 && cd ~/mesh_tier6
bash reach_up.sh
```
Runs 30 offline selftests, installs `~/bin/mesh_reach.py` (+ `reach`, `mesh-in`,
`mesh-out`), writes `~/.mesh/reach.json` with a random webhook token, adds a
`.termux/boot` hook and a 30-minute flush job on the tier-4 scheduler.

## Inbox — three doors
| door | how it arrives | dedup key |
|---|---|---|
| `telegram` | long-poll `getUpdates` for @WINGMAN_ALLY_BOT / @wingman_core_agent_bot | `update_id` |
| `webhook` | `POST http://127.0.0.1:8770/` with `X-Mesh-Token` | `id` in body |
| `filedrop` | any file dropped in `~/.mesh/inbox/` (`.txt/.md/.json`) | name+mtime |

Every accepted message becomes **exactly one** bus task, routed through tier 5's
learned policy (`route.submit_to: "auto"`) or pinned to a node you name.

## Outbox
Tier-4 digest files, bus rows of `kind=digest`, and anything from
`reach --send` get queued and delivered through the configured sink:
`file` (`~/.mesh/outbox/`), `telegram` (sendMessage), or `http` (JSON POST).
Failures retry at 0/20/90s, then dead-letter — `reach --retry-dead` replays them.

## Enable Telegram
Edit `~/.mesh/reach.json`:
```json
"telegram": {"enabled": true, "token": "<BotFather token>",
             "allow_chat_ids": ["<your chat id>"], "default_chat_id": "<your chat id>"},
"outbound": {"sink": "telegram"}
```
`allow_chat_ids` is an allowlist — anything else is dropped at the door.

## Commands
```
reach --status        inbox/outbox counts, last messages, dead letters
reach --once          one poll + flush cycle
reach --daemon        continuous (installed as boot hook)
mesh-in  "text"       push a task in without Telegram
mesh-out "text" [to]  push a message out
reach --inject "text" inject + route immediately, prints the task id
reach --selftest      30 offline tests, no network
```

## Safety notes
- Webhook binds **127.0.0.1 only** — nothing is exposed to your network.
- Config is chmod 600 (it holds your bot token).
- Tier 6 owns `~/.mesh/reach.db`; tier 1–5 state is never touched.
