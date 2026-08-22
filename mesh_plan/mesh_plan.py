#!/usr/bin/env python3
"""mesh_plan.py — tier 8 of the Pixel mesh: goal decomposition + orchestration.

t1 bus = messages. t2 router = who. t3 exec = hands. t4 sched = clock.
t5 learn = judgement. t6 reach = outside. t7 state = facts.
t8 plan = INTENT: you give an objective, the mesh builds a dependency graph of
tier-3 tasks, dispatches ready steps through the learned router, watches
results.jsonl, replans around failures, and closes the tier-7 open loop.

Storage: ~/.mesh/plan.db (own file; never touches bus/exec/sched/reach/state db).

CLI
  plan new "<objective>" [--project P] [--max-steps N] [--model|--no-model]
  plan ls [--all]                       goals and progress
  plan show <gid>                       steps, deps, state, results
  plan graph <gid>                      ascii dependency graph
  plan tick [--gid G] [--limit N]       dispatch ready steps + collect results
  plan replan <gid> [--why "..."]       rebuild the unfinished tail of a graph
  plan step add <gid> "<text>" [--after ID ...] [--node N] [--kind model|shell|notify|http]
  plan step done <sid> [--note "..."]   mark manually satisfied
  plan step fail <sid> [--why "..."]
  plan cancel <gid>
  plan --daemon [--interval S]
  plan --selftest
"""
import argparse, json, os, re, sqlite3, subprocess, sys, time
from datetime import datetime, timezone

MESH_DIR = os.environ.get("MESH_DIR", os.path.expanduser("~/.mesh"))
DB = os.path.join(MESH_DIR, "plan.db")
RESULTS = os.path.join(MESH_DIR, "results.jsonl")      # tier 3 output
CURSOR = os.path.join(MESH_DIR, "plan.cursor")
OLLAMA = os.environ.get("MESH_OLLAMA", "http://127.0.0.1:11434")
PLANNER_MODEL = os.environ.get("MESH_PLANNER_MODEL", "deepseek-r1-abliterated:8b")
MAX_ATTEMPTS = int(os.environ.get("MESH_PLAN_ATTEMPTS", "2"))
DEFAULT_NODE = os.environ.get("MESH_PLAN_NODE", "maestro")
KINDS = ("model", "shell", "notify", "http")


def now():
    return time.time()


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- schema ----
SCHEMA = """
CREATE TABLE IF NOT EXISTS goal(
  id INTEGER PRIMARY KEY, objective TEXT, project TEXT, state TEXT,
  loop_id TEXT, created REAL, updated REAL, note TEXT, replans INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS step(
  id INTEGER PRIMARY KEY, gid INTEGER, seq INTEGER, text TEXT, kind TEXT,
  node TEXT, deps TEXT, state TEXT, task TEXT, attempts INTEGER DEFAULT 0,
  result TEXT, error TEXT, created REAL, updated REAL);
CREATE INDEX IF NOT EXISTS ix_step_gid ON step(gid, state);
CREATE INDEX IF NOT EXISTS ix_step_task ON step(task);
"""


def db(path=None):
    os.makedirs(MESH_DIR, exist_ok=True)
    c = sqlite3.connect(path or DB, timeout=20)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.commit()
    return c


# ------------------------------------------------------- neighbour tiers ----
def _run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return 127, "", str(e)


def route(text, default=DEFAULT_NODE):
    """Ask tier 5 (learned) then tier 2 (keyword) which node should own a step."""
    if os.environ.get("MESH_PLAN_OFFLINE"):
        return default
    for cmd in (["sroute", text], ["route", text]):
        rc, out, _ = _run(cmd, timeout=20)
        if rc == 0 and out:
            node = out.strip().splitlines()[-1].strip().split()[0].strip(":#")
            if re.fullmatch(r"[A-Za-z0-9_.-]{2,32}", node or ""):
                return node
    return default


def dispatch(text, node, kind="model"):
    """Hand a step to tier 3. Returns task id or None."""
    payload = text if kind == "model" else f"{kind}: {text}"
    if os.environ.get("MESH_PLAN_OFFLINE"):
        return f"offline-{int(now()*1000)%10**9}"
    rc, out, err = _run(["exec", "--submit", payload, "--for", node], timeout=30)
    if rc != 0:
        rc, out, err = _run(["mesh", "task", "--node", "plan", "--text", payload, "--for", node], timeout=30)
    if rc != 0:
        return None
    m = re.search(r"#?(\d{1,12}|[a-z]+-\d+)", out or "")
    return m.group(1) if m else (out.strip()[:32] or None)


