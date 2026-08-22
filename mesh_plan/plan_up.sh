#!/usr/bin/env bash
# plan_up.sh — install + start mesh tier 8 (planner / orchestrator)
# Safe to re-run. Never touches tier 1-7 state (bus.db, exec.db, sched.db, policy.json, reach.db, state.db).
set -u
BIN="$HOME/bin"; MESH_DIR="${MESH_DIR:-$HOME/.mesh}"
mkdir -p "$BIN" "$MESH_DIR" "$HOME/.termux/boot"

SRC="$(cd "$(dirname "$0")" && pwd)"
install -m 755 "$SRC/mesh_plan.py" "$BIN/mesh_plan.py"
ln -sf "$BIN/mesh_plan.py" "$BIN/plan"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "export PATH=\$HOME/bin:\$PATH" >> "$HOME/.bashrc";; esac

echo "== selftest =="
"$BIN/mesh_plan.py" --selftest || { echo "selftest failed — not starting"; exit 1; }

# --- goal: one-liner that plans + immediately starts executing ---------------
cat > "$BIN/goal" <<EOF
#!/usr/bin/env bash
# goal "<objective>" [project] — decompose, dispatch, then watch progress.
set -e
$BIN/mesh_plan.py new "\$1" \${2:+--project "\$2"}
$BIN/mesh_plan.py tick
echo "-- watch with: plan ls | plan show <id> | plan graph <id>"
EOF
chmod +x "$BIN/goal"

# --- tier 4 hook: keep the orchestrator ticking even without the daemon ------
if command -v sched >/dev/null 2>&1; then
  sched add "plan-tick" --every 60s --sh "$BIN/mesh_plan.py tick" >/dev/null 2>&1 || true
  echo "registered tier-4 job: plan-tick every 60s"
fi

# --- boot hook ---------------------------------------------------------------
cat > "$HOME/.termux/boot/80-mesh-plan" <<EOF
#!/usr/bin/env sh
termux-wake-lock
nohup $BIN/mesh_plan.py --daemon --interval 30 >> $MESH_DIR/plan.log 2>&1 &
EOF
chmod +x "$HOME/.termux/boot/80-mesh-plan"

# --- start now ---------------------------------------------------------------
pkill -f "mesh_plan.py --daemon" 2>/dev/null || true
nohup "$BIN/mesh_plan.py" --daemon --interval 30 >> "$MESH_DIR/plan.log" 2>&1 &
sleep 1
echo
echo "tier 8 up. orchestrator pid: $(pgrep -f 'mesh_plan.py --daemon' | head -1)"
echo "  goal \"draft the ARAIKI launch checklist and save it\" ARAIKI"
echo "  plan ls ; plan show 1 ; plan graph 1 ; plan replan 1 --why \"wrong approach\""
echo "  tail -f $MESH_DIR/plan.log"
