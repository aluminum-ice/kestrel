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


# -- stopping AngryOxide cleanly --------------------------------------------

def _stop_ao(tmp_path, child, seconds=1):
    """Run stop_ao against a background ``child`` shell snippet that records
    which signal reached it, and return (what it recorded, the launcher's log)."""
    marker = tmp_path / "got"
    script = ("log() { echo \"LOG: $*\"; }\n"
              + "ao_stop_seconds=%d\n" % seconds
              + _function("stop_ao")
              + "env --default-signal=INT bash -c %s &\nao_pid=$!\nsleep 0.3\nstop_ao\n"
              "wait \"$ao_pid\" 2>/dev/null\n"
              % repr(child.replace("MARKER", str(marker))))
    out = _bash(script, {})
    return (marker.read_text() if marker.exists() else ""), out.stdout


def test_stop_ao_asks_with_sigint_and_waits_for_a_clean_exit(tmp_path):
    # Like AngryOxide: shuts down (after a moment of cleanup) on SIGINT.
    got, log = _stop_ao(tmp_path, "trap 'sleep 0.3; echo INT > MARKER; exit 0' INT; "
                                  "trap 'echo TERM > MARKER; exit 0' TERM; "
                                  "while :; do sleep 0.1; done")
    assert got.strip() == "INT"
    assert "killing" not in log


def test_stop_ao_kills_a_process_that_ignores_sigint(tmp_path):
    got, log = _stop_ao(tmp_path, "trap '' INT; while :; do sleep 0.1; done", seconds=1)
    assert got == ""
    assert "didn't stop within 1s of SIGINT; killing it" in log


def test_angryoxide_is_never_sent_sigterm_and_the_unit_signals_only_the_launcher():
    with open(LAUNCH, encoding="utf-8") as handle:
        launcher = handle.read()
    assert 'kill -TERM "$ao_pid"' not in launcher
    # Started with SIGINT reset, since & leaves it ignored in a script.
    assert 'env --default-signal=INT "$ANGRYOXIDE"' in launcher
    assert launcher.count("stop_ao") >= 4          # roll, replan, stop, definition
    unit = os.path.join(FILES, "systemd", "kestrel-capture.service")
    with open(unit, encoding="utf-8") as handle:
        text = handle.read()
    assert "KillMode=mixed" in text and "TimeoutStopSec=" in text


# -- [agent.action] transmit_modes, enforced by the launcher too ------------

TRANSMIT_CASES = [
    (None, True), ('["active", "passive"]', True), ('["passive"]', False),
    ('[]', False), ('"passive"', False), ('"Active"', True), ('["Passive"]', False),
    ('[" ACTIVE "]', True), ('["pasive"]', False), ('1', False), ('true', False),
    ('["passive", 1]', False), ('[["active"]]', False), ('{ a = "active" }', False),
]


def _toml_value(raw):
    import tomllib
    return tomllib.loads("v = %s" % raw)["v"]


def _config(tmp_path, modes):
    cfg = tmp_path / "config.toml"
    text = '[agent]\nenabled = true\n'
    if modes is not None:
        text += '[agent.action]\ntransmit_modes = %s\n' % modes
    cfg.write_text(text)
    return cfg


@pytest.mark.parametrize("modes,expected", TRANSMIT_CASES)
def test_the_launcher_and_the_agent_agree_on_transmit_modes(tmp_path, modes, expected):
    from kestrel_pi.agent import explore
    cfg = _config(tmp_path, modes)
    out = _bash(_function("agent_may_transmit") + "agent_may_transmit\n",
                {"PWN_CONFIG": str(cfg)})
    assert (out.returncode == 0) is expected
    agent_cfg = {} if modes is None else {"transmit_modes": _toml_value(modes)}
    assert explore.may_transmit(agent_cfg) is expected


@pytest.mark.parametrize("text", ["", "not toml [", '[agent]\naction = 3\n', "agent = 1\n"])
def test_an_unreadable_config_means_passive(tmp_path, text):
    cfg = tmp_path / "config.toml"
    if text:
        cfg.write_text(text)
    out = _bash(_function("agent_may_transmit") + "agent_may_transmit\n",
                {"PWN_CONFIG": str(cfg)})
    assert out.returncode != 0


def _agent_args(tmp_path, modes, plan):
    override = tmp_path / "scan-override.json"
    override.write_text(plan)
    script = ("log() { echo \"LOG $*\"; }\n" + _function("agent_may_transmit")
              + _function("build_agent_args")
              + 'args=(); iface=wlan1; dwell_sec=5\nbuild_agent_args\nprintf "%s\\n" "${args[@]}"\n')
    out = _bash(script, {"PWN_CONFIG": str(_config(tmp_path, modes)),
                         "PWN_AGENT_OVERRIDE": str(override)})
    assert out.returncode == 0, out.stderr
    return [line for line in out.stdout.splitlines() if not line.startswith("LOG ")]


@pytest.mark.parametrize("modes,plan,passive", [
    (None, '{"kind": "autohunt", "transmit": "active"}', False),
    (None, '{"kind": "autohunt", "transmit": "passive"}', True),
    ('["passive"]', '{"kind": "autohunt", "transmit": "active"}', True),
    ('[]', '{"kind": "autohunt", "transmit": "active"}', True),
    ('"passive"', '{"kind": "autohunt"}', True),
    (None, '{"kind": "autohunt", "transmit": "ACTIVE"}', True),
    (None, '{"kind": "autohunt", "transmit": 1}', True),
])
def test_an_agent_window_transmits_only_when_the_plan_and_the_setting_allow(
        tmp_path, modes, plan, passive):
    args = _agent_args(tmp_path, modes, plan)
    assert "--autohunt" in args
    assert ("--notransmit" in args) is passive


