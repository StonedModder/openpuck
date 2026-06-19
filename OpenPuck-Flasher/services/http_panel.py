from __future__ import annotations

import subprocess
import webbrowser
from pathlib import Path


class LocalPanelServer:
    def __init__(self) -> None:
        self.process: subprocess.Popen[str] | None = None

    def start(self, python_cmd: str, docs_dir: Path, port: int) -> str:
        if self.process and self.process.poll() is None:
            url = f"http://localhost:{port}"
            webbrowser.open(url)
            return url
        startup_kwargs = {}
        if subprocess.os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup_kwargs = {
                "startupinfo": startupinfo,
                "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
            }
        self.process = subprocess.Popen(
            [python_cmd, "-m", "http.server", str(port)],
            cwd=str(docs_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            **startup_kwargs,
        )
        url = f"http://localhost:{port}"
        webbrowser.open(url)
        return url

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
        self.process = None
