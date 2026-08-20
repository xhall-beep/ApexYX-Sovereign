#!/data/data/com.termux/files/usr/bin/env python3
"""mesh_guard.py — tier 9 of the Pixel mesh: admission control, budgets, audit, kill switch.

t1 bus = messages. t2 router = who. t3 exec = hands. t4 sched = clock.
t5 learn = judgement. t6 reach = outside. t7 state = facts. t8 plan = intent.
t9 guard = SURVIVAL: nothing runs unless the phone can afford it.

Four organs:
  1. sensors    battery %, charging, temperature, thermal throttle, network class,
                free storage, RAM, loadavg  (termux-battery-status when present,
                /sys + /proc fallbacks, all cached with a TTL so we never hammer).
  2. admission  policy rules -> allow / defer / deny for every task, per class
                (model / shell / http / notify) with per-node concurrency caps and
                rolling token + wall-clock budgets.
  3. audit      append-only, hash-chained JSONL of every decision and every
                shell/http payload. Tamper-evident: `guard verify` recomputes the chain.
  4. drain      kill switch with three depths: pause (stop admitting), drain
                (finish in-flight, then idle), halt (SIGTERM the daemons).

Storage: ~/.mesh/guard.db + guard.audit.jsonl (own files; never touches tiers 1-8 dbs).

CLI
  guard status                          one-screen health + current verdict
  guard check <kind> [--node N] [--tokens T] [--cost C] [--task "..."]
                                        exit 0 allow / 10 defer / 20 deny  (for scripts)
  guard admit <kind> ...                same, but records a lease; prints lease id
  guard release <lease> [--tokens T] [--ok|--fail]
  guard policy [show|set KEY=VAL ...|reset]
  guard budget [show|set model=200000/day shell=500/hour ...]
  guard audit [--tail N] [--kind K] [--since 1h]
  guard verify                          re-hash the audit chain
  guard pause [--why "..."] | guard resume
  guard drain [--halt] [--timeout S]
  guard wrap -- <cmd ...>               admit, run, audit, release (the easy path)
  guard --daemon [--interval S]         sensor sweep + auto pause/resume + lease GC
  guard --selftest
"""
import argparse, hashlib, json, os, re, shutil, signal, sqlite3, subprocess, sys, time
from datetime import datetime, timezone

MESH_DIR = os.environ.get("MESH_DIR", os.path.expanduser("~/.mesh"))
DB = os.path.join(MESH_DIR, "guard.db")
AUDIT = os.path.join(MESH_DIR, "guard.audit.jsonl")
FLAG = os.path.join(MESH_DIR, "guard.state")          # pause/drain flag, read by other tiers
KINDS = ("model", "shell", "http", "notify")
ALLOW, DEFER, DENY = "allow", "defer", "deny"
EXIT = {ALLOW: 0, DEFER: 10, DENY: 20}
SENSOR_TTL = float(os.environ.get("MESH_GUARD_TTL", "20"))

DEFAULT_POLICY = {
    # battery
    "batt_min": 20,             # below this: only notify survives
    "batt_min_model": 35,       # local LLM inference is the expensive one
    "batt_defer_window": 300,   # how long a defer suggests waiting (s)
    # thermal
    "temp_max_c": 44.0,         # deny model work above this
    "temp_warn_c": 40.0,        # defer model work above this
    # network
    "net_required_http": "any", # any | wifi  (wifi = don't burn mobile data)
    # resources
    "free_mb_min": 400,
    "load_max": 6.0,
    # concurrency
    "conc_default": 2,
    "conc_model": 1,            # one local model call at a time on a phone. always.
    "conc_shell": 3,
    "conc_http": 4,
    "conc_notify": 8,
    "conc_per_node": 2,
    "lease_ttl": 900,           # reap zombie leases
    "audit_payload_max": 2000,
}
DEFAULT_BUDGETS = {             # kind -> (amount, window seconds)
    "model": (200000, 86400),   # tokens/day
    "shell": (500, 3600),       # calls/hour
    "http": (300, 3600),
    "notify": (200, 3600),
}


