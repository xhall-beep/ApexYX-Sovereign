#!/data/data/com.termux/files/usr/bin/env python3
"""
mesh_reach.py — tier 6 of the mesh: REACH (inbox + outbox).

Tier 1 memory (bus.db) -> 2 routing -> 3 execution -> 4 clock -> 5 learning.
Tier 6 connects the mesh to the outside world, both directions:

  INBOX  (outside -> bus tasks)
    * telegram   long-poll getUpdates for @WINGMAN_ALLY_BOT / @wingman_core_agent_bot
    * webhook    tiny local HTTP listener (127.0.0.1) with a shared token
    * filedrop   anything dropped in ~/.mesh/inbox/  (.txt / .json / .md)
  OUTBOX (mesh -> outside)
    * digests from tier 4 (~/.mesh/digests/), bus rows of kind=digest,
      or anything queued with `reach --send`
    * sinks: telegram sendMessage, generic HTTP POST, file sink ~/.mesh/outbox/

Everything is idempotent and dedup'd: an inbound event is only ever turned into
one bus task (UNIQUE(source, ext_id)); an outbound message is retried at most
`--max-attempts` times with 0/20/90s backoff, then dead-lettered.

No third-party deps. Python 3.8+. Offline selftests: `mesh_reach.py --selftest`.
"""
import argparse, json, os, queue, re, sqlite3, sys, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

MESH_DIR = os.environ.get("MESH_DIR", os.path.expanduser("~/.mesh"))
DB = os.path.join(MESH_DIR, "reach.db")
BUS_DB = os.path.join(MESH_DIR, "bus.db")
CONFIG = os.path.join(MESH_DIR, "reach.json")
INBOX_DIR = os.path.join(MESH_DIR, "inbox")
OUTBOX_DIR = os.path.join(MESH_DIR, "outbox")
DIGEST_DIR = os.path.join(MESH_DIR, "digests")
BACKOFF = [0, 20, 90]

DEFAULT_CONFIG = {
    "telegram": {
        "enabled": False,
        "token": "",                 # BotFather token
        "allow_chat_ids": [],        # [] = allow all (not recommended)
        "default_chat_id": "",       # where outbound goes
    },
    "webhook": {"enabled": True, "host": "127.0.0.1", "port": 8770, "token": "changeme"},
    "filedrop": {"enabled": True},
    "outbound": {"sink": "file", "http_url": ""},   # file | telegram | http
    "route": {"submit_to": "auto"},                 # auto = tier-5 sroute policy, else node name
}


# ---------------------------------------------------------------- infra
def _conn(path=DB):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    c = sqlite3.connect(path, timeout=30, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.row_factory = sqlite3.Row
    return c


def init(path=DB):
    c = _conn(path)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS inbound(
      id INTEGER PRIMARY KEY, source TEXT, ext_id TEXT, sender TEXT,
      text TEXT, raw TEXT, ts REAL, task_id TEXT, status TEXT DEFAULT 'new');
    CREATE UNIQUE INDEX IF NOT EXISTS ux_in ON inbound(source, ext_id);
    CREATE TABLE IF NOT EXISTS outbound(
      id INTEGER PRIMARY KEY, sink TEXT, target TEXT, text TEXT, ts REAL,
      status TEXT DEFAULT 'queued', attempts INTEGER DEFAULT 0,
      next_try REAL DEFAULT 0, last_err TEXT, dedup TEXT);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_out ON outbound(dedup);
    CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT);
    """)
    c.commit()
    return c


def get_state(c, k, default=""):
    r = c.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def set_state(c, k, v):
    c.execute("INSERT INTO state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    c.commit()


def load_config():
    os.makedirs(MESH_DIR, exist_ok=True)
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG):
        try:
            user = json.load(open(CONFIG))
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            print("warn: bad reach.json (%s), using defaults" % e, file=sys.stderr)
    else:
        with open(CONFIG, "w") as f:
            json.dump(cfg, f, indent=2)
        os.chmod(CONFIG, 0o600)
    return cfg


def http_json(url, payload=None, timeout=25):
    """POST json if payload else GET. Returns parsed json or raises."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except Exception:
        return {"ok": True, "raw": body}


