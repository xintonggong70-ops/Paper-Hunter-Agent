"""跨平台完成通知（无第三方依赖，尽力而为，失败静默）。"""
from __future__ import annotations

import logging
import platform
import shutil
import subprocess

logger = logging.getLogger(__name__)


def notify_done(title: str, message: str) -> None:
    """桌面通知 + 终端响铃。任何一步失败都只 log，不影响主流程。"""
    # 终端响铃（几乎所有终端都支持）
    try:
        print("\a", end="", flush=True)
    except Exception:  # noqa: BLE001
        pass

    system = platform.system()
    try:
        if system == "Darwin":  # macOS
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{message}" with title "{title}"'],
                check=False, timeout=10,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif system == "Linux" and shutil.which("notify-send"):
            subprocess.run(["notify-send", title, message],
                           check=False, timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif system == "Windows":
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$n = New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon = [System.Drawing.SystemIcons]::Information;"
                f"$n.BalloonTipTitle = '{title}';"
                f"$n.BalloonTipText = '{message}';"
                "$n.Visible = $true; $n.ShowBalloonTip(8000);"
                "Start-Sleep -Seconds 9; $n.Dispose()"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           check=False, timeout=20,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            logger.debug("当前平台无可用桌面通知方式（%s）", system)
    except Exception as exc:  # noqa: BLE001
        logger.debug("桌面通知失败（忽略）：%s", exc)
