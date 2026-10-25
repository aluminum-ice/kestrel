"""The Phase 4 reinforcement-learning capture agent.

This subpackage is the learning agent that drives capture and learns which
channels, dwells and attack postures yield handshakes, replacing the Phase 2
scripted sweep with a policy that adapts to the RF environment. It reads its
observations and issues its control through the Phase 2 capture interface
(``docs/developer/capture-interface.md``, ``contract_version 1``) and never patches
AngryOxide, so it only ever runs on the capture dongle when capture is enabled.

Layout (see ``docs/developer/agent-interface.md``):

- :mod:`.spec` - the *fixed* env contract: observation shape, the flattened
  ``Discrete`` action vocabulary (posture x dwell x transmit) and its bijective
  decode, the reward, and channel ranking. Pure stdlib + numpy; no torch/gym,
  no hardware, so it is the unit-test seam and the thing a model is bound to.
- :mod:`.features` - builds the observation vector from the capture status and
  the append-only store delta (the stable Output surface).
- :mod:`.env` - the Gymnasium environment wrapping the capture interface.
- :mod:`.policies` - the config-selected SB3 algorithm map and the scripted
  baseline policy (both load against the same env spaces).
- :mod:`.controller` - the agent process: the decide loop, the status file and
  the episode logs. ``kestrel-agent.service`` runs it.
- :mod:`.metrics` - the agent-vs-sweep comparison, derived from the episode log.
- :mod:`.train` - offline training: replay the episode logs through SB3 into an
  updated ``model.zip``.
- :mod:`.readout` - stdlib-only reader of the agent's ``status.json`` for the
  daemon's dashboard card (never imports torch/gym, so the benign daemon can
  import it with the heavy ML stack absent).

Only :mod:`.spec`, :mod:`.features`, :mod:`.metrics` and :mod:`.readout` are
safe to import without gymnasium/torch installed; :mod:`.env`, :mod:`.policies`
and :mod:`.controller` lazy-import the heavy stack. ``__init__`` imports none of
them, so importing :mod:`kestrel_pi.agent` costs nothing.
"""

from __future__ import annotations

# The agent's runtime directory (owned by pi, created by the build). All of the
# agent's persistent artifacts live here; the boot-partition importer and the
# backup plugin reference these names.
AGENT_DIR = "/var/lib/kestrel/agent"
# Each algorithm keeps its own model file (``model-<slug>.zip`` + a matching
# ``.meta.json`` sidecar), so switching algorithm and back reloads the saved
# model instead of retraining from scratch. ``MODEL_NAME``/``MODEL_META_NAME``
# are the pre-per-algorithm single-model names, kept only as a load-time
# migration fallback for a device that trained/imported one before this change.
MODEL_NAME = "model.zip"
MODEL_META_NAME = "model.meta.json"
STATUS_NAME = "status.json"
# Records the last on-device idle-training run (finished-at + duration) so the
# dashboard can show it and it survives an agent/daemon restart.
LAST_TRAIN_NAME = "last-train.json"
# A manual "train now" request from the dashboard: the config tool drops this
# file and the agent's training thread consumes it to start a run promptly.
TRAIN_REQUEST_NAME = "train-request"
# A running count of model adoptions by source over the device's lifetime: how
# many times a locally trained model vs a promoted remote model became the
# active model. Persisted so the dashboard can show which side is winning, and
# so it survives an agent/daemon restart.
PROMOTION_TALLY_NAME = "promotion-tally.json"
EPISODES_DIR = "episodes"
# Where the remote-train plugin drops a pulled remote model for the agent to
# evaluate: ``<AGENT_DIR>/.remote-staging/<slug>/model-<slug>.{zip,meta.json}``.
# The plugin only pulls-and-stages here (torch-free); the agent process scores
# the staged model against the active one and promotes the winner (quality-aware
# promotion, docs/developer/agent-interface.md).
REMOTE_STAGING_DIR = ".remote-staging"


def model_slug(algorithm):
    """A filesystem-safe lowercase slug for an algorithm name, used to name its
    per-algorithm model file (e.g. ``RecurrentPPO`` -> ``recurrentppo``,
    ``QR-DQN`` -> ``qr-dqn``). Pure stdlib so the daemon/CLI can derive the
    filename without importing the heavy agent stack."""
    text = (algorithm or "").strip().lower()
    slug = "".join(ch if (ch.isalnum() or ch == "-") else "-" for ch in text)
    slug = slug.strip("-")
    return slug or "default"


def model_name_for(algorithm):
    """The per-algorithm model filename (e.g. ``model-dqn.zip``)."""
    return "model-%s.zip" % model_slug(algorithm)


def model_meta_name_for(algorithm):
    """The per-algorithm model sidecar filename (e.g. ``model-dqn.meta.json``)."""
    return "model-%s.meta.json" % model_slug(algorithm)


__all__ = ["AGENT_DIR", "MODEL_NAME", "MODEL_META_NAME", "STATUS_NAME",
           "LAST_TRAIN_NAME", "TRAIN_REQUEST_NAME", "PROMOTION_TALLY_NAME",
           "EPISODES_DIR", "REMOTE_STAGING_DIR", "model_slug", "model_name_for",
           "model_meta_name_for"]
