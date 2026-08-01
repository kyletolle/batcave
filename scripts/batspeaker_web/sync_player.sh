#!/usr/bin/env bash
#
# sync_player.sh — keep Bat-Speaker's shared-player files identical to the
# canonical reader copies.
#
# The ReadAlong player (readalong.js + audioutil.js + chunker.js) is one
# codebase living as byte-identical copies in two repos, because the two apps
# package JS differently (reader = esbuild bundle; Bat-Speaker = raw /web/
# modules, no build step). Until Bat-Speaker gains a build step and a true
# shared import, THIS script is the consolidation mechanism:
#
#   canonical:  <batcave-private repo>/reader/{readalong,audioutil,chunker}.js
#   synced:     this directory
#
# Edit the reader copy, then run this. test_player_sync.py fails the batcave
# suite if the copies drift.
#
# Usage:
#   sync_player.sh           copy canonical -> here (shows what changed)
#   sync_player.sh --check   no writes; exit 1 if any copy differs

set -euo pipefail

READER_SRC="${READER_SRC:-$HOME/projects/batcave-private/reader}"
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
FILES=(readalong.js audioutil.js chunker.js)

[[ -d "$READER_SRC" ]] || { echo "canonical source not found: $READER_SRC" >&2; exit 2; }

CHECK=0
[[ "${1:-}" == "--check" ]] && CHECK=1

drift=0
for f in "${FILES[@]}"; do
  if diff -q "$READER_SRC/$f" "$HERE/$f" > /dev/null 2>&1; then
    echo "ok      $f"
  elif [[ $CHECK -eq 1 ]]; then
    echo "DRIFT   $f"
    drift=1
  else
    cp "$READER_SRC/$f" "$HERE/$f"
    echo "synced  $f"
  fi
done

if [[ $drift -eq 1 ]]; then
  echo "copies differ from canonical — run sync_player.sh (no args) to sync" >&2
  exit 1
fi
