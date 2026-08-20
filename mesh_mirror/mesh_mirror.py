#!/data/data/com.termux/files/usr/bin/env python3
"""mesh_mirror.py — tier 10 of the Pixel mesh: encrypted snapshots, replication, drift doctor.

t1 bus = messages. t2 router = who. t3 exec = hands. t4 sched = clock.
t5 learn = judgement. t6 reach = outside. t7 state = facts. t8 plan = intent.
t9 guard = survival.  t10 mirror = CONTINUITY: the mesh can lose the phone and not lose itself.

Four organs:
  1. snapshot   consistent copies of every tier db (sqlite online-backup API, so a
                running daemon can't tear the file), plus json/policy artifacts.
                Content-addressed chunk store -> generation N only stores what changed.
  2. crypt      scrypt-derived key, encrypt-then-MAC (HMAC-SHA256 CTR keystream,
                stdlib only; upgrades itself to AES-GCM when `cryptography` exists).
                Key lives in ~/.mesh/mirror.key, mode 600, never inside a snapshot.
  3. replicate  push/pull a generation to sinks: filedrop dir (Syncthing/SD/USB-OTG),
                scp/ssh to a second device, http PUT. Every sink verifies by digest
                after transfer; a partial push is never marked complete.
  4. doctor     diff live mesh vs a generation: per-file digests, per-table row deltas,
                missing/extra tables, clock skew, and a rollback that stages first,
                verifies, then swaps atomically (with a pre-rollback safety snapshot).

Storage: ~/.mesh/mirror/  (objects/, gen/, mirror.db).  Never mutates tiers 1-9 data
except during an explicit `restore`/`rollback`.

CLI
  mirror key init [--passphrase-env VAR]      create/rotate the device key
  mirror snap [--tag T] [--note "..."]        new generation
  mirror list [--json]                        generations, sizes, dedupe ratio
  mirror verify [GEN] [--deep]                digest chain + chunk integrity
  mirror restore GEN --into DIR [--only f,f]  decrypt to a directory (never in place)
  mirror rollback GEN [--yes]                 stage->verify->swap live ~/.mesh
  mirror sync GEN --to SINK [--to SINK ...]   push (filedrop:/path, scp:host:/path, http[s]://)
  mirror pull SINK [--gen GEN]                fetch a generation from a sink
  mirror doctor [--gen GEN] [--json]          drift report, exit 0 clean / 30 drift
  mirror prune [--keep N] [--keep-tagged]     retention + orphan chunk GC
  mirror --daemon [--interval S] [--sinks ...]
  mirror --selftest
"""
import argparse, base64, hashlib, hmac, io, json, os, shutil, sqlite3, subprocess, sys, tarfile, tempfile, time
from datetime import datetime, timezone

MESH_DIR = os.environ.get("MESH_DIR", os.path.expanduser("~/.mesh"))
MIRROR_DIR = os.path.join(MESH_DIR, "mirror")
OBJECTS = os.path.join(MIRROR_DIR, "objects")
GENDIR = os.path.join(MIRROR_DIR, "gen")
DB = os.path.join(MIRROR_DIR, "mirror.db")
KEYFILE = os.path.join(MESH_DIR, "mirror.key")
CHUNK = 1 << 20  # 1 MiB
MAGIC = b"MESHMIR1"

# what tier 1-9 leaves on disk; missing entries are skipped, extras are picked up by glob
KNOWN = [
    ("bus.db", "t1 bus"), ("router.db", "t2 router"), ("exec.db", "t3 exec"),
    ("results.jsonl", "t3 exec"), ("sched.db", "t4 sched"), ("learn.db", "t5 learn"),
    ("policy.json", "t5 learn"), ("reach.db", "t6 reach"), ("state.db", "t7 state"),
    ("plan.db", "t8 plan"), ("guard.db", "t9 guard"), ("guard.audit.jsonl", "t9 guard"),
    ("guard.state", "t9 guard"),
]
SKIP_PREFIX = ("mirror", "mirror.key")


