#!/usr/bin/env python3
"""
mesh_sched.py — mesh tier 4: the CLOCK, the SENSES and the DIGEST.

  tier 1  mesh_bus.py    shared memory
  tier 2  mesh_router.py a brain that decides who does what
  tier 3  mesh_exec.py   hands that actually run the work
  tier 4  mesh_sched.py  a clock + senses + a nightly/morning brief

What it does
  * SCHEDULES: recurring jobs ("every 30m", "daily 07:30", "@boot", cron "*/15 * * * *")
    that submit tasks onto the tier-1 bus, routed/executed by tiers 2-3.
  * TRIGGERS: fires tasks when the phone's state changes — battery level/charging,
    network up/down, a file or directory changing, or a shell probe's output changing.
  * DIGEST: reads ~/.mesh/results.jsonl + the bus and produces a brief
    (markdown), optionally summarized by the local model, notified via termux.

Usage
  ./mesh_sched.py --selftest                 # offline proof (no bus, no ollama, no clock wait)
  ./mesh_sched.py --add "every 30m" --text "summarize new results" --for maestro
  ./mesh_sched.py --add "daily 07:30" --digest --for maestro
  ./mesh_sched.py --trigger battery:<20 --text "shell:termux-battery-status" --for local
  ./mesh_sched.py --trigger file:~/notes.md --text "re-index notes" --for maestro
  ./mesh_sched.py --list / --rm 3 / --pause 3 / --resume 3
  ./mesh_sched.py --tick                     # evaluate everything once (cron-safe)
  ./mesh_sched.py --daemon                   # evaluate every 60s
  ./mesh_sched.py --digest-now [--since 24h] [--model] [--notify]

State: ~/.mesh/sched.db  (separate file; tier 1/3 schemas untouched)
Digests are appended to ~/.mesh/digests/YYYY-MM-DD.md
"""
import argparse, json, os, re, sqlite3, subprocess, sys, time, urllib.request
from datetime import datetime, timezone, timedelta

HOME = os.path.expanduser("~")
MESH_DIR = os.environ.get("MESH_DIR", os.path.join(HOME, ".mesh"))
SCHED_DB = os.path.join(MESH_DIR, "sched.db")
RESULTS = os.path.join(MESH_DIR, "results.jsonl")
DIGEST_DIR = os.path.join(MESH_DIR, "digests")
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
DIGEST_MODEL = os.environ.get("MESH_DIGEST_MODEL", os.environ.get("MESH_ROUTER_MODEL", "qwen2.5:1.5b"))


def now_ts(): return time.time()
def iso(t=None): return datetime.fromtimestamp(t or time.time(), timezone.utc).isoformat(timespec="seconds")


# ---------------- state ------------------------------------------------------
def db():
    os.makedirs(MESH_DIR, exist_ok=True)
    c = sqlite3.connect(SCHED_DB, timeout=30)
    c.execute("""CREATE TABLE IF NOT EXISTS jobs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT,            -- 'schedule' | 'trigger'
        spec TEXT,            -- "every 30m" / "daily 07:30" / "@boot" / cron / "battery:<20"
        text TEXT,            -- task text pushed to the bus ('' + digest=1 => digest job)
        node TEXT,            -- target node
        digest INT DEFAULT 0,
        enabled INT DEFAULT 1,
        next_at REAL DEFAULT 0,
        last_at REAL DEFAULT 0,
        last_state TEXT,      -- trigger memory (edge detection)
        fires INT DEFAULT 0,
        created TEXT)""")
    c.commit()
    return c


# ---------------- schedule parsing -------------------------------------------
UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_spec(spec):
    """Return ('every', seconds) | ('daily', (h,m)) | ('boot', None) | ('cron', fields)."""
    s = spec.strip().lower()
    m = re.fullmatch(r"every\s+(\d+)\s*([smhd])", s)
    if m:
        return ("every", int(m.group(1)) * UNIT[m.group(2)])
    m = re.fullmatch(r"daily\s+(\d{1,2}):(\d{2})", s)
    if m:
        return ("daily", (int(m.group(1)), int(m.group(2))))
    if s in ("@boot", "boot"):
        return ("boot", None)
    if len(s.split()) == 5:
        return ("cron", s.split())
    raise ValueError(f"unparsable schedule: {spec!r} (try 'every 30m', 'daily 07:30', '@boot', '*/15 * * * *')")


