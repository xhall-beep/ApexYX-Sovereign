#!/data/data/com.termux/files/usr/bin/bash
# Install every free tier, then prove each one.
set -e
for d in mesh_*/; do
  n=${d%/}
  ( cd "$n" && bash "${n#mesh_}"_up.sh )
done
for d in mesh_*/; do
  n=${d%/}
  python3 "$n/$n.py" --selftest || { echo "FAIL $n"; exit 1; }
done
echo "all free tiers installed and self-tested"
