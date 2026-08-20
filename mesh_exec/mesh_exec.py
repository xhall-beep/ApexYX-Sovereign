#!/usr/bin/env python3
"""
mesh_exec.py — mesh tier 3: the EXECUTOR LOOP.

Tier 1 gave the mesh shared memory (mesh_bus.py).
Tier 2 gave it a brain that decides who does what (mesh_router.py).
Tier 3 gives it HANDS: workers that pull tasks off the bus, run them,
retry on failure with backoff, and write every result back to the bus.

  ./mesh_exec.py --selftest              # offline proof it works (no bus, no ollama)
  ./mesh_exec.py --once --node maestro   # drain the queue for one node, then exit
  ./mesh_exec.py --daemon --node maestro # worker loop (one per node; run under mesh_up)
  ./mesh_exec.py --status                # queue + attempt + failure dashboard
  ./mesh_exec.py --retry 42              # force-requeue a dead task
  ./mesh_exec.py --submit "text" --for maestro   # push a task without the router

State lives in ~/.mesh/exec.db (separate from the bus, so tier 1's schema is
never touched). Results are appended to ~/.mesh/results.jsonl AND posted back
to the bus as kind=result, so any node can read them.

Handlers (chosen by task text / prefix):
  shell:<cmd>   -> allowlisted shell (see ALLOW)
  http:<url>    -> GET/POST via env MESH_HOOK_<NODE>
  notify:<msg>  -> termux-notification if available
  anything else -> local model (ollama) using that node's model
"""
import argparse, json, os, re, shlex, sqlite3, subprocess, sys, time, urllib.request
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
MESH_DIR = os.environ.get("MESH_DIR", os.path.join(HOME, ".mesh"))
EXEC_DB = os.path.join(MESH_DIR, "exec.db")
RESULTS = os.path.join(MESH_DIR, "results.jsonl")
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")

NODE_MODEL = {
    "maestro":      os.environ.get("MESH_MODEL_MAESTRO", "deepseek-r1-abliterated:8b"),
    "local":        os.environ.get("MESH_ROUTER_MODEL", "qwen2.5:1.5b"),
    "wingman_ally": os.environ.get("MESH_MODEL_ALLY", "qwen2.5:1.5b"),
    "wingman_core": os.environ.get("MESH_MODEL_CORE", "qwen2.5:1.5b"),
}
MAX_ATTEMPTS = int(os.environ.get("MESH_MAX_ATTEMPTS", "3"))
BACKOFF = [0, 20, 90]          # seconds before attempt 1,2,3
ALLOW = {"ls", "df", "free", "uptime", "date", "pwd", "cat", "head", "tail", "grep",
         "wc", "git", "python", "python3", "termux-battery-status", "nproc", "echo", "mesh"}


def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")

