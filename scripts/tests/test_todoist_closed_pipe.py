"""Tests for todoist.py's closed-pipe handling (_QuietOnClosedPipe).

A child process installs the wrapper, writes far more than a pipe buffer holds,
and the parent reads one line and closes its end, the way `| head -1` does.
The child must exit 0 with no traceback and still reach its last statement,
which is the property that keeps a piped bulk run from stopping halfway.
"""

import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent

CHILD = r"""
import sys
sys.path.insert(0, sys.argv[1])
import todoist
sys.stdout = todoist._QuietOnClosedPipe(sys.stdout)
for i in range(200_000):
    print(f"line {i}")
sys.stderr.write("REACHED_END\n")
"""


def _run_and_hang_up(child_src):
    proc = subprocess.Popen(
        [sys.executable, "-c", child_src, str(SCRIPTS)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    first = proc.stdout.readline()
    proc.stdout.close()
    stderr = proc.stderr.read().decode()
    proc.wait(timeout=30)
    return first, proc.returncode, stderr


def test_closed_pipe_exits_clean_and_runs_to_the_end():
    first, code, stderr = _run_and_hang_up(CHILD)
    assert first == b"line 0\n"
    assert code == 0, stderr
    assert "Traceback" not in stderr
    assert "BrokenPipeError" not in stderr
    assert "REACHED_END" in stderr


def test_without_the_wrapper_the_same_run_fails():
    # Guards the test itself: if this ever passes clean, the scenario above
    # no longer reproduces a broken pipe and proves nothing.
    bare = CHILD.replace("sys.stdout = todoist._QuietOnClosedPipe(sys.stdout)\n", "")
    _, code, stderr = _run_and_hang_up(bare)
    assert code != 0
    assert "BrokenPipeError" in stderr