def now():
    return time.time()


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- schema ----
SCHEMA = """
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS lease(
  id INTEGER PRIMARY KEY, kind TEXT, node TEXT, task TEXT, tokens REAL,
  state TEXT, opened REAL, closed REAL, ok INTEGER);
CREATE INDEX IF NOT EXISTS ix_lease_open ON lease(state, kind, node);
CREATE TABLE IF NOT EXISTS usage(
  id INTEGER PRIMARY KEY, kind TEXT, amount REAL, ts REAL);
CREATE INDEX IF NOT EXISTS ix_usage ON usage(kind, ts);
CREATE TABLE IF NOT EXISTS sensor(k TEXT PRIMARY KEY, v TEXT, ts REAL);
"""


def db():
    os.makedirs(MESH_DIR, exist_ok=True)
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    try:
        c.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return c


def kv_get(c, k, default=None):
    r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default


def kv_set(c, k, v):
    c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
              (k, json.dumps(v)))
    c.commit()


def policy(c):
    p = dict(DEFAULT_POLICY)
    p.update(kv_get(c, "policy", {}) or {})
    return p


def budgets(c):
    b = {k: list(v) for k, v in DEFAULT_BUDGETS.items()}
    for k, v in (kv_get(c, "budgets", {}) or {}).items():
        b[k] = list(v)
    return b


# --------------------------------------------------------------- sensors ----
def _sh(cmd, timeout=4):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""


def _battery():
    """Returns (percent, charging, temp_c). Termux API first, /sys fallback."""
    raw = _sh("termux-battery-status")
    if raw:
        try:
            d = json.loads(raw)
            t = d.get("temperature")
            if t is not None and t > 200:      # some ROMs report deci-degrees
                t = t / 10.0
            return (d.get("percentage"), str(d.get("status", "")).lower() in
                    ("charging", "full"), t)
        except Exception:
            pass
    pct = _read("/sys/class/power_supply/battery/capacity")
    st = _read("/sys/class/power_supply/battery/status").lower()
    tmp = _read("/sys/class/power_supply/battery/temp")
    temp = None
    if tmp.lstrip("-").isdigit():
        temp = int(tmp) / 10.0
    return (int(pct) if pct.isdigit() else None, st in ("charging", "full"), temp)


def _thermal_max():
    """Hottest thermal zone in °C (SoC throttling proxy)."""
    best = None
    base = "/sys/class/thermal"
    try:
        zones = [z for z in os.listdir(base) if z.startswith("thermal_zone")]
    except Exception:
        zones = []
    for z in zones:
        v = _read(os.path.join(base, z, "temp"))
        if not v.lstrip("-").isdigit():
            continue
        t = int(v)
        t = t / 1000.0 if abs(t) > 1000 else float(t)
        if -20 < t < 130 and (best is None or t > best):
            best = t
    return best


def _network():
    raw = _sh("termux-wifi-connectioninfo")
    if raw:
        try:
            d = json.loads(raw)
            if d.get("supplicant_state", "").upper() == "COMPLETED" or d.get("ssid") not in (None, "", "<unknown ssid>"):
                return "wifi"
        except Exception:
            pass
    # fallback: any default route at all?
    if _sh("ip route get 1.1.1.1 2>/dev/null | head -1"):
        return "mobile"
    return "none"


def _free_mb(path=None):
    try:
        u = shutil.disk_usage(path or MESH_DIR)
        return int(u.free / (1024 * 1024))
    except Exception:
        return None


def _loadavg():
    try:
        return os.getloadavg()[0]
    except Exception:
        return 0.0


def sense(c, force=False):
    cached = c.execute("SELECT v, ts FROM sensor WHERE k='snap'").fetchone()
    if cached and not force and now() - cached["ts"] < SENSOR_TTL:
        return json.loads(cached["v"])
    pct, charging, btemp = _battery()
    ztemp = _thermal_max()
    temp = max([t for t in (btemp, ztemp) if t is not None], default=None)
    s = {"battery": pct, "charging": charging, "temp_c": temp,
         "net": _network(), "free_mb": _free_mb(), "load": round(_loadavg(), 2),
         "ts": now()}
    c.execute("INSERT INTO sensor(k,v,ts) VALUES('snap',?,?) "
              "ON CONFLICT(k) DO UPDATE SET v=excluded.v, ts=excluded.ts",
              (json.dumps(s), s["ts"]))
    c.commit()
    return s


