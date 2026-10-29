#!/usr/bin/env python3
"""sovereign_core.py — the conductor: one process that runs the whole mesh loop.

The ten tiers each ship as their own CLI/daemon and coordinate through the
bus and the `mesh` command. That works, but nothing in the repo ever runs
the organism end-to-end: the router daemon routes, the exec daemon executes,
the scheduler ticks, the guard daemon sweeps — four processes, four chances
for Android's phantom-process killer to quietly end the story. The design
doc (docs/termux-tutorial.md) already names the fix: "daemons are an
optimization, ticks are the contract — a single short-lived process that
evaluates everything, posts work items onto the bus, and exits."

sovereign_core is that process. One tick:

  1. sched    mesh_sched.tick() fires due schedules/triggers onto the bus
  2. route    unrouted kind=inbox items go through mesh_router (model engine
              with the deterministic keyword fallback) and are re-posted as
              addressed kind=task items
  3. admit    every new task is pre-flighted through mesh_guard.decide();
              DENY closes the task with a denial result, DEFER leaves it
              queued for a later tick, ALLOW claims it (exactly-once, tier 1
              semantics) and opens a real guard lease
  4. exec     the task runs through mesh_exec's handler dispatcher
              (shell allowlist / http / notify / local model)
  5. report   the guard lease is released (usage lands in the guard budget
              ledger), the result is appended to results.jsonl (so tier 4
              digests see it) and posted back to the bus as kind=result,
              and the task is marked done

Usage
  sovereign_core.py --once [--limit N] [--offline]   one tick, JSON summary, exit
  sovereign_core.py --daemon [--interval S]          tick forever
  sovereign_core.py --status                         bus + guard state
  sovereign_core.py --selftest                       hermetic proof (tmpdir)

Like every tier: Python 3 stdlib only, all state under MESH_DIR
(~/.mesh by default), zero network of its own (the router/exec model
calls are the tiers' own). If Ollama is down the router falls back to
keywords and model tasks fail as ordinary task failures — the loop
itself never stops.
"""
import argparse, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_NODE = "sovereign"
TIER_DIRS = ("mesh_bus", "mesh_router", "mesh_exec", "mesh_sched", "mesh_guard")

bus = router = exec_t = sched = guard = None   # tier modules, loaded by _load()


def _load(mesh_dir):
    """Import the tier modules with MESH_DIR pinned to one directory.

    Tier modules read MESH_DIR from the environment at import time and
    derive their db paths from it; set the env first, then pin the module
    globals anyway so a host process that imported them earlier cannot
    desync the core from its tiers.
    """
    global bus, router, exec_t, sched, guard, SERVED
    os.environ["MESH_DIR"] = mesh_dir
    for d in TIER_DIRS:
        p = os.path.join(HERE, d)
        if p not in sys.path:
            sys.path.insert(0, p)
    import mesh_bus, mesh_router, mesh_exec, mesh_sched, mesh_guard
    bus, router, exec_t, sched, guard = mesh_bus, mesh_router, mesh_exec, mesh_sched, mesh_guard
    bus.MESH_DIR = mesh_dir
    guard.MESH_DIR = mesh_dir
    guard.DB = os.path.join(mesh_dir, "guard.db")
    guard.AUDIT = os.path.join(mesh_dir, "guard.audit.jsonl")
    guard.FLAG = os.path.join(mesh_dir, "guard.state")
    exec_t.MESH_DIR = mesh_dir
    exec_t.EXEC_DB = os.path.join(mesh_dir, "exec.db")
    exec_t.RESULTS = os.path.join(mesh_dir, "results.jsonl")
    sched.MESH_DIR = mesh_dir
    sched.SCHED_DB = os.path.join(mesh_dir, "sched.db")
    sched.RESULTS = os.path.join(mesh_dir, "results.jsonl")
    sched.DIGEST_DIR = os.path.join(mesh_dir, "digests")
    SERVED = set(router.NODES) | {"exec", CORE_NODE, ""}
    _ensure_mesh_cli(mesh_dir)