def _cron_match(fields, dt):
    vals = [dt.minute, dt.hour, dt.day, dt.month, dt.weekday() == 6 and 0 or (dt.weekday() + 1) % 7]
    for f, v in zip(fields, vals):
        if f == "*":
            continue
        ok = False
        for part in f.split(","):
            if part.startswith("*/"):
                step = int(part[2:])
                ok = ok or (v % step == 0)
            elif "-" in part:
                a, b = part.split("-")
                ok = ok or (int(a) <= v <= int(b))
            else:
                ok = ok or (v == int(part))
        if not ok:
            return False
    return True


def next_run(spec, after=None, boot=False):
    """Compute next fire time (epoch) for a schedule spec."""
    after = after or now_ts()
    kind, val = parse_spec(spec)
    if kind == "every":
        return after + val
    if kind == "daily":
        d = datetime.fromtimestamp(after).replace(hour=val[0], minute=val[1], second=0, microsecond=0)
        if d.timestamp() <= after:
            d += timedelta(days=1)
        return d.timestamp()
    if kind == "boot":
        return after if boot else float("inf")
    # cron: scan forward minute by minute (max 1 week)
    dt = datetime.fromtimestamp(after).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(60 * 24 * 7):
        if _cron_match(val, dt):
            return dt.timestamp()
        dt += timedelta(minutes=1)
    return after + 3600


