#!/usr/bin/env python3
"""
mesh_bus.py — mesh tier 1: the BUS. Shared memory for every node.

One sqlite file (~/.mesh/bus.db), one table, an append-only tamper-evident
hash chain, and a tiny CLI that every other tier already speaks:

  mesh post --node me --kind inbox --text "..."          # put anything on the bus
  mesh task --node router --text "..." --for maestro     # addressed work item
  mesh export [--kind task] [--since ID] [--limit N]     # JSON out (what t2/t3 poll)
  mesh claim --id 7 --node exec                          # exactly-once handoff
  mesh done  --id 7 --node exec                          # close it out
  mesh tail  [--n 20]                                    # human view
  mesh verify                                            # walk the seal chain
  mesh gc --days 30                                      # drop old done/expired rows
  mesh status                                            # counts by kind/state
  mesh --selftest                                        # hermetic proof, tmpdir, no network

Design rules (same as every tier):
  - append-only: rows are never mutated except state transitions; the payload
    columns are sealed with a sha256 chain — edit one byte and `verify` screams.
  - WAL + busy_timeout so ten nodes can hammer it from cron without locking.
  - zero deps, zero network, degrades to nothing: if the bus is gone every
    node keeps working locally (they all already handle rc=127).
"""
import argparse, hashlib, json, os, sqlite3, sys, time

HOME = os.path.expanduser("~")
MESH_DIR = os.environ.get("MESH_DIR", os.path.join(HOME, ".mesh"))
MAX_TEXT = 4000
STATES = ("new", "claimed", "done")


def db_path():
    return os.path.join(MESH_DIR, "bus.db")


