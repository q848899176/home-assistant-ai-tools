"""仅管理本项目的独立服务，停服不删除模型。"""

import argparse
import json
import os
import platform
import plistlib
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from time import sleep as _restart_delay

LABEL = "com.ha-ai-tools.speech"


def launchctl_stop_command():
    return ["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"]


def launchctl_start(plist_path):
    subprocess.run(launchctl_stop_command(), capture_output=True)
    command = ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)]
    for attempt in range(12):
        result = subprocess.run(command, capture_output=True)
        if result.returncode == 0:
            return
        # bootout can return while launchd still tears down the old process.
        if result.returncode != 5 or attempt == 11:
            raise SystemExit("专用语音服务启动失败，请检查私有服务日志。")
        _restart_delay(.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["preflight", "install", "start", "status", "stop"])
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text())
    if args.command == "preflight":
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise SystemExit("此服务仅在 Apple Silicon Mac 部署")
        with socket.socket() as sock:
            sock.bind((cfg["bind_host"], cfg["port"]))
        print(json.dumps({"platform": "darwin-arm64", "python": platform.python_version(),
                          "free_gib": round(shutil.disk_usage(args.config.parent).free / 2**30, 1),
                          "port_available": True}))
    elif args.command == "install":
        subprocess.run(["uv", "sync", "--project", str(Path(__file__).parents[1]), "--extra", "inference", "--group", "dev", "--locked"], check=True)
    elif args.command == "start":
        from ha_ai_tools_speech.config import load_settings

        settings = load_settings(args.config)
        if not settings.models:
            raise SystemExit("请先安装并配置语音模型")
        args.config.chmod(0o600)
        plist_path = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        log = args.config.parent / "speech-service.log"
        log.touch(mode=0o600, exist_ok=True)
        plist_path.write_bytes(plistlib.dumps({"Label": LABEL,
            "ProgramArguments": [sys.executable, "-m", "ha_ai_tools_speech", "--config", str(args.config.resolve())],
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 15,
            "StandardOutPath": str(log), "StandardErrorPath": str(log),
            "EnvironmentVariables": {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false"}}))
        plist_path.chmod(0o600)
        launchctl_start(plist_path)
        print("语音服务已启动；模型加载完成前状态为未就绪。")
    elif args.command == "stop":
        subprocess.run(launchctl_stop_command(), capture_output=True)
        print("已停止本项目语音服务，保留配置、模型与缓存。")
    else:
        import httpx

        try:
            r = httpx.get(f'http://{cfg["bind_host"]}:{cfg["port"]}/health', headers={"Authorization": "Bearer " + cfg["api_key"]}, timeout=3, trust_env=False)
            r.raise_for_status()
            print(json.dumps(r.json(), ensure_ascii=False))
        except httpx.HTTPError:
            print(json.dumps({"state": "unavailable"}))


if __name__ == "__main__":
    main()