def loop_open(title, project=None):
    if os.environ.get("MESH_PLAN_OFFLINE"):
        return None
    args = ["state", "loop", "open", title] + (["--project", project] if project else [])
    rc, out, _ = _run(args, timeout=20)
    if rc != 0:
        return None
    m = re.search(r"(\d{1,9})", out or "")
    return m.group(1) if m else None


def loop_close(loop_id, note=""):
    if loop_id and not os.environ.get("MESH_PLAN_OFFLINE"):
        _run(["state", "loop", "close", str(loop_id)] + (["--note", note[:300]] if note else []), timeout=20)


def state_note(name, key, value):
    if not os.environ.get("MESH_PLAN_OFFLINE"):
        _run(["state", "set", name, f"{key}={value}", "--node", "plan", "--conf", "0.8"], timeout=20)


def brief(project=None):
    if os.environ.get("MESH_PLAN_OFFLINE"):
        return ""
    rc, out, _ = _run(["state", "brief"] + (["--project", project] if project else []) +
                      ["--max-chars", "900"], timeout=25)
    return out if rc == 0 else ""


# ------------------------------------------------------------ decomposer ----
VERB_SPLIT = re.compile(r"(?:\bthen\b|\bafter that\b|\bnext\b|;|\n|(?<=[a-z])\.\s+)", re.I)
NUMBERED = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+(.*)$")
SHELLY = re.compile(r"^(ls|cat|df|free|uptime|pwd|whoami|git|python|pip|curl|termux-\w+)\b")


def heuristic_plan(objective, max_steps=6):
    """Offline decomposition: always yields a runnable, ordered chain."""
    lines = [m.group(1).strip() for l in objective.splitlines() if (m := NUMBERED.match(l))]
    if len(lines) >= 2:
        parts = lines
    else:
        parts = [p.strip(" -•\t") for p in VERB_SPLIT.split(objective) if len(p.strip()) > 3]
    parts = [re.sub(r"\s+", " ", p).strip() for p in parts if p.strip()]
    if len(parts) < 2:
        obj = objective.strip().rstrip(".")
        parts = [
            f"Restate the objective and list unknowns: {obj}",
            f"Gather what the mesh already knows that bears on: {obj}",
            f"Produce the concrete deliverable for: {obj}",
            f"Check the deliverable against the objective and list gaps: {obj}",
        ]
    steps = []
    for i, p in enumerate(parts[:max_steps]):
        kind = "shell" if SHELLY.match(p) else "model"
        steps.append({"text": p[:600], "kind": kind, "deps": [i - 1] if i else []})
    return steps


PLANNER_PROMPT = """You are the planner node of a small on-device agent mesh.
Decompose the OBJECTIVE into at most {n} concrete steps that a worker node can execute.
Each step must be independently checkable. Output ONLY JSON:
{{"steps":[{{"text":"...","kind":"model|shell|notify|http","deps":[0-based indices]}}]}}
Context the mesh already knows:
{brief}
OBJECTIVE: {obj}
JSON:"""


def model_plan(objective, max_steps=6, project=None):
    import urllib.request
    prompt = PLANNER_PROMPT.format(n=max_steps, brief=brief(project)[:900] or "(none)", obj=objective)
    body = json.dumps({"model": PLANNER_MODEL, "prompt": prompt, "stream": False,
                       "options": {"temperature": 0.1, "num_ctx": 8192, "num_predict": 700}}).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body,
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=int(os.environ.get("MESH_PLANNER_TIMEOUT", "300"))) as r:
        out = json.loads(r.read().decode()).get("response", "")
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.S)
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise RuntimeError("planner returned no JSON")
    steps = json.loads(m.group(0)).get("steps", [])
    clean = []
    for i, s in enumerate(steps[:max_steps]):
        t = str(s.get("text", "")).strip()
        if not t:
            continue
        kind = s.get("kind") if s.get("kind") in KINDS else "model"
        deps = [int(d) for d in (s.get("deps") or []) if isinstance(d, (int, float)) and 0 <= int(d) < i]
        clean.append({"text": t[:600], "kind": kind, "deps": deps})
    if len(clean) < 2:
        raise RuntimeError("planner plan too thin")
    return clean


