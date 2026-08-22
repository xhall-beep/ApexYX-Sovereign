#!/usr/bin/env bash
# sched_up.sh — install + start mesh tier 4 (scheduler / triggers / digest node)
# Safe to re-run. Does not touch tier 1/2/3 state.
set -u
BIN="$HOME/bin"; MESH_DIR="${MESH_DIR:-$HOME/.mesh}"
mkdir -p "$BIN" "$MESH_DIR" "$MESH_DIR/digests" "$HOME/.termux/boot"

SRC="$(cd "$(dirname "$0")" && pwd)"
install -m 755 "$SRC/mesh_sched.py" "$BIN/mesh_sched.py"
ln -sf "$BIN/mesh_sched.py" "$BIN/sched"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "export PATH=\$HOME/bin:\$PATH" >> "$HOME/.bashrc";; esac

echo "== selftest =="
"$BIN/mesh_sched.py" --selftest || { echo "selftest failed — not starting"; exit 1; }

# --- default jobs (idempotent: only added the first time) ---------------------
if [ "$("$BIN/mesh_sched.py" --list | grep -c 'digest')" = "0" ]; then
  "$BIN/mesh_sched.py" --add "daily 07:30" --digest --for maestro          # morning brief
  "$BIN/mesh_sched.py" --add "every 6h"    --text "shell:termux-battery-status" --for local
  "$BIN/mesh_sched.py" --trigger "battery:<20" --text "notify:mesh: battery low, pausing heavy jobs" --for local
  "$BIN/mesh_sched.py" --trigger "net:up"      --text "flush queued work" --for maestro
  echo "seeded default jobs"
fi

# --- boot hook ---------------------------------------------------------------
cat > "$HOME/.termux/boot/40-mesh-sched" <<EOF
#!/usr/bin/env sh
termux-wake-lock
$BIN/mesh_sched.py --boot
nohup $BIN/mesh_sched.py --daemon --interval 60 >> $MESH_DIR/sched.log 2>&1 &
EOF
chmod +x "$HOME/.termux/boot/40-mesh-sched"

# --- start now ---------------------------------------------------------------
pkill -f "mesh_sched.py --daemon" 2>/dev/null
nohup "$BIN/mesh_sched.py" --daemon --interval 60 >> "$MESH_DIR/sched.log" 2>&1 &
sleep 1
echo
"$BIN/mesh_sched.py" --list
echo
echo "tier 4 up. scheduler daemon PID: $(pgrep -f 'mesh_sched.py --daemon' | tr '\n' ' ')"
echo "  sched --list            jobs + next fire times"
echo "  sched --digest-now --model --notify"
echo "  tail -f $MESH_DIR/sched.log"
