#!/usr/bin/env bash
# learn_up.sh — install + start mesh tier 5 (feedback loop / learned routing)
# Safe to re-run. Never touches tier 1-4 state (bus.db, exec.db, sched.db).
set -u
BIN="$HOME/bin"; MESH_DIR="${MESH_DIR:-$HOME/.mesh}"
mkdir -p "$BIN" "$MESH_DIR" "$HOME/.termux/boot"

SRC="$(cd "$(dirname "$0")" && pwd)"
install -m 755 "$SRC/mesh_learn.py" "$BIN/mesh_learn.py"
ln -sf "$BIN/mesh_learn.py" "$BIN/learn"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "export PATH=\$HOME/bin:\$PATH" >> "$HOME/.bashrc";; esac

echo "== selftest =="
"$BIN/mesh_learn.py" --selftest || { echo "selftest failed — not starting"; exit 1; }

# --- smart route: evidence first, tier-2 router as fallback ------------------
cat > "$BIN/sroute" <<EOF
#!/usr/bin/env bash
# sroute "<task>"  — route using learned policy; fall back to keyword router.
set -u
TASK="\$*"
ADV="\$($BIN/mesh_learn.py --advise "\$TASK" 2>/dev/null)"
NODE="\$(printf '%s' "\$ADV" | sed -n 's/.*"node": "\([^"]*\)".*/\1/p' | head -1)"
CONF="\$(printf '%s' "\$ADV" | sed -n 's/.*"confidence": \([0-9.]*\).*/\1/p' | head -1)"
if [ -n "\$NODE" ]; then
  echo "learned-route -> \$NODE (conf \$CONF)"
  $BIN/mesh_exec.py --submit "\$TASK" --for "\$NODE"
else
  echo "no policy yet -> keyword router"
  $BIN/mesh_router.py --route "\$TASK"
fi
EOF
chmod +x "$BIN/sroute"

# --- boot hook ---------------------------------------------------------------
cat > "$HOME/.termux/boot/50-mesh-learn" <<EOF
#!/usr/bin/env sh
termux-wake-lock
nohup $BIN/mesh_learn.py --daemon --interval 60 >> $MESH_DIR/learn.log 2>&1 &
EOF
chmod +x "$HOME/.termux/boot/50-mesh-learn"

# --- nightly judge pass + policy export via tier 4 ---------------------------
if command -v "$BIN/mesh_sched.py" >/dev/null 2>&1; then
  if [ "$("$BIN/mesh_sched.py" --list | grep -c 'mesh_learn')" = "0" ]; then
    "$BIN/mesh_sched.py" --add "daily 03:10" --text "shell:$BIN/mesh_learn.py --judge --limit 40" --for local
    echo "scheduled nightly judge pass (03:10)"
  fi
fi

pkill -f "mesh_learn.py --daemon" 2>/dev/null
nohup "$BIN/mesh_learn.py" --daemon --interval 60 >> "$MESH_DIR/learn.log" 2>&1 &
sleep 1
"$BIN/mesh_learn.py" --ingest
echo
echo "tier 5 up. learn daemon PID: $(pgrep -f 'mesh_learn.py --daemon' | tr '\n' ' ')"
echo "  learn                      scoreboard + current policy"
echo "  sroute \"plan my week\"      route by evidence, not keywords"
echo "  learn --rate <task_id> up  teach it (human feedback outranks the machine)"
echo "  learn --judge              small-model second opinion on borderline results"
echo "  cat $MESH_DIR/policy.json"
