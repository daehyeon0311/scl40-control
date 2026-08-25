#!/usr/bin/env python3
"""First-run Windows launcher for the packaged SCL-40 dashboard."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from scl40_gui import main as run_dashboard


APP_NAME = "SCL40-Control"
DEFAULTS = {
    "scl40_host": "192.168.200.99",
    "dashboard_port": 8765,
    "allow_lan": True,
    "enable_control": True,
    "pressure_ceiling_mpa": 10.0,
}


def settings_dir() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def settings_path() -> Path:
    return settings_dir() / "settings.json"


def load_settings() -> dict[str, object] | None:
    path = settings_path()
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            return None
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return {**DEFAULTS, **loaded}


def save_settings(settings: dict[str, object]) -> None:
    settings_path().write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def configure(current: dict[str, object] | None = None) -> dict[str, object] | None:
    import tkinter as tk
    from tkinter import messagebox, ttk

    values = {**DEFAULTS, **(current or {})}
    root = tk.Tk()
    root.title("SCL-40 Control · 처음 설정")
    root.resizable(False, False)

    frame = ttk.Frame(root, padding=22)
    frame.grid(sticky="nsew")
    ttk.Label(frame, text="SCL-40 Control", font=("Segoe UI", 17, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w"
    )
    ttk.Label(
        frame,
        text="처음 한 번만 장비 주소를 확인하면 다음부터는 바로 실행됩니다.",
        foreground="#52606d",
    ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 18))

    host_var = tk.StringVar(value=str(values["scl40_host"]))
    lan_var = tk.BooleanVar(value=bool(values["allow_lan"]))
    control_var = tk.BooleanVar(value=bool(values["enable_control"]))

    ttk.Label(frame, text="SCL-40 IP 주소").grid(row=2, column=0, sticky="w", pady=6)
    host_entry = ttk.Entry(frame, textvariable=host_var, width=28)
    host_entry.grid(row=2, column=1, sticky="ew", pady=6)
    ttk.Checkbutton(
        frame,
        text="같은 네트워크의 다른 PC에서도 접속 허용",
        variable=lan_var,
    ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 4))
    ttk.Checkbutton(
        frame,
        text="확인된 펌프 제어 기능 활성화",
        variable=control_var,
    ).grid(row=4, column=0, columnspan=2, sticky="w", pady=4)
    ttk.Label(
        frame,
        text="Windows 방화벽 확인창이 뜨면 사설 네트워크만 허용하세요.",
        foreground="#8a5a00",
    ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 18))

    result: dict[str, object] = {}

    def start() -> None:
        host = host_var.get().strip()
        if not host or any(char.isspace() for char in host):
            messagebox.showerror("주소 확인", "올바른 SCL-40 IP 주소를 입력하세요.", parent=root)
            return
        result.update({
            **values,
            "scl40_host": host,
            "allow_lan": lan_var.get(),
            "enable_control": control_var.get(),
        })
        save_settings(result)
        root.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(row=6, column=0, columnspan=2, sticky="e")
    ttk.Button(buttons, text="취소", command=root.destroy).pack(side="left", padx=(0, 8))
    ttk.Button(buttons, text="저장하고 시작", command=start).pack(side="left")

    root.protocol("WM_DELETE_WINDOW", root.destroy)
    host_entry.focus_set()
    host_entry.selection_range(0, "end")
    root.update_idletasks()
    x = max(0, (root.winfo_screenwidth() - root.winfo_width()) // 2)
    y = max(0, (root.winfo_screenheight() - root.winfo_height()) // 3)
    root.geometry(f"+{x}+{y}")
    root.mainloop()
    return result or None


def dashboard_is_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/info", timeout=0.7) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def show_error(message: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, "SCL-40 Control", 0x10)
    except Exception:
        print(message, file=sys.stderr)


def main() -> int:
    force_setup = "--setup" in sys.argv[1:]
    settings = load_settings()
    if force_setup or settings is None:
        settings = configure(settings)
        if settings is None:
            return 0

    port = int(settings.get("dashboard_port", 8765))
    url = f"http://127.0.0.1:{port}/"
    if dashboard_is_running(port):
        webbrowser.open(url)
        return 0

    argv = [
        str(settings["scl40_host"]),
        "--port", str(port),
        "--bind", "0.0.0.0" if settings.get("allow_lan") else "127.0.0.1",
        "--pressure-ceiling", str(settings.get("pressure_ceiling_mpa", 10.0)),
    ]
    if settings.get("enable_control"):
        argv.append("--enable-control")

    try:
        return run_dashboard(argv)
    except Exception as exc:
        show_error(
            "대시보드를 시작하지 못했습니다.\n\n"
            f"{exc}\n\n"
            f"설정 파일: {settings_path()}"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