def decompose(objective, max_steps=6, project=None, use_model=True, context=""):
    """context (progress/failures) informs the model planner only — never split into steps."""
    if use_model and not os.environ.get("MESH_PLAN_OFFLINE"):
        try:
            return model_plan((objective + "\n" + context).strip(), max_steps, project), "model"
        except Exception as e:
            print(f"[plan] planner model unavailable ({e}); using heuristic", file=sys.stderr)
    return heuristic_plan(objective, max_steps), "heuristic"


# ----------------------------------------------------------------- goals ----
def new_goal(objective, project=None, max_steps=6, use_model=True):
    c = db()
    steps, how = decompose(objective, max_steps, project, use_model)
    lid = loop_open(f"goal: {objective[:120]}", project)
    c.execute("INSERT INTO goal(objective,project,state,loop_id,created,updated,note) VALUES(?,?,?,?,?,?,?)",
              (objective, project, "active", lid, now(), now(), f"plan:{how}"))
    gid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    ids = []
    for i, s in enumerate(steps):
        deps = json.dumps([ids[d] for d in s["deps"] if d < len(ids)])
        c.execute("""INSERT INTO step(gid,seq,text,kind,node,deps,state,created,updated)
                     VALUES(?,?,?,?,?,?, 'pending', ?,?)""",
                  (gid, i, s["text"], s["kind"], s.get("node") or "", deps, now(), now()))
        ids.append(c.execute("SELECT last_insert_rowid()").fetchone()[0])
    c.commit()
    print(f"goal #{gid} created ({how}, {len(ids)} steps){' loop ' + lid if lid else ''}")
    show(gid)
    return gid


def deps_of(row_deps):
    try:
        return [int(x) for x in json.loads(row_deps or "[]")]
    except Exception:
        return []


def ready_steps(c, gid=None, limit=4):
    q = "SELECT id,gid,text,kind,node,deps,attempts FROM step WHERE state IN ('pending','retry')"
    args = []
    if gid:
        q += " AND gid=?"
        args.append(gid)
    out = []
    for sid, g, text, kind, node, dj, att in c.execute(q + " ORDER BY gid,seq", args).fetchall():
        if c.execute("SELECT state FROM goal WHERE id=?", (g,)).fetchone()[0] != "active":
            continue
        deps = deps_of(dj)
        if deps:
            states = [r[0] for r in c.execute(
                f"SELECT state FROM step WHERE id IN ({','.join('?' * len(deps))})", deps).fetchall()]
            if not states or any(s != "done" for s in states):
                continue
        out.append((sid, g, text, kind, node, att))
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------- collection ----
def collect(c, verbose=True):
    """Match tier-3 results.jsonl entries to dispatched steps."""
    if not os.path.exists(RESULTS):
        return 0, 0
    start = 0
    if os.path.exists(CURSOR):
        try:
            start = int(open(CURSOR).read().strip() or 0)
        except Exception:
            start = 0
    size = os.path.getsize(RESULTS)
    if start > size:
        start = 0
    ok = bad = 0
    with open(RESULTS) as f:
        f.seek(start)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            task = str(r.get("task") or "")
            if not task:
                continue
            row = c.execute("SELECT id,gid,attempts FROM step WHERE task=? AND state='running'", (task,)).fetchone()
            if not row:
                continue
            sid, gid, att = row
            payload = str(r.get("payload") or "")[:4000]
            if r.get("ok"):
                c.execute("UPDATE step SET state='done',result=?,updated=? WHERE id=?", (payload, now(), sid))
                ok += 1
                if verbose:
                    print(f"[plan] step {sid} done (task {task})")
            else:
                dead = att >= MAX_ATTEMPTS
                c.execute("UPDATE step SET state=?,error=?,updated=? WHERE id=?",
                          ("failed" if dead else "retry", payload, now(), sid))
                bad += 1
                if verbose:
                    print(f"[plan] step {sid} {'FAILED' if dead else 'retry'} (task {task})")
            c.commit()
        pos = f.tell()
    open(CURSOR, "w").write(str(pos))
    return ok, bad


