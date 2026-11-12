"""健康檢查。

- 系統設定頁「健康檢查」：run(system=True)，含服務、版本、HTTPS 憑證、磁碟等系統項目（以服務帳號執行）
- 維護背景服務依設定的間隔自動執行 run(system=True)
- scripts/dailyreport.sh doctor（升級後驗證）：`flask doctor-json` 輸出 run()，系統項目由腳本以 root 檢查

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


# 健康檢查等級：正常 → 事件 → 注意 → 告警 → 危險；失敗 = 功能無法運作
LEVELS = ("ok", "event", "warn", "alert", "danger", "fail")
LEVEL_LABELS = {"ok": "正常", "event": "事件", "warn": "注意", "alert": "告警", "danger": "危險", "fail": "失敗"}
SEVERE = ("alert", "danger", "fail")  # 系統管理員每一頁上方提醒、主機 doctor 結束代碼 1
STORAGE_LEVELS = ((95, "danger"), (90, "alert"), (85, "warn"), (80, "event"))  # 使用率 ≥ 門檻（%）
GB = 1024 ** 3


def _item(out, group, name, status, detail="", fix=""):
    out.append({"group": group, "name": name, "status": status, "detail": detail, "fix": fix})


def abnormal_items(items):
    """不是正常的項目，最嚴重的在前面。"""
    order = {k: i for i, k in enumerate(LEVELS)}
    return sorted((i for i in items if i["status"] != "ok"), key=lambda i: -order.get(i["status"], len(LEVELS)))


def summarize(items):
    return {k: sum(1 for i in items if i["status"] == k) for k in LEVELS}


def storage_level(pct):
    return next((lv for th, lv in STORAGE_LEVELS if pct >= th), "ok")


def _mount_of(path):
    path = os.path.realpath(path)
    while not os.path.ismount(path) and path != os.path.dirname(path):
        path = os.path.dirname(path)
    return path


def storage():
    """每個掛載點的用量（資料、截圖、備份、系統）：[{mount, uses, total, used, free, pct, level}]，單位 bytes。"""
    from . import sysconf

    cfg = current_app.config
    paths = [(current_app.instance_path, "資料庫與正式 PDF"), (cfg.get("UPLOAD_DIR"), "截圖"),
             (sysconf.backup_dir(), "備份"), ("/", "系統")]
    mounts = {}
    for p, use in paths:
        if not p:
            continue
        probe = p
        while probe and not os.path.exists(probe):  # 備份位置還沒建立時看上一層
            probe = os.path.dirname(probe)
        if not probe:
            continue
        m = _mount_of(probe)
        mounts.setdefault(m, []).append(use)
    out = []
    for m, uses in mounts.items():
        try:
            du = shutil.disk_usage(m)
        except OSError:
            continue
        # 與 df 的 Use% 相同：已用 /（已用 + 可用），不含保留給 root 的空間
        pct = du.used / (du.used + du.free) * 100 if du.used + du.free else 0
        out.append({"mount": m, "uses": "、".join(dict.fromkeys(uses)), "total": du.total, "used": du.used,
                    "free": du.free, "pct": pct, "level": storage_level(pct)})
    return out


def storage_text(s):
    return (f"已用 {s['used'] / GB:.1f} GB / 共 {s['total'] / GB:.1f} GB（{s['pct']:.1f}%），"
            f"剩餘 {s['free'] / GB:.1f} GB · {s['uses']}")


def _storage_checks(out):
    g = "儲存空間"
    for s in storage():
        lv = s["level"]
        _item(out, g, f"磁碟 {s['mount']}", lv, storage_text(s),
              "" if lv == "ok" else "清理舊資料或擴充磁碟；備份可在「系統設定 → 備份還原」減少保留份數或改存 NAS"
              + ("（已達危險等級，空間用完時資料庫與備份會寫入失敗）" if lv == "danger" else ""))


def run(system=False):
    from . import ai_providers as prov
    from . import sysconf
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
    lc = sysconf.ldap_config()
    if mode == "ldap" and not sysconf.ldap_configured(lc):
        _item(out, g, "AD 設定", "warn", "尚未設定 AD 伺服器，AD 帳號無法登入（本機緊急帳號仍可登入）",
              "用本機緊急帳號登入 → 系統設定 → AD 驗證")
    elif mode == "ldap":
        _item(out, g, "AD 設定", "ok", f"{'、'.join(lc['ldap_urls'])}（網域 {lc['ad_domain']}）")
        _ldap_reach(out, g, lc["ldap_urls"])
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
              "" if ok == "ok" else "系統設定 → 備份還原：選擇損壞前的備份還原")
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
                  "" if assigned else "AD 帳號登入一次後，在「系統設定 → 帳號管理」指派檔案管理員或二線管理員為系統管理員")
    except Exception as e:  # 資料庫打不開時其他檢查也沒有意義
        _item(out, g, "連線", "fail", str(e)[:200], f"檢查 {DIR}/.env 的 DATABASE_URL 與檔案權限")
        return out

    # ---------------------------------------------------------------- PDF
    g = "PDF 輸出"
    tpl = cfg.get("PDF_TEMPLATE_XLSX")
    if tpl and os.path.isfile(tpl) and os.access(tpl, os.R_OK):
        _item(out, g, "CheckList 範本", "ok", tpl)
        try:
            from flask import has_app_context

            if has_app_context():
                from .checklist_template import diff_rows, template_status

                st = template_status()
                if st["error"]:
                    _item(out, g, "範本與檢查項目", "warn", st["error"])
                elif not st["ok"]:
                    _item(out, g, "範本與檢查項目", "warn",
                          f"檢查項目修改過，範本有 {len(diff_rows(st))} 處需要改版；改版前 PDF 只呈現範本中有的項目",
                          "檢查項目頁 → 下載範本修改清單，改好後在「系統設定 → PDF 與上傳」上傳")
                else:
                    _item(out, g, "範本與檢查項目", "ok", "一致")
        except Exception as e:  # noqa: BLE001
            _item(out, g, "範本與檢查項目", "warn", f"無法比對：{str(e)[:160]}")
    else:
        _item(out, g, "CheckList 範本", "fail", "尚未上傳，無法產生 PDF",
              "系統設定 → PDF 與上傳 → 上傳「CFXD SLA - SCADA Daily Inspection CheckList.xlsx」")
    fonts = _fonts()
    missing = [f for f in ("Carlito", "Liberation Sans", "DejaVu Sans") if f not in fonts]
    font_dir = cfg.get("FONT_DIR")
    ms = font_dir and os.path.isdir(font_dir) and any(n.lower().endswith((".ttf", ".ttc")) for n in os.listdir(font_dir))
    if missing:
        _item(out, g, "字型", "fail", "缺少 " + "、".join(missing),
              "sudo apt-get install -y fonts-crosextra-carlito fonts-liberation fonts-dejavu-core")
    else:
        _item(out, g, "字型", "ok", "Carlito / Liberation / DejaVu" + ("，另有 Microsoft 字型" if ms else "（相容字型）"))

    # ---------------------------------------------------------------- 結案與發送
    g = "結案與發送"
    from . import graph
    from .models import Delivery

    if sysconf.get("delivery_enabled"):
        miss = graph.missing()
        if miss:
            _item(out, g, "自動發送設定", "fail", "設定不完整：" + "、".join(miss) + "，結案後的發送會中止",
                  "系統設定 → 發送與通知")
        else:
            _item(out, g, "自動發送設定", "ok", f"{sysconf.get('sp_site_url')} → {sysconf.get('mail_to')}")
    else:
        _item(out, g, "自動發送設定", "warn", "未啟用：結案只產生正式 PDF，不會上傳 SharePoint 與寄出日報 Mail",
              "系統設定 → 發送與通知 → 啟用")
    errs = Delivery.query.filter_by(state="error").count()
    _item(out, g, "發送流程", "fail" if errs else "ok", f"{errs} 份日報發送中止" if errs else "沒有中止的發送",
          "開啟日報頁最下方的流程，修正原因後按「重試」" if errs else "")
    from . import teams

    url = teams.webhook()
    wants = sysconf.get("reminder_enabled") or any(sysconf.get(teams.key(ev, "on")) for ev in teams.EVENTS if ev != "reminder")
    if wants and not url:
        _item(out, g, "Teams 內部通知", "warn",
              "尚未設定 Teams Workflow 網址：每日提醒與發送中止通知不會送出" if url == "" else "Teams 網址無法解密",
              "系統設定 → 發送與通知 → 內部通知（Teams）")
    elif wants:
        last = sysconf.get("teams_last_result") or ""
        _item(out, g, "Teams 內部通知", "warn" if "失敗" in last else "ok", last or "已設定，尚未傳送過",
              "系統設定 → 發送與通知：按「發 Teams 測試訊息」確認" if "失敗" in last else "")

    # ---------------------------------------------------------------- 背景工作
    g = "背景工作"
    now = datetime.now()
    pending = ReportImage.query.filter_by(ocr_status="pending").count()
    stuck = ReportImage.query.filter(ReportImage.ocr_status == "running",
                                     ReportImage.ocr_started_at < now - timedelta(minutes=10)).count()
    old = ReportImage.query.filter(ReportImage.ocr_status == "pending", ReportImage.created_at < now - timedelta(minutes=15)).count()
    never = ReportImage.query.filter(ReportImage.kind.in_(("screenshot", "staged")), ReportImage.ocr_status.is_(None)).count()
    hb = sysconf.get("ocr_heartbeat") or {}
    try:
        hb_age = (now - datetime.fromisoformat(hb["at"])).total_seconds() if hb.get("at") else None
    except (TypeError, ValueError):
        hb_age = None
    alive = hb_age is not None and hb_age < 180
    status = "fail" if stuck or old or ((pending or never) and not alive) else ("warn" if pending > 200 else "ok")
    detail = f"等待 {pending} 張、卡住 {stuck} 張、等超過 15 分鐘 {old} 張"
    if never:
        detail += f"、尚未排入 {never} 張（服務會自動排入）"
    detail += "；服務心跳 " + (f"{int(hb_age)} 秒前" if hb_age is not None else "沒有（服務沒有在執行，或是舊版）")
    _item(out, g, "截圖 OCR 佇列", status, detail,
          "" if status == "ok" else "sudo systemctl restart dailyreport-ocr；sudo journalctl -u dailyreport-ocr -n 50"
                                    "（每張辨識完成都會記一行：結果、秒數、佇列剩幾張）")
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
              "" if usable else "系統設定 → AI 建議：新增或啟用 AI 服務，並按「測試連線」")
    md_dir = s["check_md_dir"]
    if md_dir:
        writable = os.path.isdir(md_dir) and os.access(md_dir, os.W_OK)
        _item(out, g, "檢查結果.md 匯出", "ok" if writable else "warn", md_dir,
              "" if writable else f"系統設定 → AI 建議：改成可寫入的位置，或 sudo mkdir -p {md_dir} && sudo chown dailyreport:www-data {md_dir}")

    # ---------------------------------------------------------------- 儲存空間（80% 事件、85% 注意、90% 告警、95% 危險）
    _storage_checks(out)
    up = cfg.get("UPLOAD_DIR")
    if up and os.path.isdir(up) and not os.access(up, os.W_OK):
        _item(out, "儲存空間", "截圖資料夾", "fail", f"{up} 無法寫入", f"sudo chown -R dailyreport:www-data {up}")

    _backup_checks(out)
    if system:
        _system_checks(out)
    return out


def _backup_checks(out):
    from . import backup, maint, sysconf

    g = "備份"
    root = sysconf.backup_dir()
    err = backup.check_dir(root)
    if err:
        _item(out, g, "備份位置", "fail", f"{root} {err}", "系統設定 → 備份還原：改成可寫入的位置")
        return
    _item(out, g, "備份位置", "ok", f"{root}（可寫入；空間見「儲存空間」）")
    nxt = maint.next_backup()
    if nxt:
        _item(out, g, "排程", "ok", f"每天 {sysconf.get('backup_time')}，保留 {sysconf.get('backup_keep')} 份（下次 {nxt:%m/%d %H:%M}）")
    else:
        _item(out, g, "排程", "warn", "自動備份未啟用", "系統設定 → 備份還原：啟用每日自動備份")
    last = backup.last_backup()
    if last:
        age = (datetime.now() - last["time"]).total_seconds() / 3600
        _item(out, g, "最近一次", "ok" if age <= 48 else "warn",
              f"{last['name']}（{age:.0f} 小時前，{last['size'] / 1e6:.1f} MB，共 {len(backup.list_backups())} 份）",
              "" if age <= 48 else "系統設定 → 備份還原 → 立即備份；並確認維護背景服務 dailyreport-maint 執行中")
    else:
        _item(out, g, "最近一次", "warn", "還沒有備份", "系統設定 → 備份還原 → 立即備份")
    from .models import MaintJob

    bad = MaintJob.query.filter_by(kind="backup").order_by(MaintJob.id.desc()).first()
    if bad and bad.status == "error":
        _item(out, g, "上次備份工作", "fail", f"{bad.finished_at:%m/%d %H:%M} 失敗：{bad.message[:150]}",
              "系統設定 → 備份還原：依錯誤訊息修正後按「立即備份」")


def _run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None, ""


SERVICES = ("dailyreport", "dailyreport-ocr", "dailyreport-ai", "dailyreport-maint", "nginx")


def _system_checks(out):
    """以服務帳號（非 root）可以做到的系統檢查；scripts/dailyreport.sh doctor 以 root 做更完整的版本。"""
    from . import backup, maint

    g = "系統"
    try:
        with open("/proc/meminfo") as fh:
            mem = {ln.split(":")[0]: int(ln.split()[1]) for ln in fh if ":" in ln}
        avail = mem.get("MemAvailable", 0) // 1024
        _item(out, g, "可用記憶體", "ok" if avail >= 1024 else "warn", f"{avail} MB",
              "" if avail >= 1024 else "截圖 OCR 約需 1 GB，建議增加記憶體或 swap")
    except OSError:
        pass
    rc, ntp = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    if rc is not None:
        _item(out, g, "時間同步", "ok" if ntp == "yes" else "warn", "NTP 已同步" if ntp == "yes" else "NTP 未同步（截圖日期檢查依賴正確的系統時間）",
              "" if ntp == "yes" else "主機管理員確認防火牆允許 UDP 123 對外連到 time.google.com（安裝與升級會自動設定）")
    ver = backup.version()
    if ver:
        rc, remote = _run(["git", "-c", f"safe.directory={backup.app_dir()}", "-C", backup.app_dir(),
                           "ls-remote", "origin", "refs/heads/main"], timeout=15)
        latest = remote.split()[0] if rc == 0 and remote else ""
        if not latest:
            _item(out, g, "程式版本", "ok", f"{backup.display_version()}，無法連線檢查新版")
        elif latest == ver:
            _item(out, g, "程式版本", "ok", f"{backup.display_version()}，最新")
        else:
            _item(out, g, "程式版本", "warn", f"{backup.display_version()}，GitHub 上有新版 {latest[:7]}",
                  f"在主機執行：curl -fsSL https://raw.githubusercontent.com/nox913106/CFXD_DailyReportDocSys/main/scripts/bootstrap.sh | sudo bash -s -- upgrade")

    g = "服務"
    rc, _ = _run(["systemctl", "--version"])
    if rc is None:
        _item(out, g, "systemd", "warn", "無法查詢服務狀態（不是以 systemd 執行）")
    else:
        for s in SERVICES:
            _, state = _run(["systemctl", "is-active", s])
            _item(out, g, s, "ok" if state == "active" else "fail", "執行中" if state == "active" else (state or "未知"),
                  "" if state == "active" else f"sudo systemctl restart {s}；sudo journalctl -u {s} -n 50")
    if not maint.worker_alive():
        _item(out, g, "維護背景服務", "fail", "超過 3 分鐘沒有回應：自動備份與自動健康檢查不會執行",
              "sudo systemctl restart dailyreport-maint；sudo journalctl -u dailyreport-maint -n 50")

    g = "HTTPS"
    from .tls import current_cert

    cert = current_cert()
    if cert is None:
        _item(out, g, "憑證", "warn", "本機 443 埠沒有 HTTPS（密碼會以明文傳送）", "系統設定 → HTTPS 憑證")
    else:
        days = cert["days_left"]
        _item(out, g, "憑證", "ok" if days > 30 else ("warn" if days > 0 else "fail"),
              f"{'自簽，' if cert['self_signed'] else ''}{cert['subject']}，剩 {days} 天到期（{cert['not_after']:%Y-%m-%d}）",
              "" if days > 30 else "系統設定 → HTTPS 憑證：上傳新的憑證")


def _ldap_reach(out, g, urls):
    """每一台網域控制站都檢查；有一台可用就能登入（自動切換），全部連不上才算失敗。"""
    import socket
    from urllib.parse import urlparse

    ok, bad = [], []
    for url in urls:
        u = urlparse(url)
        port = u.port or (636 if u.scheme == "ldaps" else 389)
        try:
            socket.create_connection((u.hostname, port), timeout=3).close()
            ok.append(f"{u.hostname}:{port}")
        except OSError as e:
            bad.append(f"{u.hostname}:{port}（{e}）")
    if not bad:
        _item(out, g, "AD 連線", "ok", "、".join(ok) + " 可連線")
    elif ok:
        _item(out, g, "AD 連線", "warn", f"{'、'.join(ok)} 可連線；{'、'.join(bad)} 無法連線（登入會自動改用可連線的）",
              "確認無法連線那台網域控制站的 DNS、防火牆與服務")
    else:
        _item(out, g, "AD 連線", "fail", "、".join(bad) + " 無法連線", "確認 DNS、防火牆與網域控制站")


def _fonts():
    try:
        r = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True, timeout=10)
        return {f.strip() for line in r.stdout.splitlines() for f in line.split(",")}
    except (OSError, subprocess.SubprocessError):
        return set()
