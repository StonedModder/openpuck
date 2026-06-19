from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable


LogFn = Callable[[str], None]


class CommandError(RuntimeError):
    pass


class CommandRunner:
    def _startup_kwargs(self) -> dict:
        if os.name != "nt":
            return {}
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return {
            "startupinfo": startupinfo,
            "creationflags": creationflags,
        }

    def run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        log: LogFn | None = None,
    ) -> int:
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        if log:
            log(f"$ {' '.join(args)}")
        proc = subprocess.Popen(
            args,
            cwd=str(cwd) if cwd else None,
            env=merged_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **self._startup_kwargs(),
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            if log:
                log(line.rstrip())
        code = proc.wait()
        if code != 0:
            raise CommandError(f"Command failed with exit code {code}: {' '.join(args)}")
        return code

    def capture(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        completed = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            env=merged_env,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **self._startup_kwargs(),
        )
        return completed.stdout