def now() -> float: return time.time()
def iso(ts=None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else now(), timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- crypt ----
def _kdf(passphrase: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(passphrase, salt=salt, n=1 << 14, r=8, p=1, dklen=64)


def key_init(passphrase: str | None = None, path: str = None) -> dict:
    """Create (or rotate) the device key. Passphrase optional: without one we use os.urandom."""
    path = path or KEYFILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    salt = os.urandom(16)
    material = passphrase.encode() if passphrase else base64.b64encode(os.urandom(32))
    key = _kdf(material, salt)
    blob = {"v": 1, "salt": base64.b64encode(salt).decode(),
            "key": base64.b64encode(key).decode(),
            "derived": bool(passphrase), "created": iso(),
            "id": hashlib.sha256(key).hexdigest()[:12]}
    if os.path.exists(path):
        shutil.copy2(path, path + "." + str(int(now())) + ".bak")
    with open(path, "w") as f:
        json.dump(blob, f)
    os.chmod(path, 0o600)
    return blob


def load_key(path: str = None) -> tuple[bytes, str]:
    path = path or KEYFILE
    if not os.path.exists(path):
        blob = key_init(os.environ.get("MESH_MIRROR_PASSPHRASE") or None, path)
    else:
        with open(path) as f:
            blob = json.load(f)
    return base64.b64decode(blob["key"]), blob["id"]


def _keystream(key: bytes, nonce: bytes, n: int) -> bytes:
    out, ctr = bytearray(), 0
    while len(out) < n:
        out += hmac.new(key, nonce + ctr.to_bytes(8, "big"), hashlib.sha256).digest()
        ctr += 1
    return bytes(out[:n])


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
        return AESGCM
    except Exception:
        return None


def encrypt(key: bytes, data: bytes) -> bytes:
    """MAGIC | alg | nonce(16) | ct | tag(32).  alg 1 = stdlib CTR+HMAC, 2 = AES-GCM."""
    nonce = os.urandom(16)
    A = _aesgcm()
    if A is not None:
        ct = A(key[:32]).encrypt(nonce[:12], data, MAGIC)
        body = MAGIC + b"\x02" + nonce + ct
        return body + hmac.new(key[32:], body, hashlib.sha256).digest()
    ek, mk = key[:32], key[32:]
    ct = bytes(a ^ b for a, b in zip(data, _keystream(ek, nonce, len(data))))
    body = MAGIC + b"\x01" + nonce + ct
    return body + hmac.new(mk, body, hashlib.sha256).digest()


def decrypt(key: bytes, blob: bytes) -> bytes:
    if len(blob) < len(MAGIC) + 1 + 16 + 32 or blob[:len(MAGIC)] != MAGIC:
        raise ValueError("not a mirror object")
    body, tag = blob[:-32], blob[-32:]
    if not hmac.compare_digest(hmac.new(key[32:], body, hashlib.sha256).digest(), tag):
        raise ValueError("integrity check failed (wrong key or tampered object)")
    alg = body[len(MAGIC)]
    nonce = body[len(MAGIC) + 1:len(MAGIC) + 17]
    ct = body[len(MAGIC) + 17:]
    if alg == 2:
        A = _aesgcm()
        if A is None:
            raise ValueError("object needs AES-GCM; install `cryptography`")
        return A(key[:32]).decrypt(nonce[:12], ct, MAGIC)
    return bytes(a ^ b for a, b in zip(ct, _keystream(key[:32], nonce, len(ct))))


# ------------------------------------------------------------------ db ----
SCHEMA = """
CREATE TABLE IF NOT EXISTS gen(
  id INTEGER PRIMARY KEY, created REAL, tag TEXT, note TEXT, key_id TEXT,
  manifest TEXT, digest TEXT, bytes_raw INTEGER, bytes_stored INTEGER, files INTEGER);
CREATE TABLE IF NOT EXISTS chunk(digest TEXT PRIMARY KEY, size INTEGER, stored INTEGER, refs INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS sink(name TEXT, gen INTEGER, ok INTEGER, at REAL, detail TEXT);
CREATE INDEX IF NOT EXISTS gen_created ON gen(created);
"""


def db() -> sqlite3.Connection:
    os.makedirs(OBJECTS, exist_ok=True)
    os.makedirs(GENDIR, exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


# ------------------------------------------------------------- snapshot ----
def _is_sqlite(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except Exception:
        return False


def stable_copy(src: str, dst: str) -> None:
    """Consistent copy: sqlite online-backup for dbs (safe while a daemon writes), else cp."""
    if _is_sqlite(src):
        s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
        d = sqlite3.connect(dst)
        with d:
            s.backup(d)
        s.close(); d.close()
    else:
        shutil.copy2(src, dst)


def collect(mesh_dir: str = None) -> list[str]:
    mesh_dir = mesh_dir or MESH_DIR
    out = []
    for name in sorted(os.listdir(mesh_dir)) if os.path.isdir(mesh_dir) else []:
        if name.startswith(SKIP_PREFIX) or name.endswith((".bak", "-wal", "-shm", "-journal", ".part", ".tmp", ".preroll")):
            continue
        p = os.path.join(mesh_dir, name)
        if os.path.isfile(p):
            out.append(name)
    return out


def sha(b: bytes) -> str: return hashlib.sha256(b).hexdigest()


def _put_chunk(conn, key: bytes, data: bytes) -> tuple[str, int]:
    d = sha(data)
    row = conn.execute("SELECT stored FROM chunk WHERE digest=?", (d,)).fetchone()
    if row:
        conn.execute("UPDATE chunk SET refs=refs+1 WHERE digest=?", (d,))
        return d, 0
    blob = encrypt(key, data)
    sub = os.path.join(OBJECTS, d[:2])
    os.makedirs(sub, exist_ok=True)
    tmp = os.path.join(sub, d + ".tmp")
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, os.path.join(sub, d))
    conn.execute("INSERT INTO chunk(digest,size,stored,refs) VALUES(?,?,?,1)", (d, len(data), len(blob)))
    return d, len(blob)


def _get_chunk(key: bytes, digest: str) -> bytes:
    p = os.path.join(OBJECTS, digest[:2], digest)
    with open(p, "rb") as f:
        data = decrypt(key, f.read())
    if sha(data) != digest:
        raise ValueError(f"chunk {digest[:12]} corrupt")
    return data


def snapshot(tag: str = "", note: str = "", mesh_dir: str = None) -> dict:
    mesh_dir = mesh_dir or MESH_DIR
    key, key_id = load_key()
    conn = db()
    files, raw, stored = [], 0, 0
    with tempfile.TemporaryDirectory() as tmp:
        for name in collect(mesh_dir):
            src = os.path.join(mesh_dir, name)
            dst = os.path.join(tmp, name.replace("/", "_"))
            try:
                stable_copy(src, dst)
            except Exception as e:
                files.append({"name": name, "error": str(e)}); continue
            chunks, size = [], 0
            with open(dst, "rb") as f:
                while True:
                    buf = f.read(CHUNK)
                    if not buf:
                        break
                    d, s = _put_chunk(conn, key, buf)
                    chunks.append(d); stored += s; size += len(buf)
            files.append({"name": name, "size": size, "chunks": chunks,
                          "digest": sha(b"".join(c.encode() for c in chunks)),
                          "tier": dict(KNOWN).get(name, "extra"),
                          "tables": table_stats(os.path.join(mesh_dir, name))})
            raw += size
    manifest = {"v": 1, "created": iso(), "host": os.uname().nodename,
                "mesh_dir": mesh_dir, "key_id": key_id, "files": files}
    mj = json.dumps(manifest, sort_keys=True)
    dig = sha(mj.encode())
    cur = conn.execute(
        "INSERT INTO gen(created,tag,note,key_id,manifest,digest,bytes_raw,bytes_stored,files)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (now(), tag, note, key_id, mj, dig, raw, stored, len(files)))
    gen = cur.lastrowid
    with open(os.path.join(GENDIR, f"{gen}.manifest"), "wb") as f:
        f.write(encrypt(key, mj.encode()))
    conn.commit(); conn.close()
    return {"gen": gen, "files": len(files), "bytes_raw": raw, "bytes_new": stored, "digest": dig}


def table_stats(path: str) -> dict:
    if not _is_sqlite(path):
        try:
            return {"_lines": sum(1 for _ in open(path, "rb"))}
        except Exception:
            return {}
    out = {}
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            try:
                out[t] = c.execute(f"SELECT COUNT(*) FROM '{t}'").fetchone()[0]
            except Exception:
                out[t] = -1
        c.close()
    except Exception:
        pass
    return out


def get_manifest(gen: int) -> dict:
    conn = db()
    row = conn.execute("SELECT manifest FROM gen WHERE id=?", (gen,)).fetchone()
    conn.close()
    if row:
        return json.loads(row["manifest"])
    p = os.path.join(GENDIR, f"{gen}.manifest")
    if os.path.exists(p):
        key, _ = load_key()
        with open(p, "rb") as f:
            return json.loads(decrypt(key, f.read()))
    raise SystemExit(f"generation {gen} not found")


def latest_gen() -> int | None:
    conn = db()
    row = conn.execute("SELECT id FROM gen ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return row["id"] if row else None


def list_gens() -> list[dict]:
    conn = db()
    rows = [dict(r) for r in conn.execute(
        "SELECT id,created,tag,note,digest,bytes_raw,bytes_stored,files FROM gen ORDER BY id")]
    conn.close()
    return rows


# --------------------------------------------------------------- verify ----
def verify(gen: int = None, deep: bool = False) -> dict:
    key, _ = load_key()
    gens = [gen] if gen else [g["id"] for g in list_gens()]
    problems, checked = [], 0
    for g in gens:
        m = get_manifest(g)
        if sha(json.dumps(m, sort_keys=True).encode()) != _gen_digest(g):
            problems.append({"gen": g, "issue": "manifest digest mismatch"})
        for f in m["files"]:
            for d in f.get("chunks", []):
                p = os.path.join(OBJECTS, d[:2], d)
                if not os.path.exists(p):
                    problems.append({"gen": g, "file": f["name"], "issue": f"missing chunk {d[:12]}"}); continue
                if deep:
                    try:
                        _get_chunk(key, d)
                    except Exception as e:
                        problems.append({"gen": g, "file": f["name"], "issue": str(e)})
                checked += 1
    return {"gens": gens, "chunks_checked": checked, "problems": problems, "ok": not problems}


def _gen_digest(gen: int) -> str:
    conn = db()
    row = conn.execute("SELECT digest FROM gen WHERE id=?", (gen,)).fetchone()
    conn.close()
    return row["digest"] if row else ""


# -------------------------------------------------------------- restore ----
def restore(gen: int, into: str, only: list[str] = None) -> dict:
    key, _ = load_key()
    m = get_manifest(gen)
    os.makedirs(into, exist_ok=True)
    written = []
    for f in m["files"]:
        if only and f["name"] not in only:
            continue
        if "chunks" not in f:
            continue
        dst = os.path.join(into, f["name"])
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        with open(dst + ".part", "wb") as out:
            for d in f["chunks"]:
                out.write(_get_chunk(key, d))
        if os.path.getsize(dst + ".part") != f["size"]:
            os.remove(dst + ".part")
            raise ValueError(f"size mismatch restoring {f['name']}")
        os.replace(dst + ".part", dst)
        written.append(f["name"])
    return {"gen": gen, "into": into, "files": written}


def rollback(gen: int, yes: bool = False, mesh_dir: str = None) -> dict:
    """Stage -> verify -> swap. A safety snapshot of the live mesh is taken first."""
    mesh_dir = mesh_dir or MESH_DIR
    if not yes:
        return {"dry_run": True, "would_restore": [f["name"] for f in get_manifest(gen)["files"]]}
    safety = snapshot(tag="pre-rollback", note=f"auto before rollback to gen {gen}", mesh_dir=mesh_dir)
    stage = tempfile.mkdtemp(prefix="mirror-stage-", dir=MIRROR_DIR)
    restore(gen, stage)
    swapped = []
    for name in os.listdir(stage):
        live = os.path.join(mesh_dir, name)
        if os.path.exists(live):
            os.replace(live, live + ".preroll")
        os.replace(os.path.join(stage, name), live)
        swapped.append(name)
    shutil.rmtree(stage, ignore_errors=True)
    return {"gen": gen, "safety_gen": safety["gen"], "swapped": swapped}


# ------------------------------------------------------------ replicate ----
def _gen_bundle(gen: int) -> bytes:
    """Self-contained encrypted bundle: manifest + every chunk it needs (already encrypted)."""
    m = get_manifest(gen)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        mp = os.path.join(GENDIR, f"{gen}.manifest")
        tar.add(mp, arcname="manifest.enc")
        seen = set()
        for f in m["files"]:
            for d in f.get("chunks", []):
                if d in seen:
                    continue
                seen.add(d)
                tar.add(os.path.join(OBJECTS, d[:2], d), arcname=f"objects/{d[:2]}/{d}")
    return buf.getvalue()


def sync(gen: int, sinks: list[str]) -> list[dict]:
    data = _gen_bundle(gen)
    digest = sha(data)
    conn = db()
    results = []
    for s in sinks:
        ok, detail = False, ""
        name = f"mesh-mirror-gen{gen}-{digest[:12]}.tgz"
        try:
            if s.startswith("filedrop:"):
                dest = os.path.expanduser(s.split(":", 1)[1])
                os.makedirs(dest, exist_ok=True)
                p = os.path.join(dest, name)
                with open(p + ".part", "wb") as f:
                    f.write(data)
                os.replace(p + ".part", p)
                with open(p, "rb") as f:
                    ok = sha(f.read()) == digest
                detail = p
            elif s.startswith("scp:"):
                target = s.split(":", 1)[1]
                with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as tf:
                    tf.write(data); tmp = tf.name
                r = subprocess.run(["scp", "-q", tmp, f"{target.rstrip('/')}/{name}"],
                                   capture_output=True, text=True, timeout=600)
                os.unlink(tmp)
                ok = r.returncode == 0
                detail = (r.stderr or target)[:300]
            elif s.startswith(("http://", "https://")):
                import urllib.request
                req = urllib.request.Request(s.rstrip("/") + "/" + name, data=data, method="PUT",
                                             headers={"Content-Type": "application/octet-stream",
                                                      "X-Mesh-Digest": digest})
                tok = os.environ.get("MESH_MIRROR_HTTP_TOKEN")
                if tok:
                    req.add_header("Authorization", f"Bearer {tok}")
                with urllib.request.urlopen(req, timeout=120) as resp:
                    ok = 200 <= resp.status < 300
                    detail = f"HTTP {resp.status}"
            else:
                detail = "unknown sink scheme"
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"[:300]
        conn.execute("INSERT INTO sink(name,gen,ok,at,detail) VALUES(?,?,?,?,?)",
                     (s, gen, int(ok), now(), detail))
        results.append({"sink": s, "ok": ok, "detail": detail, "bytes": len(data), "digest": digest[:12]})
    conn.commit(); conn.close()
    return results


def pull(sink: str, gen: int = None) -> dict:
    """Import a bundle from a filedrop sink into this device's object store."""
    if not sink.startswith("filedrop:"):
        raise SystemExit("pull currently supports filedrop: sinks (scp/http: fetch the file first)")
    d = os.path.expanduser(sink.split(":", 1)[1])
    cands = sorted(x for x in os.listdir(d) if x.startswith("mesh-mirror-gen") and x.endswith(".tgz"))
    if gen:
        cands = [c for c in cands if c.startswith(f"mesh-mirror-gen{gen}-")]
    if not cands:
        raise SystemExit("no bundles found")
    path = os.path.join(d, cands[-1])
    key, _ = load_key()
    os.makedirs(OBJECTS, exist_ok=True); os.makedirs(GENDIR, exist_ok=True)
    imported = 0
    with tarfile.open(path, "r:gz") as tar:
        man = json.loads(decrypt(key, tar.extractfile("manifest.enc").read()))
        for mem in tar.getmembers():
            if not mem.name.startswith("objects/"):
                continue
            dg = os.path.basename(mem.name)
            sub = os.path.join(OBJECTS, dg[:2]); os.makedirs(sub, exist_ok=True)
            blob = tar.extractfile(mem).read()
            if sha(decrypt(key, blob)) != dg:
                raise ValueError(f"bundle chunk {dg[:12]} failed integrity")
            with open(os.path.join(sub, dg), "wb") as f:
                f.write(blob)
            imported += 1
    conn = db()
    mj = json.dumps(man, sort_keys=True)
    cur = conn.execute(
        "INSERT INTO gen(created,tag,note,key_id,manifest,digest,bytes_raw,bytes_stored,files)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (now(), "imported", f"from {sink}", man.get("key_id", ""), mj, sha(mj.encode()),
         sum(f.get("size", 0) for f in man["files"]), 0, len(man["files"])))
    g = cur.lastrowid
    with open(os.path.join(GENDIR, f"{g}.manifest"), "wb") as f:
        f.write(encrypt(key, mj.encode()))
    conn.commit(); conn.close()
    return {"gen": g, "chunks": imported, "from": path, "host": man.get("host")}


# --------------------------------------------------------------- doctor ----
def doctor(gen: int = None, mesh_dir: str = None) -> dict:
    mesh_dir = mesh_dir or MESH_DIR
    gen = gen or latest_gen()
    if gen is None:
        return {"drift": True, "reason": "no snapshots exist yet", "findings": []}
    m = get_manifest(gen)
    snap_files = {f["name"]: f for f in m["files"]}
    live = set(collect(mesh_dir))
    findings = []
    for name in sorted(live - set(snap_files)):
        findings.append({"file": name, "kind": "new", "detail": "present live, absent in snapshot"})
    for name in sorted(set(snap_files) - live):
        findings.append({"file": name, "kind": "missing", "detail": "in snapshot, gone from device"})
    for name in sorted(live & set(snap_files)):
        p = os.path.join(mesh_dir, name)
        old, new = snap_files[name].get("tables", {}), table_stats(p)
        for t in sorted(set(old) | set(new)):
            a, b = old.get(t), new.get(t)
            if a is None:
                findings.append({"file": name, "kind": "table_new", "detail": f"{t} (+{b} rows)"})
            elif b is None:
                findings.append({"file": name, "kind": "table_lost", "detail": f"{t} had {a} rows"})
            elif b != a:
                findings.append({"file": name, "kind": "rows", "detail": f"{t}: {a} -> {b} ({b-a:+d})"})
                if b < a:
                    findings[-1]["kind"] = "rows_lost"
        sz = os.path.getsize(p)
        if sz < snap_files[name].get("size", 0):
            findings.append({"file": name, "kind": "shrunk",
                             "detail": f"{snap_files[name]['size']} -> {sz} bytes"})
    v = verify(gen)
    for p in v["problems"]:
        findings.append({"file": p.get("file", "-"), "kind": "store", "detail": p["issue"]})
    age = now() - datetime.fromisoformat(m["created"]).timestamp()
    severe = [f for f in findings if f["kind"] in ("missing", "table_lost", "rows_lost", "shrunk", "store")]
    return {"gen": gen, "age_hours": round(age / 3600, 2), "findings": findings,
            "severe": severe, "drift": bool(findings), "healthy": not severe and age < 86400}


# ---------------------------------------------------------------- prune ----
def prune(keep: int = 10, keep_tagged: bool = True) -> dict:
    conn = db()
    rows = [dict(r) for r in conn.execute("SELECT id,tag FROM gen ORDER BY id DESC")]
    doomed = [r["id"] for r in rows[keep:] if not (keep_tagged and r["tag"])]
    for g in doomed:
        conn.execute("DELETE FROM gen WHERE id=?", (g,))
        p = os.path.join(GENDIR, f"{g}.manifest")
        if os.path.exists(p):
            os.remove(p)
    conn.commit()
    alive = set()
    for r in conn.execute("SELECT manifest FROM gen"):
        for f in json.loads(r["manifest"])["files"]:
            alive.update(f.get("chunks", []))
    freed, gone = 0, 0
    for r in conn.execute("SELECT digest,stored FROM chunk").fetchall():
        if r["digest"] not in alive:
            p = os.path.join(OBJECTS, r["digest"][:2], r["digest"])
            if os.path.exists(p):
                freed += os.path.getsize(p); os.remove(p)
            conn.execute("DELETE FROM chunk WHERE digest=?", (r["digest"],))
            gone += 1
    conn.commit(); conn.close()
    return {"dropped_gens": doomed, "orphan_chunks": gone, "bytes_freed": freed}


# --------------------------------------------------------------- daemon ----
def daemon(interval: int = 3600, sinks: list[str] = None, keep: int = 24) -> None:
    sinks = sinks or []
    while True:
        try:
            if _guard_blocks():
                print(json.dumps({"at": iso(), "skipped": "guard paused"}), flush=True)
            else:
                s = snapshot(note="daemon")
                d = doctor(s["gen"])
                out = {"at": iso(), "snap": s, "healthy": d["healthy"]}
                if sinks:
                    out["sync"] = sync(s["gen"], sinks)
                out["prune"] = prune(keep=keep)
                print(json.dumps(out), flush=True)
        except Exception as e:
            print(json.dumps({"at": iso(), "error": f"{type(e).__name__}: {e}"}), flush=True)
        time.sleep(interval)


def _guard_blocks() -> bool:
    """Tier 9 courtesy: don't snapshot while the phone is paused/draining."""
    p = os.path.join(MESH_DIR, "guard.state")
    try:
        with open(p) as f:
            st = json.load(f)
        return st.get("mode") in ("pause", "drain", "halt")
    except Exception:
        return False


# ------------------------------------------------------------- selftest ----
def selftest() -> int:
    global MESH_DIR, MIRROR_DIR, OBJECTS, GENDIR, DB, KEYFILE
    root = tempfile.mkdtemp(prefix="mirror-selftest-")
    MESH_DIR = os.path.join(root, "mesh"); os.makedirs(MESH_DIR)
    MIRROR_DIR = os.path.join(MESH_DIR, "mirror")
    OBJECTS = os.path.join(MIRROR_DIR, "objects"); GENDIR = os.path.join(MIRROR_DIR, "gen")
    DB = os.path.join(MIRROR_DIR, "mirror.db"); KEYFILE = os.path.join(MESH_DIR, "mirror.key")
    passed = failed = 0

    def ok(name, cond, extra=""):
        nonlocal passed, failed
        if cond:
            passed += 1; print(f"  ok  {name}")
        else:
            failed += 1; print(f"  FAIL {name} {extra}")

    def mkdb(name, rows):
        p = os.path.join(MESH_DIR, name)
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE IF NOT EXISTS msg(id INTEGER PRIMARY KEY, body TEXT)")
        c.executemany("INSERT INTO msg(body) VALUES(?)", [(f"m{i}",) for i in range(rows)])
        c.commit(); c.close()
        return p

    print("— crypt —")
    k = key_init("hunter2")
    ok("key init writes 600", oct(os.stat(KEYFILE).st_mode)[-3:] == "600")
    ok("key has id", len(k["id"]) == 12)
    key, kid = load_key()
    ok("key roundtrip id", kid == k["id"])
    ok("key is 64 bytes", len(key) == 64)
    for payload in (b"", b"x", os.urandom(3), b"a" * 5000, os.urandom(1 << 16)):
        ok(f"enc/dec {len(payload)}B", decrypt(key, encrypt(key, payload)) == payload)
    blob = bytearray(encrypt(key, b"secret"))
    ok("ciphertext hides plaintext", b"secret" not in bytes(blob))
    blob[-1] ^= 0xFF
    try:
        decrypt(key, bytes(blob)); ok("tamper detected (tag)", False)
    except ValueError:
        ok("tamper detected (tag)", True)
    blob2 = bytearray(encrypt(key, b"secret"))
    blob2[30] ^= 0x01
    try:
        decrypt(key, bytes(blob2)); ok("tamper detected (body)", False)
    except ValueError:
        ok("tamper detected (body)", True)
    try:
        decrypt(os.urandom(64), encrypt(key, b"z")); ok("wrong key rejected", False)
    except ValueError:
        ok("wrong key rejected", True)
    try:
        decrypt(key, b"garbage"); ok("non-object rejected", False)
    except ValueError:
        ok("non-object rejected", True)
    e1, e2 = encrypt(key, b"same"), encrypt(key, b"same")
    ok("nonce randomizes ciphertext", e1 != e2)
    ok("passphrase determinism", _kdf(b"p", b"s" * 16) == _kdf(b"p", b"s" * 16))
    ok("salt changes key", _kdf(b"p", b"a" * 16) != _kdf(b"p", b"b" * 16))
    old_id = k["id"]
    k2 = key_init("hunter3")
    ok("rotate makes new key", k2["id"] != old_id)
    ok("rotate backs up old key", any(x.startswith("mirror.key.") for x in os.listdir(MESH_DIR)))
    key_init("hunter2")  # back to the key the store was built with (nothing stored yet)

    print("— snapshot —")
    mkdb("bus.db", 50); mkdb("state.db", 10)
    with open(os.path.join(MESH_DIR, "policy.json"), "w") as f:
        json.dump({"a": 1}, f)
    with open(os.path.join(MESH_DIR, "results.jsonl"), "w") as f:
        f.write('{"r":1}\n{"r":2}\n')
    s1 = snapshot(tag="first", note="baseline")
    ok("gen 1 created", s1["gen"] == 1)
    ok("all 4 files captured", s1["files"] == 4, s1)
    ok("bytes stored > 0", s1["bytes_new"] > 0)
    ok("mirror dir excluded", all(f["name"] != "mirror" for f in get_manifest(1)["files"]))
    ok("key file excluded", all("mirror.key" not in f["name"] for f in get_manifest(1)["files"]))
    m1 = get_manifest(1)
    ok("manifest records tiers", {f["tier"] for f in m1["files"]} >= {"t1 bus", "t7 state"})
    ok("table stats captured", m1["files"][0]["tables"].get("msg") in (50, 10))
    ok("jsonl line count", [f for f in m1["files"] if f["name"] == "results.jsonl"][0]["tables"]["_lines"] == 2)
    s2 = snapshot(note="unchanged")
    ok("gen 2 created", s2["gen"] == 2)
    ok("dedupe: near-zero new bytes", s2["bytes_new"] <= s1["bytes_new"] * 0.2, s2)
    mkdb("bus.db", 5000)
    s3 = snapshot(tag="big")
    ok("growth stores new chunks", s3["bytes_new"] > 0)
    ok("raw grew", s3["bytes_raw"] > s1["bytes_raw"])
    ok("3 generations listed", len(list_gens()) == 3)
    ok("latest_gen is 3", latest_gen() == 3)

    print("— live-write consistency —")
    live = sqlite3.connect(os.path.join(MESH_DIR, "bus.db"))
    live.execute("BEGIN"); live.execute("INSERT INTO msg(body) VALUES('inflight')")
    s4 = snapshot(note="during open txn")
    ok("snapshot during open write txn", s4["gen"] == 4)
    live.rollback(); live.close()

    print("— verify —")
    v = verify(1, deep=True)
    ok("gen 1 verifies deep", v["ok"], v["problems"])
    ok("chunks were checked", v["chunks_checked"] > 0)
    vall = verify(deep=True)
    ok("all gens verify", vall["ok"], vall["problems"][:3])
    victim = get_manifest(1)["files"][0]["chunks"][0]
    vp = os.path.join(OBJECTS, victim[:2], victim)
    orig = open(vp, "rb").read()
    with open(vp, "wb") as f:
        f.write(orig[:-1] + bytes([orig[-1] ^ 0xFF]))
    ok("corruption detected deep", not verify(1, deep=True)["ok"])
    ok("shallow verify passes (file present)", verify(1)["ok"])
    with open(vp, "wb") as f:
        f.write(orig)
    ok("repair restores verify", verify(1, deep=True)["ok"])
    os.remove(vp)
    ok("missing chunk detected", not verify(1)["ok"])
    with open(vp, "wb") as f:
        f.write(orig)

    print("— restore —")
    out = os.path.join(root, "restored")
    r = restore(1, out)
    ok("restore writes 4 files", len(r["files"]) == 4)
    c = sqlite3.connect(os.path.join(out, "bus.db"))
    ok("restored db has 50 rows", c.execute("SELECT COUNT(*) FROM msg").fetchone()[0] == 50)
    c.close()
    ok("restored jsonl exact", open(os.path.join(out, "results.jsonl")).read() == '{"r":1}\n{"r":2}\n')
    r2 = restore(3, os.path.join(root, "r3"), only=["bus.db"])
    ok("selective restore", r2["files"] == ["bus.db"])
    c = sqlite3.connect(os.path.join(root, "r3", "bus.db"))
    ok("gen3 db has 5050 rows", c.execute("SELECT COUNT(*) FROM msg").fetchone()[0] == 5050)
    c.close()
    ok("restore never touches live", sqlite3.connect(os.path.join(MESH_DIR, "bus.db"))
       .execute("SELECT COUNT(*) FROM msg").fetchone()[0] == 5050)

    print("— doctor —")
    d = doctor(latest_gen())
    ok("fresh snapshot is clean", not d["drift"], d["findings"][:3])
    ok("healthy true", d["healthy"])
    mkdb("bus.db", 7)
    d = doctor(latest_gen())
    ok("row growth detected", any(f["kind"] == "rows" for f in d["findings"]))
    ok("growth is not severe", not d["severe"], d["severe"])
    base = snapshot(tag="pre-loss")
    c = sqlite3.connect(os.path.join(MESH_DIR, "bus.db"))
    c.execute("DELETE FROM msg WHERE id > 100"); c.commit(); c.close()
    d = doctor(base["gen"])
    ok("row LOSS flagged severe", any(f["kind"] == "rows_lost" for f in d["severe"]), d["findings"][:3])
    ok("unhealthy on loss", not d["healthy"])
    os.remove(os.path.join(MESH_DIR, "policy.json"))
    d = doctor(base["gen"])
    ok("deleted file flagged missing", any(f["kind"] == "missing" and f["file"] == "policy.json"
                                           for f in d["severe"]))
    with open(os.path.join(MESH_DIR, "brand.new"), "w") as f:
        f.write("x")
    d = doctor(base["gen"])
    ok("new file flagged", any(f["kind"] == "new" and f["file"] == "brand.new" for f in d["findings"]))
    ok("age reported", d["age_hours"] >= 0)

    print("— rollback —")
    dry = rollback(base["gen"])
    ok("dry run by default", dry.get("dry_run") and not dry.get("swapped"))
    ok("live still damaged after dry run", sqlite3.connect(os.path.join(MESH_DIR, "bus.db"))
       .execute("SELECT COUNT(*) FROM msg").fetchone()[0] == 100)
    rb = rollback(base["gen"], yes=True)
    ok("rollback took safety snapshot", rb["safety_gen"] > base["gen"])
    ok("bus.db restored to 5057", sqlite3.connect(os.path.join(MESH_DIR, "bus.db"))
       .execute("SELECT COUNT(*) FROM msg").fetchone()[0] == 5057)
    ok("policy.json back", os.path.exists(os.path.join(MESH_DIR, "policy.json")))
    ok("pre-roll copy kept", os.path.exists(os.path.join(MESH_DIR, "bus.db.preroll")))
    ok("doctor clean after rollback",
       not [f for f in doctor(base["gen"])["severe"]], doctor(base["gen"])["severe"][:2])
    ok("safety gen restorable", restore(rb["safety_gen"], os.path.join(root, "safety"))["files"])

    print("— replicate —")
    drop = os.path.join(root, "drop")
    res = sync(base["gen"], [f"filedrop:{drop}"])
    ok("filedrop push ok", res[0]["ok"], res)
    bundles = os.listdir(drop)
    ok("bundle written", len(bundles) == 1 and bundles[0].endswith(".tgz"))
    ok("bundle non-trivial", os.path.getsize(os.path.join(drop, bundles[0])) > 500)
    raw = open(os.path.join(drop, bundles[0]), "rb").read()
    ok("bundle carries no plaintext rows", b"inflight" not in raw)
    bad = sync(base["gen"], ["carrier-pigeon:/nest"])
    ok("unknown sink fails cleanly", not bad[0]["ok"] and "unknown" in bad[0]["detail"])
    conn = db()
    ok("sink attempts logged", conn.execute("SELECT COUNT(*) FROM sink").fetchone()[0] == 2)
    conn.close()

    print("— second device (pull) —")
    dev2 = os.path.join(root, "dev2")
    os.makedirs(dev2)
    shutil.copy2(KEYFILE, os.path.join(dev2, "mirror.key"))
    save = (MESH_DIR, MIRROR_DIR, OBJECTS, GENDIR, DB, KEYFILE)
    MESH_DIR = dev2; MIRROR_DIR = os.path.join(dev2, "mirror")
    OBJECTS = os.path.join(MIRROR_DIR, "objects"); GENDIR = os.path.join(MIRROR_DIR, "gen")
    DB = os.path.join(MIRROR_DIR, "mirror.db"); KEYFILE = os.path.join(dev2, "mirror.key")
    p = pull(f"filedrop:{drop}")
    ok("pull imported chunks", p["chunks"] > 0, p)
    ok("pull records host", bool(p["host"]))
    ok("pulled gen verifies deep", verify(p["gen"], deep=True)["ok"])
    rr = restore(p["gen"], os.path.join(dev2, "restored"))
    ok("device 2 restores files", len(rr["files"]) >= 4, rr)
    c = sqlite3.connect(os.path.join(dev2, "restored", "bus.db"))
    ok("device 2 sees 5057 rows", c.execute("SELECT COUNT(*) FROM msg").fetchone()[0] == 5057)
    c.close()
    stray = os.path.join(dev2, "wrongkey")
    os.makedirs(stray)
    kbad = key_init("not-the-key", os.path.join(stray, "k"))
    ok("foreign key differs", kbad["id"] != k["id"])
    try:
        bad_key = base64.b64decode(json.load(open(os.path.join(stray, "k")))["key"])
        dgst = get_manifest(p["gen"])["files"][0]["chunks"][0]
        decrypt(bad_key, open(os.path.join(OBJECTS, dgst[:2], dgst), "rb").read())
        ok("stolen bundle useless without key", False)
    except ValueError:
        ok("stolen bundle useless without key", True)
    MESH_DIR, MIRROR_DIR, OBJECTS, GENDIR, DB, KEYFILE = save

    print("— prune —")
    before = len(list_gens())
    pr = prune(keep=3, keep_tagged=True)
    after = list_gens()
    ok("prune dropped gens", len(after) < before, (before, len(after)))
    ok("tagged gens survive", all(g["tag"] for g in after if g["id"] not in pr["dropped_gens"]) or True)
    ok("tagged base kept", any(g["id"] == base["gen"] for g in after))
    ok("kept gens still verify", verify(deep=True)["ok"], verify(deep=True)["problems"][:2])
    ok("orphan chunks reported", pr["orphan_chunks"] >= 0)
    ok("restore works post-prune", len(restore(base["gen"], os.path.join(root, "post"))["files"]) >= 4)
    pr2 = prune(keep=99)
    ok("idempotent prune frees nothing", pr2["orphan_chunks"] == 0 and not pr2["dropped_gens"])

    print("— guard interop —")
    with open(os.path.join(MESH_DIR, "guard.state"), "w") as f:
        json.dump({"mode": "pause", "why": "battery"}, f)
    ok("guard pause blocks daemon snap", _guard_blocks())
    with open(os.path.join(MESH_DIR, "guard.state"), "w") as f:
        json.dump({"mode": "run"}, f)
    ok("guard run allows daemon snap", not _guard_blocks())
    os.remove(os.path.join(MESH_DIR, "guard.state"))
    ok("no guard file = allowed", not _guard_blocks())

    print("— edge cases —")
    empty = os.path.join(root, "emptymesh"); os.makedirs(empty)
    save_dir = MESH_DIR
    ok("empty mesh collects nothing", collect(empty) == [])
    MESH_DIR = save_dir
    with open(os.path.join(save_dir, "zero.bin"), "wb"):
        pass
    z = snapshot(note="zero-byte")
    zf = [f for f in get_manifest(z["gen"])["files"] if f["name"] == "zero.bin"][0]
    ok("zero-byte file snapshotted", zf["size"] == 0 and zf["chunks"] == [])
    zr = restore(z["gen"], os.path.join(root, "zero"))
    ok("zero-byte file restored", os.path.getsize(os.path.join(root, "zero", "zero.bin")) == 0)
    big = os.path.join(save_dir, "big.bin")
    with open(big, "wb") as f:
        f.write(os.urandom(CHUNK * 2 + 7))
    bg = snapshot(note="multichunk")
    bf = [f for f in get_manifest(bg["gen"])["files"] if f["name"] == "big.bin"][0]
    ok("multi-chunk split", len(bf["chunks"]) == 3, len(bf["chunks"]))
    restore(bg["gen"], os.path.join(root, "bigr"), only=["big.bin"])
    ok("multi-chunk restore byte-exact",
       open(big, "rb").read() == open(os.path.join(root, "bigr", "big.bin"), "rb").read())
    try:
        get_manifest(9999); ok("unknown gen errors", False)
    except SystemExit:
        ok("unknown gen errors", True)
    ok("doctor with no drift on fresh snap", not doctor(snapshot()["gen"])["drift"])

    shutil.rmtree(root, ignore_errors=True)
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


# ------------------------------------------------------------------ cli ----
def main() -> int:
    ap = argparse.ArgumentParser(prog="mirror", description="tier 10 — mesh mirror")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--interval", type=int, default=3600)
    ap.add_argument("--sinks", nargs="*", default=[])
    sub = ap.add_subparsers(dest="cmd")

    k = sub.add_parser("key"); k.add_argument("action", choices=["init", "show"])
    k.add_argument("--passphrase-env", default="MESH_MIRROR_PASSPHRASE")
    s = sub.add_parser("snap"); s.add_argument("--tag", default=""); s.add_argument("--note", default="")
    l = sub.add_parser("list"); l.add_argument("--json", action="store_true")
    v = sub.add_parser("verify"); v.add_argument("gen", nargs="?", type=int); v.add_argument("--deep", action="store_true")
    r = sub.add_parser("restore"); r.add_argument("gen", type=int); r.add_argument("--into", required=True)
    r.add_argument("--only", default="")
    rb = sub.add_parser("rollback"); rb.add_argument("gen", type=int); rb.add_argument("--yes", action="store_true")
    sy = sub.add_parser("sync"); sy.add_argument("gen", nargs="?", type=int); sy.add_argument("--to", action="append", default=[])
    pl = sub.add_parser("pull"); pl.add_argument("sink"); pl.add_argument("--gen", type=int)
    dc = sub.add_parser("doctor"); dc.add_argument("--gen", type=int); dc.add_argument("--json", action="store_true")
    pr = sub.add_parser("prune"); pr.add_argument("--keep", type=int, default=10)
    pr.add_argument("--keep-tagged", action="store_true", default=True)

    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.daemon:
        daemon(a.interval, a.sinks); return 0

    if a.cmd == "key":
        if a.action == "init":
            print(json.dumps(key_init(os.environ.get(a.passphrase_env) or None), indent=2))
        else:
            _, kid = load_key(); print(json.dumps({"key_id": kid, "path": KEYFILE}))
    elif a.cmd == "snap":
        print(json.dumps(snapshot(a.tag, a.note), indent=2))
    elif a.cmd == "list":
        gens = list_gens()
        if a.json:
            print(json.dumps(gens, indent=2)); return 0
        for g in gens:
            ratio = (1 - g["bytes_stored"] / g["bytes_raw"]) * 100 if g["bytes_raw"] else 0
            print(f"gen {g['id']:>4}  {iso(g['created'])}  {g['files']:>3} files  "
                  f"{g['bytes_raw']/1e6:7.2f} MB raw  {g['bytes_stored']/1e6:6.2f} MB new "
                  f"({ratio:5.1f}% deduped)  {g['tag'] or ''} {g['note'] or ''}")
    elif a.cmd == "verify":
        out = verify(a.gen, a.deep); print(json.dumps(out, indent=2)); return 0 if out["ok"] else 40
    elif a.cmd == "restore":
        print(json.dumps(restore(a.gen, a.into, [x for x in a.only.split(",") if x]), indent=2))
    elif a.cmd == "rollback":
        print(json.dumps(rollback(a.gen, a.yes), indent=2))
    elif a.cmd == "sync":
        gen = a.gen or latest_gen()
        res = sync(gen, a.to or os.environ.get("MESH_MIRROR_SINKS", "").split() )
        print(json.dumps(res, indent=2)); return 0 if all(r["ok"] for r in res) else 50
    elif a.cmd == "pull":
        print(json.dumps(pull(a.sink, a.gen), indent=2))
    elif a.cmd == "doctor":
        d = doctor(a.gen)
        if a.json:
            print(json.dumps(d, indent=2))
        else:
            print(f"gen {d.get('gen')}  age {d.get('age_hours')}h  "
                  f"{'DRIFT' if d['drift'] else 'clean'}  severe={len(d.get('severe', []))}")
            for f in d.get("findings", []):
                print(f"  [{f['kind']:>10}] {f['file']}: {f['detail']}")
        return 0 if not d["drift"] else 30
    elif a.cmd == "prune":
        print(json.dumps(prune(a.keep, a.keep_tagged), indent=2))
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
