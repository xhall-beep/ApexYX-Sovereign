#!/data/data/com.termux/files/usr/bin/bash
# bus_up.sh — install mesh tier 1 (the bus). Safe to re-run.
# Never touches any other tier's state.
set -u
BIN="$HOME/bin"; MESH_DIR="${MESH_DIR:-$HOME/.mesh}"
mkdir -p "$BIN" "$MESH_DIR"

SRC="$(cd "$(dirname "$0")" && pwd)"
install -m 755 "$SRC/mesh_bus.py" "$BIN/mesh_bus.py"

# the `mesh` command every other tier calls
cat > "$BIN/mesh" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
exec python3 "$BIN/mesh_bus.py" "\$@"
EOF
chmod +x "$BIN/mesh"
# Termux also has \$PREFIX/bin on PATH — link there if it exists and differs
if [ -n "${PREFIX:-}" ] && [ -d "$PREFIX/bin" ] && [ "$PREFIX/bin" != "$BIN" ]; then
  ln -sf "$BIN/mesh" "$PREFIX/bin/mesh"
fi
case ":$PATH:" in *":$BIN:"*) ;; *) echo "export PATH=\$HOME/bin:\$PATH" >> "$HOME/.bashrc";; esac

echo "== selftest =="
python3 "$BIN/mesh_bus.py" --selftest || { echo "selftest failed — not installing"; exit 1; }

# touch the live db into existence and prove the round-trip
mesh post --node bus --kind sys --text "bus online $(date +%s)" >/dev/null
mesh verify

cat <<'EOT'

Bus installed. The mesh has shared memory.
  mesh post --node me --kind inbox --text "..."        -> feed the router
  mesh task --node me --text "..." --for exec          -> address a worker
  mesh export | head -c 300                            -> what every node polls
  mesh tail                                            -> watch the traffic
  mesh verify                                          -> prove nobody edited history
EOT
