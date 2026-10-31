#!/usr/bin/env bash
#
# pull-agent-diagnostics.sh - pull a read-only report on the learning agent's
# training and model promotion off a Kestrel device: which models trained where,
# every contest between a candidate and the active model and why it went the way
# it did, and how each model did while it drove capture
# (docs/developer/agent-interface.md, docs/developer/remote-training.md).
#
# Run it on your COMPUTER (not the device):
#
#   bash pull-agent-diagnostics.sh [--score] [--remote-dir DIR] <user>@<host> [ssh-port]
#
#   --score           also re-score the models on the device (loads torch in a
#                     niced process as your login user, only with >= 1.5 GB RAM
#                     free; about a minute on a Pi 5): the active model, the
#                     scripted baseline and any remote model, on the held-out
#                     rows the contest uses, and on random and time-ordered
#                     splits for comparison.
#   --remote-dir DIR  the remote training host's agent folder (where it writes
#                     model-<algorithm>.zip and its .meta.json, i.e.
#                     <train_path>/agent). Its models are sent to a private temp
#                     folder on the device, scored there (weights only) and
#                     deleted. Use it when no remote model is staged on the device.
#
# It makes ONE SSH connection and reads, as your login user (no sudo):
#   - [agent] and [plugins.remote-train] settings (hosts, users and paths masked);
#   - the agent's status, contest tally and last decision, every model sidecar
#     (active, local candidate, staged remote), and the remote-train records
#     (the host's latest models, the remote models already judged);
#   - the episode log: counts, ages, rows per day, reward and handshakes per
#     policy, the actions each policy picked (numbers only; no row leaves the
#     device);
#   - the agent and daemon journals (all retained boots): trainings, launches,
#     stagings and contests with their reasons and scores;
#   - the yield of each model while it was active, rebuilt from that timeline.
# Read-only: it never starts, stops or restarts anything and never touches the
# management link. Before the report leaves the device, MAC and IP addresses, a
# non-default hostname, the login name, and the configured hosts, users and
# paths are masked. It saves one private text file,
# kestrel-agent-diagnostics-<time>.txt (mode 600), in OUT_DIR (default: the
# current folder; a folder inside a git checkout is refused). Env: SSH_KEY.

set -eu