def test_extra_args_never_repeat_notransmit():
    body = _function("build_args")
    assert "--notransmit" in body and 'continue' in body


@pytest.mark.parametrize("plan,want_args,want_dwell", [
    ('{"kind": "autohunt", "transmit": "passive", "dwell": 20}', ["--autohunt", "--notransmit"], "20"),
    ('{"kind": "band", "band": "5", "transmit": "passive", "dwell": 10}',
     ["--band", "5", "--notransmit"], "10"),
    ('{"kind": "band", "band": "2", "transmit": "active", "dwell": 2}', ["--band", "2"], "2"),
    ('{"kind": "channels", "channels": [1, 6], "transmit": "passive", "dwell": 5}',
     ["--channel", "1,6", "--notransmit"], "5"),
    ('{"kind": "band", "band": "5\\t\\n", "transmit": "active", "dwell": 10}', ["--band", "5"], "10"),
])
def test_every_field_of_the_plan_keeps_its_place(tmp_path, plan, want_args, want_dwell):
    override = tmp_path / "scan-override.json"
    override.write_text(plan)
    script = ("log() { :; }\ncapture_auto_channels() { return 1; }\n"
              + _function("agent_may_transmit") + _function("build_agent_args")
              + 'args=(); iface=wlan1; dwell_sec=5\nbuild_agent_args\n'
              'printf "%s\\n" "${args[@]}" "dwell=$dwell_sec"\n')
    out = _bash(script, {"PWN_CONFIG": str(_config(tmp_path, None)),
                         "PWN_AGENT_OVERRIDE": str(override)})
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == want_args + ["dwell=" + want_dwell]


def _stub(path, text):
    path.write_text("#!/bin/bash\n" + text)
    path.chmod(0o755)
    return str(path)


@pytest.mark.parametrize("version,checks", [
    ("AngryOxide 0.9.4", False),
    ("AngryOxide 0.9.5", True),
    ("AngryOxide 0.10.0", True),
    ("AngryOxide 1.0.0", True),
    ("AngryOxide v0.9.5", True),
    ("AngryOxide 0.9.5-rc1", False),
    ("AngryOxide", False),
    ("", False),
])
def test_rogue_m2_with_a_whitelist_needs_angryoxide_0_9_5(tmp_path, version, checks):
    binary = _stub(tmp_path / "angryoxide", 'echo "%s"\n' % version)
    script = (_function("ao_checks_whitelist_by_name")
              + "AO_WHITELIST_BY_NAME=0.9.5\n"
              + "ao_checks_whitelist_by_name && echo yes || echo no\n")
    out = _bash(script, {"ANGRYOXIDE": binary})
    assert out.stdout.strip() == ("yes" if checks else "no"), out.stderr


def test_a_missing_or_failing_angryoxide_counts_as_old(tmp_path):
    script = (_function("ao_checks_whitelist_by_name")
              + "AO_WHITELIST_BY_NAME=0.9.5\n"
              + "ao_checks_whitelist_by_name && echo yes || echo no\n")
    failing = _stub(tmp_path / "angryoxide", 'echo "AngryOxide 0.9.5"; exit 1\n')
    for binary in (str(tmp_path / "missing"), failing):
        assert _bash(script, {"ANGRYOXIDE": binary}).stdout.strip() == "no"


def _build_args(tmp_path, whitelist, by_name, extra=""):
    wl = tmp_path / "whitelist"
    wl.write_text(whitelist)
    config_get = _stub(tmp_path / "config-get", "printf '%s' \"$EXTRA\"\n")
    script = ("log() { :; }\ncfg() { printf '%s\\n' \"$2\"; }\n"
              "agent_active() { return 1; }\ncapture_auto_channels() { return 1; }\n"
              + _function("build_args")
              + "iface=wlan1; ao_base=run; ao_whitelist_by_name=%s\n" % by_name
              + 'build_args\nprintf "%s\\n" "${args[@]}"\n')
    out = _bash(script, {"PWN_WHITELIST_FILE": str(wl), "PWN_CONFIG_GET": config_get,
                         "PWN_CAPTURE_RUN_DIR": str(tmp_path), "EXTRA": extra})
    assert out.returncode == 0, out.stderr
    return out.stdout.splitlines()


@pytest.mark.parametrize("whitelist,by_name,off", [
    ("My Network\n", "no", True),
    ("00:00:5E:00:53:01\n", "no", True),
    ("My Network\n", "yes", False),
    ("", "no", False),
])
def test_rogue_m2_stays_off_with_a_whitelist_on_an_older_angryoxide(
        tmp_path, whitelist, by_name, off):
    args = _build_args(tmp_path, whitelist, by_name)
    assert ("--whitelist" in args) is bool(whitelist)
    assert ("--disable-roguem2" in args) is off


def test_extra_args_never_repeat_disable_roguem2(tmp_path):
    args = _build_args(tmp_path, "My Network\n", "no", "--disable-roguem2\n--notransmit\n")
    assert args.count("--disable-roguem2") == 1 and args.count("--notransmit") == 1
    args = _build_args(tmp_path, "My Network\n", "yes", "--disable-roguem2\n")
    assert args.count("--disable-roguem2") == 1