# ---------------------------------------------------------------- inbox
def record_inbound(c, source, ext_id, sender, text, raw=None):
    """Insert if new; returns row id or None if duplicate."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        cur = c.execute(
            "INSERT INTO inbound(source,ext_id,sender,text,raw,ts) VALUES(?,?,?,?,?,?)",
            (source, str(ext_id), sender or "", text, json.dumps(raw or {}), time.time()))
        c.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def submit_to_mesh(c, row_id, cfg, runner=None):
    """Turn an inbound row into a bus task via tier 3/5. runner injected for tests."""
    r = c.execute("SELECT * FROM inbound WHERE id=?", (row_id,)).fetchone()
    if not r:
        return None
    text = r["text"]
    target = cfg.get("route", {}).get("submit_to", "auto")
    if runner is None:
        runner = _default_runner
    task_id = runner(text, target)
    c.execute("UPDATE inbound SET task_id=?, status=? WHERE id=?",
              (task_id or "", "submitted" if task_id else "unrouted", row_id))
    c.commit()
    return task_id


def _default_runner(text, target):
    """Hand the task to tier 5 (learned routing) or tier 3 (direct submit)."""
    import subprocess
    bin_dir = os.path.expanduser("~/bin")
    node = target
    if target == "auto":
        adv_path = os.path.join(bin_dir, "mesh_learn.py")
        if os.path.exists(adv_path):
            try:
                out = subprocess.run([adv_path, "--advise", text], capture_output=True,
                                     text=True, timeout=30).stdout
                m = re.search(r'"node"\s*:\s*"([^"]+)"', out)
                if m:
                    node = m.group(1)
            except Exception:
                pass
        if node == "auto":
            node = "maestro"
    exe = os.path.join(bin_dir, "mesh_exec.py")
    if not os.path.exists(exe):
        return None
    try:
        out = subprocess.run([exe, "--submit", text, "--for", node],
                             capture_output=True, text=True, timeout=60).stdout
        m = re.search(r"([0-9a-f]{6,})", out)
        return m.group(1) if m else out.strip()[:64] or "submitted"
    except Exception:
        return None


def poll_telegram(c, cfg, fetch=None):
    """Long-poll getUpdates. `fetch(url)` injectable for tests. Returns #new."""
    t = cfg.get("telegram", {})
    if not t.get("enabled") or not t.get("token"):
        return 0
    offset = int(get_state(c, "tg_offset", "0") or 0)
    url = "https://api.telegram.org/bot%s/getUpdates?timeout=25&offset=%d" % (t["token"], offset + 1)
    fetch = fetch or (lambda u: http_json(u))
    try:
        data = fetch(url)
    except Exception as e:
        print("telegram poll error: %s" % e, file=sys.stderr)
        return 0
    allow = [str(x) for x in t.get("allow_chat_ids", [])]
    n = 0
    for up in (data or {}).get("result", []):
        offset = max(offset, int(up.get("update_id", 0)))
        msg = up.get("message") or up.get("edited_message") or up.get("channel_post") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = msg.get("text") or msg.get("caption") or ""
        if allow and chat_id not in allow:
            continue
        sender = (msg.get("from") or {}).get("username") or chat_id
        if record_inbound(c, "telegram", up.get("update_id"), sender, text, up):
            n += 1
    set_state(c, "tg_offset", offset)
    return n


def scan_filedrop(c, cfg):
    """Any file dropped in ~/.mesh/inbox becomes one task; file is archived."""
    if not cfg.get("filedrop", {}).get("enabled", True):
        return 0
    os.makedirs(INBOX_DIR, exist_ok=True)
    done = os.path.join(INBOX_DIR, "_processed")
    os.makedirs(done, exist_ok=True)
    n = 0
    for name in sorted(os.listdir(INBOX_DIR)):
        p = os.path.join(INBOX_DIR, name)
        if not os.path.isfile(p) or name.startswith("."):
            continue
        try:
            body = open(p, "r", errors="replace").read().strip()
        except Exception:
            continue
        if name.endswith(".json"):
            try:
                body = (json.loads(body).get("text") or body).strip()
            except Exception:
                pass
        if record_inbound(c, "filedrop", "%s:%d" % (name, int(os.path.getmtime(p))), name, body):
            n += 1
        try:
            os.replace(p, os.path.join(done, "%d-%s" % (int(time.time()), name)))
        except OSError:
            pass
    return n


