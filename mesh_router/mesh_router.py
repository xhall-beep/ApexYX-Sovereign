#!/usr/bin/env python3
"""
mesh_router.py — the 4th node: a small always-on model that decides WHICH node
handles a request, then dispatches it onto the mesh bus.

Nodes: wingman_ally | wingman_core | maestro | local (answer here)

Usage
  ./mesh_router.py "summarize today's APEXYX notes"      # route one request
  ./mesh_router.py --explain "..."                       # show reasoning + scores
  ./mesh_router.py --daemon                              # watch bus for kind=inbox items
  ./mesh_router.py --dry-run "..."                       # classify, don't dispatch
  ./mesh_router.py --selftest                            # no model needed

Design for Pixel 8 Pro (12GB, Tensor G3):
  - router model is small (default qwen2.5:1.5b) so it stays resident next to
    your 8B without thermal-throttling.
  - if ollama/model is unavailable it falls back to a deterministic keyword
    scorer -> the mesh NEVER stops routing.
"""
import argparse, json, os, re, shutil, subprocess, sys, time

ROUTER_MODEL = os.environ.get("MESH_ROUTER_MODEL", "qwen2.5:1.5b")
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
NODES = ["wingman_ally", "wingman_core", "maestro", "local"]

CAPABILITIES = {
    "wingman_ally":  "outreach, chat with people, telegram replies, drafting messages, social/DM tasks, reminders",
    "wingman_core":  "long-running jobs, integrations, APIs, notion/asana sync, data pulls, scheduling, automation",
    "maestro":       "reasoning, planning, code generation, architecture, research, multi-step analysis (Hermes/8B)",
    "local":         "trivial lookups, math, time, unit conversion, echo - answer instantly on the router itself",
}

KEYWORDS = {
    "wingman_ally":  ["telegram", "message", "dm", "reply", "reach out", "text ", "ping", "remind", "notify", "post to", "chat"],
    "wingman_core":  ["notion", "asana", "api", "sync", "fetch", "cron", "schedule", "integration", "database", "export", "webhook", "backup"],
    "maestro":       ["plan", "design", "architecture", "code", "refactor", "debug", "research", "analyze", "strategy", "write a script", "why", "compare", "araiki", "apexyx"],
    "local":         ["what time", "convert", "calculate", "how many", "ping test", "status", "uptime"],
}

SYSTEM = (
    "You are the router node of a 4-node personal AI mesh. Choose exactly one node.\n"
    + "\n".join(f"- {k}: {v}" for k, v in CAPABILITIES.items())
    + "\nAnswer with JSON only: {\"node\":\"<node>\",\"confidence\":0-1,\"why\":\"<8 words>\"}"
)


# ---------- routing engines -------------------------------------------------
def keyword_route(text: str):
    t = text.lower()
    scores = {n: 0.0 for n in NODES}
    for node, words in KEYWORDS.items():
        for w in words:
            if w in t:
                scores[node] += 1.0
    if max(scores.values()) == 0:
        scores["maestro"] = 0.5  # default: think about it
    best = max(scores, key=scores.get)
    total = sum(scores.values()) or 1.0
    return {"node": best, "confidence": round(scores[best] / total, 2),
            "why": "keyword fallback", "engine": "keyword", "scores": scores}


def model_route(text: str, timeout: int = 25):
    try:
        import urllib.request
        body = json.dumps({
            "model": ROUTER_MODEL,
            "prompt": f"{SYSTEM}\n\nREQUEST: {text}\nJSON:",
            "stream": False,
            "options": {"temperature": 0, "num_ctx": 2048, "num_predict": 64},
        }).encode()
        req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = json.loads(r.read().decode()).get("response", "")
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        d = json.loads(m.group(0))
        if d.get("node") not in NODES:
            return None
        d.setdefault("confidence", 0.6)
        d.setdefault("why", "")
        d["engine"] = f"model:{ROUTER_MODEL}"
        return d
    except Exception:
        return None


def route(text: str, force_keyword: bool = False):
    d = None if force_keyword else model_route(text)
    if d is None:
        d = keyword_route(text)
    elif float(d.get("confidence", 0)) < 0.35:          # low trust -> blend
        kw = keyword_route(text)
        d["node"], d["engine"], d["why"] = kw["node"], d["engine"] + "+keyword", "low confidence, keyword override"
    return d


# ---------- mesh bus dispatch ----------------------------------------------
def dispatch(decision, text, dry_run=False):
    node = decision["node"]
    if node == "local" or dry_run:
        return {"dispatched": False, "reason": "local" if node == "local" else "dry-run"}
    if not shutil.which("mesh"):
        return {"dispatched": False, "reason": "mesh CLI not found (run mesh_up.sh)"}
    cmd = ["mesh", "task", "--node", "router", "--text", text, "--for", node]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return {"dispatched": p.returncode == 0, "stdout": p.stdout.strip(),
                "stderr": p.stderr.strip()}
    except Exception as e:
        return {"dispatched": False, "reason": str(e)}


def daemon(interval=15):
    """Watch the bus for unrouted inbox items and route them."""
    print(f"[router] daemon up, model={ROUTER_MODEL}, polling {interval}s", flush=True)
    seen = set()
    while True:
        try:
            p = subprocess.run(["mesh", "export"], capture_output=True, text=True, timeout=20)
            items = json.loads(p.stdout or "[]")
            if isinstance(items, dict):
                items = items.get("items", [])
            for it in items:
                iid = it.get("id")
                if iid in seen or it.get("kind") != "inbox":
                    continue
                seen.add(iid)
                text = it.get("text", "")
                d = route(text)
                r = dispatch(d, text)
                print(f"[router] #{iid} -> {d['node']} ({d['engine']}) {r}", flush=True)
        except Exception as e:
            print(f"[router] warn: {e}", flush=True)
        time.sleep(interval)


# ---------- selftest --------------------------------------------------------
CASES = [
    ("send Marcus a telegram message about the demo", "wingman_ally"),
    ("sync the ARAIKI notion database every morning", "wingman_core"),
    ("design the architecture for the APEXYX ingest pipeline", "maestro"),
    ("what time is it in Tokyo", "local"),
    ("debug why the webhook keeps failing", None),
]

def selftest():
    ok = 0
    for text, want in CASES:
        got = keyword_route(text)
        hit = (want is None) or (got["node"] == want)
        ok += hit
        print(f"{'PASS' if hit else 'FAIL'}  {text!r} -> {got['node']} (want {want}) conf={got['confidence']}")
    print(f"\n{ok}/{len(CASES)} keyword-engine cases OK (model engine untested offline)")
    return 0 if ok == len(CASES) else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("request", nargs="*")
    ap.add_argument("--explain", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keyword", action="store_true", help="skip the model")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--interval", type=int, default=15)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        sys.exit(selftest())
    if a.daemon:
        daemon(a.interval); return
    text = " ".join(a.request).strip()
    if not text:
        ap.error("give me a request to route (or --daemon / --selftest)")
    d = route(text, force_keyword=a.keyword)
    r = dispatch(d, text, dry_run=a.dry_run)
    out = {"request": text, **d, "dispatch": r}
    if a.explain:
        print(json.dumps(out, indent=2))
    else:
        print(f"{d['node']}  ({d['engine']}, conf {d['confidence']}) — {d.get('why','')}")
        if r.get("reason"):
            print(f"  note: {r['reason']}")


if __name__ == "__main__":
    main()
