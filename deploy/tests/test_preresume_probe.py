"""RT-927 Task 3: the pre-resume probe (Frappe-free; the context is faked)."""

import re

import preresume_probe as probe
import pytest

LINE_RE = re.compile(r"^RT_PROBE [a-z0-9_]+=(pass|fail)$")


class FakeContext:
    def __init__(self, status="OK", guard=True, version="0.3.0", tick=60):
        self._status, self._guard, self._version, self._tick = status, guard, version, tick
        self.closed = False

    def compatibility_status(self) -> str:
        return self._status

    def index_guard_ok(self) -> bool:
        return self._guard

    def connector_version(self) -> str:
        return self._version

    def scheduler_tick_interval(self) -> int | None:
        return self._tick

    def close(self) -> None:
        self.closed = True


def _by_name(checks):
    return {c.name: c.ok for c in checks}


def test_all_ok_gives_four_passes_in_order():
    checks = probe.evaluate(FakeContext(), "0.3.0")
    assert [c.name for c in checks] == ["compatibility", "g5", "version", "tick"]
    assert all(c.ok for c in checks)


@pytest.mark.parametrize("status", ["DEGRADED", "WARNING", "CRITICAL", "", None])
def test_only_ok_status_passes(status):
    assert _by_name(probe.evaluate(FakeContext(status=status), "0.3.0"))["compatibility"] is False


def test_guard_false_fails_g5():
    assert _by_name(probe.evaluate(FakeContext(guard=False), "0.3.0"))["g5"] is False


def test_version_mismatch_fails_version():
    result = _by_name(probe.evaluate(FakeContext(version="0.2.0"), "0.3.0"))
    assert result["version"] is False and result["compatibility"] is True


@pytest.mark.parametrize("tick", [None, 240, 0, 59])
def test_tick_must_be_sixty(tick):
    assert _by_name(probe.evaluate(FakeContext(tick=tick), "0.3.0"))["tick"] is False


def test_a_raising_check_fails_closed_and_does_not_stop_the_others():
    class Broken(FakeContext):
        def index_guard_ok(self) -> bool:
            raise RuntimeError("db is down")

    result = _by_name(probe.evaluate(Broken(), "0.3.0"))
    assert result == {"compatibility": True, "g5": False, "version": True, "tick": True}


def test_main_output_is_rt_lines_only(monkeypatch, capsys):
    ctx = FakeContext(guard=False)
    monkeypatch.setattr(probe, "_frappe_context", lambda site: ctx)
    assert probe.main(["--site", "erp.lab", "--expected-version", "0.3.0"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out and all(LINE_RE.match(line) for line in out), out
    assert "RT_PROBE g5=fail" in out and ctx.closed


def test_main_exit_0_when_all_pass(monkeypatch, capsys):
    monkeypatch.setattr(probe, "_frappe_context", lambda site: FakeContext())
    assert probe.main(["--site", "erp.lab", "--expected-version", "0.3.0"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "RT_PROBE compatibility=pass", "RT_PROBE g5=pass", "RT_PROBE version=pass", "RT_PROBE tick=pass",
    ]


def test_main_context_failure_is_one_fail_line_and_no_message(monkeypatch, capsys):
    def boom(site):
        raise RuntimeError("password=hunter2 refused")

    monkeypatch.setattr(probe, "_frappe_context", boom)
    assert probe.main(["--site", "erp.lab", "--expected-version", "0.3.0"]) == 1
    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["RT_PROBE context=fail"]
    assert "hunter2" not in captured.out + captured.err


@pytest.mark.parametrize("argv", [
    ["--site", "bad site", "--expected-version", "0.3.0"],
    ["--site", "erp.lab", "--expected-version", "v0.3"],
    ["--site", "../x", "--expected-version", "0.3.0"],
])
def test_main_rejects_bad_arguments(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        probe.main(argv)
    assert exc.value.code == 2
    assert capsys.readouterr().out == ""