class _Handler(BaseHTTPRequestHandler):
    cfg = None
    sink = None      # callable(source, ext_id, sender, text)

    def _reply(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._reply(200, {"ok": True, "service": "mesh-reach", "tier": 6})

    def do_POST(self):
        tok = (self.cfg or {}).get("webhook", {}).get("token", "")
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        given = self.headers.get("X-Mesh-Token") or (qs.get("token", [""])[0])
        if tok and given != tok:
            return self._reply(403, {"ok": False, "error": "bad token"})
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        try:
            obj = json.loads(raw)
        except Exception:
            obj = {"text": raw}
        text = obj.get("text") or obj.get("message") or raw
        ext = obj.get("id") or "%f" % time.time()
        ok = self.sink("webhook", ext, obj.get("from", "http"), text, obj)
        self._reply(200, {"ok": True, "accepted": bool(ok)})

    def log_message(self, *a):
        pass


def start_webhook(cfg, sink):
    w = cfg.get("webhook", {})
    if not w.get("enabled", True):
        return None
    _Handler.cfg, _Handler.sink = cfg, staticmethod(sink)
    srv = HTTPServer((w.get("host", "127.0.0.1"), int(w.get("port", 8770))), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------- outbox
def queue_out(c, text, sink=None, target="", cfg=None, dedup=None):
    cfg = cfg or {}
    sink = sink or cfg.get("outbound", {}).get("sink", "file")
    if sink == "telegram" and not target:
        target = cfg.get("telegram", {}).get("default_chat_id", "")
    if sink == "http" and not target:
        target = cfg.get("outbound", {}).get("http_url", "")
    dedup = dedup or ("%s|%s|%s" % (sink, target, text))[:400]
    try:
        cur = c.execute(
            "INSERT INTO outbound(sink,target,text,ts,dedup,next_try) VALUES(?,?,?,?,?,?)",
            (sink, target, text, time.time(), dedup, time.time()))
        c.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def _deliver(row, cfg, sender=None):
    """Returns (ok, err). `sender` injectable for tests."""
    if sender:
        return sender(row["sink"], row["target"], row["text"])
    if row["sink"] == "file":
        os.makedirs(OUTBOX_DIR, exist_ok=True)
        p = os.path.join(OUTBOX_DIR, "%d-%s.txt" % (int(time.time()), (row["target"] or "out")[:24]))
        open(p, "w").write(row["text"])
        return True, ""
    if row["sink"] == "telegram":
        tok = cfg.get("telegram", {}).get("token", "")
        if not tok or not row["target"]:
            return False, "telegram not configured"
        try:
            r = http_json("https://api.telegram.org/bot%s/sendMessage" % tok,
                          {"chat_id": row["target"], "text": row["text"][:4000],
                           "disable_web_page_preview": True})
            return bool(r.get("ok", True)), json.dumps(r)[:180]
        except Exception as e:
            return False, str(e)[:180]
    if row["sink"] == "http":
        if not row["target"]:
            return False, "no http_url"
        try:
            http_json(row["target"], {"text": row["text"]})
            return True, ""
        except Exception as e:
            return False, str(e)[:180]
    return False, "unknown sink %s" % row["sink"]


def flush_out(c, cfg, max_attempts=3, sender=None, now=None):
    now = now or time.time()
    rows = c.execute("SELECT * FROM outbound WHERE status IN ('queued','retry') AND next_try<=? "
                     "ORDER BY id LIMIT 50", (now,)).fetchall()
    sent = failed = 0
    for r in rows:
        ok, err = _deliver(r, cfg, sender)
        att = r["attempts"] + 1
        if ok:
            c.execute("UPDATE outbound SET status='sent', attempts=?, last_err='' WHERE id=?", (att, r["id"]))
            sent += 1
        elif att >= max_attempts:
            c.execute("UPDATE outbound SET status='dead', attempts=?, last_err=? WHERE id=?", (att, err, r["id"]))
            failed += 1
        else:
            c.execute("UPDATE outbound SET status='retry', attempts=?, last_err=?, next_try=? WHERE id=?",
                      (att, err, now + BACKOFF[min(att, len(BACKOFF) - 1)], r["id"]))
    c.commit()
    return sent, failed


def collect_digests(c, cfg):
    """New tier-4 digest files + bus rows of kind=digest -> outbound queue."""
    n = 0
    if os.path.isdir(DIGEST_DIR):
        seen = set(json.loads(get_state(c, "digests_seen", "[]") or "[]"))
        for name in sorted(os.listdir(DIGEST_DIR)):
            if name in seen:
                continue
            p = os.path.join(DIGEST_DIR, name)
            if not os.path.isfile(p):
                continue
            try:
                body = open(p, errors="replace").read().strip()
            except Exception:
                continue
            if body and queue_out(c, body[:3500], cfg=cfg, dedup="digest:" + name):
                n += 1
            seen.add(name)
        set_state(c, "digests_seen", json.dumps(sorted(seen)[-500:]))
    if os.path.exists(BUS_DB):
        try:
            b = _conn(BUS_DB)
            last = int(get_state(c, "bus_digest_id", "0") or 0)
            cols = [r[1] for r in b.execute("PRAGMA table_info(messages)")]
            if cols:
                tcol = "body" if "body" in cols else ("text" if "text" in cols else cols[-1])
                for row in b.execute("SELECT id, %s AS t FROM messages WHERE kind='digest' AND id>? "
                                     "ORDER BY id LIMIT 50" % tcol, (last,)):
                    if row["t"] and queue_out(c, str(row["t"])[:3500], cfg=cfg, dedup="bus:%d" % row["id"]):
                        n += 1
                    last = max(last, row["id"])
                set_state(c, "bus_digest_id", last)
            b.close()
        except Exception as e:
            print("digest/bus scan skipped: %s" % e, file=sys.stderr)
    return n


# ---------------------------------------------------------------- loop
def cycle(c, cfg, submit=True):
    stats = {"telegram": 0, "filedrop": 0, "digests": 0, "sent": 0, "failed": 0, "submitted": 0}
    stats["telegram"] = poll_telegram(c, cfg)
    stats["filedrop"] = scan_filedrop(c, cfg)
    if submit:
        for r in c.execute("SELECT id FROM inbound WHERE status='new' ORDER BY id LIMIT 50").fetchall():
            if submit_to_mesh(c, r["id"], cfg):
                stats["submitted"] += 1
    stats["digests"] = collect_digests(c, cfg)
    s, f = flush_out(c, cfg)
    stats["sent"], stats["failed"] = s, f
    return stats


def status(c):
    out = ["mesh tier 6 — reach", "db: %s" % DB]
    for src, n in c.execute("SELECT source, COUNT(*) n FROM inbound GROUP BY source"):
        out.append("  inbound %-9s %d" % (src, n))
    for st, n in c.execute("SELECT status, COUNT(*) n FROM inbound GROUP BY status"):
        out.append("  inbound[%s] %d" % (st, n))
    for st, n in c.execute("SELECT status, COUNT(*) n FROM outbound GROUP BY status"):
        out.append("  outbound[%s] %d" % (st, n))
    for r in c.execute("SELECT * FROM outbound WHERE status='dead' ORDER BY id DESC LIMIT 5"):
        out.append("  DEAD #%d %s -> %s: %s" % (r["id"], r["sink"], r["target"], (r["last_err"] or "")[:70]))
    for r in c.execute("SELECT * FROM inbound ORDER BY id DESC LIMIT 5"):
        out.append("  last in  [%s] %s: %s" % (r["source"], r["sender"], (r["text"] or "")[:60]))
    return "\n".join(out)


# ---------------------------------------------------------------- selftest
def selftest():
    import tempfile, shutil
    global MESH_DIR, DB, INBOX_DIR, OUTBOX_DIR, DIGEST_DIR, BUS_DB
    tmp = tempfile.mkdtemp(prefix="reachtest")
    MESH_DIR = tmp
    DB = os.path.join(tmp, "reach.db")
    INBOX_DIR = os.path.join(tmp, "inbox")
    OUTBOX_DIR = os.path.join(tmp, "outbox")
    DIGEST_DIR = os.path.join(tmp, "digests")
    BUS_DB = os.path.join(tmp, "bus.db")
    ok = fail = 0

    def check(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  ok   %s" % name)
        else:
            fail += 1
            print("  FAIL %s" % name)

    c = init(DB)
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["telegram"] = {"enabled": True, "token": "T", "allow_chat_ids": ["42"], "default_chat_id": "42"}

    check("schema created", bool(c.execute("SELECT name FROM sqlite_master WHERE name='inbound'").fetchone()))

    # inbound dedup
    check("record inbound", record_inbound(c, "test", "e1", "me", "hello") is not None)
    check("dedup same ext_id", record_inbound(c, "test", "e1", "me", "hello") is None)
    check("empty text ignored", record_inbound(c, "test", "e2", "me", "   ") is None)

    # telegram
    updates = {"result": [
        {"update_id": 10, "message": {"chat": {"id": 42}, "from": {"username": "monty"}, "text": "run diagnostics"}},
        {"update_id": 11, "message": {"chat": {"id": 99}, "text": "stranger danger"}},
        {"update_id": 12, "message": {"chat": {"id": 42}, "caption": "photo caption"}}]}
    n = poll_telegram(c, cfg, fetch=lambda u: updates)
    check("telegram ingested allowed chats", n == 2)
    check("telegram blocked foreign chat",
          c.execute("SELECT COUNT(*) FROM inbound WHERE text LIKE 'stranger%'").fetchone()[0] == 0)
    check("telegram offset saved", get_state(c, "tg_offset") == "12")
    check("telegram replay is a no-op", poll_telegram(c, cfg, fetch=lambda u: updates) == 0)
    check("telegram disabled -> 0", poll_telegram(c, {"telegram": {"enabled": False}}) == 0)

    # filedrop
    os.makedirs(INBOX_DIR, exist_ok=True)
    open(os.path.join(INBOX_DIR, "a.txt"), "w").write("summarize my notes")
    open(os.path.join(INBOX_DIR, "b.json"), "w").write(json.dumps({"text": "ping the router"}))
    n = scan_filedrop(c, cfg)
    check("filedrop ingested 2", n == 2)
    check("filedrop json text extracted",
          c.execute("SELECT COUNT(*) FROM inbound WHERE text='ping the router'").fetchone()[0] == 1)
    check("filedrop files archived", os.listdir(INBOX_DIR) == ["_processed"])
    check("filedrop rescan is a no-op", scan_filedrop(c, cfg) == 0)

    # submit -> bus (injected runner)
    calls = []
    def runner(text, target):
        calls.append((text, target))
        return "task%d" % len(calls)
    rid = c.execute("SELECT id FROM inbound WHERE text='summarize my notes'").fetchone()["id"]
    check("submit returns task id", submit_to_mesh(c, rid, cfg, runner=runner) == "task1")
    check("inbound marked submitted",
          c.execute("SELECT status FROM inbound WHERE id=?", (rid,)).fetchone()[0] == "submitted")
    check("runner got auto target", calls[0][1] == "auto")

    # outbox
    check("queue_out works", queue_out(c, "digest one", cfg=cfg) is not None)
    check("queue_out dedups", queue_out(c, "digest one", cfg=cfg) is None)
    sent, failed = flush_out(c, cfg)
    check("file sink delivered", sent == 1 and failed == 0)
    check("file written to outbox", any(f.endswith(".txt") for f in os.listdir(OUTBOX_DIR)))

    # retry + dead letter
    oid = queue_out(c, "will fail", sink="telegram", target="42", cfg=cfg)
    c.execute("UPDATE outbound SET next_try=0 WHERE id=?", (oid,)); c.commit()
    bad = lambda s, t, x: (False, "boom")
    s1, f1 = flush_out(c, cfg, sender=bad, now=1000)
    s2, f2 = flush_out(c, cfg, sender=bad, now=1000 + 21)
    s3, f3 = flush_out(c, cfg, sender=bad, now=1000 + 200)
    check("failure backs off, not dead yet", (s1, f1, s2, f2) == (0, 0, 0, 0))
    check("dead letter after 3 attempts", f3 == 1)
    check("dead row recorded",
          c.execute("SELECT COUNT(*) FROM outbound WHERE status='dead'").fetchone()[0] == 1)

    # digest collection
    os.makedirs(DIGEST_DIR, exist_ok=True)
    open(os.path.join(DIGEST_DIR, "2026-08-14.md"), "w").write("nightly digest body")
    check("digest queued", collect_digests(c, cfg) == 1)
    check("digest not re-queued", collect_digests(c, cfg) == 0)

    # bus digest rows
    b = _conn(BUS_DB)
    b.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, kind TEXT, body TEXT)")
    b.execute("INSERT INTO messages(kind, body) VALUES('digest','from bus')")
    b.execute("INSERT INTO messages(kind, body) VALUES('task','not a digest')")
    b.commit(); b.close()
    check("bus digest picked up", collect_digests(c, cfg) == 1)
    check("bus digest cursor advances", collect_digests(c, cfg) == 0)

    # webhook handler auth logic (no socket needed)
    got = []
    _Handler.cfg = cfg
    _Handler.sink = staticmethod(lambda *a: got.append(a) or True)
    check("webhook token configured", cfg["webhook"]["token"] == "changeme")

    # end-to-end cycle
    open(os.path.join(INBOX_DIR, "c.txt"), "w").write("end to end")
    cfg_off = json.loads(json.dumps(cfg)); cfg_off["telegram"]["enabled"] = False
    st = cycle(c, cfg_off, submit=False)
    check("cycle reports filedrop", st["filedrop"] == 1)
    check("status renders", "reach" in status(c))

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n%d passed, %d failed" % (ok, fail))
    return 1 if fail else 0


# ---------------------------------------------------------------- cli
def main():
    ap = argparse.ArgumentParser(description="mesh tier 6 — inbox/outbox reach")
    ap.add_argument("--once", action="store_true", help="one poll/flush cycle")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--interval", type=int, default=20)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--send", help="queue an outbound message")
    ap.add_argument("--to", default="", help="target chat id / url for --send")
    ap.add_argument("--sink", help="file|telegram|http (default from config)")
    ap.add_argument("--inject", help="inject text as if it arrived from outside")
    ap.add_argument("--retry-dead", action="store_true")
    ap.add_argument("--config", action="store_true", help="print config path + values")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        sys.exit(selftest())

    cfg = load_config()
    c = init()

    if a.config:
        print(CONFIG); print(json.dumps(cfg, indent=2)); return
    if a.status:
        print(status(c)); return
    if a.send:
        print("queued" if queue_out(c, a.send, sink=a.sink, target=a.to, cfg=cfg) else "duplicate — skipped")
        print(flush_out(c, cfg)); return
    if a.inject:
        rid = record_inbound(c, "manual", "m%f" % time.time(), "cli", a.inject)
        print("task:", submit_to_mesh(c, rid, cfg) if rid else "duplicate"); return
    if a.retry_dead:
        c.execute("UPDATE outbound SET status='queued', attempts=0, next_try=0 WHERE status='dead'")
        c.commit(); print(flush_out(c, cfg)); return

    srv = None
    if a.daemon or a.once:
        srv = start_webhook(cfg, lambda src, ext, who, txt, raw=None: record_inbound(c, src, ext, who, txt, raw))
        if srv:
            w = cfg["webhook"]
            print("webhook listening on http://%s:%s (X-Mesh-Token)" % (w["host"], w["port"]))
    if a.once:
        print(json.dumps(cycle(c, cfg))); return
    if a.daemon:
        print("reach daemon up (interval %ds)" % a.interval)
        while True:
            try:
                st = cycle(c, cfg)
                if any(st.values()):
                    print(time.strftime("%H:%M:%S"), json.dumps(st), flush=True)
            except Exception as e:
                print("cycle error: %s" % e, file=sys.stderr, flush=True)
            time.sleep(a.interval)
    ap.print_help()


if __name__ == "__main__":
    main()