def escalate(text, attempts):
    """Second attempt gets more explicit instructions — cheap self-repair."""
    if attempts < 1:
        return text
    return ("Previous attempt failed. Be concrete, minimal and self-contained. "
            "Do exactly this and output the result only:\n" + text)


def tick(gid=None, limit=4, verbose=True):
    c = db()
    ok, bad = collect(c, verbose)
    sent = 0
    for sid, g, text, kind, node, att in ready_steps(c, gid, limit):
        nd = node or route(text)
        task = dispatch(escalate(text, att), nd, kind)
        if not task:
            c.execute("UPDATE step SET state='retry',error=?,attempts=attempts+1,updated=? WHERE id=?",
                      ("dispatch failed: tier 3 unreachable", now(), sid))
            c.commit()
            if verbose:
                print(f"[plan] step {sid} dispatch failed (exec/bus down)")
            continue
        c.execute("UPDATE step SET state='running',node=?,task=?,attempts=attempts+1,updated=? WHERE id=?",
                  (nd, task, now(), sid))
        c.commit()
        sent += 1
        if verbose:
            print(f"[plan] step {sid} → {nd} as task {task}")
    closed = reconcile(c, verbose)
    return sent, ok, bad, closed


def reconcile(c, verbose=True):
    """Finish or block goals; replan around dead ends."""
    closed = 0
    for g, obj, lid, replans in c.execute(
            "SELECT id,objective,loop_id,replans FROM goal WHERE state='active'").fetchall():
        rows = c.execute("SELECT state FROM step WHERE gid=?", (g,)).fetchall()
        states = [r[0] for r in rows]
        if not states:
            continue
        if all(s == "done" for s in states):
            res = " | ".join((r[0] or "")[:200] for r in c.execute(
                "SELECT result FROM step WHERE gid=? ORDER BY seq", (g,)).fetchall())
            c.execute("UPDATE goal SET state='done',updated=?,note=? WHERE id=?", (now(), res[:1500], g))
            c.commit()
            loop_close(lid, f"plan #{g} complete: {res[:200]}")
            state_note(f"goal {g}", "state", "done")
            closed += 1
            if verbose:
                print(f"[plan] goal #{g} COMPLETE")
            continue
        if any(s == "failed" for s in states) and not any(s in ("running", "pending", "retry") for s in states):
            if replans < int(os.environ.get("MESH_PLAN_REPLANS", "1")):
                replan(g, "a step failed permanently", verbose=verbose)
            else:
                c.execute("UPDATE goal SET state='blocked',updated=? WHERE id=?", (now(), g))
                c.commit()
                if verbose:
                    print(f"[plan] goal #{g} BLOCKED (replan budget spent)")
    return closed


def replan(gid, why="", verbose=True):
    """Drop the unfinished tail, re-decompose the remainder given what's done."""
    c = db()
    row = c.execute("SELECT objective,project,replans FROM goal WHERE id=?", (gid,)).fetchone()
    if not row:
        print(f"no goal #{gid}")
        return
    obj, project, replans = row
    done = [r[0] for r in c.execute("SELECT text FROM step WHERE gid=? AND state='done' ORDER BY seq", (gid,)).fetchall()]
    fails = [f"{t} :: {(e or '')[:200]}" for t, e in c.execute(
        "SELECT text,error FROM step WHERE gid=? AND state='failed' ORDER BY seq", (gid,)).fetchall()]
    c.execute("DELETE FROM step WHERE gid=? AND state IN ('pending','retry','failed','running')", (gid,))
    context = (f"Already done: {'; '.join(done) or 'nothing'}\n"
               f"Avoid these failed approaches: {'; '.join(fails) or 'none'}\nReason: {why}")
    steps, how = decompose(obj, 5, project, use_model=True, context=context)
    seq = (c.execute("SELECT COALESCE(MAX(seq),0) FROM step WHERE gid=?", (gid,)).fetchone()[0] or 0) + 1
    ids = []
    for i, s in enumerate(steps):
        deps = json.dumps([ids[d] for d in s["deps"] if d < len(ids)])
        c.execute("""INSERT INTO step(gid,seq,text,kind,node,deps,state,created,updated)
                     VALUES(?,?,?,?,'',?, 'pending', ?,?)""",
                  (gid, seq + i, s["text"], s["kind"], deps, now(), now()))
        ids.append(c.execute("SELECT last_insert_rowid()").fetchone()[0])
    c.execute("UPDATE goal SET replans=?,state='active',updated=? WHERE id=?", (replans + 1, now(), gid))
    c.commit()
    if verbose:
        print(f"[plan] goal #{gid} replanned ({how}): {len(ids)} new steps")


