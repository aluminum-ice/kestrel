"""The ``hostrun`` plugin library: commands whose output another host decides
(backup, remote-train) keep at most a set number of bytes of it, and are
stopped past that. Real child processes, no network."""

import os
import subprocess
import sys
import time

import pytest

from kestrel_pi import load_library

PLUGIN_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "plugins"))
LIB = load_library(os.path.join(PLUGIN_DIR, "backup.py"), "hostrun", api=0)
PY = [sys.executable, "-I", "-c"]


def test_a_quiet_command_returns_its_output_and_status():
    res = LIB.run(PY + ["import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"],
                  timeout=20)
    assert res == LIB.Result(3, "out\n", "err\n", False)


def test_input_reaches_stdin():
    res = LIB.run(PY + ["import sys; print(sys.stdin.read().upper())"], input="hello",
                  timeout=20)
    assert res.stdout == "HELLO\n" and res.returncode == 0


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_a_flood_is_cut_at_the_limit_and_the_command_stopped(stream):
    start = time.monotonic()
    res = LIB.run(PY + ["import sys\nwhile True: sys.%s.write('x' * 65536)" % stream],
                  timeout=60, limit=100_000)
    assert res.truncated
    assert len(getattr(res, stream)) == 100_000
    assert res.returncode != 0                      # killed
    assert time.monotonic() - start < 30


def test_output_just_at_the_limit_is_kept_whole():
    res = LIB.run(PY + ["import sys; sys.stdout.write('y' * 1000)"], timeout=20, limit=1000)
    assert res.stdout == "y" * 1000 and not res.truncated and res.returncode == 0


def test_a_flood_from_a_grandchild_is_stopped_too():
    # rsync and scp start ssh; a child that keeps the pipe is killed with them
    code = ("import subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-I', '-c', "
            "'import sys\\nwhile True: sys.stderr.write(\"z\" * 65536)'])\n")
    start = time.monotonic()
    res = LIB.run(PY + [code], timeout=60, limit=50_000)
    assert res.truncated and len(res.stderr) == 50_000
    assert time.monotonic() - start < 30


def test_a_grandchild_left_holding_the_pipe_doesnt_hang_the_caller():
    code = ("import subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(120)'])\n"
            "print('done')\n")
    start = time.monotonic()
    res = LIB.run(PY + [code], timeout=60)
    assert res.stdout == "done\n" and res.returncode == 0
    assert time.monotonic() - start < 30


def test_a_timeout_kills_and_raises_like_subprocess():
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        LIB.run(PY + ["import time; time.sleep(120)"], timeout=1)
    assert time.monotonic() - start < 30
    assert LIB.TimeoutExpired is subprocess.TimeoutExpired


def test_a_missing_command_raises_file_not_found():
    with pytest.raises(FileNotFoundError):
        LIB.run(["/nonexistent/kestrel-no-such-tool"], timeout=5)


def test_undecodable_bytes_are_replaced():
    res = LIB.run(PY + ["import sys; sys.stdout.buffer.write(b'ok\\xff')"], timeout=20)
    assert res.stdout == "ok�"