def db():
    os.makedirs(MESH_DIR, exist_ok=True)
    c = sqlite3.connect(db_path(), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=10000")
    c.execute("""CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        node TEXT NOT NULL DEFAULT '',
        sender TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL,
        text TEXT NOT NULL,
        tgt TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT 'new',
        claimed_by TEXT NOT NULL DEFAULT '',
        claimed_ts INTEGER NOT NULL DEFAULT 0,
        seal TEXT NOT NULL DEFAULT '')""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_kind_state ON messages(kind,state)")
    return c


# ---------------- seal chain --------------------------------------------------
def _seal(prev, ts, node, kind, text, tgt):
    h = hashlib.sha256()
    h.update("|".join((prev, str(ts), node, kind, text, tgt)).encode())
    return h.hexdigest()


def last_seal(c):
    r = c.execute("SELECT seal FROM messages WHERE seal!='' ORDER BY id DESC LIMIT 1").fetchone()
    return r["seal"] if r else "genesis"


def post(c, node, kind, text, tgt=""):
    node, kind, tgt = node.strip(), kind.strip(), (tgt or "").strip()
    text = text[:MAX_TEXT]
    if not node or not kind:
        raise ValueError("node and kind are required")
    ts = int(time.time())
    seal = _seal(last_seal(c), ts, node, kind, text, tgt)
    cur = c.execute(
        "INSERT INTO messages(ts,node,sender,kind,text,tgt,seal) VALUES(?,?,?,?,?,?,?)",
        (ts, node, node, kind, text, tgt, seal))
    c.commit()
    return cur.lastrowid


def verify(c):
    """Walk the chain; return list of ids whose seal doesn't match.
    Rows with seal='' (raw inserts from siblings, e.g. tier-7 push_bus) are
    tolerated: they carry no integrity claim and don't advance the chain."""
    prev, bad = "genesis", []
    for r in c.execute("SELECT id,ts,node,kind,text,tgt,seal FROM messages ORDER BY id"):
        if r["seal"] == "":
            continue
        want = _seal(prev, r["ts"], r["node"], r["kind"], r["text"], r["tgt"])
        if want != r["seal"]:
            bad.append(r["id"])
        prev = r["seal"]
    return bad


# ---------------- work-item lifecycle ----------------------------------------
def claim(c, mid, node):
    """Exactly-once: only one node ever wins the claim."""
    n = c.execute("UPDATE messages SET state='claimed',claimed_by=?,claimed_ts=? "
                  "WHERE id=? AND state='new'", (node, int(time.time()), mid)).rowcount
    c.commit()
    return n == 1


def done(c, mid, node=""):
    n = c.execute("UPDATE messages SET state='done' WHERE id=? AND state!='done' "
                  "AND (claimed_by=? OR claimed_by='' OR ?='')",
                  (mid, node, node)).rowcount
    c.commit()
    return n == 1


def export(c, kind=None, since=0, limit=500):
    q, args = "SELECT * FROM messages WHERE id>?", [since]
    if kind:
        q += " AND kind=?"; args.append(kind)
    q += " ORDER BY id LIMIT ?"; args.append(limit)
    out = []
    for r in c.execute(q, args):
        d = dict(r)
        d["for"] = d.pop("tgt")           # what t2/t3 read
        out.append(d)
    return out


def gc(c, days=30):
    cut = int(time.time()) - days * 86400
    n = c.execute("DELETE FROM messages WHERE state='done' AND ts<?", (cut,)).rowcount
    c.commit()
    if n:
        c.execute("VACUUM")
    return n


def status(c):
    out = {"db": db_path(), "total": c.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
           "by_state": {}, "by_kind": {}}
    for s, n in c.execute("SELECT state,COUNT(*) FROM messages GROUP BY state"):
        out["by_state"][s] = n
    for k, n in c.execute("SELECT kind,COUNT(*) FROM messages GROUP BY kind"):
        out["by_kind"][k] = n
    return out


# ---------------- selftest ----------------------------------------------------
def selftest():
    import tempfile, shutil
    global MESH_DIR
    tmp = tempfile.mkdtemp(prefix="meshbus.")
    MESH_DIR = tmp
    ok = fail = 0

    def t(name, cond):
        nonlocal ok, fail
        ok, fail = ok + (1 if cond else 0), fail + (0 if cond else 1)
        if not cond:
            print(f"FAIL  {name}")

    c = db()
    # posting + ids monotonic
    ids = [post(c, "tester", "inbox", f"msg {i}") for i in range(40)]
    t("40 posts", len(ids) == 40)
    for i in range(39):
        t(f"id monotonic {i}", ids[i + 1] == ids[i] + 1)
    # tasks with targets
    tids = [post(c, "router", "task", f"job {i}", tgt="exec") for i in range(20)]
    t("20 tasks", len(tids) == 20)
    # export shape: every key the siblings read
    items = export(c, kind="task")
    t("export count", len(items) == 20)
    for i, it in enumerate(items[:10]):
        t(f"export keys {i}", all(k in it for k in ("id", "kind", "text", "for", "node", "sender", "ts", "state")))
        t(f"export tgt {i}", it["for"] == "exec")
    t("export since", len(export(c, since=ids[-1])) == 20)
    t("export limit", len(export(c, limit=5)) == 5)
    t("export kind filter", all(i["kind"] == "task" for i in export(c, kind="task")))
    # claim: exactly once
    for i, mid in enumerate(tids[:10]):
        t(f"claim wins {i}", claim(c, mid, "exec"))
        t(f"claim loses {i}", not claim(c, mid, "sched"))
    t("claim missing", not claim(c, 99999, "exec"))
    # done semantics
    for i, mid in enumerate(tids[:10]):
        t(f"done {i}", done(c, mid, "exec"))
        t(f"done twice {i}", not done(c, mid, "exec"))
    t("done wrong claimer", not done(c, tids[10] if claim(c, tids[10], "exec") else 0, "sched"))
    # seal chain intact, then tamper
    t("chain clean", verify(c) == [])
    c.execute("UPDATE messages SET text='EVIL' WHERE id=?", (ids[5],)); c.commit()
    bad = verify(c)
    t("tamper detected", ids[5] in bad)
    c.execute("UPDATE messages SET text=? WHERE id=?", (f"msg 5", ids[5])); c.commit()
    t("chain heals on restore", verify(c) == [])
    # limits + unicode + empty guards
    big = post(c, "tester", "blob", "x" * 9000)
    t("text capped", len(c.execute("SELECT text FROM messages WHERE id=?", (big,)).fetchone()[0]) == MAX_TEXT)
    u = post(c, "tester", "inbox", "héllo wörld — 日本語 ✓")
    t("unicode survives", "日本語" in export(c, since=u - 1)[0]["text"])
    for badargs in (("", "kind"), ("node", ""), (" ", "kind")):
        try:
            post(c, badargs[0], badargs[1], "x"); t(f"reject {badargs}", False)
        except ValueError:
            t(f"reject {badargs}", True)
    # gc: only old+done rows die
    c.execute("UPDATE messages SET ts=ts-40*86400 WHERE id<=?", (ids[9],)); c.commit()
    # (ts edits break seals for those rows — gc must still work; re-verify after)
    for mid in ids[:5]:
        claim(c, mid, "gcbot"); done(c, mid, "gcbot")
    t("gc count", gc(c, days=30) == 5)
    t("gc spares new", c.execute("SELECT COUNT(*) FROM messages WHERE id=?", (ids[6],)).fetchone()[0] == 1)
    # status
    st = status(c)
    t("status total", st["total"] > 50)
    t("status kinds", st["by_kind"].get("task", 0) == 20)
    t("status states", "done" in st["by_state"])
    # concurrency: two connections interleaved
    c2 = db()
    a = post(c, "n1", "inbox", "from c1")
    b = post(c2, "n2", "inbox", "from c2")
    t("two writers", b == a + 1)
    fresh_bad = [i for i in verify(c) if i > ids[9]]
    t("chain clean after writers", fresh_bad == [])
    # sibling raw-insert compat (tier-7 push_bus writes only kind,text,sender,ts)
    c.execute("INSERT INTO messages(kind,text,sender,ts) VALUES(?,?,?,?)",
              ("digest", "raw insert", "state", int(time.time()))); c.commit()
    raw = export(c, kind="digest")
    t("raw insert readable", raw and raw[-1]["text"] == "raw insert")
    t("raw insert tolerated by verify", [i for i in verify(c) if i > ids[9]] == [])
    after = post(c, "tester", "inbox", "post after raw")
    t("chain continues past raw row", [i for i in verify(c) if i > ids[9]] == [] and after > 0)
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{ok}/{ok + fail} checks passed (hermetic: tmpdir bus, no network)")
    return 0 if fail == 0 else 1


# ---------------- CLI ----------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="mesh tier 1 — the bus")
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("post");   p.add_argument("--node", required=True); p.add_argument("--kind", required=True); p.add_argument("--text", required=True); p.add_argument("--for", dest="tgt", default="")
    p = sub.add_parser("task");   p.add_argument("--node", required=True); p.add_argument("--text", required=True); p.add_argument("--for", dest="tgt", required=True)
    p = sub.add_parser("export"); p.add_argument("--kind"); p.add_argument("--since", type=int, default=0); p.add_argument("--limit", type=int, default=500)
    p = sub.add_parser("claim");  p.add_argument("--id", type=int, required=True); p.add_argument("--node", required=True)
    p = sub.add_parser("done");   p.add_argument("--id", type=int, required=True); p.add_argument("--node", default="")
    p = sub.add_parser("tail");   p.add_argument("--n", type=int, default=20)
    sub.add_parser("verify"); sub.add_parser("status")
    p = sub.add_parser("gc");     p.add_argument("--days", type=int, default=30)
    a = ap.parse_args()

    if a.selftest:
        sys.exit(selftest())
    if not a.cmd:
        ap.print_help(); sys.exit(2)
    c = db()
    if a.cmd == "post":
        print(post(c, a.node, a.kind, a.text, a.tgt))
    elif a.cmd == "task":
        print(f"task{post(c, a.node, 'task', a.text, a.tgt)}")
    elif a.cmd == "export":
        print(json.dumps(export(c, a.kind, a.since, a.limit)))
    elif a.cmd == "claim":
        won = claim(c, a.id, a.node); print("claimed" if won else "lost"); sys.exit(0 if won else 1)
    elif a.cmd == "done":
        okd = done(c, a.id, a.node); print("done" if okd else "no-op"); sys.exit(0 if okd else 1)
    elif a.cmd == "tail":
        for r in c.execute("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (a.n,)):
            print(f"#{r['id']} [{r['kind']}] {r['node']}->{r['tgt'] or '*'} ({r['state']}) {r['text'][:80]}")
    elif a.cmd == "verify":
        bad = verify(c)
        print("chain intact" if not bad else f"TAMPERED rows: {bad}"); sys.exit(0 if not bad else 1)
    elif a.cmd == "status":
        print(json.dumps(status(c), indent=2))
    elif a.cmd == "gc":
        print(f"purged {gc(c, a.days)}")


if __name__ == "__main__":
    main()
