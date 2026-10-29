"""Start the CV server hidden at login, and put the page on the desktop.

    cvgen service install     register autostart, start the server now, copy the page to the desktop
    cvgen service uninstall   remove autostart, stop the server, remove the desktop copy
    cvgen service status      is it registered, is it running

Windows: a shortcut in the Startup folder that runs pythonw (no console window, no admin rights
needed; a Task Scheduler logon task usually needs elevation). macOS: a LaunchAgent. Linux: a
systemd user service. The server logs to logs/server.log and reads ANTHROPIC_API_KEY from .env.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from cvgen.server import DEFAULT_PORT, LOG_FILE, REPO_ROOT, TOKEN_HEADER, configured_port, load_token, page_html

NAME = "CV Generator server"
LABEL = "com.cvgen.server"  # macOS LaunchAgent label
UNIT = "cvgen-server.service"  # Linux systemd user unit
PAGE_NAME = "CV Generator.html"
PAGE_MARKER = "/*CVGEN-CONFIG*/"


def server_command(python: str | None = None) -> list[str]:
    """The command autostart runs: the windowless interpreter on Windows."""
    python = python or sys.executable
    if sys.platform == "win32":
        pythonw = Path(python).with_name("pythonw.exe")
        python = str(pythonw if pythonw.exists() else python)
    return [python, "-m", "cvgen.server", "--log-file", str(LOG_FILE)]


# ---------------------------
# PER-OS AUTOSTART
# ---------------------------
def windows_startup_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def windows_shortcut_script(shortcut: Path, command: list[str], workdir: Path) -> str:
    def ps(s):  # single-quoted PowerShell string
        return "'" + str(s).replace("'", "''") + "'"
    args = subprocess.list2cmdline(command[1:])
    return (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({ps(shortcut)}); "
            f"$s.TargetPath = {ps(command[0])}; $s.Arguments = {ps(args)}; $s.WorkingDirectory = {ps(workdir)}; "
            f"$s.WindowStyle = 7; $s.Description = {ps(NAME)}; $s.Save()")


def launch_agent_plist(command: list[str], workdir: Path, log_file: Path) -> str:
    args = "\n".join(f"        <string>{a}</string>" for a in command)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args}
    </array>
    <key>WorkingDirectory</key>
    <string>{workdir}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardErrorPath</key>
    <string>{log_file}</string>
</dict>
</plist>
"""


def systemd_unit(command: list[str], workdir: Path) -> str:
    return f"""[Unit]
Description={NAME}

[Service]
WorkingDirectory={workdir}
ExecStart={" ".join(_sh(a) for a in command)}
Restart=on-failure

[Install]
WantedBy=default.target
"""


def _sh(arg: str) -> str:
    return arg if all(c.isalnum() or c in "/._-=:" for c in arg) else "'" + arg.replace("'", "'\\''") + "'"


def autostart_path() -> Path:
    if sys.platform == "win32":
        return windows_startup_dir() / f"{NAME}.lnk"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    return Path.home() / ".config" / "systemd" / "user" / UNIT


def register(run=subprocess.run) -> Path:
    command, path = server_command(), autostart_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        run(["powershell", "-NoProfile", "-NonInteractive", "-Command", windows_shortcut_script(path, command, REPO_ROOT)],
            check=True, capture_output=True)
    elif sys.platform == "darwin":
        path.write_text(launch_agent_plist(command, REPO_ROOT, LOG_FILE), encoding="utf-8")
        run(["launchctl", "unload", str(path)], capture_output=True)
        run(["launchctl", "load", "-w", str(path)], check=True)  # RunAtLoad also starts it now
    else:
        path.write_text(systemd_unit(command, REPO_ROOT), encoding="utf-8")
        run(["systemctl", "--user", "daemon-reload"], check=True)
        run(["systemctl", "--user", "enable", "--now", UNIT], check=True)
    return path


def unregister(run=subprocess.run) -> bool:
    path = autostart_path()
    existed = path.exists()
    if sys.platform == "darwin" and existed:
        run(["launchctl", "unload", "-w", str(path)], capture_output=True)
    elif sys.platform not in ("win32", "darwin"):
        run(["systemctl", "--user", "disable", "--now", UNIT], capture_output=True)
    if existed:
        path.unlink()
    if sys.platform not in ("win32", "darwin"):
        run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return existed


# ---------------------------
# RUNNING SERVER
# ---------------------------
def is_running(port: int, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as r:
            return json.loads(r.read()).get("ok") is True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def start_now(port: int) -> bool:
    """Start the server in the background now (Windows; launchd/systemd start it themselves)."""
    if is_running(port):
        return True
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        subprocess.Popen(server_command(), cwd=REPO_ROOT, creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        if is_running(port):
            return True
        time.sleep(0.5)
    return False


def stop(port: int) -> bool:
    if not is_running(port):
        return False
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown", data=b"{}", method="POST", headers={
        "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}", TOKEN_HEADER: load_token()})
    try:
        urllib.request.urlopen(request, timeout=5).read()
    except (urllib.error.URLError, OSError):
        pass
    for _ in range(20):
        if not is_running(port, timeout=0.5):
            return True
        time.sleep(0.25)
    return False


# ---------------------------
# DESKTOP PAGE
# ---------------------------
def desktop_dir() -> Path:
    if sys.platform == "win32":
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                  "[Environment]::GetFolderPath('Desktop')"], capture_output=True, text=True, check=True)
            if out.stdout.strip():
                return Path(out.stdout.strip())  # follows OneDrive redirection
        except (OSError, subprocess.CalledProcessError):
            pass
    elif shutil.which("xdg-user-dir"):
        out = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True)
        if out.stdout.strip():
            return Path(out.stdout.strip())
    return Path.home() / "Desktop"


def copy_page(port: int, desktop: Path | None = None) -> Path:
    desktop = desktop or desktop_dir()
    desktop.mkdir(parents=True, exist_ok=True)
    target = desktop / PAGE_NAME
    target.write_text(page_html(port, load_token()), encoding="utf-8")
    return target


def remove_page(desktop: Path | None = None) -> Path | None:
    target = (desktop or desktop_dir()) / PAGE_NAME
    if target.is_file() and PAGE_MARKER in target.read_text(encoding="utf-8", errors="ignore"):
        target.unlink()
        return target
    return None


# ---------------------------
# COMMANDS
# ---------------------------
def install() -> list[str]:
    port = configured_port()
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    load_token()
    lines = [f"Autostart registered: {register()}"]
    running = start_now(port)
    lines.append(f"Server running at http://127.0.0.1:{port}/" if running
                 else f"Server didn't answer yet; see {LOG_FILE}")
    lines.append(f"Page copied to: {copy_page(port)}")
    lines.append(f"Logs: {LOG_FILE}")
    return lines


def uninstall() -> list[str]:
    port = configured_port()
    lines = ["Autostart removed: " + str(autostart_path()) if unregister() else "Autostart was not registered."]
    lines.append("Server stopped." if stop(port) else "Server was not running.")
    page = remove_page()
    lines.append(f"Removed {page}" if page else "No desktop page to remove.")
    return lines


def status() -> list[str]:
    port = configured_port()
    path = autostart_path()
    return [f"Autostart: {'registered (' + str(path) + ')' if path.exists() else 'not registered'}",
            f"Server: {'running at http://127.0.0.1:%d/' % port if is_running(port) else 'not running'}",
            f"Logs: {LOG_FILE}"]
