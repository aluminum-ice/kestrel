# Running the tests

The on-device `kestrel_pi` package (the plugin host, the web dashboard and
the learning agent) ships with a `pytest` suite. It is the fast feedback loop
for the application code — no image build, no hardware, no radio.

## `make test`

```bash
make test
# = cd builder/extra/files/kestrel_pi && python3 -m pytest -q
```

It runs against the package in the source tree, so you run it on the build host,
not inside the image.

**Dependencies.** The package declares no runtime dependencies of its own (the
image's `pi` venv provides them), so the tests need a few libraries present in
the environment you run them in:

- `flask` and `requests` — the web/daemon tests. Missing `flask` skips the web
  suite (`importorskip`).
- `numpy` — the agent `spec`/`metrics`/`readout` tests (a hard import for those).
- `pillow` — the e-ink renderer tests; `cryptography` — the dashboard's HTTPS
  certificate tests.
- `gymnasium` (light, pure Python; the image's pinned version) — the agent env,
  policy and training-reward tests (`test_agent_env`, `test_agent_policies`,
  `test_agent_train_reward`), which `importorskip` it.
- The heavy ML stack (`stable-baselines3`, `sb3-contrib`, and therefore
  `torch`) — only the policy tests that build a real model. These
  `importorskip` it, so on a host without it they **skip** rather than fail.
  A handful of expected skips is normal; it is not a broken run.

In a cloud Claude Code session, `.claude/hooks/session-start.sh` installs the
lint tools but not these; `pip install flask requests numpy pillow cryptography
"gymnasium==1.4.0"` (into the same interpreter) gets everything but the heavy ML
stack running, and installing that is only worth it when you are changing the
agent policies.

## What CI runs

The **Test** workflow (`.forgejo/workflows/test.yml`) runs `make test` on every
push and pull request, with the dependencies above except the heavy ML stack, so
the tests that need stable-baselines3 skip there. The **Lint** workflow runs
`make lint`, which only **byte-compiles** the package (`py_compile`). Run
`make test` yourself as well when you change anything under
`builder/extra/files/kestrel_pi/`, and the SB3 policy tests too when you change
the agent policies.

## Layout and coverage

Tests live in `builder/extra/files/kestrel_pi/tests/` as `test_*.py` and are
excluded from the built package (`pyproject.toml` `exclude = ["tests*"]`), so
nothing test-only ships in the image. They cover, roughly:

- **Daemon / plugin framework** — `test_daemon`, `test_loader`, `test_dispatch`,
  `test_context`, `test_options`, `test_ui`, `test_reference_plugins`,
  `test_load_helper`, `test_load_library`, and `test_core_lean` (core holds no
  plugin-only code and never loads a plugin library).
- **Web + privileged helpers** — `test_web`, `test_plugins_apply`.
- **Capture + config tooling** — `test_capture`, `test_config_set`,
  `test_connectivity`.
- **Learning agent** — `test_agent_spec`, `test_agent_env`,
  `test_agent_policies`, `test_agent_metrics`, `test_agent_readout`,
  `test_agent_training`, `test_agent_train_reward`.
- **In-place updates** — `test_updates`, `test_update_apply`.

## Adding a test

Drop a `test_*.py` into `tests/`. Keep it pure stdlib plus the libraries above;
`importorskip` anything heavy (the ML stack, or `flask` if you somehow test
without it) so the suite still runs where that dependency is absent. Keep the
target at **Python 3.11** (the image's interpreter) — no 3.12+ syntax, the same
rule as the package itself.

**Plugins that use a shared plugin library.** Load the plugin from the
checkout (`builder/extra/files/plugins/`) as the existing plugin tests do;
`kestrel_pi.load_library` finds `_lib_<name>.py` beside it, so no setup is
needed. Test a library's own behaviour once, against the library module
(`load_library(<a plugin path>, "<name>", api=N)` returns it), and keep each
plugin's tests for what the plugin adds. To patch a library global (a clock,
an address lookup), patch it on the library module with `monkeypatch`, not on
the plugin module: the plugin calls through the library, and the library
module is shared by every plugin that loads it. See
[Shared plugin libraries](plugin-development.md#shared-plugin-libraries).

See also: [Architecture](architecture.md), [Tech stack](tech-stack.md),
[Plugin development](plugin-development.md).
