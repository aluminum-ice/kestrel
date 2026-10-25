#!/usr/bin/env bash
#
# Pull a PII-free snapshot of the learning agent's model-promotion state off a
# field unit, so quality-aware promotion can be validated off-device
# (docs/developer/agent-interface.md, docs/developer/remote-training.md).
#
# Runs on your WORKSTATION (not the device) and reads the device READ-ONLY over
# SSH. It ships in the image under /usr/local/share/kestrel/scripts/ so you
# can copy it off a unit, but you run it from your own machine. It collects:
#   - the per-algorithm model sidecars (model-<slug>.meta.json) with their
#     source (local/remote), provenance, data_watermark and any eval fields;
#   - status.json (the live agent card: model_source, metrics, ...);
#   - the remote-staging sidecars (.remote-staging/<slug>/model-<slug>.meta.json)
#     that the remote-train plugin left for the agent to score;
#   - episodes/context.json (the coarse, PII-free device tag);
#   - directory listings (with timestamps) of the agent dir and staging dir;
#   - the promotion-decision lines from the agent + daemon journals (the
#     "promotion check ... | scores={...}", "staged ...", "promoted ..." log
#     lines that record each decision).
#
# It deliberately does NOT pull the model .zip files, the raw episode logs, any
# capture output, SSH keys or config, so the bundle is safe to share.
#
# Usage (from a repo checkout, or a copy pulled off a unit):
#   builder/extra/files/scripts/pull-agent-diagnostics.sh user@host [ssh-port]
#
# Env overrides:
#   AGENT_DIR   on-device agent dir (default /var/lib/kestrel/agent)
#   SSH_KEY     identity file to use (default: your ssh default)
#
# Then send the printed .tar.gz back for review.

set -eu

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: $0 user@host [ssh-port]" >&2
  exit 2
fi

target=$1
port=${2:-22}
agent_dir=${AGENT_DIR:-/var/lib/kestrel/agent}

ssh_cmd="ssh -p $port -o BatchMode=yes -o ConnectTimeout=20"
if [ -n "${SSH_KEY:-}" ]; then
  ssh_cmd="$ssh_cmd -i $SSH_KEY"
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="agent-diagnostics-$stamp"
mkdir -p "$out"

echo "Collecting agent promotion diagnostics from $target (agent dir: $agent_dir) ..."

# 1. Sidecars, status, context and staged sidecars, tarred on the device so a
#    single transfer carries them. Globs expand on the device; no match is not
#    fatal (the agent may have no model or nothing staged yet).
# shellcheck disable=SC2086
if ! $ssh_cmd "$target" "cd '$agent_dir' 2>/dev/null && tar -czf - \
      model-*.meta.json status.json episodes/context.json \
      .remote-staging/*/model-*.meta.json 2>/dev/null" > "$out/agent-meta.tar.gz"; then
  echo "  note: could not read $agent_dir metadata (is the path right?)" >&2
fi

# 2. Listings, so model/staging files and their mtimes are visible even when a
#    sidecar is missing.
# shellcheck disable=SC2086
$ssh_cmd "$target" \
  "ls -la --time-style=full-iso '$agent_dir' '$agent_dir'/.remote-staging/* 2>&1 || true" \
  > "$out/agent-dir-listing.txt" || true

# 3. The promotion-decision trail from both journals (agent process + daemon).
#    These lines record every stage/score/promote decision verbatim.
# shellcheck disable=SC2086
$ssh_cmd "$target" \
  "journalctl -u kestrel-agent -u kestrel-daemon --no-pager -n 5000 2>&1 \
     | grep -iE 'promot|staged|held-out|bootstrap|env-spec|keeping the local|score' \
     || echo '(no matching journal lines, or the journal is not readable without privileges; retry with a privileged account if empty)'" \
  > "$out/promotion-journal.txt" || true

# 4. A small manifest naming what was collected and from where.
{
  echo "collected: $stamp"
  echo "target:    $target (port $port)"
  echo "agent_dir: $agent_dir"
  echo "contents:"
  echo "  agent-meta.tar.gz      model + staged sidecars, status.json, context.json"
  echo "  agent-dir-listing.txt  agent + staging directory listings with mtimes"
  echo "  promotion-journal.txt  stage/score/promote decision log lines"
} > "$out/MANIFEST.txt"

bundle="$out.tar.gz"
tar -czf "$bundle" "$out"
rm -rf "$out"

echo "Done. Share this bundle:"
echo "  $(pwd)/$bundle"