# -------------------------------------------------------------- reporting ----
GLYPH = {"pending": "○", "retry": "◍", "running": "▸", "done": "✓", "failed": "×", "skipped": "-"}


def ls(all_=False):
    c = db()
    q = "SELECT id,objective,project,state,created FROM goal"
    if not all_:
        q += " WHERE state='active'"
    rows = c.execute(q + " ORDER BY id DESC LIMIT 40").fetchall()
    if not rows:
        print("no goals")
        return
    print(f"{'id':<5}{'state':<9}{'progress':<10}{'project':<10}objective")
    for gid, obj, pr, st, cr in rows:
        d = c.execute("SELECT COUNT(*) FROM step WHERE gid=? AND state='done'", (gid,)).fetchone()[0]
        t = c.execute("SELECT COUNT(*) FROM step WHERE gid=?", (gid,)).fetchone()[0]
        print(f"{gid:<5}{st:<9}{f'{d}/{t}':<10}{(pr or '-'):<10}{obj[:60]}")


def show(gid):
    c = db()
    g = c.execute("SELECT objective,project,state,created,replans,loop_id FROM goal WHERE id=?", (gid,)).fetchone()
    if not g:
        print(f"no goal #{gid}")
        return
    print(f"goal #{gid} [{g[2]}] {g[0]}")
    print(f"  project={g[1] or '-'} created={iso(g[3])} replans={g[4]} loop={g[5] or '-'}")
    for sid, seq, text, kind, node, dj, st, task, att, res, err in c.execute(
            """SELECT id,seq,text,kind,node,deps,state,task,attempts,result,error
               FROM step WHERE gid=? ORDER BY seq""", (gid,)).fetchall():
        deps = deps_of(dj)
        print(f"  {GLYPH.get(st, '?')} [{sid}] {text[:74]}")
        print(f"      kind={kind} node={node or '-'} deps={deps or '-'} task={task or '-'} tries={att}")
        if res:
            print(f"      → {res[:160]}")
        if err:
            print(f"      ! {err[:160]}")


def graph(gid):
    c = db()
    rows = c.execute("SELECT id,text,deps,state FROM step WHERE gid=? ORDER BY seq", (gid,)).fetchall()
    if not rows:
        print(f"no steps for goal #{gid}")
        return
    print(f"goal #{gid} dependency graph")
    for sid, text, dj, st in rows:
        deps = deps_of(dj)
        arrow = ("  ".join(f"[{d}]" for d in deps) + " → ") if deps else "start → "
        print(f"  {arrow}{GLYPH.get(st, '?')} [{sid}] {text[:60]}")


def daemon(interval):
    print(f"[plan] orchestrator up poll={interval}s db={DB}", flush=True)
    while True:
        try:
            s, ok, bad, cl = tick(limit=4, verbose=True)
            if s or ok or bad or cl:
                print(f"[plan] cycle: {s} dispatched, {ok} done, {bad} failed, {cl} goals closed", flush=True)
        except Exception as e:
            print(f"[plan] warn: {e}", flush=True)
        time.sleep(interval)


