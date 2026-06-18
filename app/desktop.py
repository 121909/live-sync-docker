#!/usr/bin/env python3
import os
import shutil
import sys
import threading
import time
import webbrowser
from pathlib import Path


def app_root():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = app_root()
DATA_DIR = ROOT / "data"
BIN_DIR = ROOT / "bin"


def configure_environment():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "state").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "hls").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "work").mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("LIVE_SYNC_RUNTIME_DIR", str(DATA_DIR))
    os.environ.setdefault("LIVE_SYNC_STATE_DIR", str(DATA_DIR / "state"))
    os.environ.setdefault("HLS_DIR", str(DATA_DIR / "hls"))
    os.environ.setdefault("WORK_DIR", str(DATA_DIR / "work"))
    os.environ.setdefault("PORT", "18080")

    if BIN_DIR.exists():
        os.environ["PATH"] = str(BIN_DIR) + os.pathsep + os.environ.get("PATH", "")


def dependency_status():
    missing = []
    for command in ("ffmpeg",):
        if shutil.which(command) is None:
            missing.append(command)
    return missing


def open_browser_later(port):
    def worker():
        time.sleep(1.5)
        webbrowser.open(f"http://127.0.0.1:{port}/")

    threading.Thread(target=worker, daemon=True).start()


def main():
    configure_environment()
    missing = dependency_status()
    if missing:
        print("缺少运行依赖：" + ", ".join(missing), file=sys.stderr)
        print("请把 ffmpeg 放到软件目录的 bin 文件夹，或安装到系统 PATH。", file=sys.stderr)
        input("按回车退出...")
        return 2

    port = int(os.environ.get("PORT", "18080"))
    open_browser_later(port)

    from app.server import main as server_main

    server_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