score=0
remote_dir=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --score) score=1; shift ;;
    --remote-dir) [ "$#" -ge 2 ] || { echo "--remote-dir needs a folder" >&2; exit 2; }
                  remote_dir=$2; shift 2 ;;
    --remote-dir=*) remote_dir=${1#--remote-dir=}; shift ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) break ;;
  esac
done
if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: bash $0 [--score] [--remote-dir DIR] <user>@<host> [ssh-port]" >&2
  exit 2
fi
target=$1
port=${2:-22}
out_dir=${OUT_DIR:-.}
mkdir -p "$out_dir"
if git -C "$out_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "refusing: $out_dir is inside a git checkout; set OUT_DIR to a folder outside it" >&2
  exit 2
fi

# A remote host's models travel inside the one SSH session, base64-encoded.
remote_b64=""
if [ -n "$remote_dir" ]; then
  [ -d "$remote_dir" ] || { echo "no such folder: $remote_dir" >&2; exit 2; }
  files=$(cd "$remote_dir" && ls model-*.zip model-*.meta.json 2>/dev/null || true)
  [ -n "$files" ] || { echo "no model-*.zip / model-*.meta.json in $remote_dir" >&2; exit 2; }
  # shellcheck disable=SC2086
  remote_b64=$(cd "$remote_dir" && tar -czf - $files | base64 -w0)
  [ "${#remote_b64}" -lt 80000000 ] || { echo "the remote models are too big to send" >&2; exit 2; }
  echo "sending $(printf '%s\n' "$files" | wc -l) remote model file(s) for scoring"
  score=1
fi

ssh_opts=(-p "$port" -o ConnectTimeout=20)
[ -n "${SSH_KEY:-}" ] && ssh_opts+=(-i "$SSH_KEY")
stamp=$(date -u +%Y%m%dT%H%M%SZ)
report="$out_dir/kestrel-agent-diagnostics-$stamp.txt"
umask 077

if [ "$score" = 1 ]; then
  echo "Collecting from the device (read-only; a minute or two with scoring)..."
else
  echo "Collecting from the device (read-only)..."
fi
if ! { printf 'SCORE=%s\nREMOTE_B64=%s\n' "$score" "$remote_b64"
       cat <<'REMOTE'
set -u
export LC_ALL=C
AGENT_DIR=/var/lib/kestrel/agent
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
chmod 700 "$work"
if [ -n "$REMOTE_B64" ]; then
  mkdir -p "$work/remote"
  printf '%s' "$REMOTE_B64" | base64 -d | tar -xzf - -C "$work/remote" --no-same-owner --no-same-permissions
fi
REMOTE_B64=""
user=$(id -un)
venv_py=$(getent passwd "$user" | cut -d: -f6)/.venv/bin/python
[ -x "$venv_py" ] || venv_py=/home/pi/.venv/bin/python
nice -n 15 ionice -c 3 "$venv_py" -I - "$AGENT_DIR" "$work/remote" "$SCORE" <<'PY' 2>&1 | python3 -I -c '
import os, re, socket, sys, tomllib
t = sys.stdin.read()
try:
    cfg = tomllib.load(open("/etc/kestrel/config.toml", "rb"))
except Exception:
    cfg = {}
vals = set()
for table in ("remote-train", "backup"):
    for k, v in ((cfg.get("plugins") or {}).get(table) or {}).items():
        if isinstance(v, str) and len(v) >= 3 and any(w in k for w in ("host", "user", "path", "key")):
            vals.add(v)
for v in sorted(vals, key=len, reverse=True):
    t = t.replace(v, "<masked>")
t = re.sub(r"\b([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b", "<mac>", t)
t = re.sub(r"\b(\d{1,3}\.){3}\d{1,3}\b", "<ipv4>", t)
host = socket.gethostname()
if host and host != "kestrel":
    t = re.sub(r"(?<![\w./-])%s(?![\w./-])" % re.escape(host), "<hostname>", t)
user = os.environ.get("USER") or ""
if user and user != "pi":
    t = re.sub(r"\b%s\b" % re.escape(user), "<user>", t)
sys.stdout.write(t)
'
import collections, glob, json, os, re, statistics, subprocess, sys, time, tomllib
from datetime import datetime, timezone

AGENT_DIR, REMOTE_DIR, SCORE = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
NOW = time.time()
from kestrel_pi.agent import metrics as M, policies as P, spec as S
from kestrel_pi.agent import EPISODES_DIR, REMOTE_STAGING_DIR, PROMOTION_TALLY_NAME
import kestrel_pi.agent as A
CANDIDATES_DIR = getattr(A, "CANDIDATES_DIR", ".candidates")
EVALUATED_NAME = getattr(A, "EVALUATED_NAME", "evaluated.json")
HOST_MODELS_NAME = getattr(A, "HOST_MODELS_NAME", "host-models.json")
FAIR = hasattr(M, "contest_decision")          # a device with fair contests


def sec(title):
    print("\n===== %s =====" % title)


def ago(ts):
    try:
        s = NOW - float(ts)
    except (TypeError, ValueError):
        return "?"
    if s < 0:
        return "in the future"
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return "%.1f%s ago" % (s / n, unit)
    return "%ds ago" % s


def iso(ts):
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as h:
            return json.load(h)
    except FileNotFoundError:
        return None
    except Exception as exc:
        return {"_unreadable": type(exc).__name__}


print("Kestrel learning-agent diagnostics")
print("collected:", iso(NOW))
print("version:  ", (open("/etc/kestrel/version").read().strip()
                     if os.path.exists("/etc/kestrel/version") else "?"))
mem = {}
for line in open("/proc/meminfo"):
    k, v = line.split(":", 1)
    mem[k] = int(v.split()[0]) * 1024
print("memory:    %d MB total, %d MB available" % (mem.get("MemTotal", 0) >> 20,
                                                   mem.get("MemAvailable", 0) >> 20))

# ------------------------------------------------------------ settings
sec("settings")
try:
    cfg = tomllib.load(open("/etc/kestrel/config.toml", "rb"))
except Exception as exc:
    cfg = {}
    print("config.toml unreadable:", type(exc).__name__)
agent_cfg = cfg.get("agent") or {}
rt_cfg = (cfg.get("plugins") or {}).get("remote-train") or {}
print("[agent]", {k: v for k, v in agent_cfg.items() if not isinstance(v, dict)})
for sub in ("reward", "action"):
    if isinstance(agent_cfg.get(sub), dict):
        print("[agent.%s]" % sub, agent_cfg[sub])
print("[plugins.remote-train]", {k: ("<set>" if any(w in k for w in ("host", "user", "path", "key"))
                                    and v else v) for k, v in rt_cfg.items()})
algorithm = agent_cfg.get("algorithm") or "RecurrentPPO"
margin = float(agent_cfg.get("promotion_margin", 0.10) or 0.0)
max_stale = int(agent_cfg.get("promotion_max_stale_episodes", 900) or 0)
local_steps = int(agent_cfg.get("train_timesteps", 3000) or 3000)
remote_steps = int(rt_cfg.get("timesteps", 200000) or 200000)
print("effective: algorithm=%s promotion_margin=%.2f max_stale_episodes=%d "
      "on-device train_timesteps=%d remote timesteps=%d (%.0fx)"
      % (algorithm, margin, max_stale, local_steps, remote_steps,
         remote_steps / max(1, local_steps)))

# ------------------------------------------------------------ state
sec("agent state")
st = load_json(os.path.join(AGENT_DIR, "status.json")) or {}
for k in ("running", "policy", "algorithm", "model", "model_source", "train", "training",
          "can_train", "train_blocked", "last_train_secs", "promotion_tally", "decisions",
          "detail"):
    if k in st:
        print("%-16s %s" % (k, st[k]))
if st.get("last_trained"):
    print("%-16s %s" % ("last_trained", ago(st["last_trained"])))
if st.get("updated"):
    print("%-16s %s" % ("status updated", ago(st["updated"])))
print("promotion-tally.json:", load_json(os.path.join(AGENT_DIR, PROMOTION_TALLY_NAME)))
ld = st.get("last_decision")
if isinstance(ld, dict):
    print("last contest:", {k: (ago(v) if k == "at" else v) for k, v in ld.items()})
print("fair contests on this device:", "yes" if FAIR else "no (before 1.0.11-beta.2)")
lt = load_json(os.path.join(AGENT_DIR, "last-train.json"))
print("last-train.json:", {k: (ago(v) if k in ("at", "finished", "ts") else v)
                           for k, v in (lt or {}).items()} if isinstance(lt, dict) else lt)
if isinstance(st.get("metrics"), dict):
    print("status metrics:", json.dumps(st["metrics"], sort_keys=True)[:1500])

# ------------------------------------------------------------ episodes
rows = M.load_rows(os.path.join(AGENT_DIR, EPISODES_DIR))
rows.sort(key=lambda r: float(r.get("ts", 0) or 0))
sec("episode log")
files = sorted(glob.glob(os.path.join(AGENT_DIR, EPISODES_DIR, "*.jsonl")))
print("files: %d, %.1f MB" % (len(files), sum(os.path.getsize(f) for f in files) / 1e6))
with_obs = [r for r in rows if isinstance(r.get("obs"), list) and len(r["obs"]) == S.OBS_SIZE]
print("rows: %d (with an observation: %d)" % (len(rows), len(with_obs)))
if rows:
    print("oldest row %s, newest row %s" % (ago(rows[0].get("ts")), ago(rows[-1].get("ts"))))
    days = collections.Counter(iso(r.get("ts", 0))[:10] for r in rows)
    print("rows per day (last 14):", dict(sorted(days.items())[-14:]))
    by_pol = collections.defaultdict(list)
    for r in rows:
        by_pol[r.get("policy") or "?"].append(r)
    for pol, rs in sorted(by_pol.items()):
        secs = sum(int(r.get("window_seconds", 0) or 0) for r in rs)
        hs = sum(int(r.get("new_hashlines", 0) or 0) for r in rs)
        rewards = [float(r.get("reward", 0) or 0) for r in rs]
        acts = collections.Counter(int(r.get("action", -1)) for r in rs)
        top = ", ".join("%s x%d" % ("/".join(map(str, S.decode(a))) if 0 <= a < S.ACTION_N else a, n)
                        for a, n in acts.most_common(4))
        print("policy %-9s rows=%d hours=%.1f handshakes=%d (%.2f/h) reward mean=%.4f median=%.4f"
              % (pol, len(rs), secs / 3600, hs, hs / max(secs / 3600, 1e-9),
                 statistics.fmean(rewards), statistics.median(rewards)))
        print("       top actions: %s (distinct: %d)" % (top, len(acts)))

# ------------------------------------------------------------ models
sec("models (sidecars)")
def describe(meta, label):
    if not isinstance(meta, dict):
        print("%s: none" % label)
        return
    wm_ts, wm_n = M.meta_watermark(meta)
    prov = meta.get("provenance") or {}
    print("%s: source=%s algorithm=%s spec=v%s train_seconds=%s" % (
        label, meta.get("source"), meta.get("algorithm"), meta.get("spec_version"),
        meta.get("train_seconds")))
    print("   data watermark: newest row %s, %s rows; %d rows logged since"
          % (ago(wm_ts) if wm_ts else "none", wm_n, M.new_rows_since(rows, wm_ts)))
    if prov:
        print("   reward model:", {k: v for k, v in prov.items() if not isinstance(v, (list, dict))})
    extra = {k: meta.get(k) for k in ("holdout", "warm_start", "timesteps", "train_rows",
                                       "held_out_rows", "promoted_at", "contest") if k in meta}
    if extra:
        if "promoted_at" in extra:
            extra["promoted_at"] = ago(extra["promoted_at"])
        print("   training/contest:", extra)
for path in sorted(glob.glob(os.path.join(AGENT_DIR, "model-*.meta.json"))) + \
        sorted(glob.glob(os.path.join(AGENT_DIR, "model.meta.json"))):
    describe(load_json(path), "active " + os.path.basename(path))
    zp = path.replace(".meta.json", ".zip")
    if os.path.exists(zp):
        print("   model file written %s" % ago(os.path.getmtime(zp)))
for path in sorted(glob.glob(os.path.join(AGENT_DIR, CANDIDATES_DIR, "local", "*",
                                         "model-*.meta.json"))):
    describe(load_json(path), "LOCAL CANDIDATE " + os.path.relpath(path, AGENT_DIR))
staged = sorted(glob.glob(os.path.join(AGENT_DIR, REMOTE_STAGING_DIR, "*", "model-*.meta.json")))
for path in staged:
    describe(load_json(path), "STAGED " + os.path.relpath(path, AGENT_DIR))
if not staged:
    print("no remote model staged right now (a staged model is cleared once it has "
          "been judged%s)" % ("" if FAIR else ", or when the pull-time pre-filter rejects it"))
remote_metas = sorted(glob.glob(os.path.join(REMOTE_DIR, "model-*.meta.json")))
for path in remote_metas:
    describe(load_json(path), "REMOTE HOST " + os.path.basename(path))

# ------------------------------------------------------------ journal timeline
sec("timeline from the journal (all retained boots)")
PATTERNS = [
    ("start", re.compile(r"agent starting: algorithm=(\S+) policy=(\S+) train=(\S+)")),
    ("train_rows", re.compile(r"train: training (\S+) on (\d+) logged window\(s\) for (\d+) timestep\(s\) \((\S+) reward model(?:; (\d+) held-out row\(s\) kept out; ([^)]*))?\)")),
    ("contest", re.compile(r"contest \((\S+)\): (\S+) candidate vs active \((\S+)\): (promote|keep) - (.*?); (\d+) held-out rows")),
    ("contest_promoted", re.compile(r"contest \((\S+)\): promoted the (\S+) candidate")),
    ("deferred", re.compile(r"contest \((\S+)\) deferred")),
    ("launched", re.compile(r"remote-train: launched (\S+) on the host \((.*)\)")),
    ("local_done", re.compile(r"(\S+) training finished \((\d+)s\)")),
    ("local_fail", re.compile(r"training did not complete")),
    ("staged", re.compile(r"remote-train: staged a (\S+) model")),
    ("check", re.compile(r"promotion check \((\S+)\): (.*?) \| scores=(\{.*\})")),
    ("promoted", re.compile(r"promoted a remote (\S+) model into the active slot: (.*)")),
    ("too_few", re.compile(r"too few logged windows to score")),
    ("not_promoting", re.compile(r"staged (\S+) model (is env-spec|did not load).*not promoting")),
    ("score_err", re.compile(r"scoring the staged .* failed")),
    ("rt_error", re.compile(r"remote-train: (episode upload failed|launch failed|could not start host prep|cannot stage)")),
]
try:
    out = subprocess.run(["journalctl", "-u", "kestrel-agent", "-u", "kestrel-daemon",
                          "--no-pager", "-o", "short-unix", "-q"],
                         capture_output=True, text=True, timeout=120).stdout
except Exception as exc:
    out = ""
    print("journal unreadable:", type(exc).__name__)
events = []
for line in out.splitlines():
    parts = line.split(" ", 1)
    try:
        ts = float(parts[0])
    except (ValueError, IndexError):
        continue
    for kind, rx in PATTERNS:
        m = rx.search(line)
        if m:
            events.append((ts, kind, m.groups()))
            break
if events:
    print("journal covers %s to %s" % (ago(events[0][0]), ago(events[-1][0])))
else:
    print("no agent or remote-train events in the retained journal")
counts = collections.Counter(k for _t, k, _g in events)
print("event counts:", dict(counts))
reasons = collections.Counter()
lifts = []
for ts, kind, g in events:
    if kind == "check":
        reasons[re.sub(r"[-\d.]+", "#", g[1])] += 1
        try:
            sc = json.loads(g[2].replace("'", '"'))
            if all(k in sc for k in ("remote", "local", "baseline")):
                lifts.append((ts, sc["remote"] - sc["baseline"], sc["local"] - sc["baseline"]))
        except ValueError:
            pass
contests = collections.Counter()
for ts, kind, g in events:
    if kind == "contest":
        contests[(g[1], g[3], re.sub(r"[-+]?\d+\.\d+", "#", g[4]))] += 1
if contests:
    print("contest results (source, decision, reason):")
    for (src, dec, why), n in contests.most_common():
        print("  %3d  %-6s %-7s %s" % (n, src, dec, why))
launches = collections.Counter(g[0] for _t, k, g in events if k == "launched")
if launches:
    print("remote launches by algorithm:", dict(launches))
if reasons:
    print("promotion-check reasons (before fair contests):")
    for r, n in reasons.most_common():
        print("  %3d  %s" % (n, r))
if lifts:
    print("lift over baseline at each check (remote vs local):")
    for ts, rl, ll in lifts[-15:]:
        print("  %s  remote %+.4f  local %+.4f  %s" % (iso(ts), rl, ll,
                                                   "remote ahead" if rl > ll else "local ahead"))
# Between two stagings, how many local trainings replaced the model.
local_between = []
n_local = 0
for ts, kind, g in events:
    if kind == "local_done":
        n_local += 1
    elif kind == "staged":
        local_between.append(n_local)
        n_local = 0
if counts.get("staged"):
    print("local trainings between consecutive stagings:", local_between[1:] or "(one staging)")
train_runs = [g for _t, k, g in events if k == "train_rows"]
if train_runs:
    print("on-device training runs: %d; windows used (last 5): %s; timesteps: %s; reward models: %s"
          % (len(train_runs), [int(g[1]) for g in train_runs[-5:]],
             sorted({int(g[2]) for g in train_runs}),
             dict(collections.Counter(g[3] for g in train_runs))))
print("last 30 events:")
for ts, kind, g in events[-30:]:
    print("  %s  %-13s %s" % (iso(ts), kind, " ".join(str(x) for x in g)[:200]))

# ------------------------------------------------------------ yield per model
sec("real-world yield of each model while it was active")
# Segments start at each model swap; rows logged under the learned policy are
# credited to the model active then. Before fair contests every finished local
# training replaced the model; since then only a won contest does.
first_contest = next((ts for ts, k, _g in events
                      if k in ("contest", "contest_promoted", "deferred")), None)
swaps = []
for ts, k, g in events:
    if k == "contest_promoted":
        swaps.append((ts, g[1]))
    elif k == "promoted":
        swaps.append((ts, "remote"))
    elif k == "local_done" and (first_contest is None or ts < first_contest):
        swaps.append((ts, "local"))
if not swaps:
    print("no model swaps in the retained journal")
else:
    seg = collections.defaultdict(lambda: [0, 0, 0.0, 0])   # rows, secs, reward, handshakes
    j = 0
    current = None
    for r in rows:
        ts = float(r.get("ts", 0) or 0)
        while j < len(swaps) and swaps[j][0] <= ts:
            current = swaps[j][1]
            j += 1
        if r.get("policy") != "learned":
            key = r.get("policy") or "?"
        else:
            key = "learned/%s" % (current or "before-journal")
        s = seg[key]
        s[0] += 1
        s[1] += int(r.get("window_seconds", 0) or 0)
        s[2] += float(r.get("reward", 0) or 0)
        s[3] += int(r.get("new_hashlines", 0) or 0)
    for key, (n, secs, rew, hs) in sorted(seg.items()):
        print("%-24s rows=%-6d hours=%-6.1f reward/row=%.4f handshakes/h=%.2f"
              % (key, n, secs / 3600, rew / max(n, 1), hs / max(secs / 3600, 1e-9)))
    print("model swaps: %d local, %d remote" % (sum(1 for _t, s in swaps if s == "local"),
                                                sum(1 for _t, s in swaps if s == "remote")))

# ------------------------------------------------------------ the pre-filter
sec("will a remote model be trained and judged?")
local_meta = P.read_meta(AGENT_DIR, algorithm)
local_ts, _ = M.meta_watermark(local_meta)
host_models = load_json(os.path.join(AGENT_DIR, REMOTE_STAGING_DIR, HOST_MODELS_NAME)) or {}
judged = load_json(os.path.join(AGENT_DIR, REMOTE_STAGING_DIR, EVALUATED_NAME)) or []
if FAIR:
    min_new = int(rt_cfg.get("remote_train_min_new_episodes", 300) or 300)
    print("host models last seen:", {k: {"latest": ago(v.get("latest_ts")), "seen": ago(v.get("seen_at"))}
                                     for k, v in host_models.items() if isinstance(v, dict)}
          if isinstance(host_models, dict) else host_models)
    print("remote models already judged:", len(judged) if isinstance(judged, list) else judged)
    for slug, v in (host_models.items() if isinstance(host_models, dict) else []):
        if isinstance(v, dict):
            n = M.new_rows_since(rows, v.get("latest_ts"))
            print("  %s: %d rows since the host's last model (launches at %d)" % (slug, n, min_new))
cands = [(p, load_json(p)) for p in staged + remote_metas]
if not cands:
    print("no remote sidecar to judge (none staged; pass --remote-dir to judge the host's)")
for path, meta in cands:
    if not isinstance(meta, dict) or meta.get("algorithm") != algorithm:
        continue
    r_ts, _ = M.meta_watermark(meta)
    new_since = M.new_rows_since(rows, r_ts)
    print("%s:" % os.path.basename(path))
    if FAIR:
        fp = M.model_fingerprint(meta)
        print("  already judged: %s; trained under the held-out scheme: %s"
              % (fp in judged if isinstance(judged, list) else "?", meta.get("holdout") or "no"))
    elif hasattr(M, "remote_model_wins"):
        print("  pull-time pre-filter (remote watermark >= local): %s  "
              "(remote newest row %s, local newest row %s)"
              % (M.remote_model_wins(meta, local_meta), ago(r_ts) if r_ts else "none",
                 ago(local_ts) if local_ts else "none"))
    print("  staleness guard: %d rows logged since the remote's data (limit %s) -> %s"
          % (new_since, max_stale or "off",
             "blocked" if max_stale and new_since > max_stale else "ok"))

# ------------------------------------------------------------ re-scoring
if SCORE:
    sec("re-scoring (held-out, as promotion does and fairer)")
    if mem.get("MemAvailable", 0) < 1536 << 20:
        print("skipped: less than 1.5 GB RAM available")
        SCORE = False
if SCORE:
    import numpy as np
    from kestrel_pi.agent import train as T
    pols = {}
    # Which models kept the held-out rows out of their training (so those rows
    # are never in-sample for them).
    trained_holdout_free = {}
    lp = P.model_path_for(AGENT_DIR, algorithm)
    pol, why = P.load_learned(algorithm, lp) if lp and os.path.exists(lp) else (None, "no active model")
    if pol is not None:
        pols["active(%s)" % ((local_meta or {}).get("source") or "?")] = (pol, local_ts)
        trained_holdout_free["active(%s)" % ((local_meta or {}).get("source") or "?")] = \
            bool((local_meta or {}).get("holdout"))
    else:
        print("active model:", why)
    for path, meta in cands:
        if not isinstance(meta, dict) or meta.get("algorithm") != algorithm:
            continue
        zp = path.replace(".meta.json", ".zip")
        rp, why = P.load_learned(algorithm, zp)
        tag = "staged" if REMOTE_STAGING_DIR in path else "remote-host"
        if rp is None:
            print(tag, "model:", why)
        else:
            pols[tag] = (rp, M.meta_watermark(meta)[0])
            trained_holdout_free[tag] = bool(meta.get("holdout"))
    pols["baseline"] = (P.BaselinePolicy(dwell_seconds=agent_cfg.get("dwell_seconds", 5)), None)
    print("policies:", ", ".join(pols))

    def run_split(label, train_rows, val_rows):
        triples = T._rows_with_obs(val_rows)
        if len(triples) < 16 or not T._rows_with_obs(train_rows):
            print("%s: too few rows" % label)
            return None
        reward_fn = T._scoring_reward_fn(train_rows, triples)
        reward_fn = reward_fn[0] if isinstance(reward_fn, tuple) else reward_fn
        val_obs = [o for o, _a, _r in triples]
        res = {}
        for name, (pol, wm) in pols.items():
            t0 = time.monotonic()
            score = T.score_policy(pol, val_obs, reward_fn)
            pol.reset()
            acts = collections.Counter(int(pol.predict(np.asarray(o, dtype=np.float32))[0])
                                       for o in val_obs[:400])
            seen = (sum(1 for r in val_rows if wm and float(r.get("ts", 0) or 0) <= wm
                        and not (trained_holdout_free.get(name) and M.is_holdout(r)))
                    / max(len(val_rows), 1)) if wm else None
            res[name] = score
            print("  %-16s score=%+.5f  distinct greedy actions=%d top=%s%s  (%.1fs)"
                  % (name, score, len(acts),
                     "/".join(map(str, S.decode(acts.most_common(1)[0][0]))) if acts else "-",
                     "  in-sample=%.0f%%" % (100 * seen) if seen is not None else "",
                     time.monotonic() - t0))
        base = res.get("baseline", 0.0)
        print("  %s (%d held-out rows): lift over baseline: %s"
              % (label, len(triples),
                 ", ".join("%s %+.5f" % (n, s - base) for n, s in res.items() if n != "baseline")))
        return res

    def decide(res, label):
        if not res or FAIR:
            return      # on a device with fair contests, E gives the verdict
        active = next((n for n in res if n.startswith("active")), None)
        for rname in ("staged", "remote-host"):
            if rname in res and active:
                wm = pols[rname][1]
                if FAIR:
                    ok, why = M.contest_decision(
                        [], res[rname] - res["baseline"], res[active] - res["baseline"],
                        margin=margin, min_z=0.0, new_since=M.new_rows_since(rows, wm),
                        max_stale_episodes=max_stale)
                    why += " (means only; see E for the paired test)"
                else:
                    ok, why = M.promotion_decision(
                        {"remote": res[rname], "local": res[active], "baseline": res["baseline"]},
                        has_local=True, new_since_remote=M.new_rows_since(rows, wm),
                        margin=margin, max_stale_episodes=max_stale)
                print("  -> %s vs %s on %s: %s (%s)" % (rname, active, label,
                                                       "PROMOTE" if ok else "keep", why))

    n = len(rows)
    print("\nA. a random 20%% split (seed 0)%s:" % (
        ", for comparison" if FAIR else ", exactly as promotion scores"))
    order = np.random.default_rng(0).permutation(n).tolist()
    nv = max(1, int(n * 0.2))
    decide(run_split("seed 0", [rows[i] for i in order[nv:]], [rows[i] for i in order[:nv]]),
           "seed 0")
    print("\nB. other random splits (seeds 1-4), to see how much the verdict moves:")
    for seed in range(1, 5):
        order = np.random.default_rng(seed).permutation(n).tolist()
        decide(run_split("seed %d" % seed, [rows[i] for i in order[nv:]],
                         [rows[i] for i in order[:nv]]), "seed %d" % seed)
    print("\nC. time-ordered: the newest 20% held out (data neither model's newest "
          "rows had seen, if their watermarks are older):")
    cut = n - nv
    decide(run_split("newest 20%", rows[:cut], rows[cut:]), "newest 20%")
    newest_wm = max((wm for _p, wm in pols.values() if wm), default=None)
    if newest_wm:
        after = [r for r in rows if float(r.get("ts", 0) or 0) > newest_wm]
        before = [r for r in rows if float(r.get("ts", 0) or 0) <= newest_wm]
        print("\nD. only rows newer than every model's data (%d rows):" % len(after))
        decide(run_split("unseen by all", before, after), "unseen by all")
if SCORE and FAIR and hasattr(T, "evaluate"):
    print("\nE. the contest itself: held-out hour blocks no model trains on, newest 2,000:")
    pol_only = {n: p for n, (p, _wm) in pols.items()}
    # As the agent does: a model trained before rows were held out saw them all,
    # so only held-out rows newer than its data count.
    old_wms = [M.meta_watermark(m)[0] for m in [local_meta] + [m for _p, m in cands]
               if isinstance(m, dict) and m and not m.get("holdout")]
    res = T.evaluate(rows, pol_only, min_ts=max(old_wms) if old_wms else None)
    if old_wms:
        print("  (a model here predates held-out rows: scoring only rows newer than its data)")
    if res is None:
        print("  too few held-out rows to score yet")
    else:
        base = res["mean"]["baseline"]
        print("  %d rows (%s reward model)" % (res["rows"], res["reward_model"]))
        for n, m in res["mean"].items():
            print("  %-16s mean %+.5f  lift %+.5f" % (n, m, m - base))
        active = next((n for n in res["mean"] if n.startswith("active")), None)
        for rname in ("staged", "remote-host"):
            if rname in res["mean"] and active:
                diffs = [a - b for a, b in zip(res["per_row"][rname], res["per_row"][active])]
                ok, why = M.contest_decision(
                    diffs, res["mean"][rname] - base, res["mean"][active] - base,
                    margin=margin, min_z=float(agent_cfg.get("promotion_min_z", 1.0) or 0),
                    new_since=M.new_rows_since(rows, pols[rname][1]),
                    max_stale_episodes=max_stale)
                print("  -> %s vs %s: %s (%s)" % (rname, active, "PROMOTE" if ok else "keep", why))
print("\n(end of report)")
PY
REMOTE
     } | ssh "${ssh_opts[@]}" "$target" 'bash -s' >"$report.tmp" 2>&1; then
  echo "note: the SSH session ended with an error; the partial report was still saved." >&2
fi
mv "$report.tmp" "$report"
chmod 600 "$report"
echo "Report: $report (private; read it before sharing)"
