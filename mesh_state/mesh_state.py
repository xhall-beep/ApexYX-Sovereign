#!/usr/bin/env python3
"""mesh_state.py — tier 7 of the Pixel mesh: the shared world-model.

Tier 1 bus = messages. Tier 2 router = who. Tier 3 exec = hands.
Tier 4 sched = clock. Tier 5 learn = judgement. Tier 6 reach = outside world.
Tier 7 state = MEMORY OF FACTS: entities, projects, open loops, last-known values.

Every node reads a compact BRIEF before acting and writes back what it learned,
so the mesh stops being amnesiac between tasks.

Storage: ~/.mesh/state.db (own file; never touches bus.db / exec.db / sched.db).

CLI
  state ent <kind> <name> [--project P] [--alias A ...]   upsert an entity
  state set <name> key=value [key=value ...] [--source S] [--node N] [--conf F]
  state get <name>                                        last-known values
  state forget <name> [key]                               retire fact(s)/entity
  state loop open "<title>" [--project P] [--due ISO] [--node N]
  state loop close <id> [--note "..."]
  state loop touch <id> --note "..."
  state loops [--project P] [--all]
  state brief [--project P] [--max-chars N] [--json]      context block for prompts
  state ingest [--limit N]                                harvest tier3 results.jsonl + tier1 bus
  state import <file.json>                                bulk load (Notion/ARAIKI export)
  state gc [--days N]                                     prune superseded/closed history
  state --daemon [--interval S]                           periodic ingest + stale-loop nudges
  state --selftest
"""
import argparse, json, os, re, sqlite3, sys, time, hashlib
from datetime import datetime, timezone

MESH_DIR = os.environ.get("MESH_DIR", os.path.expanduser("~/.mesh"))
DB = os.path.join(MESH_DIR, "state.db")
RESULTS = os.path.join(MESH_DIR, "results.jsonl")     # tier 3
BUS = os.path.join(MESH_DIR, "bus.db")                # tier 1
CURSOR = os.path.join(MESH_DIR, "state.cursor")

KINDS = ("project", "person", "service", "device", "doc", "task", "value", "other")


