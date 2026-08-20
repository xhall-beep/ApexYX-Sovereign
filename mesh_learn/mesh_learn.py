#!/usr/bin/env python3
"""
mesh_learn.py — mesh tier 5: the FEEDBACK LOOP.

t1 bus = memory. t2 router = brain. t3 exec = hands. t4 sched = clock.
t5 gives the mesh JUDGEMENT: every result is scored, per-node/per-model history
accumulates, and routing decisions start coming from evidence instead of
keyword guesses — with automatic escalation 1.5B -> 8B when confidence is low.

  ./mesh_learn.py --selftest             # offline proof (no bus, no ollama)
  ./mesh_learn.py --ingest               # pull new results.jsonl rows, score them
  ./mesh_learn.py --daemon               # ingest every 60s
  ./mesh_learn.py --stats                # per class/node/model scoreboard
  ./mesh_learn.py --advise "text"        # -> JSON {node, model, confidence, escalate}
  ./mesh_learn.py --rate <task_id> up    # human feedback (weight 3), also: down
  ./mesh_learn.py --judge                # re-score pending rows with the small model
  ./mesh_learn.py --export               # ~/.mesh/policy.json for the router

State: ~/.mesh/learn.db + ~/.mesh/policy.json. Tier 1-4 state is never written.
"""
import argparse, json, math, os, re, sqlite3, sys, time, urllib.request
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
MESH_DIR = os.environ.get("MESH_DIR", os.path.join(HOME, ".mesh"))
LEARN_DB = os.path.join(MESH_DIR, "learn.db")
RESULTS = os.path.join(MESH_DIR, "results.jsonl")
POLICY = os.path.join(MESH_DIR, "policy.json")
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
JUDGE_MODEL = os.environ.get("MESH_JUDGE_MODEL", "qwen2.5:1.5b")
BIG_NODE = os.environ.get("MESH_BIG_NODE", "maestro")
BIG_MODEL = os.environ.get("MESH_MODEL_MAESTRO", "deepseek-r1-abliterated:8b")
ESCALATE_AT = float(os.environ.get("MESH_ESCALATE_AT", "0.55"))
NODES = ["maestro", "local", "wingman_ally", "wingman_core"]

# task classes the mesh learns per-node reliability for
CLASSES = {
    "code":     r"\b(code|python|script|bug|traceback|regex|sql|compile|refactor)\b",
    "reason":   r"\b(plan|why|analy[sz]e|compare|strategy|design|decide|tradeoff)\b",
    "shell":    r"^shell:|\b(battery|storage|ls |df |pkg |termux-)\b",
    "notify":   r"^notify:",
    "http":     r"^http:|https?://",
    "summary":  r"\b(summari[sz]e|digest|tl;?dr|recap|brief)\b",
    "chat":     r".",          # fallback, always matches last
}


def iso(): return datetime.now(timezone.utc).isoformat(timespec="seconds")


def classify(text: str) -> str:
    t = (text or "").strip().lower()
    for name, pat in CLASSES.items():
        if name == "chat":
            continue
        if re.search(pat, t):
            return name
    return "chat"