def _ensure_mesh_cli(mesh_dir):
    """Give the tiers the `mesh` command they shell out to (tier 4's submit
    path, tier 3's fallbacks), pointing at THIS repo's bus — the same wrapper
    bus_up.sh installs into ~/bin, kept inside MESH_DIR so the core is
    self-contained and hermetic runs never touch the real install."""
    bin_dir = os.path.join(mesh_dir, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    shim = os.path.join(bin_dir, "mesh")
    body = '#!/bin/sh\nexec python3 "%s" "$@"\n' % os.path.join(HERE, "mesh_bus", "mesh_bus.py")
    if not os.path.exists(shim) or open(shim).read() != body:
        with open(shim, "w") as f:
            f.write(body)
        os.chmod(shim, 0o755)
    if bin_dir not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")


# ------------------------------------------------------------- task kinds ---
def guard_kind(text):
    """Map a task to the guard admission class of its handler (tier 3's
    prefix scheme); anything unprefixed is a model call."""
    t = text.strip()
    if t.startswith("shell:"):
        return "shell"
    if t.startswith("http:") and "://" in t:
        return "http"
    if t.startswith("notify:"):
        return "notify"
    return "model"


def est_tokens(text):
    """Rough pre-admission token estimate for model tasks (~4 chars/token).
    The guard releases model leases against this estimate — tier 3's model
    handler does not surface provider token counts."""
    return max(1.0, len(text) / 4.0)


SERVED = None   # set in _load: router nodes + the generic worker targets


def eff_node(tgt):
    """The persona that executes a task addressed to `tgt`. Router nodes
    execute as themselves; generic targets (exec / sovereign / untargeted)
    execute as maestro, tier 3's default worker."""
    return tgt if tgt in router.NODES else "maestro"


# ----------------------------------------------------------------- cycle ----
def cycle(limit=25, offline=False, verbose=False):
    """One full tick. Returns a summary dict."""
    bc = bus.db()
    gc = guard.db()
    out = {"sched_fired": 0, "routed": 0, "done": 0, "failed": 0,
           "deferred": 0, "denied": 0}

    # 1. scheduler: due jobs/triggers post their tasks onto the bus
    try:
        out["sched_fired"] = sched.tick(verbose=False)
    except Exception as e:
        if verbose:
            print("[core] sched tick failed: %s" % e, flush=True)

    # 2. route unrouted inbox items into addressed tasks
    for it in bus.export(bc, kind="inbox"):
        if it["state"] != "new":
            continue
        d = router.route(it["text"], force_keyword=offline)
        node = d["node"] if d.get("node") in router.NODES else "maestro"
        if not bus.claim(bc, it["id"], CORE_NODE):
            continue
        bus.post(bc, CORE_NODE, "task", it["text"], tgt=node)
        bus.done(bc, it["id"], CORE_NODE)
        out["routed"] += 1
        if verbose:
            print("[core] routed #%s -> %s (%s)" % (it["id"], node, d.get("engine")), flush=True)

    # 3-5. admit, execute, report
    worked = 0
    for it in bus.export(bc, kind="task"):
        if worked >= limit:
            break
        if it["state"] != "new" or it["for"] not in SERVED:
            continue
        node = eff_node(it["for"])
        kind = guard_kind(it["text"])
        est = est_tokens(it["text"]) if kind == "model" else 0.0

        verdict, why, _retry = guard.decide(gc, kind, node=node, tokens=est)
        if verdict == guard.DEFER:
            out["deferred"] += 1
            continue                       # stays 'new'; a later tick retries
        if verdict == guard.DENY:
            if bus.claim(bc, it["id"], CORE_NODE):
                _report(bc, node, it["id"], False, "guard denied: %s" % why)
                bus.done(bc, it["id"], CORE_NODE)
                out["denied"] += 1
            continue

        if not bus.claim(bc, it["id"], CORE_NODE):
            continue                       # another worker won it; fine
        worked += 1
        v, why, _r, lease = guard.admit(gc, kind, node=node, tokens=est,
                                        task=it["text"])
        if v != guard.ALLOW:               # lost a race with the sensors
            _report(bc, node, it["id"], False, "guard %s after claim: %s" % (v, why))
            bus.done(bc, it["id"], CORE_NODE)
            out["deferred" if v == guard.DEFER else "denied"] += 1
            continue
        try:
            res = exec_t.execute(node, it["text"])
            guard.release(gc, lease, tokens=est, ok=True)
            _report(bc, node, it["id"], True, res)
            out["done"] += 1
            if verbose:
                print("[core] #%s done (%s, %d chars)" % (it["id"], node, len(res)), flush=True)
        except Exception as e:
            guard.release(gc, lease, tokens=est, ok=False)
            _report(bc, node, it["id"], False, str(e)[:500])
            out["failed"] += 1
            if verbose:
                print("[core] #%s failed (%s): %s" % (it["id"], node, e), flush=True)
        bus.done(bc, it["id"], CORE_NODE)
    return out


def _report(bc, node, bus_id, ok, payload):
    """Result onto the bus (tier 3's shape) + results.jsonl for tier 4."""
    body = json.dumps({"task": bus_id, "ok": ok, "result": str(payload)[:1500]})
    bus.post(bc, node, "result", body[:3500])
    exec_t.log_result(node, bus_id, ok, payload)


def status(mesh_dir):
    _load(mesh_dir)
    bc, gc = bus.db(), guard.db()
    st = bus.status(bc)
    st["guard_flag"] = guard.flag_state(gc)
    st["guard_open_leases"] = len(guard.open_leases(gc))
    return st


# --------------------------------------------------------------- selftest ---
def selftest():
    import shutil, tempfile
    tmp = tempfile.mkdtemp(prefix="sovereigntest")
    _load(tmp)
    ok = fail = 0

    def T(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  ok   %s" % name)
        else:
            fail += 1
            print("  FAIL %s" % name)

    bc = bus.db()

    # boot: the tiers' state files materialise under the tmp MESH_DIR
    guard.db()
    T("boots: bus + guard dbs created",
      os.path.exists(os.path.join(tmp, "bus.db")) and
      os.path.exists(os.path.join(tmp, "guard.db")))
    T("mesh CLI shim installed", os.access(os.path.join(tmp, "bin", "mesh"), os.X_OK))

    # one addressed task, end to end
    tid = bus.post(bc, "tester", "task", "shell:echo sovereign-ok", tgt="maestro")
    s1 = cycle(offline=True)
    row = bc.execute("SELECT state FROM messages WHERE id=?", (tid,)).fetchone()
    T("task claimed and closed", row["state"] == "done" and s1["done"] == 1)
    results = [json.loads(r["text"]) for r in bus.export(bc, kind="result")]
    T("result written back to bus",
      any(r.get("task") == tid and r.get("ok") and "sovereign-ok" in r.get("result", "")
          for r in results))
    gc = guard.db()
    T("guard lease opened and closed", len(guard.open_leases(gc)) == 0)
    T("guard recorded shell usage", guard.used(gc, "shell")[0] >= 1)
    T("guard audit chain verifies", guard.audit_verify()[0])

    # one unrouted inbox item: routed, dispatched, executed
    iid = bus.post(bc, "tester", "inbox", "shell:echo routed-ok")
    s2 = cycle(offline=True)
    irow = bc.execute("SELECT state FROM messages WHERE id=?", (iid,)).fetchone()
    results = [json.loads(r["text"]) for r in bus.export(bc, kind="result")]
    T("inbox routed and executed",
      s2["routed"] == 1 and irow["state"] == "done" and
      any(r.get("ok") and "routed-ok" in r.get("result", "") for r in results))

    # guard denial closes a task with a denial result instead of spinning
    guard.kv_set(gc, "budgets", {"notify": [0, 3600]})
    nid = bus.post(bc, "tester", "task", "notify:budget says no", tgt="local")
    s3 = cycle(offline=True)
    nrow = bc.execute("SELECT state FROM messages WHERE id=?", (nid,)).fetchone()
    results = [json.loads(r["text"]) for r in bus.export(bc, kind="result")]
    T("guard-denied task closed with denial",
      s3["denied"] == 1 and nrow["state"] == "done" and
      any(r.get("task") == nid and not r.get("ok") and "guard denied" in r.get("result", "")
          for r in results))
    guard.kv_set(gc, "budgets", {})

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n%d passed, %d failed" % (ok, fail))
    return 0 if fail == 0 else 1


# -------------------------------------------------------------------- cli ---
def main():
    ap = argparse.ArgumentParser(prog="sovereign_core",
                                 description="mesh conductor — one tick runs the whole loop")
    ap.add_argument("--mesh-dir", default=os.environ.get("MESH_DIR",
                    os.path.expanduser("~/.mesh")))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--offline", action="store_true",
                    help="keyword routing only (no router model call)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    if a.status:
        print(json.dumps(status(a.mesh_dir), indent=2))
        return 0
    _load(a.mesh_dir)
    if a.daemon:
        print("[core] sovereign up mesh_dir=%s interval=%ds" % (a.mesh_dir, a.interval),
              flush=True)
        while True:
            try:
                s = cycle(limit=a.limit, offline=a.offline, verbose=True)
                if any(s.values()):
                    print("[core] tick %s" % json.dumps(s), flush=True)
            except Exception as e:
                print("[core] tick error: %s" % e, flush=True)
            time.sleep(a.interval)
    s = cycle(limit=a.limit, offline=a.offline, verbose=True)
    print(json.dumps(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
