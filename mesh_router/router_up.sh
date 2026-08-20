#!/data/data/com.termux/files/usr/bin/bash
# router_up.sh — install the router node on Termux (Pixel 8 Pro)
set -e
BIN="$PREFIX/bin"; MESHDIR="$HOME/.mesh"
mkdir -p "$MESHDIR"
cp mesh_router.py "$MESHDIR/mesh_router.py"; chmod +x "$MESHDIR/mesh_router.py"

cat > "$BIN/route" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
exec python3 "$MESHDIR/mesh_router.py" "\$@"
EOF
chmod +x "$BIN/route"

echo "[1/4] pulling the small router model (~1GB)…"
if command -v ollama >/dev/null; then
  (pgrep -f "ollama serve" >/dev/null || (nohup ollama serve >"$MESHDIR/ollama.log" 2>&1 & sleep 3))
  ollama pull "${MESH_ROUTER_MODEL:-qwen2.5:1.5b}" || echo "  ! pull failed — keyword engine will cover you"
else
  echo "  ! ollama not installed — keyword engine will cover you"
fi

echo "[2/4] bumping the 8B context 4096 -> 8192…"
if command -v ollama >/dev/null && ollama list 2>/dev/null | grep -q deepseek-r1-abliterated; then
  printf 'FROM deepseek-r1-abliterated:8b\nPARAMETER num_ctx 8192\n' > "$MESHDIR/Modelfile.8k"
  ollama create deepseek-r1-abliterated:8b-8k -f "$MESHDIR/Modelfile.8k" || true
fi

echo "[3/4] boot hook…"
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/20-router" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
sleep 5
nohup python3 $MESHDIR/mesh_router.py --daemon >> $MESHDIR/router.log 2>&1 &
EOF
chmod +x "$HOME/.termux/boot/20-router"

echo "[4/4] selftest…"
python3 "$MESHDIR/mesh_router.py" --selftest

cat <<'EOT'

Router node installed.
  route "send Marcus a telegram about the demo"     -> picks the node + dispatches to the bus
  route --explain --dry-run "..."                   -> see the scoring, dispatch nothing
  route --daemon &                                  -> auto-route everything posted as kind=inbox
  mesh post --node me --kind inbox --text "..."     -> feed the router from any node
EOT
