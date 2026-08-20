#!/data/data/com.termux/files/usr/bin/bash
# exec_up.sh — install mesh tier 3 (executor loop) on Termux / Pixel 8 Pro
set -e
BIN="$PREFIX/bin"; [ -d "$BIN" ] || BIN="$HOME/bin"; mkdir -p "$BIN" "$HOME/.mesh"
cp mesh_exec.py "$HOME/.mesh/mesh_exec.py"; chmod +x "$HOME/.mesh/mesh_exec.py"
cat > "$BIN/exec" <<'E'
#!/data/data/com.termux/files/usr/bin/bash
exec python3 "$HOME/.mesh/mesh_exec.py" "$@"
E
chmod +x "$BIN/exec"

echo "[1/4] selftest"; python3 "$HOME/.mesh/mesh_exec.py" --selftest

echo "[2/4] worker launchers"
cat > "$HOME/.mesh/workers_up.sh" <<'W'
#!/data/data/com.termux/files/usr/bin/bash
# one worker per node; maestro polls slower (8B is heavy on Tensor G3)
pkill -f "mesh_exec.py --daemon" 2>/dev/null || true
nohup exec --daemon --node maestro      --interval 20 >> "$HOME/.mesh/exec_maestro.log" 2>&1 &
nohup exec --daemon --node wingman_core --interval 10 >> "$HOME/.mesh/exec_core.log"    2>&1 &
nohup exec --daemon --node wingman_ally --interval 10 >> "$HOME/.mesh/exec_ally.log"    2>&1 &
nohup exec --daemon --node local        --interval  8 >> "$HOME/.mesh/exec_local.log"   2>&1 &
sleep 1; pgrep -af "mesh_exec.py --daemon" || echo "workers failed to start"
W
chmod +x "$HOME/.mesh/workers_up.sh"

echo "[3/4] boot hook"
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/30-mesh-exec" <<'B'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
sleep 8
"$HOME/.mesh/workers_up.sh"
B
chmod +x "$HOME/.termux/boot/30-mesh-exec"

echo "[4/4] starting workers"; "$HOME/.mesh/workers_up.sh" || true
cat <<'M'

TIER 3 LIVE — the mesh now has hands.

  exec --status                       queue / attempts / dead letters
  exec --submit "shell:termux-battery-status" --for local
  exec --submit "draft the APEXYX v2 ingest plan" --for maestro
  exec --once --node maestro          drain one node manually
  exec --retry <id>                   resurrect a dead task
  tail -f ~/.mesh/exec_maestro.log    watch a worker
  tail -f ~/.mesh/results.jsonl       every result, forever

Task syntax: plain text -> that node's model; shell:<cmd> (allowlisted);
notify:<msg> -> phone notification; http:<url> -> webhook.
Router (tier 2) already posts kind=task items — workers pick them up automatically.
M
