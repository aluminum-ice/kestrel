#!/bin/bash
# Import learning-agent model(s) staged on the boot partition, then remove the
# staged files so it is a one-shot seed and config.toml stays the lasting source
# of truth. Runs from kestrel-agent-provision.service on EVERY boot when a
# model file is present (ConditionPathExistsGlob). Mirrors the Wi-Fi/whitelist
# boot-partition provisioning flow. See docs/user-guide/boot-partition-provisioning.md.
#
# Files on /boot/firmware (the FAT partition the operator can write by card).
# Each algorithm keeps its own model on the device, so you may stage more than
# one model and all are imported:
#   kestrel-model.zip              a model (its sidecar names its algorithm)
#   kestrel-model.meta.json        that model's sidecar: {"algorithm": "..."}
#   kestrel-model-<name>.zip       another model (any distinct <name>)
#   kestrel-model-<name>.meta.json its sidecar
#   kestrel-model-episodes.tar.gz  optional shared episode-log archive
#   kestrel-model-active           optional: one line naming the algorithm
#                                        to make active ([agent] algorithm)
#
# Every model file is paired with its "<model>.meta.json" sidecar, whose
# algorithm is enum-validated here (untrusted removable-card input, the same
# input-validation boundary as the web dropdown) before anything is written. The
# sidecar's algorithm - not the file name - decides where the model lands
# (model-<slug>.zip in the agent dir), so each algorithm keeps a separate model.
# A model with a missing or invalid sidecar is skipped and LEFT staged so the
# operator can see and fix it; the others still import.
#
# Which algorithm becomes active ([agent] algorithm + policy = learned):
#   1. the kestrel-model-active file, if it names an imported algorithm;
#   2. else the plain kestrel-model.zip's algorithm, if that one imported;
#   3. else, if exactly one model imported, that one;
#   4. else [agent] is left unchanged (all models are imported and available;
#      pick one with: sudo kestrel agent algorithm <ALGO>).
# It never enables the agent or capture - both stay opt-in.
set -euo pipefail
shopt -s nullglob
# shellcheck source=common.sh
. "${PWN_LIB_DIR:-/usr/local/lib/kestrel}/common"

boot=${PWN_BOOT_DIR:-/boot/firmware}
episodes_src="$boot/kestrel-model-episodes.tar.gz"
active_src="$boot/kestrel-model-active"

# The algorithms a model may declare. Kept in step with
# kestrel_pi.agent.policies.ALGORITHMS; validated here so a bad value from a
# removable card never selects a Python class to import.
AGENT_ALGORITHMS="RecurrentPPO PPO DQN QR-DQN"

algo_valid() {
    local a
    for a in $AGENT_ALGORITHMS; do [ "$1" = "$a" ] && return 0; done
    return 1
}

# The algorithm from a sidecar (stdlib python3; the venv is not needed here).
read_algo() {
    PWN_META="$1" python3 - <<'PY' 2>/dev/null || true
import json, os
try:
    with open(os.environ["PWN_META"], encoding="utf-8") as fh:
        print(str(json.load(fh).get("algorithm", "")))
except Exception:
    pass
PY
}

# Filesystem-safe model slug for an algorithm. Matches
# kestrel_pi.agent.model_slug exactly so the imported file name is the one
# the agent loads.
slugify() {
    PWN_ALGO="$1" python3 - <<'PY'
import os
a = os.environ.get("PWN_ALGO", "").strip().lower()
s = "".join(c if (c.isalnum() or c == "-") else "-" for c in a).strip("-")
print(s or "default")
PY
}

models=("$boot"/kestrel-model*.zip)
[ "${#models[@]}" -gt 0 ] || exit 0

mkdir -p "$PWN_AGENT_DIR" "$PWN_AGENT_DIR/episodes"

imported_algos=""          # space-separated, imported this run
legacy_algo=""             # the plain kestrel-model.zip's algorithm
remove=()                  # staged files to remove at the end (only imported)

for model_src in "${models[@]}"; do
    meta_src="${model_src%.zip}.meta.json"
    if [ ! -f "$meta_src" ]; then
        log "staged model $model_src has no ${meta_src##*/} sidecar; skipping it (fix the sidecar and reboot)"
        continue
    fi
    algo=$(read_algo "$meta_src")
    if ! algo_valid "$algo"; then
        log "staged model ${model_src##*/} sidecar has an unknown algorithm '${algo:-<none>}'; skipping it (expected one of: $AGENT_ALGORITHMS)"
        continue
    fi
    slug=$(slugify "$algo")
    install -o pi -g pi -m 0644 "$model_src" "$PWN_AGENT_DIR/model-$slug.zip"
    install -o pi -g pi -m 0644 "$meta_src" "$PWN_AGENT_DIR/model-$slug.meta.json"
    log "imported agent model ${model_src##*/} as model-$slug.zip (algorithm $algo)"
    imported_algos="$imported_algos $algo"
    [ "$model_src" = "$boot/kestrel-model.zip" ] && legacy_algo="$algo"
    remove+=("$model_src" "$meta_src")
done

if [ -z "${imported_algos// /}" ]; then
    log "no valid staged model imported; leaving staged files in place"
    exit 0
fi

if [ -f "$episodes_src" ]; then
    if tar -xzf "$episodes_src" -C "$PWN_AGENT_DIR/episodes" 2>/dev/null; then
        chown -R pi:pi "$PWN_AGENT_DIR/episodes"
        log "imported staged episode logs"
        remove+=("$episodes_src")
    else
        log "could not extract $episodes_src; continuing without it"
    fi
fi

# Decide the active algorithm by the documented precedence.
active=""
if [ -f "$active_src" ]; then
    want=$(head -n 1 "$active_src" 2>/dev/null | tr -d '[:space:]' || true)
    for a in $imported_algos; do
        [ "$want" = "$a" ] && active="$a" && break
    done
    if [ -n "$want" ] && [ -z "$active" ]; then
        log "kestrel-model-active names '$want', which was not among the imported models; ignoring it"
    fi
    remove+=("$active_src")
fi
if [ -z "$active" ] && [ -n "$legacy_algo" ]; then
    active="$legacy_algo"
fi
if [ -z "$active" ]; then
    # Exactly one imported? (set -- on the space-separated list)
    # shellcheck disable=SC2086
    set -- $imported_algos
    [ "$#" -eq 1 ] && active="$1"
fi

if [ -n "$active" ]; then
    PWN_CONFIG="$PWN_CONFIG" "$PWN_CONFIG_SET" agent algorithm "\"$active\"" \
        || die "could not set agent.algorithm in $PWN_CONFIG"
    PWN_CONFIG="$PWN_CONFIG" "$PWN_CONFIG_SET" agent policy '"learned"' \
        || die "could not set agent.policy in $PWN_CONFIG"
    log "set [agent] algorithm=$active policy=learned; enable the agent to use it (sudo kestrel agent enable)"
else
    log "imported models for:${imported_algos}; none selected as active (stage kestrel-model-active, or run: sudo kestrel agent algorithm <ALGO>)"
fi

# One-shot: remove the staged files we imported so config.toml is the lasting
# source of truth and each model is imported once per time it is dropped.
rm -f "${remove[@]}"