def now() -> float:
    return time.time()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def norm(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


# ---------------------------------------------------------------- schema ----
SCHEMA = """
CREATE TABLE IF NOT EXISTS entity(
  id INTEGER PRIMARY KEY, key TEXT UNIQUE, name TEXT, kind TEXT,
  project TEXT, created REAL, updated REAL, retired INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS alias(
  entity_id INTEGER, alias TEXT, UNIQUE(alias));
CREATE TABLE IF NOT EXISTS fact(
  id INTEGER PRIMARY KEY, entity_id INTEGER, k TEXT, v TEXT,
  source TEXT, node TEXT, conf REAL DEFAULT 0.6, ts REAL, superseded INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS fact_lookup ON fact(entity_id,k,superseded);
CREATE TABLE IF NOT EXISTS loop(
  id INTEGER PRIMARY KEY, title TEXT, project TEXT, status TEXT DEFAULT 'open',
  node TEXT, opened REAL, due REAL, touched REAL, closed REAL, note TEXT,
  sig TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS journal(
  id INTEGER PRIMARY KEY, ts REAL, kind TEXT, ref TEXT, detail TEXT);
"""


def db(path=None):
    p = path or DB
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    c = sqlite3.connect(p, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    return c


def jrn(c, kind, ref, detail=""):
    c.execute("INSERT INTO journal(ts,kind,ref,detail) VALUES(?,?,?,?)",
              (now(), kind, str(ref), detail[:400]))


# -------------------------------------------------------------- entities ----
def upsert_entity(c, name, kind="other", project=None, aliases=()):
    if kind not in KINDS:
        kind = "other"
    k = norm(name)
    row = resolve(c, name)
    if row:
        c.execute("UPDATE entity SET kind=COALESCE(?,kind), project=COALESCE(?,project),"
                  " updated=?, retired=0 WHERE id=?",
                  (kind if kind != "other" else None, project, now(), row["id"]))
        eid = row["id"]
    else:
        cur = c.execute("INSERT INTO entity(key,name,kind,project,created,updated)"
                        " VALUES(?,?,?,?,?,?)", (k, name.strip(), kind, project, now(), now()))
        eid = cur.lastrowid
        jrn(c, "entity", eid, f"{kind}:{name}")
    for a in aliases:
        try:
            c.execute("INSERT OR IGNORE INTO alias(entity_id,alias) VALUES(?,?)", (eid, norm(a)))
        except sqlite3.IntegrityError:
            pass
    c.commit()
    return eid


def resolve(c, name):
    k = norm(name)
    r = c.execute("SELECT * FROM entity WHERE key=?", (k,)).fetchone()
    if r:
        return r
    a = c.execute("SELECT entity_id FROM alias WHERE alias=?", (k,)).fetchone()
    if a:
        return c.execute("SELECT * FROM entity WHERE id=?", (a["entity_id"],)).fetchone()
    # loose contains-match, longest name wins (helps free-text ingest)
    rows = c.execute("SELECT * FROM entity WHERE retired=0").fetchall()
    hits = [r for r in rows if r["key"] and (r["key"] in k or k in r["key"])]
    return sorted(hits, key=lambda r: -len(r["key"]))[0] if hits else None


def set_fact(c, name, k, v, source="cli", node="local", conf=0.6, kind="other", project=None):
    eid = upsert_entity(c, name, kind=kind, project=project)
    prev = c.execute("SELECT * FROM fact WHERE entity_id=? AND k=? AND superseded=0",
                     (eid, k)).fetchone()
    if prev:
        if str(prev["v"]) == str(v):
            c.execute("UPDATE fact SET ts=?, conf=MAX(conf,?), source=? WHERE id=?",
                      (now(), conf, source, prev["id"]))
            c.commit()
            return prev["id"], "confirmed"
        # lower-confidence, older-source claims do not overwrite fresh strong facts
        if conf < prev["conf"] - 0.25 and now() - prev["ts"] < 3600:
            c.commit()
            return prev["id"], "rejected"
        c.execute("UPDATE fact SET superseded=1 WHERE id=?", (prev["id"],))
    cur = c.execute("INSERT INTO fact(entity_id,k,v,source,node,conf,ts) VALUES(?,?,?,?,?,?,?)",
                    (eid, k, str(v), source, node, conf, now()))
    c.execute("UPDATE entity SET updated=? WHERE id=?", (now(), eid))
    jrn(c, "fact", eid, f"{k}={v} ({source})")
    c.commit()
    return cur.lastrowid, "updated" if prev else "new"


def get_facts(c, name):
    e = resolve(c, name)
    if not e:
        return None, []
    rows = c.execute("SELECT * FROM fact WHERE entity_id=? AND superseded=0 ORDER BY ts DESC",
                     (e["id"],)).fetchall()
    return e, rows


def forget(c, name, key=None):
    e = resolve(c, name)
    if not e:
        return 0
    if key:
        n = c.execute("UPDATE fact SET superseded=1 WHERE entity_id=? AND k=? AND superseded=0",
                      (e["id"], key)).rowcount
    else:
        c.execute("UPDATE fact SET superseded=1 WHERE entity_id=?", (e["id"],))
        n = c.execute("UPDATE entity SET retired=1 WHERE id=?", (e["id"],)).rowcount
    jrn(c, "forget", e["id"], key or "*")
    c.commit()
    return n


# ----------------------------------------------------------- open loops -----
def loop_sig(title, project):
    return hashlib.sha1(f"{norm(title)}|{norm(project or '')}".encode()).hexdigest()[:16]


def loop_open(c, title, project=None, due=None, node="local", note=""):
    sig = loop_sig(title, project)
    ex = c.execute("SELECT * FROM loop WHERE sig=?", (sig,)).fetchone()
    if ex:
        if ex["status"] != "open":
            c.execute("UPDATE loop SET status='open', closed=NULL, touched=? WHERE id=?",
                      (now(), ex["id"]))
            c.commit()
        return ex["id"], "reopened" if ex["status"] != "open" else "exists"
    cur = c.execute("INSERT INTO loop(title,project,status,node,opened,due,touched,note,sig)"
                    " VALUES(?,?,'open',?,?,?,?,?,?)",
                    (title.strip(), project, node, now(), due, now(), note, sig))
    jrn(c, "loop.open", cur.lastrowid, title)
    c.commit()
    return cur.lastrowid, "new"


def loop_close(c, lid, note=""):
    n = c.execute("UPDATE loop SET status='closed', closed=?, touched=?, note=? WHERE id=? AND status!='closed'",
                  (now(), now(), note, lid)).rowcount
    if n:
        jrn(c, "loop.close", lid, note)
    c.commit()
    return n


def loop_touch(c, lid, note=""):
    n = c.execute("UPDATE loop SET touched=?, note=COALESCE(NULLIF(?,''),note) WHERE id=?",
                  (now(), note, lid)).rowcount
    c.commit()
    return n


def loops(c, project=None, show_all=False):
    q = "SELECT * FROM loop WHERE 1=1"
    a = []
    if not show_all:
        q += " AND status='open'"
    if project:
        q += " AND project=?"
        a.append(project)
    return c.execute(q + " ORDER BY (due IS NULL), due, touched DESC", a).fetchall()


def stale_loops(c, days=3):
    cut = now() - days * 86400
    return [r for r in loops(c) if (r["touched"] or r["opened"]) < cut]


def overdue_loops(c):
    return [r for r in loops(c) if r["due"] and r["due"] < now()]


# ---------------------------------------------------------------- brief -----
def brief(c, project=None, max_chars=1200, as_json=False):
    """The compact world-model every node reads before acting."""
    ents = c.execute("SELECT * FROM entity WHERE retired=0" +
                     (" AND (project=? OR name=?)" if project else "") +
                     " ORDER BY updated DESC LIMIT 40",
                     ([project, project] if project else [])).fetchall()
    data = {"generated": iso(now()), "project": project, "entities": [], "open_loops": []}
    for e in ents:
        fs = c.execute("SELECT k,v,ts,conf FROM fact WHERE entity_id=? AND superseded=0"
                       " ORDER BY conf DESC, ts DESC LIMIT 6", (e["id"],)).fetchall()
        if not fs and e["kind"] == "other":
            continue
        data["entities"].append({
            "name": e["name"], "kind": e["kind"], "project": e["project"],
            "facts": {f["k"]: f["v"] for f in fs},
            "updated": iso(e["updated"] or e["created"]),
        })
    for l in loops(c, project)[:20]:
        data["open_loops"].append({
            "id": l["id"], "title": l["title"], "project": l["project"],
            "age_h": round((now() - l["opened"]) / 3600, 1),
            "due": iso(l["due"]) if l["due"] else None,
            "overdue": bool(l["due"] and l["due"] < now()),
        })
    if as_json:
        return json.dumps(data, indent=2)

    lines = ["## MESH STATE (%s)%s" % (data["generated"], f" — {project}" if project else "")]
    if data["entities"]:
        lines.append("known:")
        for e in data["entities"]:
            f = "; ".join(f"{k}={v}" for k, v in e["facts"].items())
            lines.append(f"- [{e['kind']}] {e['name']}" + (f": {f}" if f else ""))
    if data["open_loops"]:
        lines.append("open loops:")
        for l in data["open_loops"]:
            tag = " OVERDUE" if l["overdue"] else ""
            lines.append(f"- #{l['id']} {l['title']} ({l['age_h']}h old{tag})")
    if len(lines) == 1:
        lines.append("(empty — no facts recorded yet)")
    out = "\n".join(lines)
    if len(out) > max_chars:                 # truncate on a line boundary
        keep = out[:max_chars].rsplit("\n", 1)[0]
        out = keep + "\n… (truncated)"
    return out


# --------------------------------------------------------------- ingest -----
FACT_RE = re.compile(r"\b([A-Za-z][\w .-]{1,40}?)\s*(?:=|:| is | are )\s*([^\n;,]{1,80})")
LOOP_RE = re.compile(r"\b(?:todo|TODO|next step|follow[- ]up|blocked on|waiting on)\b[:\- ]\s*(.{4,100})")


def extract(text, source="ingest", node="local"):
    """Very conservative extraction: returns (facts, loops)."""
    facts, lps = [], []
    for m in LOOP_RE.finditer(text or ""):
        lps.append(m.group(1).strip().rstrip("."))
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or len(line) > 160:
            continue
        if LOOP_RE.search(line):
            continue
        m = FACT_RE.match(line)
        if m:
            subj, val = m.group(1).strip(), m.group(2).strip()
            if len(subj) < 2 or not val:
                continue
            if " " in subj:
                ent, key = subj.rsplit(" ", 1)
            else:
                ent, key = subj, "value"
            facts.append((ent.strip(), key.strip().lower(), val))
    return facts, lps


def ingest(c, limit=200):
    """Harvest tier-3 results + tier-1 bus messages into state."""
    cur = {"results": 0, "bus": 0}
    if os.path.exists(CURSOR):
        try:
            cur.update(json.load(open(CURSOR)))
        except Exception:
            pass
    nf = nl = 0
    # tier 3 results.jsonl
    if os.path.exists(RESULTS):
        with open(RESULTS) as fh:
            lines = fh.readlines()
        for raw in lines[cur["results"]:cur["results"] + limit]:
            try:
                r = json.loads(raw)
            except Exception:
                continue
            txt = str(r.get("output") or r.get("text") or "")
            node = r.get("node", "local")
            fs, ls = extract(txt)
            for ent, k, v in fs[:5]:
                set_fact(c, ent, k, v, source="exec", node=node, conf=0.45)
                nf += 1
            for t in ls[:5]:
                loop_open(c, t, project=r.get("project"), node=node)
                nl += 1
        cur["results"] = min(len(lines), cur["results"] + limit)
    # tier 1 bus
    if os.path.exists(BUS):
        try:
            b = sqlite3.connect(BUS, timeout=10)
            b.row_factory = sqlite3.Row
            rows = b.execute("SELECT * FROM messages WHERE id>? ORDER BY id LIMIT ?",
                             (cur["bus"], limit)).fetchall()
            for r in rows:
                d = dict(r)
                txt = str(d.get("text") or d.get("body") or d.get("payload") or "")
                fs, ls = extract(txt)
                for ent, k, v in fs[:5]:
                    set_fact(c, ent, k, v, source="bus",
                             node=str(d.get("sender") or d.get("node") or "bus"), conf=0.4)
                    nf += 1
                for t in ls[:5]:
                    loop_open(c, t, node=str(d.get("sender") or "bus"))
                    nl += 1
                cur["bus"] = max(cur["bus"], d.get("id", cur["bus"]))
            b.close()
        except Exception as e:
            jrn(c, "ingest.err", "bus", str(e))
            c.commit()
    json.dump(cur, open(CURSOR, "w"))
    return nf, nl


def import_json(c, path):
    """Bulk import: {"entities":[{name,kind,project,facts:{}}], "loops":[{title,project,due}]}"""
    d = json.load(open(path))
    n = 0
    for e in d.get("entities", []):
        upsert_entity(c, e["name"], e.get("kind", "other"), e.get("project"), e.get("aliases", []))
        for k, v in (e.get("facts") or {}).items():
            set_fact(c, e["name"], k, v, source=e.get("source", "import"), conf=0.8)
            n += 1
    for l in d.get("loops", []):
        loop_open(c, l["title"], l.get("project"), l.get("due"), l.get("node", "import"))
        n += 1
    return n


def gc(c, days=30):
    cut = now() - days * 86400
    a = c.execute("DELETE FROM fact WHERE superseded=1 AND ts<?", (cut,)).rowcount
    b = c.execute("DELETE FROM loop WHERE status='closed' AND closed<?", (cut,)).rowcount
    d = c.execute("DELETE FROM journal WHERE ts<?", (cut,)).rowcount
    c.commit()
    c.execute("VACUUM")
    return a, b, d


def push_bus(kind, text):
    """Best-effort nudge onto the tier-1 bus (no hard dependency)."""
    if not os.path.exists(BUS):
        return False
    try:
        b = sqlite3.connect(BUS, timeout=10)
        cols = [r[1] for r in b.execute("PRAGMA table_info(messages)")]
        if "text" in cols and "kind" in cols:
            f = {"kind": kind, "text": text}
            if "sender" in cols:
                f["sender"] = "state"
            if "ts" in cols:
                f["ts"] = now()
            b.execute("INSERT INTO messages(%s) VALUES(%s)" %
                      (",".join(f), ",".join("?" * len(f))), tuple(f.values()))
            b.commit()
        b.close()
        return True
    except Exception:
        return False


# -------------------------------------------------------------- selftest ----
def selftest():
    import tempfile, shutil
    tmp = tempfile.mkdtemp()
    global DB, RESULTS, BUS, CURSOR
    DB = os.path.join(tmp, "state.db"); RESULTS = os.path.join(tmp, "results.jsonl")
    BUS = os.path.join(tmp, "bus.db"); CURSOR = os.path.join(tmp, "state.cursor")
    ok = fail = 0

    def t(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1; print(f"  ok   {name}")
        else:
            fail += 1; print(f"  FAIL {name}")

    c = db()
    t("schema", bool(c.execute("SELECT name FROM sqlite_master WHERE name='entity'").fetchone()))
    eid = upsert_entity(c, "ARAIKI", "project", aliases=["araiki core"])
    t("entity insert", eid > 0)
    t("entity idempotent", upsert_entity(c, "araiki", "project") == eid)
    t("alias resolve", resolve(c, "ARAIKI CORE")["id"] == eid)
    set_fact(c, "ARAIKI", "status", "design", source="cli", conf=0.7)
    e, fs = get_facts(c, "ARAIKI")
    t("fact stored", fs and fs[0]["v"] == "design")
    set_fact(c, "ARAIKI", "status", "build", source="cli", conf=0.7)
    e, fs = get_facts(c, "ARAIKI")
    t("last-known wins", len([f for f in fs if f["k"] == "status"]) == 1 and fs[0]["v"] == "build")
    t("history kept", c.execute("SELECT COUNT(*) a FROM fact WHERE superseded=1").fetchone()["a"] == 1)
    _, st = set_fact(c, "ARAIKI", "status", "build")
    t("confirm not duplicate", st == "confirmed")
    _, st = set_fact(c, "ARAIKI", "status", "guess", conf=0.1)
    t("weak claim rejected", st == "rejected" and get_facts(c, "ARAIKI")[1][0]["v"] == "build")
    lid, s1 = loop_open(c, "wire tier7 into router", project="APEXYX")
    t("loop open", s1 == "new")
    t("loop dedup", loop_open(c, "Wire tier7 into ROUTER", project="APEXYX")[1] == "exists")
    t("loops listed", len(loops(c, project="APEXYX")) == 1)
    t("touch", loop_touch(c, lid, "poked") == 1)
    t("close", loop_close(c, lid) == 1)
    t("closed hidden", len(loops(c, project="APEXYX")) == 0)
    t("reopen", loop_open(c, "wire tier7 into router", project="APEXYX")[1] == "reopened")
    b = brief(c)
    t("brief has entity", "ARAIKI" in b and "status=build" in b)
    t("brief has loop", "open loops" in b)
    t("brief truncates", len(brief(c, max_chars=80)) <= 100)
    t("brief json", json.loads(brief(c, as_json=True))["entities"][0]["name"])
    fs2, ls2 = extract("Pixel battery = 62%\nTODO: rotate the webhook token\nsome prose line here")
    t("extract fact", ("Pixel", "battery", "62%") in [(a, k, v) for a, k, v in fs2])
    t("extract loop", any("rotate the webhook token" in x for x in ls2))
    with open(RESULTS, "w") as fh:
        fh.write(json.dumps({"node": "n8b", "output": "Ollama model = deepseek-r1\nfollow-up: bump num_ctx"}) + "\n")
    nf, nl = ingest(c)
    t("ingest facts", nf >= 1)
    t("ingest loops", nl >= 1)
    t("ingest cursor", json.load(open(CURSOR))["results"] == 1)
    nf2, _ = ingest(c)
    t("ingest no replay", nf2 == 0)
    bb = sqlite3.connect(BUS)
    bb.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, sender TEXT, kind TEXT, text TEXT, ts REAL)")
    bb.execute("INSERT INTO messages(sender,kind,text,ts) VALUES('n15','note','Termux storage = 128GB',?)", (now(),))
    bb.commit(); bb.close()
    ingest(c)
    t("bus ingest", get_facts(c, "Termux")[1] and get_facts(c, "Termux")[1][0]["v"] == "128GB")
    t("push_bus", push_bus("state", "hello") is True)
    p = os.path.join(tmp, "imp.json")
    json.dump({"entities": [{"name": "APEXYX", "kind": "project", "facts": {"owner": "monty"}}],
               "loops": [{"title": "ship tier 8", "project": "APEXYX"}]}, open(p, "w"))
    t("import", import_json(c, p) == 2)
    t("import fact conf", get_facts(c, "APEXYX")[1][0]["conf"] == 0.8)
    t("forget key", forget(c, "APEXYX", "owner") == 1 and not get_facts(c, "APEXYX")[1])
    t("forget entity", forget(c, "APEXYX") == 1)
    t("stale loops none", stale_loops(c, days=3) == [])
    c.execute("UPDATE loop SET touched=?, opened=? WHERE id=?", (now() - 9e5, now() - 9e5, lid)); c.commit()
    t("stale loops found", len(stale_loops(c, days=3)) == 1)
    c.execute("UPDATE loop SET due=? WHERE id=?", (now() - 100, lid)); c.commit()
    t("overdue", len(overdue_loops(c)) == 1)
    a, bq, d = gc(c, days=0)
    t("gc prunes", a >= 1 and d >= 1)
    t("gc keeps open loops", len(loops(c)) >= 1)
    t("project scope", "ARAIKI" not in brief(c, project="APEXYX"))
    c.close(); shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


# ------------------------------------------------------------------ cli -----
def daemon(interval=300, stale_days=3):
    while True:
        c = db()
        try:
            nf, nl = ingest(c)
            st, od = stale_loops(c, stale_days), overdue_loops(c)
            if st or od:
                msg = "state: %d overdue, %d stale loops" % (len(od), len(st))
                push_bus("digest", msg + "\n" + "\n".join(f"#{l['id']} {l['title']}" for l in (od + st)[:5]))
                os.system("command -v termux-notification >/dev/null && "
                          "termux-notification -t 'mesh state' -c %s >/dev/null 2>&1" % json.dumps(msg))
            print(f"[{iso(now())}] ingest +{nf} facts +{nl} loops; {len(st)} stale")
        except Exception as e:
            print("daemon error:", e)
        finally:
            c.close()
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser(prog="state", add_help=True)
    ap.add_argument("cmd", nargs="*")
    ap.add_argument("--project"); ap.add_argument("--alias", action="append", default=[])
    ap.add_argument("--source", default="cli"); ap.add_argument("--node", default="local")
    ap.add_argument("--conf", type=float, default=0.6); ap.add_argument("--note", default="")
    ap.add_argument("--due"); ap.add_argument("--max-chars", type=int, default=1200)
    ap.add_argument("--limit", type=int, default=200); ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--all", action="store_true"); ap.add_argument("--json", action="store_true")
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--daemon", action="store_true"); ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        sys.exit(selftest())
    if a.daemon:
        daemon(a.interval); return

    c = db()
    cmd = a.cmd[0] if a.cmd else "brief"
    rest = a.cmd[1:]
    try:
        if cmd == "brief":
            print(brief(c, a.project, a.max_chars, a.json))
        elif cmd == "ent":
            kind, name = rest[0], " ".join(rest[1:])
            print("entity", upsert_entity(c, name, kind, a.project, a.alias))
        elif cmd == "set":
            name = rest[0]
            for pair in rest[1:]:
                k, _, v = pair.partition("=")
                fid, st = set_fact(c, name, k, v, a.source, a.node, a.conf, project=a.project)
                print(f"{name}.{k} -> {v} [{st}]")
        elif cmd == "get":
            e, fs = get_facts(c, " ".join(rest))
            if not e:
                print("unknown entity"); sys.exit(1)
            print(f"[{e['kind']}] {e['name']}" + (f" ({e['project']})" if e["project"] else ""))
            for f in fs:
                print(f"  {f['k']:<16} {f['v']:<30} conf={f['conf']:.2f} {f['source']}@{iso(f['ts'])}")
        elif cmd == "forget":
            print("retired", forget(c, rest[0], rest[1] if len(rest) > 1 else None))
        elif cmd == "loop":
            sub = rest[0]
            if sub == "open":
                print(loop_open(c, " ".join(rest[1:]), a.project, None, a.node, a.note))
            elif sub == "close":
                print("closed", loop_close(c, int(rest[1]), a.note))
            elif sub == "touch":
                print("touched", loop_touch(c, int(rest[1]), a.note))
        elif cmd == "loops":
            for l in loops(c, a.project, a.all):
                age = (now() - (l["touched"] or l["opened"])) / 3600
                print(f"#{l['id']:<4} {l['status']:<7} {l['title'][:60]:<62} {age:.1f}h  {l['project'] or ''}")
        elif cmd == "ingest":
            print("ingested facts=%d loops=%d" % ingest(c, a.limit))
        elif cmd == "import":
            print("imported", import_json(c, rest[0]))
        elif cmd == "gc":
            print("pruned facts=%d loops=%d journal=%d" % gc(c, a.days))
        else:
            ap.print_help()
    finally:
        c.close()


if __name__ == "__main__":
    main()
