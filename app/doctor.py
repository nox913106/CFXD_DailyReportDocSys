"""部署健檢（應用程式內部的部分）：由 `flask --app wsgi doctor-json` 輸出 JSON，
scripts/dailyreport.sh doctor 再加上系統、服務、網路的檢查一起顯示。

每一項：group, name, status（ok / warn / fail）, detail, fix（照著做就能修好的指令或步驟）。
"""

import os
import shutil
import subprocess
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import text

from . import db

DEFAULT_SECRET = ("dev-only-change-me", "請改成一段很長的隨機字串")
DIR = os.environ.get("DR_DIR", "/opt/dailyreport")


def _item(out, group, name, status, detail="", fix=""):
    out.append({"group": group, "name": name, "status": status, "detail": detail, "fix": fix})


def run():
    from . import ai_providers as prov
    from .models import AIProvider, CheckReport, Report, ReportImage, User

    out = []
    cfg = current_app.config

    # ---------------------------------------------------------------- 設定
    g = "設定"
    sk = cfg.get("SECRET_KEY") or ""
    if sk in DEFAULT_SECRET or len(sk) < 32:
        _item(out, g, "SECRET_KEY", "fail", "使用預設值或太短",
              f"在 {DIR}/.env 設定 SECRET_KEY=$(openssl rand -hex 32)，再執行 sudo bash {DIR}/scripts/dailyreport.sh restart")
    else:
        _item(out, g, "SECRET_KEY", "ok", f"{len(sk)} 字元")
    mode = cfg.get("AUTH_MODE")
    if mode != "ldap":
        _item(out, g, "驗證模式", "fail", f"AUTH_MODE={mode}（任何帳號都能登入）", f"在 {DIR}/.env 設定 AUTH_MODE=ldap")
    else:
        _item(out, g, "驗證模式", "ok", "AD（LDAP）")
    url = cfg.get("LDAP_URL") or ""
    if mode == "ldap" and (not url or "example" in url or not cfg.get("AD_DOMAIN") or "example" in cfg.get("AD_DOMAIN", "")):
        _item(out, g, "AD 設定", "warn", "LDAP_URL / AD_DOMAIN 尚未設定，AD 帳號無法登入（本機緊急帳號仍可登入）",
              f"編輯 {DIR}/.env 的 LDAP_URL、AD_DOMAIN、LDAP_BASE_DN，再執行 sudo bash {DIR}/scripts/dailyreport.sh restart")
    elif mode == "ldap":
        _item(out, g, "AD 設定", "ok", f"{url}（網域 {cfg.get('AD_DOMAIN')}）")
        _ldap_reach(out, g, url)
    if cfg.get("SESSION_COOKIE_SECURE"):
        _item(out, g, "Cookie", "ok", "只經 HTTPS 傳送")
    else:
        _item(out, g, "Cookie", "warn", "COOKIE_SECURE 未開啟（以 HTTP 部署時正常）",
              f"使用 HTTPS 時在 {DIR}/.env 設定 COOKIE_SECURE=1")

    # ---------------------------------------------------------------- 資料庫
    g = "資料庫"
    try:
        ok = db.session.execute(text("PRAGMA integrity_check")).scalar()
        mode_j = db.session.execute(text("PRAGMA journal_mode")).scalar()
        path = db.engine.url.database or ""
        size = os.path.getsize(path) / 1e6 if path and os.path.exists(path) else 0
        _item(out, g, "完整性", "ok" if ok == "ok" else "fail", f"{ok}（{path}，{size:.1f} MB）",
              "" if ok == "ok" else f"從備份還原：sudo bash {DIR}/scripts/dailyreport.sh restore <備份資料夾>")
        _item(out, g, "WAL 模式", "ok" if mode_j == "wal" else "warn", mode_j,
              "" if mode_j == "wal" else "重新啟動服務後會自動切換")
        n_reports = Report.query.count()
        n_users = User.query.count()
        admins = [u for u in User.query.all() if u.is_admin]
        local = [u for u in admins if u.is_local_admin]
        _item(out, g, "資料", "ok", f"日報 {n_reports} 份、帳號 {n_users} 個")
        if not admins:
            _item(out, g, "系統管理員", "fail", "沒有任何系統管理員",
                  f"sudo -u dailyreport {DIR}/.venv/bin/flask --app wsgi create-admin --username sysadmin")
        else:
            assigned = len(admins) - len(local)
            _item(out, g, "系統管理員", "ok" if assigned else "warn",
                  f"本機緊急帳號 {len(local)} 個、指派 {assigned} 個",
                  "" if assigned else "AD 帳號登入一次後，在「帳號管理」指派檔案管理員或二線管理員為系統管理員")
    except Exception as e:  # 資料庫打不開時其他檢查也沒有意義
        _item(out, g, "連線", "fail", str(e)[:200], f"檢查 {DIR}/.env 的 DATABASE_URL 與檔案權限")
        return out

    # ---------------------------------------------------------------- PDF
    g = "PDF 輸出"
    tpl = cfg.get("PDF_TEMPLATE_XLSX")
    if tpl and os.path.isfile(tpl) and os.access(tpl, os.R_OK):
        _item(out, g, "CheckList 範本", "ok", tpl)
    else:
        _item(out, g, "CheckList 範本", "fail", f"找不到 {tpl}，無法產生 PDF",
              f"sudo bash {DIR}/scripts/dailyreport.sh template \"/路徑/CFXD SLA - SCADA Daily Inspection CheckList.xlsx\"")
    fonts = _fonts()
    missing = [f for f in ("Carlito", "Liberation Sans", "DejaVu Sans") if f not in fonts]
    font_dir = cfg.get("FONT_DIR")
    ms = font_dir and os.path.isdir(font_dir) and any(n.lower().endswith((".ttf", ".ttc")) for n in os.listdir(font_dir))
    if missing:
        _item(out, g, "字型", "fail", "缺少 " + "、".join(missing),
              "sudo apt-get install -y fonts-crosextra-carlito fonts-liberation fonts-dejavu-core")
    else:
        _item(out, g, "字型", "ok", "Carlito / Liberation / DejaVu" + ("，另有 Microsoft 字型" if ms else "（相容字型）"))

    # ---------------------------------------------------------------- 背景工作
    g = "背景工作"
    now = datetime.now()
    pending = ReportImage.query.filter_by(ocr_status="pending").count()
    stuck = ReportImage.query.filter(ReportImage.ocr_status == "running",
                                     ReportImage.ocr_started_at < now - timedelta(minutes=10)).count()
    old = ReportImage.query.filter(ReportImage.ocr_status == "pending", ReportImage.created_at < now - timedelta(hours=1)).count()
    status = "fail" if stuck or old else ("warn" if pending > 200 else "ok")
    _item(out, g, "截圖 OCR 佇列", status, f"等待 {pending} 張、卡住 {stuck} 張、等超過 1 小時 {old} 張",
          "" if status == "ok" else "sudo systemctl restart dailyreport-ocr；sudo journalctl -u dailyreport-ocr -n 50")
    failed = ReportImage.query.filter_by(ocr_status="error").count()
    if failed:
        _item(out, g, "截圖 OCR 失敗", "warn", f"{failed} 張辨識失敗", "在日報的截圖分頁按「全部重新辨識」")
    s = prov.settings()
    providers = AIProvider.query.count()
    usable = len(prov.available_providers())
    if not s["ai_enabled"]:
        _item(out, g, "AI 建議", "ok", f"未啟用（已設定 {providers} 個服務）")
    else:
        ai_wait = CheckReport.query.filter_by(ai_status="pending").count()
        _item(out, g, "AI 建議", "ok" if usable else "fail", f"可用服務 {usable}/{providers}、等待產生 {ai_wait} 份",
              "" if usable else "到「系統設定」新增或啟用 AI 服務，並按「測試連線」")
    md_dir = s["check_md_dir"]
    if md_dir:
        writable = os.path.isdir(md_dir) and os.access(md_dir, os.W_OK)
        _item(out, g, "檢查結果.md 匯出", "ok" if writable else "warn", md_dir,
              "" if writable else f"sudo mkdir -p {md_dir} && sudo chown dailyreport:www-data {md_dir}")

    # ---------------------------------------------------------------- 儲存空間
    g = "儲存空間"
    up = cfg.get("UPLOAD_DIR")
    if up and os.path.isdir(up):
        free = shutil.disk_usage(up).free / 1e9
        writable = os.access(up, os.W_OK)
        _item(out, g, "截圖資料夾", "ok" if writable and free > 5 else ("fail" if not writable else "warn"),
              f"{up}，剩餘 {free:.1f} GB" + ("" if writable else "，無法寫入"),
              "" if writable and free > 5 else f"sudo chown -R dailyreport:www-data {up}；清理磁碟或擴充容量")
    return out


def _ldap_reach(out, g, url):
    import socket
    from urllib.parse import urlparse

    u = urlparse(url)
    port = u.port or (636 if u.scheme == "ldaps" else 389)
    try:
        socket.create_connection((u.hostname, port), timeout=3).close()
        _item(out, g, "AD 連線", "ok", f"{u.hostname}:{port} 可連線")
    except OSError as e:
        _item(out, g, "AD 連線", "fail", f"{u.hostname}:{port} 無法連線（{e}）", "確認 DNS、防火牆與網域控制站")


def _fonts():
    try:
        r = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True, timeout=10)
        return {f.strip() for line in r.stdout.splitlines() for f in line.split(",")}
    except (OSError, subprocess.SubprocessError):
        return set()
