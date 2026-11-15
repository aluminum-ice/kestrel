"""The boot-partition model import (agent-provision.sh) writes the agent dir
only as pi.

agent-provision runs as root at boot, and the agent dir is pi's. The test runs
the real script off-device with a ``runuser`` stand-in on PATH that records
each call and runs the command as the test user, so it can tell what ran
"as pi" and what ran as the script itself."""

import io
import json
import os
import shutil
import subprocess
import tarfile

import pytest

FILES = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
SCRIPT = os.path.join(FILES, "agent-provision.sh")

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="runs the script as a normal user")


def _episodes_archive(path, names=("2026-01-01.jsonl",)):
    with tarfile.open(path, "w:gz") as tar:
        for name in names:
            data = b'{"window": 1}\n'
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def _setup(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copy(os.path.join(FILES, "common.sh"), lib / "common")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "runuser-calls"
    runuser = bin_dir / "runuser"
    # runuser -u pi -- CMD...: record CMD, then run it
    runuser.write_text('#!/bin/bash\n[ "$1" = -u ] && [ "$2" = pi ] && [ "$3" = -- ] || exit 99\n'
                       'shift 3\necho "$1" >> "%s"\nexec "$@"\n' % calls)
    runuser.chmod(0o755)
    config_set = bin_dir / "config-set"
    config_set.write_text('#!/bin/bash\necho "$*" >> "%s"\n' % (tmp_path / "config-set-calls"))
    config_set.chmod(0o755)
    boot = tmp_path / "boot"
    boot.mkdir()
    agent = tmp_path / "state" / "agent"
    agent.mkdir(parents=True)
    env = dict(os.environ,
               PATH="%s:%s" % (bin_dir, os.environ["PATH"]),
               PWN_LIB_DIR=str(lib), PWN_BOOT_DIR=str(boot),
               PWN_STATE_DIR=str(tmp_path / "state"), PWN_AGENT_DIR=str(agent),
               PWN_DEFAULTS=str(tmp_path / "no-defaults"),
               PWN_CONFIG=str(tmp_path / "config.toml"), PWN_CONFIG_SET=str(config_set))
    return boot, agent, calls, env


def _stage_model(boot, name="kestrel-model", algorithm="PPO"):
    (boot / (name + ".zip")).write_bytes(b"PK model bytes")
    (boot / (name + ".meta.json")).write_text(json.dumps({"algorithm": algorithm}))


def _run(env):
    return subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True)


def test_models_and_episodes_are_written_by_pi(tmp_path):
    boot, agent, calls, env = _setup(tmp_path)
    _stage_model(boot)
    _episodes_archive(boot / "kestrel-model-episodes.tar.gz")
    result = _run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (agent / "model-ppo.zip").read_bytes() == b"PK model bytes"
    assert json.loads((agent / "model-ppo.meta.json").read_text())["algorithm"] == "PPO"
    assert (agent / "episodes" / "2026-01-01.jsonl").is_file()
    assert not list(agent.glob("*.tmp.*"))
    # the copies, the folder and the extraction all ran through runuser -u pi
    ran_as_pi = calls.read_text().split()
    assert ran_as_pi.count("sh") == 2
    assert "mkdir" in ran_as_pi and "tar" in ran_as_pi
    # one-shot: the staged files are gone
    assert sorted(os.listdir(boot)) == []


def test_a_linked_episodes_folder_is_refused(tmp_path):
    boot, agent, calls, env = _setup(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (agent / "episodes").symlink_to(elsewhere)
    _stage_model(boot)
    _episodes_archive(boot / "kestrel-model-episodes.tar.gz")
    result = _run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "isn't a plain folder" in result.stdout
    assert os.listdir(elsewhere) == []
    assert "tar" not in calls.read_text().split()
    # the model still imports, and the archive stays staged
    assert (agent / "model-ppo.zip").is_file()
    assert sorted(os.listdir(boot)) == ["kestrel-model-episodes.tar.gz"]


def test_a_model_that_cant_be_copied_stays_staged(tmp_path):
    boot, agent, _calls, env = _setup(tmp_path)
    _stage_model(boot)
    agent.chmod(0o555)                          # nothing can be written there
    try:
        result = _run(env)
    finally:
        agent.chmod(0o755)
    assert "leaving it staged" in result.stdout
    assert (boot / "kestrel-model.zip").is_file()
    assert not (tmp_path / "config-set-calls").exists()


def test_the_script_writes_nothing_into_the_agent_dir_as_root():
    with open(SCRIPT, encoding="utf-8") as handle:
        code = [line.split("#", 1)[0] for line in handle]
    joined = "\n".join(code)
    assert "install -o pi" not in joined and "chown" not in joined
    for line in code:
        if "tar " in line and "-x" in line:
            assert "as_pi tar" in line, line
        if "mkdir" in line:
            assert "as_pi mkdir" in line, line
