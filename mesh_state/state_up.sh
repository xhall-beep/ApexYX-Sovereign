#!/data/data/com.termux/files/usr/bin/bash
# state_up.sh — install + start mesh tier 7 (shared world-model)
# Safe to re-run. Never touches tier 1-6 state (bus.db, exec.db, sched.db, policy.json, reach.db).
set -u
BIN="$HOME/bin"; MESH_DIR="${MESH_DIR:-$HOME/.mesh}"
mkdir -p "$BIN" "$MESH_DIR" "$HOME/.termux/boot"

SRC="$(cd "$(dirname "$0")" && pwd)"
install -m 755 "$SRC/mesh_state.py" "$BIN/mesh_state.py"
ln -sf "$BIN/mesh_state.py" "$BIN/state"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "export PATH=\$HOME/bin:\$PATH" >> "$HOME/.bashrc";; esac

echo "== selftest =="
"$BIN/mesh_state.py" --selftest || { echo "selftest failed — not starting"; exit 1; }

# --- seed the world-model with what we already know --------------------------
if [ ! -f "$MESH_DIR/state.db" ] || [ "$("$BIN/mesh_state.py" loops --all | wc -l)" = "0" ]; then
  "$BIN/mesh_state.py" ent project ARAIKI  >/dev/null
  "$BIN/mesh_state.py" ent project APEXYX  >/dev/null
  "$BIN/mesh_state.py" ent device "Pixel 8 Pro" --alias pixel >/dev/null
  "$BIN/mesh_state.py" set "Pixel 8 Pro" host=termux --conf 0.9 >/dev/null
  "$BIN/mesh_state.py" set mesh tier=7 --conf 0.9 >/dev/null
  echo "seeded base entities"
fi

# --- state-ctx: prepend the brief to any prompt before it hits a model --------
cat > "$BIN/state-ctx" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
# state-ctx "<prompt>" [project] — echo prompt with the mesh world-model prepended.
#   ollama run deepseek-r1-abliterated:8b "\$(state-ctx 'what should I do next?' ARAIKI)"
B="\$($BIN/mesh_state.py brief \${2:+--project "\$2"} --max-chars 1200)"
printf '%s\n\n---\nUser: %s\n' "\$B" "\$1"
EOF
chmod +x "$BIN/state-ctx"

# --- boot hook: ingest daemon ------------------------------------------------
cat > "$HOME/.termux/boot/70-mesh-state" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
nohup $BIN/mesh_state.py --daemon --interval 300 >> $MESH_DIR/state.log 2>&1 &
EOF
chmod +x "$HOME/.termux/boot/70-mesh-state"

# --- tier 4 hooks: nightly gc + morning state digest -------------------------
if [ -x "$BIN/mesh_sched.py" ]; then
  if [ "$("$BIN/mesh_sched.py" --list 2>/dev/null | grep -c 'mesh_state.*gc')" = "0" ]; then
    "$BIN/mesh_sched.py" --add "daily 03:40" --text "shell:$BIN/mesh_state.py gc --days 30" --for local >/dev/null \
      && echo "scheduled nightly state gc"
  fi
  if [ "$("$BIN/mesh_sched.py" --list 2>/dev/null | grep -c 'mesh_state.*brief')" = "0" ]; then
    "$BIN/mesh_sched.py" --add "daily 08:00" --text "shell:$BIN/mesh_state.py brief" --for digest >/dev/null \
      && echo "scheduled 08:00 state brief -> digest"
  fi
fi

# --- start now ---------------------------------------------------------------
pkill -f "mesh_state.py --daemon" 2>/dev/null
nohup "$BIN/mesh_state.py" --daemon --interval 300 >> "$MESH_DIR/state.log" 2>&1 &
sleep 1
echo
echo "tier 7 up. try:"
echo "  state brief"
echo "  state set ARAIKI status=building --conf 0.9"
echo "  state loop open \"wire state-ctx into router prompts\" --project ARAIKI"
echo "  state loops"
echo "  ollama run deepseek-r1-abliterated:8b \"\$(state-ctx 'what is my next move?' ARAIKI)\""