def db(path=None):
    os.makedirs(MESH_DIR, exist_ok=True)
    c = sqlite3.connect(path or LEARN_DB)
    c.execute("""CREATE TABLE IF NOT EXISTS obs(
        id INTEGER PRIMARY KEY, task_id TEXT, node TEXT, model TEXT, class TEXT,
        ok INTEGER, score REAL, weight REAL DEFAULT 1.0, latency REAL,
        attempts INTEGER DEFAULT 1, src TEXT, ts TEXT, text TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS cursor(k TEXT PRIMARY KEY, v TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS i_cls ON obs(class,node)")
    c.commit()
    return c


# ---------------------------------------------------------------- scoring ----
BAD = re.compile(r"(traceback|exception|error:|command not found|refus|i can'?t|"
                 r"as an ai|timed out|connection refused|null|undefined)", re.I)
HEDGE = re.compile(r"(i'?m not sure|might be|maybe|unclear|cannot determine)", re.I)


def heuristic_score(rec: dict) -> float:
    """0..1 quality estimate from the raw result record — no model needed."""
    out = str(rec.get("output") or rec.get("result") or "")
    ok = rec.get("ok")
    if ok is False or rec.get("status") in ("dead", "failed", "error"):
        return 0.0
    s = 0.62
    n = len(out.strip())
    if n == 0:
        return 0.05
    if n < 12:
        s -= 0.25
    elif n > 80:
        s += 0.12
    if BAD.search(out):
        s -= 0.45
    if HEDGE.search(out):
        s -= 0.12
    att = int(rec.get("attempts") or 1)
    s -= 0.12 * max(0, att - 1)
    lat = float(rec.get("latency") or rec.get("ms", 0) or 0)
    if lat and lat > 60:
        s -= 0.08
    if rec.get("exit_code") in (0, None) and rec.get("kind") == "shell":
        s += 0.1
    return max(0.0, min(1.0, s))


def wilson(succ: float, n: float, z=1.28) -> float:
    """Lower bound of a confidence interval — punishes thin evidence."""
    if n <= 0:
        return 0.0
    p = succ / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / d)


# --------------------------------------------------------------- ingestion ---
def read_results(path=RESULTS):
    if not os.path.exists(path):
        return []
    with open(path, "r", errors="replace") as f:
        return [l for l in f.read().splitlines() if l.strip()]


def ingest(path=RESULTS, conn=None, verbose=True) -> int:
    c = conn or db()
    row = c.execute("SELECT v FROM cursor WHERE k='results_line'").fetchone()
    start = int(row[0]) if row else 0
    lines = read_results(path)
    added = 0
    for i, line in enumerate(lines[start:], start=start):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        text = rec.get("text") or rec.get("task") or ""
        cls = rec.get("class") or classify(text)
        sc = heuristic_score(rec)
        c.execute("""INSERT INTO obs(task_id,node,model,class,ok,score,weight,latency,
                                     attempts,src,ts,text)
                     VALUES(?,?,?,?,?,?,1.0,?,?,'heuristic',?,?)""",
                  (str(rec.get("id") or rec.get("task_id") or i), rec.get("node") or "local",
                   rec.get("model") or "", cls, 1 if sc >= 0.5 else 0, sc,
                   float(rec.get("latency") or 0), int(rec.get("attempts") or 1),
                   rec.get("ts") or iso(), text[:400]))
        added += 1
    c.execute("INSERT OR REPLACE INTO cursor VALUES('results_line',?)", (str(len(lines)),))
    c.commit()
    if verbose:
        print(f"ingested {added} new result(s); cursor={len(lines)}")
    return added


def rate(task_id: str, direction: str, conn=None):
    """Human feedback outranks the machine: weight 3, score 1.0 / 0.0."""
    c = conn or db()
    r = c.execute("SELECT node,model,class,text FROM obs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                  (str(task_id),)).fetchone()
    if not r:
        print(f"no observation for task {task_id}"); return 1
    sc = 1.0 if direction.lower() in ("up", "good", "+1") else 0.0
    c.execute("""INSERT INTO obs(task_id,node,model,class,ok,score,weight,src,ts,text)
                 VALUES(?,?,?,?,?,?,3.0,'human',?,?)""",
              (str(task_id), r[0], r[1], r[2], 1 if sc else 0, sc, iso(), r[3]))
    c.commit()
    print(f"recorded human {direction} for task {task_id} ({r[2]} @ {r[0]})")
    return 0


# ------------------------------------------------------------------ policy ---
def table(conn=None):
    c = conn or db()
    rows = c.execute("""SELECT class,node,model,SUM(score*weight),SUM(weight),AVG(latency)
                        FROM obs GROUP BY class,node,model""").fetchall()
    out = []
    for cls, node, model, succ, n, lat in rows:
        out.append({"class": cls, "node": node, "model": model or "-",
                    "n": round(n or 0, 1), "mean": round((succ or 0) / (n or 1), 3),
                    "confidence": round(wilson(succ or 0, n or 0), 3),
                    "latency": round(lat or 0, 1)})
    out.sort(key=lambda r: (r["class"], -r["confidence"]))
    return out


def policy(conn=None):
    """Best node per class + whether the mesh trusts it enough to skip the 8B."""
    best = {}
    for r in table(conn):
        cur = best.get(r["class"])
        if not cur or r["confidence"] > cur["confidence"]:
            best[r["class"]] = r
    pol = {"generated": iso(), "escalate_at": ESCALATE_AT, "classes": {}}
    for cls, r in best.items():
        pol["classes"][cls] = {"node": r["node"], "model": r["model"],
                               "confidence": r["confidence"], "n": r["n"],
                               "escalate": r["confidence"] < ESCALATE_AT}
    return pol


def advise(text: str, conn=None) -> dict:
    cls = classify(text)
    pol = policy(conn)
    p = pol["classes"].get(cls)
    if not p or p["n"] < 3:
        # not enough evidence yet -> play safe on the big model, keep learning
        return {"text": text[:200], "class": cls, "node": BIG_NODE, "model": BIG_MODEL,
                "confidence": round(p["confidence"], 3) if p else 0.0,
                "escalate": True, "reason": "insufficient evidence (n<3)"}
    if p["confidence"] < ESCALATE_AT and p["node"] != BIG_NODE:
        return {"text": text[:200], "class": cls, "node": BIG_NODE, "model": BIG_MODEL,
                "confidence": p["confidence"], "escalate": True,
                "reason": f"{p['node']} confidence {p['confidence']} < {ESCALATE_AT}"}
    return {"text": text[:200], "class": cls, "node": p["node"], "model": p["model"],
            "confidence": p["confidence"], "escalate": False,
            "reason": f"learned from {p['n']} scored result(s)"}


def export(conn=None):
    os.makedirs(MESH_DIR, exist_ok=True)
    pol = policy(conn)
    with open(POLICY, "w") as f:
        json.dump(pol, f, indent=2)
    print(f"wrote {POLICY} ({len(pol['classes'])} classes)")
    return pol


# ------------------------------------------------------------- model judge ---
def ollama(model, prompt, timeout=90):
    req = urllib.request.Request(
        OLLAMA.rstrip("/") + "/api/generate",
        data=json.dumps({"model": model, "prompt": prompt, "stream": False,
                         "options": {"num_ctx": 2048, "temperature": 0}}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["response"]


def judge(limit=20, conn=None):
    """Second opinion from the small model on heuristic-scored rows."""
    c = conn or db()
    rows = c.execute("""SELECT id,text,class FROM obs WHERE src='heuristic'
                        AND score BETWEEN 0.2 AND 0.8 ORDER BY id DESC LIMIT ?""",
                     (limit,)).fetchall()
    done = 0
    for _id, text, cls in rows:
        try:
            raw = ollama(JUDGE_MODEL,
                         "Rate how well this output answers its task. "
                         "Reply with ONLY a number 0-10.\n\n" + (text or "")[:1200])
            m = re.search(r"\d+(\.\d+)?", raw)
            if not m:
                continue
            s = max(0.0, min(1.0, float(m.group()) / 10.0))
            c.execute("UPDATE obs SET score=?, ok=?, src='judge' WHERE id=?",
                      (s, 1 if s >= 0.5 else 0, _id))
            done += 1
        except Exception as e:
            print("judge unavailable:", e); break
    c.commit()
    print(f"judged {done} row(s)")
    return done


def daemon(interval=60):
    print(f"learn daemon: ingest every {interval}s")
    while True:
        try:
            ingest(verbose=False)
            export()
        except Exception as e:
            print("learn loop error:", e)
        time.sleep(interval)


# ---------------------------------------------------------------- selftest ---
def selftest() -> int:
    import tempfile
    fails, n = [], 0

    def ck(label, cond):
        nonlocal n
        n += 1
        if not cond:
            fails.append(label)

    ck("classify code", classify("fix this python traceback") == "code")
    ck("classify shell", classify("shell:termux-battery-status") == "shell")
    ck("classify notify", classify("notify:hello") == "notify")
    ck("classify summary", classify("summarize today") == "summary")
    ck("classify reason", classify("compare two plans") == "reason")
    ck("classify fallback", classify("yo") == "chat")

    ck("score empty", heuristic_score({"output": ""}) < 0.1)
    ck("score error", heuristic_score({"output": "Traceback (most recent call last)"}) < 0.3)
    ck("score good", heuristic_score({"output": "x" * 200}) > 0.6)
    ck("score failed flag", heuristic_score({"output": "ok", "ok": False}) == 0.0)
    ck("score retries hurt",
       heuristic_score({"output": "x" * 200, "attempts": 3}) <
       heuristic_score({"output": "x" * 200, "attempts": 1}))

    ck("wilson zero", wilson(0, 0) == 0.0)
    ck("wilson thin < thick", wilson(2, 2) < wilson(20, 20))
    ck("wilson bounded", 0 <= wilson(5, 10) <= 1)

    d = tempfile.mkdtemp()
    global MESH_DIR, LEARN_DB, POLICY
    MESH_DIR, LEARN_DB, POLICY = d, os.path.join(d, "learn.db"), os.path.join(d, "policy.json")
    c = db()
    rp = os.path.join(d, "results.jsonl")
    with open(rp, "w") as f:
        for i in range(6):
            f.write(json.dumps({"id": f"t{i}", "node": "local", "model": "qwen2.5:1.5b",
                                "text": "summarize today's notes",
                                "output": "Traceback: boom"}) + "\n")
        for i in range(6):
            f.write(json.dumps({"id": f"m{i}", "node": "maestro", "model": "deepseek-r1-abliterated:8b",
                                "text": "summarize today's notes",
                                "output": "Here is a clear summary of the day. " * 6}) + "\n")
    ck("ingest count", ingest(rp, c, verbose=False) == 12)
    ck("ingest is incremental", ingest(rp, c, verbose=False) == 0)

    t = {r["node"]: r for r in table(c) if r["class"] == "summary"}
    ck("maestro beats local", t["maestro"]["confidence"] > t["local"]["confidence"])
    a = advise("summarize today's notes", c)
    ck("advise picks winner", a["node"] == "maestro")
    ck("advise no escalate", a["escalate"] is False)
    b = advise("shell:df -h", c)
    ck("advise escalates unknown class", b["escalate"] is True and b["node"] == BIG_NODE)
    rate("t0", "up", c)
    ck("human weight applied",
       c.execute("SELECT weight FROM obs WHERE src='human'").fetchone()[0] == 3.0)
    ck("human feedback moves score",
       {r["node"]: r for r in table(c) if r["class"] == "summary"}["local"]["confidence"]
       > t["local"]["confidence"])
    pol = export(c)
    ck("policy exported", os.path.exists(POLICY) and "summary" in pol["classes"])

    print(f"\n{n - len(fails)}/{n} checks passed")
    for f_ in fails:
        print("  FAIL:", f_)
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--ingest", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--advise")
    ap.add_argument("--rate", nargs=2, metavar=("TASK_ID", "UP_OR_DOWN"))
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--export", action="store_true")
    a = ap.parse_args()

    if a.selftest: sys.exit(selftest())
    if a.rate: sys.exit(rate(a.rate[0], a.rate[1]))
    if a.ingest: ingest(); export(); return
    if a.judge: judge(a.limit); export(); return
    if a.advise: print(json.dumps(advise(a.advise), indent=2)); return
    if a.export: export(); return
    if a.daemon: daemon(a.interval); return

    ingest(verbose=False)
    rows = table()
    if not rows:
        print("no scored results yet — run some tasks, then: learn --ingest"); return
    print(f"{'class':10} {'node':14} {'model':26} {'n':>5} {'mean':>6} {'conf':>6}")
    for r in rows:
        print(f"{r['class']:10} {r['node']:14} {r['model'][:26]:26} "
              f"{r['n']:>5} {r['mean']:>6} {r['confidence']:>6}")
    print("\npolicy:", json.dumps(policy()["classes"], indent=2))


if __name__ == "__main__":
    main()
