"""Tests for the census desktop report (``tools/census-report/census_report.py``).

They live here rather than under ``tools/`` so ``make test`` runs them with no
Makefile change, and they reuse the census plugin tests' fake DHT: no test
touches the network."""

import importlib.util
import io
import os

import test_census as tc

SCRIPT = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "..", "tools", "census-report",
    "census_report.py"))


def _script():
    spec = importlib.util.spec_from_file_location("census_report_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_report_loads_the_plugin_helpers_from_its_own_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                       # any working directory
    report = _script()
    dht, proto = report.load_helpers()
    assert proto.PROTOCOL == "kestrel-census/1" and proto.SLOTS == 64
    assert hasattr(dht, "get_item") and os.path.isdir(report.PLUGINS)
    assert report.parse_routers(" a.example:1, bad, b.example:65536,c.example:2") == [
        ("a.example", 1), ("c.example", 2)]


def test_report_tables_cover_every_code():
    report = _script()
    _dht, proto = report.load_helpers()
    rows = [tc._row(version="1.0.12", contests=(2, 1, 0, 0)),
            tc._row(version="1.0.12-beta.3", algorithm=2, remote=4, contests=(0, 0, 1, 1)),
            tc._row(version="master", state=2, remote=2),
            tc._row(version="1.0.11", state=1, algorithm=4, remote=3),
            tc._row(version="1.0.11", state=0, algorithm=0, remote=1)]
    text = report.render(proto, "2027-01-15", proto.Tally(rows, hour=8), tc.NOW, True)
    assert text.startswith("Kestrel census, 2027-01-15 (UTC), read 08:00Z - approximate "
                           "and unverified\n")
    assert "Devices: online now 5 · reported today 5" in text
    lines = text.splitlines()
    releases = lines[lines.index(next(l for l in lines if l.startswith("Release"))) + 1:][:4]
    assert [l.split()[0] for l in releases] == ["1.0.12", "1.0.12-beta.3", "1.0.11", "unknown"]
    for label in ("learned", "scripted sweep", "not running", "no agent", "RecurrentPPO",
                  "PPO", "not enabled", "enabled, not configured", "configured, failing",
                  "configured, no new model", "working"):
        assert any(l.startswith(label + " ") for l in lines), label
    assert any(l.split()[:2] == ["all", "devices"] and l.endswith("2 of 3      1 of 2")
               for l in lines)
    assert any(l.startswith("remote training working") and "0 of 0" in l for l in lines)
    # Another day's report has no "online now".
    other = report.render(proto, "2027-01-14", proto.Tally(rows), tc.NOW, False)
    assert "Devices: reported that day 5" in other
    assert "No reports found." in report.render(proto, "2027-01-15", proto.Tally([]),
                                                 tc.NOW, True)


def test_report_csv_rows(tmp_path):
    import csv
    report = _script()
    _dht, proto = report.load_helpers()
    tally = proto.Tally([tc._row(remote=4, contests=(1, 2, 3, 0))], hour=8)
    path = report.write_csv(proto, str(tmp_path / "out"), "2027-01-15", tally)
    rows = list(csv.reader(open(path, encoding="utf-8")))
    assert rows[0] == ["day", "table", "item", "value"]
    assert ["2027-01-15", "devices", "reported", "1"] in rows
    assert ["2027-01-15", "Release", "1.0.12", "1"] in rows
    assert ["2027-01-15", "Training contests", "all devices, remote won", "3 of 3"] in rows
    assert os.path.basename(path) == "census-2027-01-15.csv"


def _run(tmp_path, net, argv, clock, resolve=None):
    report = _script()
    out = io.StringIO()
    router = sorted(net.nodes)[0]
    code = report.main(
        argv + ["--state", str(tmp_path / "report-state"),
                "--bootstrap", "router.example.net:6881"],
        open_transport=lambda device: tc.FakeTransport(net),
        resolve=resolve or (lambda host, port: router),
        now=clock, accept=tc._test_net, bits=8,
        client_opts={"clock": clock, "sleep": clock.sleep},
        wait=lambda seconds: clock.sleep(seconds) or False, out=out)
    return code, out.getvalue()


def test_report_end_to_end_against_a_fake_dht(tmp_path):
    net = tc.FakeDHT(invalid=0.2, seed=45)
    for i in range(3):
        writer, _m, _n, clock = tc._worker(tmp_path, net=net, seed=110 + i, name="w%d" % i,
                                           read=False)
        tc._run_for(writer, clock, 3600)
    day = writer._day["day"]
    before = len(net.log)
    code, text = _run(tmp_path, net, ["--day", day, "--csv", str(tmp_path / "csv")], clock)
    assert code == 0, text
    assert "reported today 3" in text and "1.0.12 " in text
    assert (tmp_path / "csv" / ("census-%s.csv" % day)).exists()
    assert oct(os.stat(tmp_path / "report-state").st_mode & 0o777) == "0o700"
    # The report only reads: no put ever went out.
    assert net.log[before:] and not any(m == b"put" for _a, m, _t in net.log[before:])
    # A second run joins from the saved node list, without the router.
    code, _text = _run(tmp_path, net, ["--day", day], clock,
                       resolve=lambda host, port: None)
    assert code == 0


def test_report_exits_2_when_it_cant_join(tmp_path):
    net = tc.FakeDHT(seed=46)
    code, _text = _run(tmp_path, net, [], tc.FakeClock(start=tc.NOW),
                       resolve=lambda host, port: None)
    assert code == 2


def test_report_early_in_the_day_points_to_yesterday(tmp_path):
    net = tc.FakeDHT(seed=47)
    clock = tc.FakeClock(start=tc.NOW - 7 * 3600)        # 01:00 UTC
    code, text = _run(tmp_path, net, ["--slots", "2"], clock)
    assert code == 0 and "No reports found." in text
    assert "run with --day 2027-01-14 for yesterday's full count" in text
