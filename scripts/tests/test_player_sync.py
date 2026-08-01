"""The shared ReadAlong player files must not drift from their canonical home.

readalong.js / audioutil.js / chunker.js in batspeaker_web/ are byte-identical
synced copies of the reader's files (batcave-private repo). Until Bat-Speaker
gains a build step and imports a true shared package, sync_player.sh --check
is the drift alarm; this test wires it into the suite.

Skips when the canonical repo isn't checked out (CI, other machines).
"""

import os
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SYNC = HERE.parent / "batspeaker_web" / "sync_player.sh"
READER_SRC = Path(
    os.environ.get("READER_SRC", Path.home() / "projects" / "batcave-private" / "reader")
)


@pytest.mark.skipif(not READER_SRC.is_dir(), reason="canonical reader repo not present")
def test_player_copies_match_canonical():
    result = subprocess.run(
        [str(SYNC), "--check"], capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"shared player files drifted from {READER_SRC}:\n{result.stdout}{result.stderr}"
    )