# ---------------- bus bridge -------------------------------------------------
def _mesh(*args, timeout=25):
    try:
        p = subprocess.run(["mesh", *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return 127, "", str(e)


def submit(text, node):
    rc, out, err = _mesh("task", "--node", "sched", "--text", text, "--for", node)
    if rc != 0:
        # bus offline: hand straight to tier 3 so nothing is lost
        rc2, out2, _ = _run(["mesh_exec.py", "--submit", text, "--for", node])
        return f"bus offline ({err or 'no mesh CLI'}) -> exec: {out2 or rc2}"
    return out or "submitted"


def _run(parts):
    try:
        p = subprocess.run(parts, capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return 127, "", str(e)


# ---------------- triggers ---------------------------------------------------
def probe(spec):
    """Return a comparable state string for a trigger spec, or None if unavailable."""
    s = spec.strip()
    if s.startswith("battery:"):
        lvl, charging = battery()
        if lvl is None:
            return None
        cond = s.split(":", 1)[1]
        m = re.fullmatch(r"([<>])(\d+)", cond)
        if m:
            hit = (lvl < int(m.group(2))) if m.group(1) == "<" else (lvl > int(m.group(2)))
            return f"hit={hit}"
        if cond in ("charging", "unplugged"):
            return f"charging={charging}"
        return f"level={lvl}"
    if s.startswith("net:"):
        return f"online={online()}"
    if s.startswith("file:"):
        path = os.path.expanduser(s.split(":", 1)[1])
        try:
            if os.path.isdir(path):
                sig = sorted((f, os.path.getmtime(os.path.join(path, f))) for f in os.listdir(path))
                return str(hash(str(sig)))
            return f"{os.path.getmtime(path)}:{os.path.getsize(path)}"
        except OSError:
            return "missing"
    if s.startswith("sh:"):
        rc, out, _ = _run(["sh", "-c", s.split(":", 1)[1]])
        return f"{rc}:{out[:200]}"
    raise ValueError(f"unknown trigger: {spec!r} (battery:<20 | battery:charging | net:up | file:PATH | sh:CMD)")


def fires_on(spec, prev, cur):
    """Edge detection: fire only on a meaningful change."""
    if cur is None or prev == cur:
        return False
    if prev is None:
        return cur.endswith("=True")      # first observation only fires on a positive condition
    return True


def battery():
    rc, out, _ = _run(["termux-battery-status"])
    if rc != 0:
        return None, None
    try:
        d = json.loads(out)
        return int(d.get("percentage", -1)), d.get("status", "") == "CHARGING"
    except Exception:
        return None, None


def online():
    try:
        urllib.request.urlopen("https://clients3.google.com/generate_204", timeout=6)
        return True
    except Exception:
        return False


# ---------------- digest -----------------------------------------------------
def read_results(since_s):
    cutoff = now_ts() - since_s
    rows = []
    try:
        with open(RESULTS) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("ts") or d.get("time") or ""
                try:
                    ts = datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()
                except Exception:
                    ts = now_ts()
                if ts >= cutoff:
                    d["_ts"] = ts
                    rows.append(d)
    except FileNotFoundError:
        pass
    return rows


def build_digest(since_s=86400, use_model=False, rows=None):
    rows = read_results(since_s) if rows is None else rows
    ok = [r for r in rows if r.get("ok")]
    bad = [r for r in rows if not r.get("ok")]
    per_node = {}
    for r in rows:
        per_node.setdefault(r.get("node", "?"), [0, 0])[0 if r.get("ok") else 1] += 1
    lvl, chg = battery()
    lines = [f"# mesh digest — {datetime.now().strftime('%Y-%m-%d %H:%M')}", "",
             f"**{len(rows)} tasks** in the last {int(since_s/3600)}h — {len(ok)} ok, {len(bad)} failed."]
    if lvl is not None:
        lines.append(f"Battery {lvl}%{' (charging)' if chg else ''}.")
    lines.append("")
    if per_node:
        lines.append("| node | ok | failed |")
        lines.append("|---|---|---|")
        for n, (a, b) in sorted(per_node.items()):
            lines.append(f"| {n} | {a} | {b} |")
        lines.append("")
    if bad:
        lines.append("## Needs attention")
        for r in bad[:10]:
            lines.append(f"- `{r.get('node','?')}` #{r.get('task','?')}: {str(r.get('result'))[:160]}")
        lines.append("")
    if ok:
        lines.append("## Output highlights")
        for r in ok[-8:]:
            lines.append(f"- `{r.get('node','?')}`: {str(r.get('result'))[:200].strip()}")
    text = "\n".join(lines)
    if use_model and ok:
        try:
            blob = "\n".join(str(r.get("result"))[:400] for r in ok[-15:])
            s = model(f"In 4 bullet points, summarize what this agent mesh accomplished. "
                      f"Be concrete, no preamble.\n\n{blob}")
            text += "\n\n## Summary\n" + s
        except Exception as e:
            text += f"\n\n_(model summary unavailable: {e})_"
    return text


def model(prompt):
    body = json.dumps({"model": DIGEST_MODEL, "prompt": prompt, "stream": False,
                       "options": {"temperature": 0.2, "num_ctx": 8192, "num_predict": 400}}).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=int(os.environ.get("MESH_MODEL_TIMEOUT", "240"))) as r:
        out = json.loads(r.read().decode()).get("response", "")
    return re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()[:4000]


def write_digest(text, notify=False):
    os.makedirs(DIGEST_DIR, exist_ok=True)
    path = os.path.join(DIGEST_DIR, datetime.now().strftime("%Y-%m-%d") + ".md")
    with open(path, "a") as f:
        f.write(text + "\n\n---\n\n")
    _mesh("post", "--node", "sched", "--kind", "digest", "--text", text[:3500])
    if notify:
        head = text.split("\n")[2] if len(text.split("\n")) > 2 else "digest ready"
        _run(["termux-notification", "--title", "mesh digest", "--content", re.sub(r"\*", "", head)[:200]])
    return path


# ---------------- engine -----------------------------------------------------
def tick(boot=False, verbose=True):
    c = db()
    fired = 0
    for row in c.execute("SELECT id,kind,spec,text,node,digest,next_at,last_state FROM jobs WHERE enabled=1").fetchall():
        jid, kind, spec, text, node, dg, nxt, last = row
        try:
            if kind == "schedule":
                if boot and parse_spec(spec)[0] == "boot":
                    pass
                elif not nxt or nxt > now_ts():
                    continue
                out = _do(jid, text, node, dg)
                c.execute("UPDATE jobs SET last_at=?,fires=fires+1,next_at=? WHERE id=?",
                          (now_ts(), next_run(spec, boot=False), jid))
                fired += 1
                if verbose:
                    print(f"[{iso()}] job {jid} ({spec}) fired -> {out}")
            else:
                cur = probe(spec)
                if fires_on(spec, last, cur):
                    out = _do(jid, text, node, dg)
                    fired += 1
                    if verbose:
                        print(f"[{iso()}] trigger {jid} ({spec}) {last} -> {cur} :: {out}")
                    c.execute("UPDATE jobs SET last_at=?,fires=fires+1 WHERE id=?", (now_ts(), jid))
                if cur is not None:
                    c.execute("UPDATE jobs SET last_state=? WHERE id=?", (cur, jid))
        except Exception as e:
            print(f"[{iso()}] job {jid} error: {e}", file=sys.stderr)
        c.commit()
    return fired


def _do(jid, text, node, dg):
    if dg:
        return "digest -> " + write_digest(build_digest(use_model=True), notify=True)
    return submit(text, node)


def daemon(interval=60):
    print(f"mesh_sched daemon: tick every {interval}s (ctrl-C to stop)")
    tick(boot=True)
    while True:
        try:
            tick()
        except Exception as e:
            print("tick error:", e, file=sys.stderr)
        time.sleep(interval)


def listing():
    c = db()
    rows = c.execute("SELECT id,kind,spec,node,digest,enabled,next_at,fires,last_state FROM jobs ORDER BY id").fetchall()
    if not rows:
        print("no jobs. add one:  mesh_sched.py --add 'daily 07:30' --digest --for maestro")
        return
    print(f"{'id':>3} {'kind':9} {'spec':18} {'node':13} {'next':17} {'fires':>5}  state")
    for i, k, s, n, dg, en, nx, fz, ls in rows:
        nxt = "-" if not nx or nx == float("inf") else datetime.fromtimestamp(nx).strftime("%m-%d %H:%M")
        tag = ("digest " if dg else "") + ("" if en else "[paused]")
        print(f"{i:>3} {k:9} {s:18} {n:13} {nxt:17} {fz:>5}  {tag}{ls or ''}")


# ---------------- selftest ---------------------------------------------------
def selftest():
    ok = []
    def chk(name, cond):
        ok.append(cond); print(("PASS  " if cond else "FAIL  ") + name)

    chk("parse every 30m", parse_spec("every 30m") == ("every", 1800))
    chk("parse daily 07:30", parse_spec("daily 07:30") == ("daily", (7, 30)))
    chk("parse @boot", parse_spec("@boot")[0] == "boot")
    chk("parse cron */15", parse_spec("*/15 * * * *")[0] == "cron")
    try:
        parse_spec("sometimes"); chk("rejects junk spec", False)
    except ValueError:
        chk("rejects junk spec", True)

    t = next_run("every 30m", after=1000)
    chk("every 30m -> +1800s", t == 2800)
    d = next_run("daily 07:30")
    chk("daily is in the future", d > now_ts() and datetime.fromtimestamp(d).hour == 7)
    base = datetime(2026, 1, 1, 10, 7).timestamp()
    nr = datetime.fromtimestamp(next_run("*/15 * * * *", after=base))
    chk("cron */15 -> 10:15", (nr.hour, nr.minute) == (10, 15))

    chk("edge: None->True fires", fires_on("battery:<20", None, "hit=True"))
    chk("edge: None->False quiet", not fires_on("battery:<20", None, "hit=False"))
    chk("edge: False->True fires", fires_on("battery:<20", "hit=False", "hit=True"))
    chk("edge: no change quiet", not fires_on("battery:<20", "hit=True", "hit=True"))
    chk("edge: unavailable quiet", not fires_on("net:up", "online=True", None))

    tmp = os.path.join(MESH_DIR, ".selftest.tmp")
    os.makedirs(MESH_DIR, exist_ok=True)
    open(tmp, "w").write("a")
    p1 = probe(f"file:{tmp}")
    time.sleep(0.01); open(tmp, "w").write("abc")
    chk("file trigger sees change", probe(f"file:{tmp}") != p1)
    os.remove(tmp)
    chk("file trigger handles missing", probe(f"file:{tmp}") == "missing")
    chk("sh probe works", probe("sh:echo hi").endswith("hi"))

    rows = [{"node": "maestro", "ok": True, "task": 1, "result": "wrote plan", "ts": iso()},
            {"node": "local", "ok": False, "task": 2, "result": "blocked command: rm", "ts": iso()}]
    d = build_digest(rows=rows)
    chk("digest counts tasks", "2 tasks" in d and "1 ok, 1 failed" in d)
    chk("digest lists failures", "blocked command" in d)

    c = db()
    c.execute("DELETE FROM jobs WHERE spec='selftest'")
    c.execute("INSERT INTO jobs(kind,spec,text,node,next_at,created) VALUES('trigger','selftest','x','local',0,?)", (iso(),))
    c.commit()
    n = c.execute("SELECT COUNT(*) FROM jobs WHERE spec='selftest'").fetchone()[0]
    c.execute("DELETE FROM jobs WHERE spec='selftest'"); c.commit()
    chk("job store add/remove", n == 1)

    bad = ok.count(False)
    print(f"\n{len(ok)-bad}/{len(ok)} checks passed (no bus, no ollama, no waiting required)")
    return 1 if bad else 0


# ---------------- cli --------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="mesh tier 4 — scheduler, triggers, digest")
    ap.add_argument("--add", metavar="SPEC")
    ap.add_argument("--trigger", metavar="SPEC")
    ap.add_argument("--text", default="")
    ap.add_argument("--for", dest="node", default="local")
    ap.add_argument("--digest", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--rm", type=int)
    ap.add_argument("--pause", type=int)
    ap.add_argument("--resume", type=int)
    ap.add_argument("--tick", action="store_true")
    ap.add_argument("--boot", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--digest-now", action="store_true")
    ap.add_argument("--since", default="24h")
    ap.add_argument("--model", action="store_true")
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        sys.exit(selftest())
    if a.add or a.trigger:
        spec = a.add or a.trigger
        kind = "schedule" if a.add else "trigger"
        if kind == "schedule":
            nxt = next_run(spec)
        else:
            probe(spec); nxt = 0
        if not a.text and not a.digest:
            sys.exit("give --text or --digest")
        c = db()
        cur = c.execute("""INSERT INTO jobs(kind,spec,text,node,digest,next_at,created)
                           VALUES(?,?,?,?,?,?,?)""",
                        (kind, spec, a.text, a.node, 1 if a.digest else 0, nxt, iso()))
        c.commit()
        print(f"added {kind} #{cur.lastrowid}: {spec} -> {a.node} "
              f"{'(digest)' if a.digest else repr(a.text)}")
        return
    if a.rm or a.pause or a.resume:
        c = db()
        if a.rm: c.execute("DELETE FROM jobs WHERE id=?", (a.rm,)); print(f"removed #{a.rm}")
        if a.pause: c.execute("UPDATE jobs SET enabled=0 WHERE id=?", (a.pause,)); print(f"paused #{a.pause}")
        if a.resume:
            c.execute("UPDATE jobs SET enabled=1 WHERE id=?", (a.resume,))
            row = c.execute("SELECT kind,spec FROM jobs WHERE id=?", (a.resume,)).fetchone()
            if row and row[0] == "schedule":
                c.execute("UPDATE jobs SET next_at=? WHERE id=?", (next_run(row[1]), a.resume))
            print(f"resumed #{a.resume}")
        c.commit(); return
    if a.digest_now:
        m = re.fullmatch(r"(\d+)([smhd])", a.since.strip())
        secs = int(m.group(1)) * UNIT[m.group(2)] if m else 86400
        text = build_digest(secs, use_model=a.model)
        print(text)
        print("\nsaved:", write_digest(text, notify=a.notify))
        return
    if a.daemon:
        daemon(a.interval); return
    if a.tick or a.boot:
        print(f"fired {tick(boot=a.boot)} job(s)"); return
    listing()


if __name__ == "__main__":
    main()
