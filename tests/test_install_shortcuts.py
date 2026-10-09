"""Steam library result handling in the production installer caller."""

from frameport.core.events import Reporter
from frameport.install import installer


class FakeFrame:
    def __init__(self, status):
        self.status = status
        self.calls = []

    def agent(self, command, timeout=None, **args):
        self.calls.append((command, timeout, args))
        if command == "shortcuts":
            return {"started": True}
        if command == "shortcut_status":
            return self.status
        raise AssertionError(f"unexpected agent command: {command}")


class ControlledClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def run_add(monkeypatch, status, packages):
    clock = ControlledClock()
    monkeypatch.setattr(installer.time, "time", clock.time)
    monkeypatch.setattr(installer.time, "sleep", clock.sleep)
    frame = FakeFrame(status)
    reporter = Reporter()
    checks = []
    reporter.subscribe(lambda ev: ev.kind == "check" and checks.append(
        (ev.data["name"], ev.data["ok"], ev.message)))
    result = installer.add_to_steam(frame, packages, reporter, wait=600)
    return result, frame, checks, clock


def test_partial_shortcut_result_reports_confirmed_pending_and_failed_packages(monkeypatch):
    packages = ["com.game.added", "com.game.pending", "com.game.failed"]
    status = {
        "state": "partial",
        "added": [{"package": "com.game.added", "appid": 1234}],
        "deferred": ["com.game.pending"],
        "errors": ["com.game.pending: devkit fallback deferred: Steam pipe has no reader",
                   "com.game.failed: devkit fallback failed: malformed game id"],
    }

    result, frame, checks, clock = run_add(monkeypatch, status, packages)

    assert result is status
    assert [call[0] for call in frame.calls] == ["shortcuts", "shortcut_status"]
    assert clock.sleeps == [3]
    assert checks == [
        ("Steam library: com.game.added", True, "shortcut 1234"),
        ("Steam library: com.game.pending", None,
         "pending automatic retry when Steam's command pipe is ready"),
        ("Steam library: com.game.failed", False,
         "com.game.failed: devkit fallback failed: malformed game id"),
    ]


def test_deferred_shortcut_result_is_pending_without_timeout_polling(monkeypatch):
    packages = ["com.game.one", "com.game.two"]
    status = {"state": "deferred", "deferred": packages,
              "errors": [f"{pkg}: Steam command pipe has no reader" for pkg in packages]}

    _result, frame, checks, clock = run_add(monkeypatch, status, packages)

    assert [call[0] for call in frame.calls] == ["shortcuts", "shortcut_status"]
    assert clock.sleeps == [3]
    assert checks == [
        (f"Steam library: {pkg}", None, "pending automatic retry when Steam's command pipe is ready")
        for pkg in packages
    ]


def test_done_shortcut_result_distinguishes_devkit_and_unconfirmed_entry(monkeypatch):
    packages = ["com.game.devkit", "com.game.missing"]
    status = {"state": "done", "added": [{"package": "com.game.devkit", "appid": 9876, "via": "devkit"}],
              "errors": []}

    _result, frame, checks, clock = run_add(monkeypatch, status, packages)

    assert [call[0] for call in frame.calls] == ["shortcuts", "shortcut_status"]
    assert clock.sleeps == [3]
    assert checks == [
        ("Steam library: com.game.devkit", True, "devkit entry 9876"),
        ("Steam library: com.game.missing", False, "the Frame didn't confirm the library entry in time"),
    ]
