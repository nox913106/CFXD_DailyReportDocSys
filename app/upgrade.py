"""系統設定頁「升級到最新版」按鈕。

網頁服務沒有 root 權限，不能自己升級。按下按鈕只會在 instance/upgrade_request/ 寫一個要求檔；
主機上以 root 執行的 dailyreport-upgrade.path 看到要求檔，就執行與主機指令
`bootstrap.sh upgrade` 相同的升級（升級前備份 → 更新程式 → 相依套件 → 重新啟動服務 → 健康檢查）。
要求檔的內容不會被採用（只看有沒有），升級來源固定是安裝時設定的 GitHub 分支。
進度與記錄由 root 寫在網頁服務不能寫入的資料夾（預設 /var/lib/dailyreport-upgrade），網頁只讀取。
"""

import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime

from flask import current_app

CHANGELOG_URL = "https://raw.githubusercontent.com/nox913106/CFXD_DailyReportDocSys/main/CHANGELOG.md"
UPGRADE_CMD = ("curl -fsSL https://raw.githubusercontent.com/nox913106/CFXD_DailyReportDocSys/main/scripts/bootstrap.sh"
               " | sudo bash -s -- upgrade")
_cache = {}


def state_dir():
    return current_app.config.get("UPGRADE_STATE_DIR") or os.environ.get("UPGRADE_STATE_DIR") or "/var/lib/dailyreport-upgrade"


def request_path():
    return os.path.join(current_app.instance_path, "upgrade_request", "request.json")


def available():
    """主機有沒有裝好升級觸發器（v4.43.0 以後的版本用主機指令升級一次才會有）。"""
    try:
        r = subprocess.run(["systemctl", "is-active", "dailyreport-upgrade.path"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "active"
    except (OSError, subprocess.TimeoutExpired):
        return False


def status():
    """{state: running/done/error, msg, from, to, at, pending, log}；從來沒升級過時 state 為空。"""
    st = {}
    try:
        with open(os.path.join(state_dir(), "status.json"), encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        pass
    st["pending"] = os.path.exists(request_path())
    st["log"] = log_tail()
    return st


def busy():
    st = status()
    return st["pending"] or st.get("state") == "running"


def log_tail(lines=60):
    try:
        with open(os.path.join(state_dir(), "upgrade.log"), encoding="utf-8", errors="replace") as fh:
            text = fh.read()[-20000:]
    except OSError:
        return ""
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return "\n".join(text.splitlines()[-lines:])


def request(user):
    os.makedirs(os.path.dirname(request_path()), exist_ok=True)
    tmp = request_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"by": user.username, "at": datetime.now().isoformat(timespec="seconds")}, fh)
    os.replace(tmp, request_path())


def _cached(key, fn, ttl=300):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def remote_head():
    """GitHub main 最新的 commit（無法連線時為空字串）。"""
    from .backup import app_dir

    def get():
        try:
            r = subprocess.run(["git", "-c", f"safe.directory={app_dir()}", "-C", app_dir(), "ls-remote", "origin",
                                "refs/heads/main"], capture_output=True, text=True, timeout=15)
            return r.stdout.split()[0] if r.returncode == 0 and r.stdout.strip() else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""
    return _cached("head", get)


def _ver(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def new_versions(current):
    """GitHub 上比目前新的版本：[{version, date, type, text}]（新到舊）；無法連線時回傳 None。"""
    def get():
        try:
            with urllib.request.urlopen(CHANGELOG_URL, timeout=8) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            return None
    text = _cached("changelog", get)
    if text is None:
        return None
    rows = []
    for line in text.splitlines():
        m = re.match(r"^\|\s*(v\d+\.\d+\.\d+)\s*\|\s*([^|]*)\|\s*([^|]*)\|\s*([^|]*)\|\s*(.*)\|\s*$", line)
        if m:
            v, d, _, t, body = (x.strip() for x in m.groups())
            if not current or _ver(v) > _ver(current):
                rows.append({"version": v, "date": d, "type": t, "text": body})
    return rows
