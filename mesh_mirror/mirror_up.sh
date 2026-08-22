#!/usr/bin/env bash
# tier 10 — mirror: install, key, first snapshot, boot daemon, aliases.
set -euo pipefail
MESH_DIR="${MESH_DIR:-$HOME/.mesh}"
BIN="$HOME/bin"; mkdir -p "$BIN" "$MESH_DIR"
SRC="$(cd "$(dirname "$0")" && pwd)"
install -m 755 "$SRC/mesh_mirror.py" "$BIN/mesh_mirror.py"

grep -q 'alias mirror=' "$HOME/.bashrc" 2>/dev/null || cat >> "$HOME/.bashrc" <<'A'
alias mirror='python3 ~/bin/mesh_mirror.py'
alias snap='python3 ~/bin/mesh_mirror.py snap'
alias doctor='python3 ~/bin/mesh_mirror.py doctor'
A

echo "== selftest =="
python3 "$BIN/mesh_mirror.py" --selftest | tail -1

echo "== key =="
python3 "$BIN/mesh_mirror.py" key init            # set MESH_MIRROR_PASSPHRASE first to derive it
echo ">> BACK UP $MESH_DIR/mirror.key OFF THE PHONE. No key, no restore."

echo "== first snapshot =="
python3 "$BIN/mesh_mirror.py" snap --tag genesis --note "tier 10 install"
python3 "$BIN/mesh_mirror.py" doctor || true

# hourly snapshot + push daemon; add sinks e.g. --sinks filedrop:/sdcard/Sync/mesh
if command -v termux-wake-lock >/dev/null 2>&1; then termux-wake-lock || true; fi
pkill -f "mesh_mirror.py --daemon" 2>/dev/null || true
nohup python3 "$BIN/mesh_mirror.py" --daemon --interval "${MIRROR_INTERVAL:-3600}" \
  ${MESH_MIRROR_SINKS:+--sinks $MESH_MIRROR_SINKS} \
  >> "$MESH_DIR/mirror.daemon.log" 2>&1 &
echo "daemon pid $!  (log: $MESH_DIR/mirror.daemon.log)"

# survive reboot alongside the other tiers
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/40-mirror.sh" <<B
#!/usr/bin/env sh
termux-wake-lock
python3 $BIN/mesh_mirror.py --daemon --interval ${MIRROR_INTERVAL:-3600} >> $MESH_DIR/mirror.daemon.log 2>&1 &
B
chmod +x "$HOME/.termux/boot/40-mirror.sh"
echo "tier 10 up. try:  mirror list | mirror doctor | mirror sync --to filedrop:/sdcard/Sync/mesh"
