"""Autostart registration content and the desktop page copy, without touching the real system."""

import sys
from pathlib import Path, PurePosixPath

import pytest

from cvgen import server, service


def test_server_command_uses_pythonw_on_windows(monkeypatch, tmp_path):
    python = tmp_path / "python.exe"
    (tmp_path / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "platform", "win32")
    cmd = service.server_command(str(python))
    assert cmd[:3] == [str(tmp_path / "pythonw.exe"), "-m", "cvgen.server"]
    assert cmd[3:] == ["--log-file", str(server.LOG_FILE)]


def test_windows_shortcut_script_quotes_paths():
    script = service.windows_shortcut_script(Path(r"C:\Users\O'Neil\Startup\CV Generator server.lnk"),
                                             [r"C:\Py\pythonw.exe", "-m", "cvgen.server", "--log-file", r"D:\repo dir\logs\server.log"],
                                             Path(r"D:\repo dir"))
    assert "'C:\\Users\\O''Neil\\Startup\\CV Generator server.lnk'" in script  # PowerShell-escaped quote
    assert "$s.TargetPath = 'C:\\Py\\pythonw.exe'" in script
    assert '-m cvgen.server --log-file "D:\\repo dir\\logs\\server.log"' in script
    assert "$s.WindowStyle = 7" in script and "$s.WorkingDirectory = 'D:\\repo dir'" in script


def test_launch_agent_plist():
    plist = service.launch_agent_plist(["/usr/bin/python3", "-m", "cvgen.server", "--log-file", "/repo/logs/server.log"],
                                       PurePosixPath("/repo"), PurePosixPath("/repo/logs/server.log"))
    assert "<string>com.cvgen.server</string>" in plist and "<key>RunAtLoad</key>\n    <true/>" in plist
    assert "<string>-m</string>" in plist and "<string>/repo</string>" in plist


def test_systemd_unit(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    unit = service.systemd_unit(["/usr/bin/python3", "-m", "cvgen.server", "--log-file", "/home/a b/logs/server.log"],
                                PurePosixPath("/home/a b/repo"))
    assert "ExecStart=/usr/bin/python3 -m cvgen.server --log-file '/home/a b/logs/server.log'" in unit
    assert "WantedBy=default.target" in unit and "WorkingDirectory=/home/a b/repo" in unit


def test_autostart_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "win32")
    assert service.autostart_path().name == "CV Generator server.lnk" and "Startup" in str(service.autostart_path())
    monkeypatch.setattr(sys, "platform", "darwin")
    assert service.autostart_path().name == "com.cvgen.server.plist"
    monkeypatch.setattr(sys, "platform", "linux")
    assert service.autostart_path().name == "cvgen-server.service"


def test_register_linux_uses_systemctl(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(service.Path, "home", classmethod(lambda cls: tmp_path))
    calls = []
    path = service.register(run=lambda cmd, **kw: calls.append(cmd))
    assert path == tmp_path / ".config/systemd/user/cvgen-server.service" and path.is_file()
    assert ["systemctl", "--user", "enable", "--now", "cvgen-server.service"] in calls
    assert service.unregister(run=lambda cmd, **kw: calls.append(cmd)) and not path.exists()


def test_desktop_copy_has_token_and_port_and_is_removed(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "load_token", lambda: "tok-xyz")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-secret-value-123")
    page = service.copy_page(8765, desktop=tmp_path)
    text = page.read_text(encoding="utf-8")
    assert page.name == "CV Generator.html" and '"token": "tok-xyz"' in text and '"port": 8765' in text
    assert "sk-ant" not in text
    assert service.remove_page(desktop=tmp_path) == page and not page.exists()
    other = tmp_path / "CV Generator.html"
    other.write_text("<html>someone else's file</html>", encoding="utf-8")
    assert service.remove_page(desktop=tmp_path) is None and other.exists()  # only removes our own page


class FakeServer:
    """Stands in for the running server: is_running/stop/_spawn flip one flag."""

    def __init__(self, monkeypatch, tmp_path, running, autostart=False):
        self.running, self.spawned = running, 0
        monkeypatch.setattr(service, "is_running", lambda port, timeout=1.0: self.running)
        monkeypatch.setattr(service, "stop", self._stop)
        monkeypatch.setattr(service, "_spawn", self._spawn)
        monkeypatch.setattr(service, "load_token", lambda: "tok")
        monkeypatch.setattr(service, "LOG_FILE", tmp_path / "logs" / "server.log")
        shortcut = tmp_path / "autostart.lnk"
        if autostart:
            shortcut.write_text("x")
        monkeypatch.setattr(service, "autostart_path", lambda: shortcut)

    def _stop(self, port):
        was, self.running = self.running, False
        return was

    def _spawn(self):
        self.spawned += 1
        self.running = True


def test_start_spawns_only_when_not_running(monkeypatch, tmp_path):
    fake = FakeServer(monkeypatch, tmp_path, running=False)
    assert service.start()[0].startswith("Server running at http://127.0.0.1:")
    assert fake.spawned == 1
    assert service.start()[0].startswith("Server already running")
    assert fake.spawned == 1


def test_stop_reports_autostart(monkeypatch, tmp_path):
    fake = FakeServer(monkeypatch, tmp_path, running=True, autostart=True)
    lines = service.stop_server()
    assert lines[0] == "Server stopped." and "next login" in lines[1] and not fake.running
    assert service.stop_server() == ["Server was not running."]


def test_stop_without_autostart_says_nothing_about_login(monkeypatch, tmp_path):
    FakeServer(monkeypatch, tmp_path, running=True)
    assert service.stop_server() == ["Server stopped."]


def test_cli_service_start_and_stop(monkeypatch, tmp_path, capsys):
    from cvgen import cli
    FakeServer(monkeypatch, tmp_path, running=False)
    assert cli.main(["service", "start"]) == 0
    assert cli.main(["service", "stop"]) == 0
    out = capsys.readouterr().out
    assert "Server running at" in out and "Server stopped." in out
