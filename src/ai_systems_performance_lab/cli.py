"""Start the local API and browser UI as one supervised development command."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from collections.abc import Sequence
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[2]
UI_ROOT = PROJECT_ROOT / "ui"


def _port(value: str | None, default: int, name: str) -> int:
    try:
        port = int(value) if value else default
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer port") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return port


def _setting(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _port_is_in_use(host: str, port: int) -> bool:
    # A successful TCP connection is the usual case; bind also detects a
    # listener that does not accept connections yet.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.15)
        if probe.connect_ex((host, port)) == 0:
            return True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return True
    return False


def _ensure_ui_dependencies() -> None:
    vite_entry = UI_ROOT / "node_modules" / "vite" / "bin" / "vite.js"
    if vite_entry.is_file():
        return
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise RuntimeError("Node.js and npm are required to prepare the browser UI")
    if not (UI_ROOT / "package-lock.json").is_file():
        raise RuntimeError("ui/package-lock.json is missing; cannot install reproducibly")
    print("Installing browser UI dependencies from ui/package-lock.json (npm ci)...", flush=True)
    subprocess.run([npm, "ci"], cwd=UI_ROOT, check=True)


def _wait_for_url(url: str, process: subprocess.Popen[bytes], label: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{label} exited during startup (exit code {process.returncode})")
        try:
            with urlopen(url, timeout=0.5) as response:
                if response.status < 500:
                    return
        except (OSError, URLError):
            time.sleep(0.2)
    raise RuntimeError(f"{label} did not become ready within 30 seconds: {url}")


def _stop(processes: Sequence[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        if process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main() -> None:
    """Run API and Vite, open the local UI, and stop both on Ctrl+C."""
    api_host = _setting("LAB_API_HOST", "127.0.0.1")
    api_port = _port(os.getenv("LAB_API_PORT"), 8003, "LAB_API_PORT")
    ui_host = _setting("LAB_UI_HOST", "127.0.0.1")
    ui_port = _port(os.getenv("LAB_UI_PORT"), 5174, "LAB_UI_PORT")
    if api_port == ui_port and api_host == ui_host:
        raise RuntimeError("API and UI must use different ports")

    for host, port, label in (
        (api_host, api_port, "API"),
        (ui_host, ui_port, "UI"),
    ):
        if _port_is_in_use(host, port):
            raise RuntimeError(
                f"{label} port {port} on {host} is already in use. "
                "Choose another port in .env; the launcher will not stop an unrelated process."
            )

    _ensure_ui_dependencies()
    node = shutil.which("node.exe") or shutil.which("node")
    vite_entry = UI_ROOT / "node_modules" / "vite" / "bin" / "vite.js"
    if not node or not vite_entry.is_file():
        raise RuntimeError("Could not find Node.js or the Vite executable after dependency setup")

    api_process: subprocess.Popen[bytes] | None = None
    ui_process: subprocess.Popen[bytes] | None = None
    processes: list[subprocess.Popen[bytes]] = []
    try:
        api_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "ai_systems_performance_lab.api.main:app",
                "--host",
                api_host,
                "--port",
                str(api_port),
            ],
            cwd=PROJECT_ROOT,
        )
        processes.append(api_process)
        api_probe_host = "127.0.0.1" if api_host in {"0.0.0.0", "::"} else api_host
        _wait_for_url(f"http://{api_probe_host}:{api_port}/health", api_process, "API")

        ui_process = subprocess.Popen(
            [
                node,
                str(vite_entry),
                "--host",
                ui_host,
                "--port",
                str(ui_port),
                "--strictPort",
            ],
            cwd=UI_ROOT,
        )
        processes.append(ui_process)
        ui_probe_host = "127.0.0.1" if ui_host in {"0.0.0.0", "::"} else ui_host
        ui_url = f"http://{ui_probe_host}:{ui_port}/"
        _wait_for_url(ui_url, ui_process, "UI")

        print(f"\nAPI ready: http://{api_probe_host}:{api_port}/health", flush=True)
        print(f"UI ready:  {ui_url}", flush=True)
        print("Press Ctrl+C here to stop both services. No AWS calls are made until you submit a Bedrock agent turn.", flush=True)
        webbrowser.open(ui_url)
        while True:
            for process, label in ((api_process, "API"), (ui_process, "UI")):
                if process.poll() is not None:
                    raise RuntimeError(f"{label} exited (exit code {process.returncode})")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping API and UI...", flush=True)
    finally:
        _stop(processes)


if __name__ == "__main__":
    main()
