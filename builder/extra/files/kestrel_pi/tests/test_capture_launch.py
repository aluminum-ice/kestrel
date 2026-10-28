"""The capture launcher's relaunch helpers, run in bash against temp dirs.

``capture-launch.sh`` runs as root, so these cover the two pieces the agent's
plan file and the run dir reach: the content fingerprint that decides a
relaunch (it must not follow a symlink or block on a FIFO in the pi-owned agent
dir) and the raw-file cleanup that keeps the previous run's files across a
relaunch for a new plan."""

import os
import re
import subprocess

import pytest

FILES = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LAUNCH = os.path.join(FILES, "capture-launch.sh")


def _function(name):
    with open(LAUNCH, encoding="utf-8") as handle:
        text = handle.read()
    match = re.search(r"^%s\(\) \{\n.*?^\}\n" % re.escape(name), text, re.S | re.M)
    assert match, name
    return match.group(0)


def _bash(script, env):
    return subprocess.run(["bash", "-c", script], env=dict(os.environ, **env),
                          capture_output=True, text=True, timeout=20)


def _override_sum(path):
    out = _bash(_function("agent_override_sum") + "agent_override_sum\n",
                {"PWN_AGENT_OVERRIDE": str(path)})
    return out.stdout.strip()


def test_override_sum_follows_content_not_mtime(tmp_path):
    path = tmp_path / "scan-override.json"
    path.write_text('{"kind": "autohunt", "dwell": 2}')
    first = _override_sum(path)
    os.utime(path, (1, 1))
    assert _override_sum(path) == first and len(first) == 64
    path.write_text('{"kind": "autohunt", "dwell": 5}')
    assert _override_sum(path) != first


@pytest.mark.parametrize("kind", ["missing", "symlink", "fifo", "directory", "huge"])
def test_override_sum_refuses_anything_but_a_small_regular_file(tmp_path, kind):
    path = tmp_path / "scan-override.json"
    if kind == "symlink":
        target = tmp_path / "elsewhere"
        target.write_text("{}")
        os.symlink(target, path)
    elif kind == "fifo":
        os.mkfifo(path)            # must not block the launcher
    elif kind == "directory":
        path.mkdir()
    elif kind == "huge":
        path.write_text("x" * 70000)
    assert _override_sum(path) == "none"


def _clear(run_dir, keep=""):
    script = (_function("clear_raw_artifacts")
              + 'clear_raw_artifacts %s\n' % (('"%s"' % keep) if keep else ""))
    out = _bash(script, {"PWN_CAPTURE_RUN_DIR": str(run_dir)})
    assert out.returncode == 0, out.stderr
    return sorted(os.listdir(run_dir))


def _run_dir(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    for name in ("kestrel-100-000001.kismet", "kestrel-100-000001.pcapng",
                 "kestrel-200-000002.kismet", "kestrel-200-000002.pcapng",
                 "My Network_00-00-5E-00-53-01.hc22000"):
        (run / name).write_text("x")
    return run


def test_clear_keeps_only_the_named_launch_and_hashlines(tmp_path):
    assert _clear(_run_dir(tmp_path), keep="kestrel-200-000002") == [
        "My Network_00-00-5E-00-53-01.hc22000",
        "kestrel-200-000002.kismet", "kestrel-200-000002.pcapng"]


def test_clear_without_a_launch_keeps_only_hashlines(tmp_path):
    assert _clear(_run_dir(tmp_path)) == ["My Network_00-00-5E-00-53-01.hc22000"]


def test_a_relaunch_for_a_new_plan_keeps_the_previous_runs_files():
    with open(LAUNCH, encoding="utf-8") as handle:
        text = handle.read()
    assert 'if [ "$replan" = yes ] && [ -n "$prev_base" ]; then\n        clear_raw_artifacts "$prev_base"' in text
    assert '--output "$PWN_CAPTURE_RUN_DIR/$ao_base"' in text
    assert "agent_override_mtime" not in text