# ---------------- state ------------------------------------------------------
def db():
    os.makedirs(MESH_DIR, exist_ok=True)
    c = sqlite3.connect(EXEC_DB, timeout=30)
    c.execute("""CREATE TABLE IF NOT EXISTS work(
        bus_id TEXT PRIMARY KEY, node TEXT, text TEXT, state TEXT,
        attempts INT DEFAULT 0, next_at REAL DEFAULT 0,
        result TEXT, error TEXT, created TEXT, updated TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_state ON work(state,node,next_at)")
    c.commit()
    return c

def upsert(c, bus_id, node, text):
    c.execute("""INSERT OR IGNORE INTO work(bus_id,node,text,state,created,updated)
                 VALUES(?,?,?, 'queued', ?, ?)""", (str(bus_id), node, text, now(), now()))
    c.commit()

# ---------------- bus bridge -------------------------------------------------
def _mesh(*args, timeout=25):
    try:
        p = subprocess.run(["mesh", *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return 127, "", str(e)

def bus_pull(node):
    """Read kind=task items addressed to `node` from the tier-1 bus."""
    rc, out, _ = _mesh("export")
    if rc != 0:
        return []
    try:
        items = json.loads(out or "[]")
    except Exception:
        return []
    if isinstance(items, dict):
        items = items.get("items", [])
    got = []
    for it in items:
        if it.get("kind") != "task":
            continue
        tgt = it.get("for") or it.get("target") or it.get("to") or ""
        if node and tgt and tgt != node:
            continue
        got.append((it.get("id"), it.get("text", ""), tgt or node))
    return got

def bus_post_result(node, bus_id, ok, payload):
    body = json.dumps({"task": bus_id, "ok": ok, "result": payload})[:3500]
    _mesh("post", "--node", node, "--kind", "result", "--text", body)

# ---------------- handlers ---------------------------------------------------
def h_shell(cmd):
    parts = shlex.split(cmd)
    if not parts or parts[0] not in ALLOW:
        raise RuntimeError(f"blocked command: {parts[0] if parts else '(empty)'}")
    p = subprocess.run(parts, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip()[:500])
    return p.stdout.strip()[:4000]

def h_http(node, url):
    hook = os.environ.get(f"MESH_HOOK_{node.upper()}")
    target = url or hook
    if not target:
        raise RuntimeError(f"no URL and no MESH_HOOK_{node.upper()} set")
    with urllib.request.urlopen(target, timeout=30) as r:
        return r.read().decode(errors="replace")[:4000]

def h_notify(msg):
    try:
        subprocess.run(["termux-notification", "--title", "mesh", "--content", msg[:200]],
                       capture_output=True, timeout=15)
        return f"notified: {msg[:120]}"
    except Exception:
        return f"(no termux-api) {msg[:120]}"

def h_model(node, text):
    model = NODE_MODEL.get(node, NODE_MODEL["local"])
    body = json.dumps({"model": model, "prompt": text, "stream": False,
                       "options": {"temperature": 0.2, "num_ctx": 8192, "num_predict": 512}}).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=int(os.environ.get("MESH_MODEL_TIMEOUT", "240"))) as r:
        out = json.loads(r.read().decode()).get("response", "")
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()
    if not out:
        raise RuntimeError("empty model response")
    return out[:6000]

def execute(node, text):
    t = text.strip()
    if t.startswith("shell:"):  return h_shell(t[6:].strip())
    if t.startswith("http:") and "://" in t: return h_http(node, t[5:].strip())
    if t.startswith("notify:"): return h_notify(t[7:].strip())
    return h_model(node, t)

# ---------------- loop -------------------------------------------------------
def drain(node, limit=25, verbose=True):
    c = db()
    for bid, text, tgt in bus_pull(node):
        upsert(c, bid, tgt, text)
    rows = c.execute("""SELECT bus_id,node,text,attempts FROM work
                        WHERE state IN ('queued','retry') AND node=? AND next_at<=?
                        ORDER BY attempts, created LIMIT ?""",
                     (node, time.time(), limit)).fetchall()
    done = fail = 0
    for bid, nd, text, attempts in rows:
        c.execute("UPDATE work SET state='running',updated=? WHERE bus_id=?", (now(), bid))
        c.commit()
        try:
            res = execute(nd, text)
            c.execute("UPDATE work SET state='done',result=?,attempts=?,updated=? WHERE bus_id=?",
                      (res, attempts + 1, now(), bid)); c.commit()
            log_result(nd, bid, True, res); bus_post_result(nd, bid, True, res[:1500]); done += 1
            if verbose: print(f"[exec] #{bid} done ({len(res)} chars)", flush=True)
        except Exception as e:
            a = attempts + 1
            dead = a >= MAX_ATTEMPTS
            nxt = 0 if dead else time.time() + BACKOFF[min(a, len(BACKOFF) - 1)]
            c.execute("UPDATE work SET state=?,error=?,attempts=?,next_at=?,updated=? WHERE bus_id=?",
                      ("dead" if dead else "retry", str(e)[:500], a, nxt, now(), bid)); c.commit()
            log_result(nd, bid, False, str(e))
            if dead: bus_post_result(nd, bid, False, str(e)[:500])
            fail += 1
            if verbose: print(f"[exec] #{bid} {'DEAD' if dead else 'retry'} attempt {a}: {e}", flush=True)
    return done, fail

def log_result(node, bid, ok, payload):
    os.makedirs(MESH_DIR, exist_ok=True)
    with open(RESULTS, "a") as f:
        f.write(json.dumps({"ts": now(), "node": node, "task": bid, "ok": ok,
                            "payload": str(payload)[:4000]}) + "\n")

def daemon(node, interval):
    print(f"[exec] worker up node={node} model={NODE_MODEL.get(node)} poll={interval}s", flush=True)
    while True:
        try:
            d, f = drain(node)
            if d or f: print(f"[exec] cycle: {d} done, {f} failed", flush=True)
        except Exception as e:
            print(f"[exec] warn: {e}", flush=True)
        time.sleep(interval)

def status():
    c = db()
    rows = c.execute("SELECT node,state,COUNT(*),SUM(attempts) FROM work GROUP BY node,state").fetchall()
    if not rows:
        print("queue empty"); return
    print(f"{'node':<14}{'state':<10}{'count':>6}{'attempts':>10}")
    for n, s, cnt, at in rows:
        print(f"{n:<14}{s:<10}{cnt:>6}{(at or 0):>10}")
    dead = c.execute("SELECT bus_id,node,substr(text,1,50),error FROM work WHERE state='dead'").fetchall()
    for d in dead:
        print(f"  DEAD #{d[0]} [{d[1]}] {d[2]!r} :: {d[3]}")

# ---------------- selftest ---------------------------------------------------
def selftest():
    global MESH_DIR, EXEC_DB, RESULTS, MAX_ATTEMPTS
    import tempfile
    MESH_DIR = tempfile.mkdtemp(); EXEC_DB = os.path.join(MESH_DIR, "exec.db")
    RESULTS = os.path.join(MESH_DIR, "results.jsonl")
    ok = []
    c = db()
    upsert(c, "t1", "maestro", "shell:echo mesh-online")
    upsert(c, "t2", "maestro", "shell:rm -rf /")          # must be blocked -> retries -> dead
    upsert(c, "t3", "maestro", "notify:tier3 live")
    d, f = drain("maestro", verbose=False)
    ok.append(("shell handler runs", d >= 2))
    ok.append(("unsafe command blocked", f == 1))
    r = c.execute("SELECT result FROM work WHERE bus_id='t1'").fetchone()[0]
    ok.append(("result captured", "mesh-online" in r))
    for _ in range(MAX_ATTEMPTS):
        c.execute("UPDATE work SET next_at=0 WHERE bus_id='t2'"); c.commit()
        drain("maestro", verbose=False)
    st, att = c.execute("SELECT state,attempts FROM work WHERE bus_id='t2'").fetchone()
    ok.append((f"retry+backoff then dead (attempts={att})", st == "dead" and att == MAX_ATTEMPTS))
    ok.append(("results.jsonl written", os.path.exists(RESULTS) and len(open(RESULTS).read().splitlines()) >= 4))
    ok.append(("idempotent claim", (upsert(c, "t1", "maestro", "x") or
               c.execute("SELECT COUNT(*) FROM work WHERE bus_id='t1'").fetchone()[0]) == 1))
    for name, good in ok:
        print(f"{'PASS' if good else 'FAIL'}  {name}")
    bad = sum(1 for _, g in ok if not g)
    print(f"\n{len(ok)-bad}/{len(ok)} checks passed (bus + ollama not required)")
    return 1 if bad else 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", default="maestro")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--interval", type=int, default=10)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--retry")
    ap.add_argument("--submit")
    ap.add_argument("--for", dest="target", default="maestro")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest: sys.exit(selftest())
    if a.status:   return status()
    if a.retry:
        c = db(); c.execute("UPDATE work SET state='retry',attempts=0,next_at=0,updated=? WHERE bus_id=?",
                            (now(), a.retry)); c.commit(); print(f"requeued #{a.retry}"); return
    if a.submit:
        rc, out, err = _mesh("task", "--node", "cli", "--text", a.submit, "--for", a.target)
        if rc != 0:
            c = db(); bid = f"local-{int(time.time())}"; upsert(c, bid, a.target, a.submit)
            print(f"bus unavailable ({err or 'no mesh CLI'}) — queued locally as #{bid}")
        else:
            print(out or "submitted")
        return
    if a.daemon: daemon(a.node, a.interval); return
    d, f = drain(a.node)
    print(f"{d} done, {f} failed/retrying")

if __name__ == "__main__":
    main()