# --------------------------------------------------------------- selftest ----
def selftest():
    import tempfile
    global MESH_DIR, DB, RESULTS, CURSOR
    tmp = tempfile.mkdtemp()
    MESH_DIR, DB = tmp, os.path.join(tmp, "plan.db")
    RESULTS, CURSOR = os.path.join(tmp, "results.jsonl"), os.path.join(tmp, "plan.cursor")
    os.environ["MESH_PLAN_OFFLINE"] = "1"
    ok = []

    def t(name, cond):
        ok.append((name, bool(cond)))

    # decomposition
    h = heuristic_plan("collect logs then summarize them then notify me")
    t("splits on 'then'", len(h) == 3)
    t("chain deps", h[1]["deps"] == [0] and h[2]["deps"] == [1])
    t("vague goal expands", len(heuristic_plan("ship ARAIKI v2")) == 4)
    num = heuristic_plan("1. df -h\n2. summarize disk\n3. notify me")
    t("numbered list parsed", len(num) == 3)
    t("shell kind detected", num[0]["kind"] == "shell")
    t("max-steps honored", len(heuristic_plan("a; b; c; d; e; f; g; h", max_steps=3)) == 3)

    # graph
    gid = new_goal("check disk then report", max_steps=4, use_model=False)
    c = db()
    steps = c.execute("SELECT id,state,deps FROM step WHERE gid=? ORDER BY seq", (gid,)).fetchall()
    t("goal stored", gid == 1 and len(steps) == 2)
    t("first step ready", [s[0] for s in ready_steps(c, gid)] == [steps[0][0]])
    t("blocked step not ready", steps[1][0] not in [s[0] for s in ready_steps(c, gid)])

    # dispatch
    sent, *_ = tick(gid=gid, verbose=False)
    t("one dispatch per cycle", sent == 1)
    task = c.execute("SELECT task FROM step WHERE id=?", (steps[0][0],)).fetchone()[0]
    t("task id recorded", bool(task))
    t("running state", c.execute("SELECT state FROM step WHERE id=?", (steps[0][0],)).fetchone()[0] == "running")
    t("no double dispatch while running", tick(gid=gid, verbose=False)[0] == 0)

    # collect success
    with open(RESULTS, "a") as f:
        f.write(json.dumps({"ts": now(), "node": "maestro", "task": task, "ok": True, "payload": "42G free"}) + "\n")
    sent, done, bad, closed = tick(gid=gid, verbose=False)
    t("result collected", done == 1)
    t("step1 done", c.execute("SELECT state FROM step WHERE id=?", (steps[0][0],)).fetchone()[0] == "done")
    t("dependent dispatched after dep done", sent == 1)
    t("cursor advanced", os.path.exists(CURSOR) and int(open(CURSOR).read()) > 0)

    # collect failure + retry + escalation
    task2 = c.execute("SELECT task FROM step WHERE id=?", (steps[1][0],)).fetchone()[0]
    with open(RESULTS, "a") as f:
        f.write(json.dumps({"ts": now(), "task": task2, "ok": False, "payload": "model timeout"}) + "\n")
    tick(gid=gid, verbose=False)
    t("failure → retry", c.execute("SELECT state FROM step WHERE id=?", (steps[1][0],)).fetchone()[0] in ("retry", "running"))
    t("escalation wraps prompt", escalate("do x", 1).startswith("Previous attempt failed"))
    t("no escalation first try", escalate("do x", 0) == "do x")

    # permanent failure → replan
    c.execute("UPDATE step SET state='running',attempts=9 WHERE id=?", (steps[1][0],))
    c.commit()
    tk = c.execute("SELECT task FROM step WHERE id=?", (steps[1][0],)).fetchone()[0]
    with open(RESULTS, "a") as f:
        f.write(json.dumps({"ts": now(), "task": tk, "ok": False, "payload": "dead"}) + "\n")
    tick(gid=gid, verbose=False)
    g_state = c.execute("SELECT state,replans FROM goal WHERE id=?", (gid,)).fetchone()
    t("replan triggered", g_state[1] == 1)
    t("goal still active after replan", g_state[0] == "active")
    t("replan steps are real work, not context lines",
      not any((r[0] or "").startswith(("Already done", "Avoid these", "Reason:")) for r in
              c.execute("SELECT text FROM step WHERE gid=?", (gid,)).fetchall()))
    t("replan added steps", c.execute("SELECT COUNT(*) FROM step WHERE gid=? AND state='pending'", (gid,)).fetchone()[0] > 0)

    # completion path
    g2 = new_goal("solo objective", max_steps=1, use_model=False)
    tick(gid=g2, verbose=False)
    for (sid, tsk) in c.execute("SELECT id,task FROM step WHERE gid=? AND state='running'", (g2,)).fetchall():
        with open(RESULTS, "a") as f:
            f.write(json.dumps({"ts": now(), "task": tsk, "ok": True, "payload": "done"}) + "\n")
    for _ in range(6):
        tick(gid=g2, verbose=False)
        rows = c.execute("SELECT id,task FROM step WHERE gid=? AND state='running'", (g2,)).fetchall()
        if not rows:
            break
        with open(RESULTS, "a") as f:
            for sid, tsk in rows:
                f.write(json.dumps({"ts": now(), "task": tsk, "ok": True, "payload": "done"}) + "\n")
    t("goal completes", c.execute("SELECT state FROM goal WHERE id=?", (g2,)).fetchone()[0] == "done")

    # isolation + robustness
    t("own db only", set(os.listdir(tmp)) <= {"plan.db", "plan.db-wal", "plan.db-shm", "results.jsonl", "plan.cursor"})
    open(RESULTS, "a").write("not json\n")
    t("garbage line survived", isinstance(collect(c, False), tuple))
    t("deps parse robust", deps_of("junk") == [])
    t("route falls back offline", route("anything") == DEFAULT_NODE)
    c.execute("UPDATE goal SET state='cancelled' WHERE id=?", (gid,))
    c.commit()
    t("cancelled goal not dispatched", tick(gid=gid, verbose=False)[0] == 0)
    t("show/graph/ls run", all(f(gid) is None for f in (show, graph)) and ls(True) is None)
    t("missing goal handled", show(999) is None and graph(999) is None)

    bad = [n for n, v in ok if not v]
    for n, v in ok:
        print(f"  {'ok ' if v else 'FAIL'} {n}")
    print(f"{len(ok) - len(bad)}/{len(ok)} passed")
    return 1 if bad else 0


