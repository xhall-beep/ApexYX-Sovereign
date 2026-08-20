#!/data/data/com.termux/files/usr/bin/bash
# guard_up.sh — install + start mesh tier 9 (guard: admission control, budgets, audit, kill switch)
# Safe to re-run. Never touches tier 1-8 state (bus/exec/sched/reach/state/plan dbs, policy.json).
set -u
BIN="$HOME/bin"; MESH_DIR="${MESH_DIR:-$HOME/.mesh}"
mkdir -p "$BIN" "$MESH_DIR" "$HOME/.termux/boot"

SRC="$(cd "$(dirname "$0")" && pwd)"
install -m 755 "$SRC/mesh_guard.py" "$BIN/mesh_guard.py"
ln -sf "$BIN/mesh_guard.py" "$BIN/guard"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "export PATH=\$HOME/bin:\$PATH" >> "$HOME/.bashrc";; esac

echo "== selftest =="
"$BIN/mesh_guard.py" --selftest || { echo "selftest failed — not starting"; exit 1; }

# --- safe: run anything under the guard ---------------------------------------
cat > "$BIN/safe" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
# safe [--kind model|shell|http|notify] [--node N] [--tokens T] -- <cmd ...>
exec $BIN/mesh_guard.py wrap "\$@"
EOF
chmod +x "$BIN/safe"

# --- tier 4 hooks --------------------------------------------------------------
if command -v sched >/dev/null 2>&1; then
  sched add "guard-sense" --every 60s --sh "$BIN/mesh_guard.py status >/dev/null" >/dev/null 2>&1 || true
  echo "registered tier-4 job: guard-sense every 60s"
fi

# --- boot hook -----------------------------------------------------------------
cat > "$HOME/.termux/boot/90-mesh-guard" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
nohup $BIN/mesh_guard.py --daemon --interval 60 >> $MESH_DIR/guard.log 2>&1 &
EOF
chmod +x "$HOME/.termux/boot/90-mesh-guard"

# --- start now -----------------------------------------------------------------
pkill -f "mesh_guard.py --daemon" 2>/dev/null || true
nohup "$BIN/mesh_guard.py" --daemon --interval 60 >> "$MESH_DIR/guard.log" 2>&1 &
sleep 1
echo
echo "tier 9 up. guard pid: $(pgrep -f 'mesh_guard.py --daemon' | head -1)"
echo "  guard status"
echo "  safe --kind model --node ollama --tokens 1200 -- ollama run qwen2.5:1.5b 'hi'"
echo "  guard budget set model=200000/day shell=500/hour"
echo "  guard policy set batt_min_model=45 temp_max_c=42"
echo "  guard audit --tail 30 ; guard verify"
echo "  guard pause --why 'on the road' ; guard resume ; guard drain --halt"
echo
echo "wire tiers 3/8 into it (one line each, optional):"
echo "  in mesh_exec handlers:  guard check \$kind --node \$node || defer"
echo "  or just prefix commands with: safe --kind shell --"
