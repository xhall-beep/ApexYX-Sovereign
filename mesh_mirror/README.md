# Tier 10 — mirror (continuity layer)

    bash mirror_up.sh                      # install, key, first snap, hourly daemon, boot hook
    mirror snap --tag before-refactor
    mirror list                            # generations + dedupe ratio
    mirror doctor                          # drift vs latest snapshot (exit 30 = drift)
    mirror verify --deep                   # decrypt+rehash every chunk
    mirror sync --to filedrop:/sdcard/Sync/mesh --to scp:pixel2:/sdcard/mesh
    mirror pull filedrop:/sdcard/Sync/mesh # on the 2nd device
    mirror restore 12 --into ~/tmp/r12     # safe, never in place
    mirror rollback 12 --yes               # safety snap -> stage -> verify -> atomic swap
    mirror prune --keep 24                 # tagged generations are never pruned

Key: `~/.mesh/mirror.key` (600). Copy it off-device once — bundles are useless without it.
`MESH_MIRROR_PASSPHRASE=... mirror key init` derives the key from a passphrase (scrypt) instead.
Daemon skips snapshots while tier 9 guard is paused/draining.