# -------------------------------------------------------------------- CLI ----
def main():
    ap = argparse.ArgumentParser(prog="plan")
    ap.add_argument("cmd", nargs="*")
    ap.add_argument("--project")
    ap.add_argument("--max-steps", type=int, default=6)
    ap.add_argument("--no-model", action="store_true")
    ap.add_argument("--model", action="store_true")
    ap.add_argument("--gid", type=int)
    ap.add_argument("--limit", type=int, default=4)
    ap.add_argument("--after", action="append", type=int, default=[])
    ap.add_argument("--node")
    ap.add_argument("--kind", default="model", choices=KINDS)
    ap.add_argument("--why", default="")
    ap.add_argument("--note", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if a.daemon:
        return daemon(a.interval)
    cmd = a.cmd[0] if a.cmd else "ls"
    rest = a.cmd[1:]
    if cmd == "new":
        if not rest:
            return print('usage: plan new "<objective>"')
        return new_goal(" ".join(rest), a.project, a.max_steps, use_model=not a.no_model) and None
    if cmd == "ls":
        return ls(a.all)
    if cmd == "show":
        return show(int(rest[0]))
    if cmd == "graph":
        return graph(int(rest[0]))
    if cmd == "tick":
        s, d, f, cl = tick(a.gid, a.limit)
        return print(f"{s} dispatched, {d} done, {f} failed, {cl} goals closed")
    if cmd == "replan":
        return replan(int(rest[0]), a.why)
    if cmd == "cancel":
        c = db()
        c.execute("UPDATE goal SET state='cancelled',updated=? WHERE id=?", (now(), int(rest[0])))
        c.commit()
        return print(f"goal #{rest[0]} cancelled")
    if cmd == "step":
        c = db()
        sub = rest[0] if rest else ""
        if sub == "add":
            gid, text = int(rest[1]), " ".join(rest[2:])
            seq = (c.execute("SELECT COALESCE(MAX(seq),0) FROM step WHERE gid=?", (gid,)).fetchone()[0] or 0) + 1
            c.execute("""INSERT INTO step(gid,seq,text,kind,node,deps,state,created,updated)
                         VALUES(?,?,?,?,?,?, 'pending', ?,?)""",
                      (gid, seq, text, a.kind, a.node or "", json.dumps(a.after), now(), now()))
            c.commit()
            return print(f"step added to goal #{gid}")
        if sub in ("done", "fail"):
            sid = int(rest[1])
            c.execute("UPDATE step SET state=?,result=?,error=?,updated=? WHERE id=?",
                      ("done" if sub == "done" else "failed", a.note or "manual", a.why or None, now(), sid))
            c.commit()
            reconcile(c)
            return print(f"step {sid} marked {sub}")
        return print("usage: plan step add|done|fail ...")
    print(__doc__)


if __name__ == "__main__":
    main()
