from kestrel_pi import connectivity
from kestrel_pi.connectivity import ConnectivityMonitor


def test_edge_fires_once_and_stays_online(monkeypatch):
    # Pretend wlan0 has an IP, bnep0 does not.
    monkeypatch.setattr(
        connectivity, "iface_has_ipv4", lambda iface: iface == "wlan0"
    )
    reachable = {"v": True}
    fired = []
    mon = ConnectivityMonitor(
        on_online=lambda: fired.append(1),
        probe=lambda: reachable["v"],
        offline_confirmations=1,            # flip offline on the first failure
    )
    assert mon.poll_once() is True          # offline -> online edge
    assert mon.state.online is True
    assert mon.state.active_paths() == ["wlan0"]
    assert mon.poll_once() is False         # still online: no new edge
    assert len(fired) == 1

    reachable["v"] = False
    assert mon.poll_once() is False         # went offline
    assert mon.state.online is False
    reachable["v"] = True
    assert mon.poll_once() is True          # edge again
    assert len(fired) == 2


def test_online_trusts_networkmanager_only_where_unambiguous(monkeypatch):
    # NM 'full' is online and 'portal'/'none' are offline with no probe; but
    # 'limited'/'unknown' are NOT trusted as offline (NM can under-report a link
    # that actually routes, e.g. a broken-resolver LAN), so they fall through to
    # the DNS-free IP probe.
    probed = []
    monkeypatch.setattr(connectivity, "ip_probe",
                        lambda *a, **k: probed.append(1) or True)

    monkeypatch.setattr(connectivity, "nmcli_connectivity", lambda: "full")
    assert connectivity.nmcli_connectivity_online() is True

    for verdict in ("portal", "none"):
        monkeypatch.setattr(connectivity, "nmcli_connectivity", lambda v=verdict: v)
        assert connectivity.nmcli_connectivity_online() is False
    assert probed == []   # 'full'/'portal'/'none' are definite; no probe needed

    for verdict in ("limited", "unknown", ""):
        probed.clear()
        monkeypatch.setattr(connectivity, "nmcli_connectivity", lambda v=verdict: v)
        assert connectivity.nmcli_connectivity_online() is True
        assert probed == [1]   # confirmed by the IP probe, not trusted blindly


def test_limited_verdict_is_offline_only_when_the_ip_probe_also_fails(monkeypatch):
    # The field case: NM rates the default-route link 'limited' because its
    # check tripped over broken DNS, but the link really routes -> online. If
    # the IP probe also fails, it is genuinely offline.
    monkeypatch.setattr(connectivity, "nmcli_connectivity", lambda: "limited")

    monkeypatch.setattr(connectivity, "ip_probe", lambda *a, **k: True)
    assert connectivity.nmcli_connectivity_online() is True

    monkeypatch.setattr(connectivity, "ip_probe", lambda *a, **k: False)
    assert connectivity.nmcli_connectivity_online() is False


def test_ip_probe_is_dns_free_and_tries_each_host(monkeypatch):
    # ip_probe connects by IP literal (never resolves a name) and returns True
    # on the first host that accepts a TCP connection.
    attempts = []

    class _Sock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_create_connection(addr, timeout=None):
        attempts.append(addr)
        if addr[0] == "1.1.1.1":
            raise OSError("refused")
        return _Sock()

    import socket
    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    assert connectivity.ip_probe() is True
    assert attempts == [("1.1.1.1", 443), ("8.8.8.8", 443)]

    attempts.clear()
    monkeypatch.setattr(socket, "create_connection",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert connectivity.ip_probe() is False


def test_offline_is_debounced_over_consecutive_polls(monkeypatch):
    # A single failed probe must NOT flip to offline; it takes
    # offline_confirmations consecutive failures. Online still resumes at once.
    monkeypatch.setattr(connectivity, "iface_has_ipv4", lambda iface: iface == "bnep0")
    reachable = {"v": True}
    mon = ConnectivityMonitor(
        on_online=lambda: None,
        probe=lambda: reachable["v"],
        offline_confirmations=2,
    )
    assert mon.poll_once() is True and mon.state.online is True   # online

    reachable["v"] = False
    assert mon.poll_once() is False and mon.state.online is True  # 1st fail: held
    assert mon.poll_once() is False and mon.state.online is False # 2nd fail: offline

    reachable["v"] = True
    assert mon.poll_once() is True and mon.state.online is True   # back online at once

    # A lone blip between good polls never flips offline, and the streak resets.
    reachable["v"] = False
    assert mon.poll_once() is False and mon.state.online is True  # 1 fail: held
    reachable["v"] = True
    assert mon.poll_once() is False and mon.state.online is True  # recovered, no edge
    reachable["v"] = False
    assert mon.poll_once() is False and mon.state.online is True  # streak reset: held


def test_no_ip_means_offline_without_probe(monkeypatch):
    monkeypatch.setattr(connectivity, "iface_has_ipv4", lambda iface: False)
    probed = []
    mon = ConnectivityMonitor(
        on_online=lambda: None,
        probe=lambda: probed.append(1) or True,
    )
    assert mon.poll_once() is False
    assert mon.state.online is False
    assert probed == []   # probe skipped when no path has an IP
