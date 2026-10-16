# batspeaker_web — Bat-Speaker's served web player

Raw ES modules served under `/web/` by `batspeaker-serve` — no build step.
Restart the service (or use the page's ↻) after editing served code; old
code keeps running otherwise.

## Shared player files (synced copies, not sources)

`readalong.js`, `audioutil.js`, `chunker.js`, and `sources.js` are **byte-identical synced
copies** of the canonical files in the batcave-private repo (`reader/`). One
player codebase, two packaging worlds: the reader bundles with esbuild, this
app serves raw modules, so until Bat-Speaker gains a build step the files are
mirrored rather than imported.

- **Edit the reader copy**, never these. Then run `./sync_player.sh` here.
- `./sync_player.sh --check` (and `tests/test_player_sync.py`) fail on drift.
- `batspeaker-player.js` is Bat-Speaker's own glue — its home is here.
- `worker.bundle.js` is the reader's **built** Kokoro synthesis worker, copied
  by the same script but **gitignored** here (derived, ~2.3 MB). It powers the
  on-device WebGPU voice; without it the page simply doesn't offer that option.
  After a reader rebuild, run `./sync_player.sh` again.

End-state (a real shared package + esbuild step for this app, which also
unlocks markdown-it in the reading pane) is written up in the vault:
"Reader × Bat-Speaker — Shared Player Consolidation (Pickup Prompt)".
