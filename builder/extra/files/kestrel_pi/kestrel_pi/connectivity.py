"""The connectivity surface the daemon sources ``on_internet_available`` from.

"Online" is true when *either* management path has real upstream reachability:
the BT PAN (``bnep0``) or the onboard Wi-Fi station (``wlan0``). It is a real
probe, not just link-up, so a tethered phone with no data or a captive portal
reads as offline. The monitor fires the edge on the transition from no-path to
any-path and stays online while at least one path is reachable, so one radio
dropping while the other is up does not flap the hook. The capture dongle (no
IP, monitor mode) is never considered.

The reachability probe is injectable so the monitor is unit-testable without a
network; the default probe trusts NetworkManager's own connectivity verdict when
it is definite (``full`` online, ``portal``/``none`` offline), and otherwise
confirms with a DNS-free TCP reachability probe over the real default route. The
confirmation step matters on a multi-homed device whose links disagree, or on a
LAN whose resolver is broken or whose check URL is filtered (e.g. a split-horizon
Pi-hole the device's subnet can't reach): NM then under-reports a link that
actually routes as ``limited``, which used to read - and flap - as offline. An
explicit HTTP ``[web] probe_url`` override is still available for operators who
want a strict, captive-portal-sensitive HTTP check.
"""

from __future__ import annotations

import subprocess

# Management interfaces considered, most-preferred first. The capture dongle is
# deliberately absent.
MANAGEMENT_INTERFACES = ("bnep0", "wlan0")


def iface_has_ipv4(iface):
    """True if ``iface`` currently has an IPv4 address."""
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "dev", iface],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "inet " in out


# A lightweight captive-portal / reachability check used when NetworkManager's
# own connectivity check can't give a verdict. It returns an empty HTTP 204, so
# a captive portal (which injects a redirect or a body) reads as offline.
DEFAULT_PROBE_URL = "http://connectivity-check.gstatic.com/generate_204"