# ----------------------------------------------------------------- audit ----
def audit_last_hash():
    if not os.path.exists(AUDIT):
        return "0" * 64
    last = None
    with open(AUDIT, "rb") as f:
        for line in f:
            if line.strip():
                last = line
    if not last:
        return "0" * 64
    try:
        return json.loads(last)["h"]
    except Exception:
        return "0" * 64


def audit_write(event, **fields):
    os.makedirs(MESH_DIR, exist_ok=True)
    prev = audit_last_hash()
    rec = {"ts": round(now(), 3), "event": event, **fields, "prev": prev}
    body = json.dumps(rec, sort_keys=True, ensure_ascii=False)
    rec["h"] = hashlib.sha256((prev + body).encode()).hexdigest()
    with open(AUDIT, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec["h"]


def audit_verify():
    if not os.path.exists(AUDIT):
        return True, 0, None
    prev = "0" * 64
    n = 0
    with open(AUDIT) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                return False, n, i
            h = rec.pop("h", None)
            if rec.get("prev") != prev:
                return False, n, i
            body = json.dumps(rec, sort_keys=True, ensure_ascii=False)
            if hashlib.sha256((prev + body).encode()).hexdigest() != h:
                return False, n, i
            prev = h
            n += 1
    return True, n, None


# ------------------------------------------------------------- admission ----
def open_leases(c):
    reap(c)
    return c.execute("SELECT * FROM lease WHERE state='open'").fetchall()


def reap(c):
    p = policy(c)
    cut = now() - p["lease_ttl"]
    c.execute("UPDATE lease SET state='reaped', closed=? WHERE state='open' AND opened<?",
              (now(), cut))
    c.commit()


def used(c, kind):
    amount, window = budgets(c).get(kind, (None, 3600))
    if amount is None:
        return 0.0, None, window
    r = c.execute("SELECT COALESCE(SUM(amount),0) a FROM usage WHERE kind=? AND ts>?",
                  (kind, now() - window)).fetchone()
    return float(r["a"]), float(amount), window


def flag_state(c):
    return kv_get(c, "flag", {"mode": "run", "why": "", "ts": 0})


def decide(c, kind, node=None, tokens=0.0, snap=None):
    """-> (verdict, reason, retry_after_seconds)"""
    p = policy(c)
    s = snap if snap is not None else sense(c)
    fl = flag_state(c)
    if fl["mode"] == "halt":
        return DENY, "kill switch: halted (%s)" % (fl.get("why") or "manual"), None
    if fl["mode"] in ("pause", "drain"):
        return DEFER, "kill switch: %s (%s)" % (fl["mode"], fl.get("why") or "manual"), p["batt_defer_window"]
    if kind not in KINDS:
        return DENY, "unknown kind %r" % kind, None

    # --- battery ---
    b, chg = s.get("battery"), s.get("charging")
    if b is not None and not chg:
        if kind != "notify" and b < p["batt_min"]:
            return DEFER, "battery %d%% < %d%% floor" % (b, p["batt_min"]), p["batt_defer_window"]
        if kind == "model" and b < p["batt_min_model"]:
            return DEFER, "battery %d%% < %d%% model floor" % (b, p["batt_min_model"]), p["batt_defer_window"]

    # --- thermal ---
    t = s.get("temp_c")
    if t is not None and kind in ("model", "shell"):
        if t >= p["temp_max_c"]:
            return DENY, "thermal %.1f°C >= max %.1f°C" % (t, p["temp_max_c"]), None
        if kind == "model" and t >= p["temp_warn_c"]:
            return DEFER, "thermal %.1f°C >= warn %.1f°C" % (t, p["temp_warn_c"]), 120

    # --- network ---
    if kind in ("http", "notify"):
        net = s.get("net")
        if net == "none":
            return DEFER, "no network", 60
        if kind == "http" and p["net_required_http"] == "wifi" and net != "wifi":
            return DEFER, "policy wants wifi, on %s" % net, 300

    # --- resources ---
    fm = s.get("free_mb")
    if fm is not None and fm < p["free_mb_min"] and kind in ("model", "shell"):
        return DENY, "free storage %dMB < %dMB" % (fm, p["free_mb_min"]), None
    if s.get("load", 0) > p["load_max"] and kind in ("model", "shell"):
        return DEFER, "loadavg %.2f > %.1f" % (s["load"], p["load_max"]), 60

    # --- concurrency ---
    leases = open_leases(c)
    cap_kind = p.get("conc_%s" % kind, p["conc_default"])
    cur = sum(1 for l in leases if l["kind"] == kind)
    if cur >= cap_kind:
        return DEFER, "%s concurrency %d/%d" % (kind, cur, cap_kind), 30
    if node:
        curn = sum(1 for l in leases if l["node"] == node)
        if curn >= p["conc_per_node"]:
            return DEFER, "node %s concurrency %d/%d" % (node, curn, p["conc_per_node"]), 30

    # --- budget ---
    u, cap, window = used(c, kind)
    ask = float(tokens) if kind == "model" else 1.0
    if cap is not None and u + ask > cap:
        return DENY, "budget %s %.0f+%.0f > %.0f per %ds" % (kind, u, ask, cap, window), window
    return ALLOW, "ok (batt=%s temp=%s net=%s %s %.0f/%.0f)" % (
        b, ("%.1f" % t) if t is not None else "?", s.get("net"), kind, u, cap or 0), None


def admit(c, kind, node=None, tokens=0.0, task=None):
    v, why, retry = decide(c, kind, node, tokens)
    lease = None
    if v == ALLOW:
        cur = c.execute("INSERT INTO lease(kind,node,task,tokens,state,opened) "
                        "VALUES(?,?,?,?, 'open', ?)", (kind, node, task, tokens, now()))
        lease = cur.lastrowid
        c.commit()
    audit_write("admit", kind=kind, node=node, task=(task or "")[:policy(c)["audit_payload_max"]],
                verdict=v, why=why, lease=lease, tokens=tokens)
    return v, why, retry, lease


def release(c, lease_id, tokens=None, ok=True):
    r = c.execute("SELECT * FROM lease WHERE id=?", (lease_id,)).fetchone()
    if not r:
        return False
    amt = float(tokens if tokens is not None else (r["tokens"] or 0))
    if r["kind"] == "model":
        spend = amt
    else:
        spend = 1.0
    c.execute("UPDATE lease SET state='closed', closed=?, ok=?, tokens=? WHERE id=?",
              (now(), 1 if ok else 0, amt, lease_id))
    c.execute("INSERT INTO usage(kind,amount,ts) VALUES(?,?,?)", (r["kind"], spend, now()))
    c.commit()
    audit_write("release", lease=lease_id, kind=r["kind"], node=r["node"],
                ok=bool(ok), spend=spend)
    return True


def set_flag(c, mode, why=""):
    kv_set(c, "flag", {"mode": mode, "why": why, "ts": now()})
    os.makedirs(MESH_DIR, exist_ok=True)
    with open(FLAG, "w") as f:
        f.write(mode + "\n")          # other tiers can `[ "$(cat ~/.mesh/guard.state)" = run ]`
    audit_write("flag", mode=mode, why=why)


# ------------------------------------------------------------------ wrap ----
def wrap(c, kind, cmd, node=None, tokens=0.0, timeout=None):
    task = " ".join(cmd)
    v, why, retry, lease = admit(c, kind, node=node, tokens=tokens, task=task)
    if v != ALLOW:
        sys.stderr.write("guard: %s — %s%s\n" % (v, why,
                         (" (retry in %ss)" % retry) if retry else ""))
        return EXIT[v]
    t0 = now()
    try:
        p = subprocess.run(cmd, timeout=timeout)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        rc = 124
    except Exception as e:
        sys.stderr.write("guard: exec failed: %s\n" % e)
        rc = 127
    release(c, lease, tokens=tokens, ok=(rc == 0))
    audit_write("exec", kind=kind, node=node, cmd=task[:policy(c)["audit_payload_max"]],
                rc=rc, secs=round(now() - t0, 2))
    return rc


# --------------------------------------------------------------- daemon -----
def daemon(c, interval):
    auto = None
    while True:
        try:
            s = sense(c, force=True)
            reap(c)
            p = policy(c)
            b, chg, t = s.get("battery"), s.get("charging"), s.get("temp_c")
            critical = (b is not None and not chg and b <= max(5, p["batt_min"] - 10)) or \
                       (t is not None and t >= p["temp_max_c"] + 3)
            fl = flag_state(c)
            if critical and fl["mode"] == "run":
                set_flag(c, "pause", "auto: batt=%s temp=%s" % (b, t))
                auto = True
            elif not critical and auto and fl["mode"] == "pause" and \
                    fl.get("why", "").startswith("auto:"):
                set_flag(c, "run", "auto: recovered")
                auto = False
            audit_write("sense", **{k: s[k] for k in ("battery", "charging", "temp_c", "net", "free_mb", "load")})
        except Exception as e:
            sys.stderr.write("guard daemon: %s\n" % e)
        time.sleep(interval)


def drain(c, halt=False, timeout=120):
    set_flag(c, "drain", "manual drain")
    t0 = now()
    while now() - t0 < timeout:
        n = len(open_leases(c))
        if n == 0:
            break
        sys.stderr.write("draining… %d in flight\r" % n)
        time.sleep(2)
    left = len(open_leases(c))
    if halt:
        set_flag(c, "halt", "manual halt")
        for pat in ("mesh_plan.py --daemon", "mesh_exec.py --daemon", "mesh_sched.py --daemon",
                    "mesh_reach.py --daemon"):
            subprocess.run(["pkill", "-f", pat], capture_output=True)
        audit_write("halt", leases_left=left)
    return left


# ------------------------------------------------------------------- ui -----
def bar(v, lo, hi, w=18):
    if v is None:
        return "?" * 3
    frac = max(0.0, min(1.0, (float(v) - lo) / (hi - lo) if hi > lo else 0))
    n = int(frac * w)
    return "[" + "#" * n + "." * (w - n) + "]"


def cmd_status(c):
    s = sense(c, force=True)
    p, fl = policy(c), flag_state(c)
    print("mesh tier 9 — guard   %s" % iso(now()))
    print("  mode      %s%s" % (fl["mode"].upper(), ("  (%s)" % fl["why"]) if fl.get("why") else ""))
    print("  battery   %s%% %s %s" % (s["battery"], bar(s["battery"], 0, 100),
                                      "charging" if s["charging"] else ""))
    print("  temp      %s °C %s" % (("%.1f" % s["temp_c"]) if s["temp_c"] is not None else "?",
                                    bar(s["temp_c"], 20, 50)))
    print("  network   %s      storage %s MB     load %.2f" % (s["net"], s["free_mb"], s["load"]))
    print("  leases    %d open" % len(open_leases(c)))
    print("  budgets:")
    for k in KINDS:
        u, cap, w = used(c, k)
        print("    %-7s %8.0f / %-8s per %5ds %s" % (k, u, ("%.0f" % cap) if cap else "-", w,
                                                     bar(u, 0, cap or 1)))
    print("  verdicts:")
    for k in KINDS:
        v, why, retry = decide(c, k, snap=s)
        print("    %-7s %-6s %s" % (k, v, why))
    ok, n, bad = audit_verify()
    print("  audit     %d records, chain %s" % (n, "OK" if ok else "BROKEN at line %s" % bad))


def parse_window(s):
    m = re.match(r"^(\d+)\s*([smhd])?$", str(s).strip())
    if not m:
        return None
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(m.group(2) or "s", 1)


# --------------------------------------------------------------- selftest ---
def selftest():
    import tempfile
    global MESH_DIR, DB, AUDIT, FLAG
    tmp = tempfile.mkdtemp(prefix="guardtest")
    MESH_DIR, DB = tmp, os.path.join(tmp, "guard.db")
    AUDIT, FLAG = os.path.join(tmp, "a.jsonl"), os.path.join(tmp, "flag")
    c = db()
    ok = fail = 0

    def T(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  ok   %s" % name)
        else:
            fail += 1
            print("  FAIL %s" % name)

    good = {"battery": 90, "charging": False, "temp_c": 30.0, "net": "wifi",
            "free_mb": 8000, "load": 0.5, "ts": now()}

    def S(**kw):
        d = dict(good)
        d.update(kw)
        return d

    # sensors
    T("sense returns keys", set(sense(c)) >= {"battery", "temp_c", "net", "free_mb", "load"})
    T("sense cached", sense(c)["ts"] == sense(c)["ts"])
    T("free_mb positive", (_free_mb() or 1) > 0)
    T("loadavg numeric", isinstance(_loadavg(), float))

    # admission — happy path
    for k in KINDS:
        T("allow %s when healthy" % k, decide(c, k, snap=S())[0] == ALLOW)
    # battery
    T("low batt defers shell", decide(c, "shell", snap=S(battery=10))[0] == DEFER)
    T("low batt allows notify", decide(c, "notify", snap=S(battery=10))[0] == ALLOW)
    T("mid batt defers model", decide(c, "model", snap=S(battery=30))[0] == DEFER)
    T("charging ignores batt floor", decide(c, "model", snap=S(battery=10, charging=True))[0] == ALLOW)
    # thermal
    T("hot denies model", decide(c, "model", snap=S(temp_c=46))[0] == DENY)
    T("warm defers model", decide(c, "model", snap=S(temp_c=41))[0] == DEFER)
    T("warm allows http", decide(c, "http", snap=S(temp_c=41))[0] == ALLOW)
    T("hot denies shell", decide(c, "shell", snap=S(temp_c=46))[0] == DENY)
    # network
    T("no net defers http", decide(c, "http", snap=S(net="none"))[0] == DEFER)
    T("mobile ok by default", decide(c, "http", snap=S(net="mobile"))[0] == ALLOW)
    kv_set(c, "policy", {"net_required_http": "wifi"})
    T("wifi-only policy defers mobile", decide(c, "http", snap=S(net="mobile"))[0] == DEFER)
    kv_set(c, "policy", {})
    # resources
    T("low storage denies shell", decide(c, "shell", snap=S(free_mb=10))[0] == DENY)
    T("high load defers model", decide(c, "model", snap=S(load=99))[0] == DEFER)
    T("unknown kind denied", decide(c, "telepathy", snap=S())[0] == DENY)

    # concurrency
    v, why, r, l1 = admit(c, "model", node="ollama", tokens=10)
    T("first model admitted", v == ALLOW and l1)
    v2, _, _, l2 = admit(c, "model", node="ollama", tokens=10)
    T("second model deferred (conc 1)", v2 == DEFER and l2 is None)
    T("release closes lease", release(c, l1, tokens=10, ok=True))
    v3, _, _, l3 = admit(c, "model", node="ollama", tokens=10)
    T("model admitted after release", v3 == ALLOW)
    release(c, l3, tokens=10)
    a = admit(c, "shell", node="maestro")[3]
    b_ = admit(c, "shell", node="maestro")[3]
    v4, why4, _, _ = admit(c, "shell", node="maestro")
    T("per-node cap of 2 holds", v4 == DEFER and "node" in why4)
    release(c, a); release(c, b_)

    # budgets
    c.execute("DELETE FROM usage WHERE kind='model'"); c.commit()
    kv_set(c, "budgets", {"model": [100, 3600]})
    l = admit(c, "model", tokens=90)[3]
    release(c, l, tokens=90)
    T("usage recorded", used(c, "model")[0] >= 90)
    T("over budget denied", decide(c, "model", tokens=50, snap=S())[0] == DENY)
    T("under budget allowed", decide(c, "model", tokens=5, snap=S())[0] == ALLOW)
    kv_set(c, "budgets", {})
    T("shell budget counts calls", used(c, "shell")[0] == 2)

    # kill switch
    set_flag(c, "pause", "test")
    T("pause defers everything", all(decide(c, k, snap=S())[0] == DEFER for k in KINDS))
    T("flag file written", open(FLAG).read().strip() == "pause")
    set_flag(c, "halt", "test")
    T("halt denies", decide(c, "shell", snap=S())[0] == DENY)
    set_flag(c, "run", "")
    T("resume allows", decide(c, "shell", snap=S())[0] == ALLOW)

    # leases / reaping
    old = admit(c, "http")[3]
    c.execute("UPDATE lease SET opened=? WHERE id=?", (now() - 99999, old)); c.commit()
    T("zombie lease reaped", len(open_leases(c)) == 0)
    T("release of unknown lease is False", release(c, 999999) is False)

    # audit
    okc, n, bad = audit_verify()
    T("audit chain verifies (%d recs)" % n, okc and n > 5)
    with open(AUDIT) as f:
        lines = f.readlines()
    rec = json.loads(lines[2]); rec["why"] = "tampered"
    lines[2] = json.dumps(rec) + "\n"
    with open(AUDIT, "w") as f:
        f.writelines(lines)
    okc2, _, bad2 = audit_verify()
    T("tamper detected", not okc2 and bad2 == 3)
    os.remove(AUDIT)
    T("audit_write bootstraps", bool(audit_write("boot", x=1)) and audit_verify()[0])

    # wrap
    rc = wrap(c, "shell", ["/bin/sh", "-c", "exit 0"], node="maestro")
    T("wrap runs allowed cmd", rc == 0)
    rc = wrap(c, "shell", ["/bin/sh", "-c", "exit 3"], node="maestro")
    T("wrap propagates rc", rc == 3)
    set_flag(c, "pause", "test")
    rc = wrap(c, "shell", ["/bin/sh", "-c", "exit 0"])
    T("wrap blocked when paused", rc == EXIT[DEFER])
    set_flag(c, "run", "")
    T("no leases leaked by wrap", len(open_leases(c)) == 0)

    # misc
    T("parse_window 2h", parse_window("2h") == 7200)
    T("parse_window bad", parse_window("banana") is None)
    T("bar renders", bar(50, 0, 100).count("#") == 9)
    T("policy merges defaults", policy(c)["conc_model"] == 1)
    T("exit codes distinct", len(set(EXIT.values())) == 3)

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n%d passed, %d failed" % (ok, fail))
    return 0 if fail == 0 else 1


# -------------------------------------------------------------------- cli ---
def main():
    ap = argparse.ArgumentParser(prog="guard", add_help=True)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("status")
    for name in ("check", "admit"):
        s = sub.add_parser(name)
        s.add_argument("kind")
        s.add_argument("--node")
        s.add_argument("--tokens", type=float, default=0.0)
        s.add_argument("--task")
    s = sub.add_parser("release"); s.add_argument("lease", type=int)
    s.add_argument("--tokens", type=float); s.add_argument("--fail", action="store_true")
    s = sub.add_parser("policy"); s.add_argument("args", nargs="*")
    s = sub.add_parser("budget"); s.add_argument("args", nargs="*")
    s = sub.add_parser("audit"); s.add_argument("--tail", type=int, default=20)
    s.add_argument("--kind"); s.add_argument("--since")
    sub.add_parser("verify")
    s = sub.add_parser("pause"); s.add_argument("--why", default="manual")
    sub.add_parser("resume")
    s = sub.add_parser("drain"); s.add_argument("--halt", action="store_true")
    s.add_argument("--timeout", type=int, default=120)
    s = sub.add_parser("wrap"); s.add_argument("--kind", default="shell")
    s.add_argument("--node"); s.add_argument("--tokens", type=float, default=0.0)
    s.add_argument("--timeout", type=int); s.add_argument("argv", nargs="*")

    # everything after the first bare "--" is the child command, verbatim
    argv, tail = sys.argv[1:], []
    if "--" in argv:
        i = argv.index("--")
        argv, tail = argv[:i], argv[i + 1:]
    a = ap.parse_args(argv)
    if tail:
        a.argv = list(getattr(a, "argv", []) or []) + tail
    if a.selftest:
        return selftest()
    c = db()
    if a.daemon:
        return daemon(c, a.interval) or 0
    cmd = a.cmd or "status"

    if cmd == "status":
        cmd_status(c); return 0
    if cmd == "check":
        v, why, retry = decide(c, a.kind, a.node, a.tokens)
        audit_write("check", kind=a.kind, node=a.node, verdict=v, why=why)
        print("%s: %s%s" % (v, why, (" retry_after=%ss" % retry) if retry else ""))
        return EXIT[v]
    if cmd == "admit":
        v, why, retry, lease = admit(c, a.kind, a.node, a.tokens, a.task)
        print("%s: %s%s" % (v, why, (" lease=%d" % lease) if lease else ""))
        return EXIT[v]
    if cmd == "release":
        print("released" if release(c, a.lease, a.tokens, ok=not a.fail) else "no such lease")
        return 0
    if cmd == "policy":
        args = a.args
        if args and args[0] == "set":
            p = kv_get(c, "policy", {}) or {}
            for kvs in args[1:]:
                k, _, v = kvs.partition("=")
                try:
                    v = json.loads(v)
                except Exception:
                    pass
                p[k] = v
            kv_set(c, "policy", p); audit_write("policy", set=p)
        elif args and args[0] == "reset":
            kv_set(c, "policy", {}); audit_write("policy", reset=True)
        for k, v in sorted(policy(c).items()):
            print("  %-18s %s" % (k, v))
        return 0
    if cmd == "budget":
        args = a.args
        if args and args[0] == "set":
            b = kv_get(c, "budgets", {}) or {}
            for kvs in args[1:]:
                k, _, v = kvs.partition("=")
                amt, _, win = v.partition("/")
                b[k] = [float(amt), parse_window(win) or {"day": 86400, "hour": 3600,
                        "min": 60}.get(win, 3600)]
            kv_set(c, "budgets", b); audit_write("budget", set=b)
        for k in KINDS:
            u, cap, w = used(c, k)
            print("  %-7s %.0f / %s per %ds" % (k, u, cap, w))
        return 0
    if cmd == "audit":
        since = parse_window(a.since) if a.since else None
        rows = []
        if os.path.exists(AUDIT):
            with open(AUDIT) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if a.kind and r.get("kind") != a.kind:
                        continue
                    if since and r["ts"] < now() - since:
                        continue
                    rows.append(r)
        for r in rows[-a.tail:]:
            extra = " ".join("%s=%s" % (k, v) for k, v in r.items()
                             if k not in ("ts", "event", "prev", "h"))
            print("%s  %-8s %s" % (iso(r["ts"]), r["event"], extra[:160]))
        return 0
    if cmd == "verify":
        ok, n, bad = audit_verify()
        print("chain OK — %d records" % n if ok else "CHAIN BROKEN at line %s (%d ok)" % (bad, n))
        return 0 if ok else 1
    if cmd == "pause":
        set_flag(c, "pause", a.why); print("paused"); return 0
    if cmd == "resume":
        set_flag(c, "run", ""); print("running"); return 0
    if cmd == "drain":
        left = drain(c, a.halt, a.timeout)
        print("\ndrained (%d still open)%s" % (left, " — HALTED" if a.halt else ""))
        return 0
    if cmd == "wrap":
        cmdv = [x for x in (getattr(a, "argv", None) or []) if x != "--"]
        if not cmdv:
            print("usage: guard wrap --kind shell -- <cmd ...>"); return 2
        return wrap(c, a.kind, cmdv, a.node, a.tokens, a.timeout)
    ap.print_help(); return 2


if __name__ == "__main__":
    sys.exit(main())
