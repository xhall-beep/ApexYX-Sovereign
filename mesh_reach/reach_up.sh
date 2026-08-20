#!/data/data/com.termux/files/usr/bin/bash
# reach_up.sh — install + start mesh tier 6 (inbox/outbox reach)
# Safe to re-run. Never touches tier 1-5 state (bus.db, exec.db, sched.db, policy.json).
set -u
BIN="$HOME/bin"; MESH_DIR="${MESH_DIR:-$HOME/.mesh}"
mkdir -p "$BIN" "$MESH_DIR/inbox" "$MESH_DIR/outbox" "$HOME/.termux/boot"

SRC="$(cd "$(dirname "$0")" && pwd)"
install -m 755 "$SRC/mesh_reach.py" "$BIN/mesh_reach.py"
ln -sf "$BIN/mesh_reach.py" "$BIN/reach"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "export PATH=\$HOME/bin:\$PATH" >> "$HOME/.bashrc";; esac

echo "== selftest =="
"$BIN/mesh_reach.py" --selftest || { echo "selftest failed — not starting"; exit 1; }

# --- config: create with a random webhook token on first run -----------------
if [ ! -f "$MESH_DIR/reach.json" ]; then
  "$BIN/mesh_reach.py" --config >/dev/null
  TOK="$(head -c 18 /dev/urandom | base64 | tr -d '/+=' )"
  python3 - "$MESH_DIR/reach.json" "$TOK" <<'PY'
import json,sys
p,tok=sys.argv[1],sys.argv[2]
c=json.load(open(p)); c["webhook"]["token"]=tok
json.dump(c,open(p,"w"),indent=2)
PY
  chmod 600 "$MESH_DIR/reach.json"
  echo "created $MESH_DIR/reach.json (webhook token generated)"
fi

# --- convenience: drop a task in from anywhere --------------------------------
cat > "$BIN/mesh-in" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
# mesh-in "<text>"  — push text into the mesh from outside (file-drop path)
printf '%s' "\$*" > "$MESH_DIR/inbox/cli-\$(date +%s%N).txt"
echo "dropped -> $MESH_DIR/inbox"
EOF
chmod +x "$BIN/mesh-in"

cat > "$BIN/mesh-out" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
# mesh-out "<text>" [target] — send a message out through the configured sink
$BIN/mesh_reach.py --send "\$1" --to "\${2:-}"
EOF
chmod +x "$BIN/mesh-out"

# --- boot hook ---------------------------------------------------------------
cat > "$HOME/.termux/boot/60-mesh-reach" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
nohup $BIN/mesh_reach.py --daemon --interval 20 >> $MESH_DIR/reach.log 2>&1 &
EOF
chmod +x "$HOME/.termux/boot/60-mesh-reach"

# --- hourly outbox flush via tier 4 scheduler (safety net) --------------------
if [ -x "$BIN/mesh_sched.py" ]; then
  if [ "$("$BIN/mesh_sched.py" --list 2>/dev/null | grep -c 'mesh_reach')" = "0" ]; then
    "$BIN/mesh_sched.py" --add "every 30m" --text "shell:$BIN/mesh_reach.py --once" --for local \
      && echo "scheduled outbox flush every 30m"
  fi
fi

pkill -f "mesh_reach.py --daemon" 2>/dev/null
nohup "$BIN/mesh_reach.py" --daemon --interval 20 >> "$MESH_DIR/reach.log" 2>&1 &
sleep 1
echo
echo "tier 6 up. reach daemon PID: $(pgrep -f 'mesh_reach.py --daemon' | tr '\n' ' ')"
echo "  reach --status                 inbox/outbox scoreboard + dead letters"
echo "  mesh-in \"check battery\"        outside -> bus task"
echo "  mesh-out \"done\"                mesh -> outside (file/telegram/http sink)"
echo "  reach --config                 show config path; add your Telegram token there"
echo "  curl -s -H \"X-Mesh-Token: \$(python3 -c 'import json;print(json.load(open(\"$MESH_DIR/reach.json\"))[\"webhook\"][\"token\"])')\" \\"
echo "       -d '{\"text\":\"hello mesh\"}' http://127.0.0.1:8770/"