def nmcli_connectivity():
    """NetworkManager's last connectivity verdict as its raw string (``full``,
    ``limited``, ``portal``, ``none`` or ``unknown``), or ``""`` if nmcli can't
    be run.

    Reads the cached state (``networking connectivity``), not ``... check``:
    the daemon runs as the unprivileged ``pi`` user, and forcing a re-check over
    D-Bus can be denied for an unprivileged process, so ``check`` returned
    ``unknown`` to the daemon (and it fell through to the HTTP probe) even while
    ``sudo nmcli networking connectivity check`` reported ``full``. The cached
    property read needs no privilege; NM keeps it current from its own periodic
    and on-change rechecks."""
    try:
        return subprocess.run(
            ["nmcli", "-t", "networking", "connectivity"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


# A DNS-free reachability probe: a successful TCP handshake to a well-known
# anycast address means the default route reaches the internet, without a name
# lookup (which can fail on a split-horizon / broken-resolver LAN while routing
# is perfectly fine) and without the captive-portal empty-204 body check. Two
# addresses so one operator-blocked anycast provider doesn't read as offline.
IP_PROBE_HOSTS = ("1.1.1.1", "8.8.8.8")
IP_PROBE_PORT = 443


def ip_probe(hosts=IP_PROBE_HOSTS, port=IP_PROBE_PORT, timeout=4):
    """True if a TCP connection to any ``hosts`` IP on ``port`` succeeds.

    IP literals only, so this never does DNS. It tests the real default-route
    egress, which is also the path the daemon's networked plugins use to reach
    upstream. It does not inspect HTTP, so it is not captive-portal aware on its
    own - that is why the caller still trusts NM's ``portal`` verdict first, and
    why ``[web] probe_url`` stays available for a strict HTTP check."""
    import socket

    for host in hosts:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def nmcli_connectivity_online():
    """Default probe: online when there is real upstream reachability.

    Trust NetworkManager's verdict only where it is unambiguous: ``full`` is
    online, and ``portal``/``none`` are offline (a detected captive portal, or no
    connected device at all). A ``limited`` verdict - and the ``unknown`` of a
    stock image with connectivity checking disabled, or a missing nmcli - is
    *not* trusted as offline: NM's own check can trip over a broken resolver or a
    filtered check URL and under-report a link that actually routes (seen in the
    field on a split-horizon Pi-hole LAN the device's subnet couldn't reach). In
    those cases confirm with :func:`ip_probe`, a DNS-free TCP reachability test
    over the real default route. This both removes the DNS dependency and stops
    the dashboard flapping offline when a multi-homed device's NM aggregate dips
    while one link churns. An operator who wants a strict HTTP / captive-portal
    check can set ``[web] probe_url`` (that makes the HTTP probe the whole check;
    see the daemon wiring)."""
    verdict = nmcli_connectivity()
    if verdict == "full":
        return True
    if verdict in ("portal", "none"):
        return False
    # 'limited' / 'unknown' / '' -> NM is unsure or under-reporting; confirm with
    # a DNS-free probe of the real egress path rather than trusting it.
    return ip_probe()


def http_probe(url, timeout):
    """Explicit HTTP probe: online iff the URL returns an empty 204/200.

    A captive portal injects a body or a redirect, so a non-empty or non-2xx
    response reads as offline."""
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            if resp.status not in (200, 204):
                return False
            body = resp.read(1)
            return not body
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        return False


class ConnectivityState:
    """Read-only connectivity view handed to plugins as ``context.connectivity``."""

    def __init__(self):
        self.online = False
        # iface -> bool (has an IPv4 address right now)
        self.paths = {iface: False for iface in MANAGEMENT_INTERFACES}

    def active_paths(self):
        return [iface for iface, up in self.paths.items() if up]


class ConnectivityMonitor:
    """Tracks the management paths and fires ``on_online`` on the offline→online
    edge. Poll-based; the daemon calls :meth:`poll_once` on a timer."""

    def __init__(self, on_online, probe=None, logger=None,
                 interfaces=MANAGEMENT_INTERFACES, offline_confirmations=2):
        self.state = ConnectivityState()
        self._on_online = on_online
        self._probe = probe or nmcli_connectivity_online
        self._log = logger
        self._interfaces = tuple(interfaces)
        # Consecutive unreachable polls required before reporting offline, so a
        # single failed probe over a slow/flaky management link (e.g. a BT-PAN
        # tether, where the upstream probe can time out while the link is up)
        # doesn't flap the state to offline and straight back. Online is still
        # reported on the first reachable poll.
        self._offline_confirmations = max(1, int(offline_confirmations))
        self._fail_streak = 0

    def poll_once(self):
        """Refresh path state and reachability; fire the edge if we just came
        online. Returns ``True`` on the offline→online transition.

        Asymmetric hysteresis: online is reported immediately on a reachable
        poll, but offline only after ``offline_confirmations`` consecutive
        unreachable polls - a transient blip holds the current online state."""
        any_ip = False
        for iface in self._interfaces:
            has_ip = iface_has_ipv4(iface)
            self.state.paths[iface] = has_ip
            any_ip = any_ip or has_ip

        # No management path has an IP at all: definitely offline, skip the
        # (costly) upstream probe.
        reachable = bool(any_ip) and bool(self._probe())
        was_online = self.state.online

        if reachable:
            self._fail_streak = 0
            self.state.online = True
            if not was_online:
                if self._log:
                    self._log.info(
                        "online over %s", ", ".join(self.state.active_paths()) or "?"
                    )
                try:
                    self._on_online()
                except Exception:  # noqa: BLE001
                    if self._log:
                        self._log.exception("on_online callback failed")
                return True
            return False

        # Not reachable this poll.
        self._fail_streak += 1
        if was_online and self._fail_streak < self._offline_confirmations:
            # A transient blip: hold the online state until it is confirmed over
            # consecutive polls, so the dashboard doesn't flash offline.
            if self._log:
                self._log.debug(
                    "connectivity probe failed (%d/%d); holding online",
                    self._fail_streak, self._offline_confirmations)
            return False
        self.state.online = False
        if was_online and self._log:
            self._log.info("offline (no management path reachable)")
        return False
