"""Shared plugin library: run a command that talks to another host, with its
output held to a size.

``backup`` and ``remote-train`` run ``ssh``, ``rsync`` and ``scp`` against a
host the operator set up, inside the always-on daemon. What those commands
print comes from that host, so this library keeps at most ``limit`` bytes of
each of stdout and stderr, and stops the command, with every process it
started, as soon as either goes past that. Use it instead of
``subprocess.run(..., capture_output=True)`` for any command whose output a
remote host decides. Load it with ``kestrel_pi.load_library`` (see
docs/developer/plugin-libraries.md)::

    from kestrel_pi import Plugin, load_library

    hostrun = load_library(__file__, "hostrun", api=0)

    class MyPlugin(Plugin):
        def _pull(self):
            try:
                res = hostrun.run(["rsync", "-tz", "host:file", "/tmp/file"],
                                  timeout=600)
            except (OSError, hostrun.TimeoutExpired) as exc:
                return str(exc)
            if res.truncated:
                return "the host sent too much output"
            return res.stderr if res.returncode else ""

``run`` raises what ``subprocess.run`` raises (``FileNotFoundError`` and other
``OSError`` when the command can't start, ``subprocess.TimeoutExpired`` after
``timeout``), so callers keep their ``except`` clauses.

Version: ``LIB_API = (0, 1)``, provisional: two plugins use it, and it may
still change in a minor release.
"""

import os
import signal
import subprocess
import threading
from collections import namedtuple

LIB_API = (0, 1)

# Ample for any status line, error message or rsync --stats block; far below
# what would hurt the daemon on a 512 MB board.
OUTPUT_LIMIT = 64 * 1024

TimeoutExpired = subprocess.TimeoutExpired

Result = namedtuple("Result", "returncode stdout stderr truncated")
Result.__doc__ = """A finished command: its exit status, up to the limit of each
stream (text, undecodable bytes replaced) and whether either went past it."""


def _kill(proc):
    """Stop the command and everything it started (its own session)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def run(cmd, input=None, timeout=None, limit=OUTPUT_LIMIT):
    """Run ``cmd`` (a list) and return a :data:`Result`.

    ``input`` (str) is written to its stdin, which is otherwise closed. stdout
    and stderr are read as they come; past ``limit`` bytes in either, the
    command and its children are killed and ``truncated`` is True. After
    ``timeout`` seconds they are killed too and ``TimeoutExpired`` is raised.
    """
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    kept = {"stdout": bytearray(), "stderr": bytearray()}
    over = threading.Event()

    def _read(name, stream):
        buf = kept[name]
        try:
            while True:
                chunk = stream.read1(65536)
                if not chunk:
                    return
                room = limit - len(buf)
                buf.extend(chunk[:max(0, room)])
                if len(chunk) > room:
                    over.set()
                    _kill(proc)
                    return
        except (OSError, ValueError):
            return

    def _write():
        try:
            proc.stdin.write(input.encode("utf-8"))
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    threads = [threading.Thread(target=_read, args=("stdout", proc.stdout), daemon=True),
               threading.Thread(target=_read, args=("stderr", proc.stderr), daemon=True)]
    if input is not None:
        threads.append(threading.Thread(target=_write, daemon=True))
    for thread in threads:
        thread.start()
    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(proc)
        proc.wait()
        _finish(proc, threads)
        raise subprocess.TimeoutExpired(cmd, timeout) from None
    # A grandchild may still hold a pipe open after the command itself exits:
    # then stop the rest of its session, so the readers end.
    for thread in threads:
        thread.join(timeout=1)
    if any(thread.is_alive() for thread in threads):
        _kill(proc)
    _finish(proc, threads)
    return Result(returncode,
                  bytes(kept["stdout"]).decode("utf-8", "replace"),
                  bytes(kept["stderr"]).decode("utf-8", "replace"),
                  over.is_set())


def _finish(proc, threads):
    for thread in threads:
        thread.join(timeout=5)
    for stream in (proc.stdout, proc.stderr):
        try:
            stream.close()
        except OSError:
            pass
