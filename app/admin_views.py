"""系統設定的其他分頁：系統狀態與健康檢查、AD 驗證、本機緊急帳號、備份還原、PDF 與上傳、HTTPS 憑證。

權限檢查在 settings_views 的 before_request（只有系統管理員）。所有變更寫入稽核紀錄。
"""

import os
import re
import shutil
from datetime import datetime

from flask import abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user
from werkzeug.security import check_password_hash, generate_password_hash

from . import backup as bk
from . import db, maint, sysconf
from . import tls as tlsmod
from .models import (MAINT_KIND_LABELS, MAINT_STATUS_LABELS, MAINT_TRIGGER_LABELS, AuditLog, MaintJob, User)
from .settings_views import _audit, _int, bp

MIN_PASSWORD = 12
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")


def _jobs(limit=10):
    return MaintJob.query.order_by(MaintJob.id.desc()).limit(limit).all()


def _job_ctx():
    return {"kind_labels": MAINT_KIND_LABELS, "status_labels": MAINT_STATUS_LABELS,
            "trigger_labels": MAINT_TRIGGER_LABELS, "worker_alive": maint.worker_alive()}


# ---------------------------------------------------------------- 系統狀態 / 健康檢查


@bp.route("/", methods=["GET"])
def index():
    last = sysconf.get("health_last")
    if last and last.get("at"):
        last = {**last, "at": datetime.fromisoformat(last["at"])}
    groups = {}
    for it in (last or {}).get("items", []):
        groups.setdefault(it["group"], []).append(it)
    infra = {"資料庫": db.engine.url.database, "驗證模式": current_app.config["AUTH_MODE"],
             "截圖資料夾": current_app.config["UPLOAD_DIR"], "程式版本": (bk.version() or "")[:7],
             "反向代理": "是" if os.environ.get("BEHIND_PROXY") == "1" else "否",
             "Cookie 只經 HTTPS": "是" if current_app.config["SESSION_COOKIE_SECURE"] else "否"}
    return render_template("admin_status.html", tab="status", last=last, groups=groups, infra=infra,
                           refreshing=bool(sysconf.get("health_refresh")),
                           health=sysconf.many(("health_enabled", "health_interval_hours")),
                           jobs=_jobs(5), **_job_ctx())


@bp.route("/health/run", methods=["POST"])
def health_run():
    s = maint.run_health(trigger="manual")
    flash(f"健康檢查完成：正常 {s['ok']}、注意 {s['warn']}、失敗 {s['fail']}。",
          "error" if s["fail"] else "success")
    return redirect(url_for("settings.index"))


@bp.route("/health/config", methods=["POST"])
def health_config():
    on = request.form.get("health_enabled") == "1"
    hours = _int("health_interval_hours", 1, 168, 6)
    sysconf.put("health_enabled", on, current_user)
    sysconf.put("health_interval_hours", hours, current_user)
    _audit("sys_settings", "健康檢查", f"自動檢查={'開' if on else '關'}、每 {hours} 小時")
    db.session.commit()
    flash("已儲存健康檢查設定。", "success")
    return redirect(url_for("settings.index"))


# ---------------------------------------------------------------- AD 驗證

LDAP_FORM = ("ldap_url", "ad_domain", "ldap_base_dn", "ldap_required_group", "ldap_approver_group", "approver_users")


def _ldap_from_form():
    f = request.form
    c = {k: f.get(k, "").strip() for k in LDAP_FORM}
    c["ad_domain"] = c["ad_domain"].lower()
    c["ldap_url"] = "\n".join(sysconf.url_list(f.get("ldap_url", "")))
    for k in ("ldap_required_group", "ldap_approver_group"):
        c[k] = "\n".join(sysconf.group_list(f.get(k, "")))
    c["approver_users"] = ",".join(sorted({u.strip().lower() for u in c["approver_users"].split(",") if u.strip()}))
    c["ldap_timeout"] = _int("ldap_timeout", 1, 60, 5)
    pem = f.get("ldap_ca_pem", "").strip()
    if f.get("clear_ca") == "1":
        pem = ""
    elif not pem:
        pem = sysconf.get("ldap_ca_pem") or ""  # 沒有貼新的就沿用
    upload = request.files.get("ldap_ca_file")
    if upload and upload.filename:
        pem = upload.read(256 * 1024).decode("ascii", errors="ignore").strip()
    c["ldap_ca_pem"] = pem
    return c


def _check_ldap(c):
    for url in sysconf.url_list(c["ldap_url"]):
        if not re.match(r"^ldaps?://[^/\s]+$", url, re.I):
            return f"伺服器網址「{url}」格式應為 ldaps://dc01.corp.local:636（多台時一行一台）"
    if c["ldap_ca_pem"]:
        from cryptography import x509

        try:
            x509.load_pem_x509_certificates(c["ldap_ca_pem"].encode())
        except ValueError:
            return "CA 憑證不是 PEM 格式（應以 -----BEGIN CERTIFICATE----- 開頭）"
    for k, label in (("ldap_required_group", "允許登入的群組"), ("ldap_approver_group", "檔案管理員群組")):
        bad = [g for g in sysconf.group_list(c[k]) if not re.match(r"^(CN|OU)=.+,DC=", g, re.I)]
        if bad:
            return f"{label}：「{bad[0]}」不是群組 DN（應類似 CN=群組,OU=…,DC=corp,DC=local；多個群組一行一個）"
    return None


@bp.route("/ldap", methods=["GET", "POST"])
def ldap():
    if request.method == "POST":
        c = _ldap_from_form()
        err = _check_ldap(c)
        if err:
            flash(err, "error")
            return render_template("admin_ldap.html", tab="ldap", c=c, sources={}, steps=None, carry_pem="")
        old = sysconf.ldap_config()
        changed = [k for k in c if c[k] != old.get(k)]
        for k, v in c.items():
            sysconf.put(k, v, current_user)
        if changed:
            _audit("sys_settings", "AD 驗證", "、".join("CA 憑證" if k == "ldap_ca_pem" else f"{k}={c[k]}" for k in changed))
        db.session.commit()
        flash("已儲存 AD 設定，下一次登入就會套用（不需要重啟）。", "success")
        return redirect(url_for("settings.ldap"))
    c = sysconf.ldap_config()
    return render_template("admin_ldap.html", tab="ldap", c=c, steps=None,
                           sources={k: sysconf.source(k) for k in LDAP_FORM})


@bp.route("/ldap/test", methods=["POST"])
def ldap_test():
    """用表單上的值（還沒儲存也可以）測試，避免存了錯的設定。"""
    from .auth import ldap_test as run_test

    c = _ldap_from_form()
    err = _check_ldap(c)
    steps = [(False, "設定", err)] if err else run_test(c, request.form.get("test_user", "").strip(),
                                                         request.form.get("test_password", ""))
    stored = sysconf.get("ldap_ca_pem") or ""
    return render_template("admin_ldap.html", tab="ldap", c=c, steps=steps, sources={},
                           test_user=request.form.get("test_user", ""),
                           carry_pem=c["ldap_ca_pem"] if c["ldap_ca_pem"] != stored else "")


# ---------------------------------------------------------------- 本機緊急帳號（在帳號管理頁顯示）


def _local_redirect():
    return redirect(url_for("views.users") + "#local")


def _pw_ok(pw, pw2):
    if len(pw) < MIN_PASSWORD:
        flash(f"密碼至少需要 {MIN_PASSWORD} 個字元。", "error")
        return False
    if pw != pw2:
        flash("兩次輸入的密碼不一致。", "error")
        return False
    return True


@bp.route("/local/create", methods=["POST"])
def local_create():
    username = request.form.get("username", "").strip().lower()
    if not USERNAME_RE.match(username):
        flash("帳號名稱需 3～64 個字元，只能用英數字與 . _ -。", "error")
        return _local_redirect()
    if not _pw_ok(request.form.get("password", ""), request.form.get("password2", "")):
        return _local_redirect()
    if User.query.filter_by(username=username).first():
        flash(f"{username} 已存在（AD 帳號不能改成本機帳號），請換一個名稱。", "error")
        return _local_redirect()
    u = User(username=username, display_name=request.form.get("display_name", "").strip() or "System Administrator")
    u.set_role("admin")
    u.password_hash = generate_password_hash(request.form["password"])
    db.session.add(u)
    _audit("local_account", username, "新增本機緊急帳號")
    db.session.commit()
    flash(f"已新增本機緊急帳號 {username}。", "success")
    return _local_redirect()


def _local_user(uid):
    u = db.session.get(User, uid)
    if u is None or not u.is_local_admin or not u.password_hash:
        abort(404)
    return u


@bp.route("/local/<int:uid>/password", methods=["POST"])
def local_password(uid):
    u = _local_user(uid)
    if u.id == current_user.id and not check_password_hash(u.password_hash, request.form.get("current_password", "")):
        flash("目前的密碼不正確。", "error")
        return _local_redirect()
    if not _pw_ok(request.form.get("password", ""), request.form.get("password2", "")):
        return _local_redirect()
    u.password_hash = generate_password_hash(request.form["password"])
    _audit("local_account", u.username, "變更密碼" if u.id == current_user.id else "重設密碼")
    db.session.commit()
    flash(f"已更新 {u.username} 的密碼。", "success")
    return _local_redirect()


@bp.route("/local/<int:uid>/disable", methods=["POST"])
def local_disable(uid):
    u = _local_user(uid)
    if u.id == current_user.id:
        flash("不能停用自己正在使用的帳號。", "error")
        return _local_redirect()
    others = User.query.filter(User.role == "admin", User.password_hash.isnot(None), User.id != u.id).count()
    if not others:
        flash("至少要保留一個本機緊急帳號（AD 無法驗證時唯一的登入方式）。", "error")
        return _local_redirect()
    name = u.username
    # 不刪除（稽核紀錄、日報會參照）：清除密碼並改名，原名稱之後不會被 AD 帳號接手成系統管理員
    u.username = f"{name}#停用{u.id}"[:128]
    u.password_hash = None
    u.set_role("first_line")
    _audit("local_account", name, "停用本機緊急帳號")
    db.session.commit()
    flash(f"已停用 {name}。", "success")
    return _local_redirect()


# ---------------------------------------------------------------- 備份還原


@bp.route("/backup", methods=["GET"])
def backup():
    jobs = _jobs(15)
    busy = any(j.status in ("pending", "running") for j in jobs)
    root = sysconf.backup_dir()
    return render_template("admin_backup.html", tab="backup", items=bk.list_backups(), jobs=jobs, busy=busy,
                           root=root, dir_error=bk.check_dir(root), next_run=maint.next_backup(),
                           b=sysconf.many(("backup_enabled", "backup_time", "backup_keep", "backup_dir",
                                           "backup_include_env")),
                           default_dir=current_app.config.get("BACKUP_DIR") or sysconf.DEFAULT_BACKUP_DIR,
                           **_job_ctx())


@bp.route("/backup/config", methods=["POST"])
def backup_config():
    f = request.form
    t = f.get("backup_time", "02:30").strip()
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", t):
        flash("備份時間格式為 HH:MM，例如 02:30。", "error")
        return redirect(url_for("settings.backup"))
    d = f.get("backup_dir", "").strip().rstrip("/")
    if d:
        err = bk.check_dir(d)
        if err:
            flash(f"備份位置 {d} {err}", "error")
            return redirect(url_for("settings.backup"))
        inst = os.path.realpath(current_app.instance_path)
        if os.path.realpath(d) == inst or os.path.realpath(d).startswith(inst + os.sep):
            flash("備份位置不能放在資料夾 instance 裡面（磁碟壞掉時會一起遺失）。", "error")
            return redirect(url_for("settings.backup"))
    new = {"backup_enabled": f.get("backup_enabled") == "1", "backup_time": t,
           "backup_keep": _int("backup_keep", 1, 365, 14), "backup_dir": d,
           "backup_include_env": f.get("backup_include_env") == "1"}
    old = sysconf.many(new)
    for k, v in new.items():
        sysconf.put(k, v, current_user)
    if t != old["backup_time"]:
        # 改了時間：今天新的時間如果已經過了，不要立刻補做
        sysconf.put("backup_last_scheduled", datetime.now().isoformat(timespec="seconds"), current_user)
    changed = [k for k in new if new[k] != old[k]]
    if changed:
        _audit("sys_settings", "備份", "、".join(f"{k}={new[k]}" for k in changed))
    db.session.commit()
    flash("已儲存備份設定。", "success")
    return redirect(url_for("settings.backup"))


@bp.route("/backup/now", methods=["POST"])
def backup_now():
    job = maint.enqueue("backup", current_user)
    if job is None:
        flash("已有備份或還原工作在進行，請稍候。", "error")
    else:
        _audit("backup", "立即備份")
        db.session.commit()
        flash("已排入備份，通常在一分鐘內完成。", "success")
    return redirect(url_for("settings.backup"))


@bp.route("/backup/<name>/restore", methods=["POST"])
def backup_restore(name):
    try:
        bk.verify(bk.path_of(name))
    except bk.BackupError as e:
        flash(f"不能還原：{e}", "error")
        return redirect(url_for("settings.backup"))
    if request.form.get("confirm", "").strip() != name[:8]:
        flash(f"請輸入備份日期 {name[:8]} 確認還原。", "error")
        return redirect(url_for("settings.backup"))
    job = maint.enqueue("restore", current_user, param=name)
    if job is None:
        flash("已有備份或還原工作在進行，請稍候。", "error")
    else:
        flash(f"已排入還原 {name}。還原前會自動備份目前的資料；完成後請重新整理頁面。", "success")
    return redirect(url_for("settings.backup"))


@bp.route("/backup/<name>/delete", methods=["POST"])
def backup_delete(name):
    try:
        bk.delete(name)
    except bk.BackupError as e:
        flash(str(e), "error")
        return redirect(url_for("settings.backup"))
    _audit("backup", name, "刪除備份")
    db.session.commit()
    flash(f"已刪除備份 {name}。", "success")
    return redirect(url_for("settings.backup"))


@bp.route("/backup/<name>/<fname>", methods=["GET"])
def backup_download(name, fname):
    if fname not in bk.DOWNLOADABLE:
        abort(404)
    try:
        path = os.path.join(bk.path_of(name), fname)
    except bk.BackupError:
        abort(404)
    if not os.path.isfile(path):
        abort(404)
    _audit("backup", name, f"下載 {fname}")
    db.session.commit()
    return send_file(path, as_attachment=True, download_name=f"dailyreport-{name}-{fname}")


# ---------------------------------------------------------------- PDF 與上傳

FONT_EXT = (".ttf", ".ttc", ".otf")


def _font_dir():
    return current_app.config["FONT_DIR"]


def _clear_pdf_cache():
    d = os.path.join(current_app.instance_path, "pdf_cache")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)


def _template_info():
    path = current_app.config["PDF_TEMPLATE_XLSX"]
    if not os.path.isfile(path):
        return None
    st = os.stat(path)
    return {"path": path, "size": st.st_size, "mtime": datetime.fromtimestamp(st.st_mtime)}


@bp.route("/pdf", methods=["GET"])
def pdf():
    d = _font_dir()
    fonts = sorted(f for f in os.listdir(d) if f.lower().endswith(FONT_EXT)) if os.path.isdir(d) else []
    return render_template("admin_pdf.html", tab="pdf", tpl=_template_info(), fonts=fonts,
                           max_upload_mb=sysconf.get("max_upload_mb"))


def required_sheets():
    # PDF 實際讀取的工作表名稱（不是網頁上的分頁標題）
    from .pdf.report_pdf import SHEETS

    return list(SHEETS)


@bp.route("/pdf/template", methods=["GET", "POST"])
def pdf_template():
    path = current_app.config["PDF_TEMPLATE_XLSX"]
    if request.method == "GET":
        if not os.path.isfile(path):
            abort(404)
        return send_file(path, as_attachment=True, download_name=os.path.basename(path))
    f = request.files.get("template")
    if not f or not f.filename.lower().endswith(".xlsx"):
        flash("請選擇 .xlsx 檔案。", "error")
        return redirect(url_for("settings.pdf"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".upload.xlsx"
    f.save(tmp)
    try:
        import openpyxl

        wb = openpyxl.load_workbook(tmp, read_only=True)
        missing = [s for s in required_sheets() if s not in wb.sheetnames]
        wb.close()
    except Exception as e:  # 不是有效的 xlsx
        os.remove(tmp)
        flash(f"無法讀取這個檔案：{e}", "error")
        return redirect(url_for("settings.pdf"))
    if missing:
        os.remove(tmp)
        flash("這不是 CheckList 範本：缺少工作表 " + "、".join(missing), "error")
        return redirect(url_for("settings.pdf"))
    if os.path.isfile(path):
        shutil.copy2(path, path + ".previous")  # 上一版保留一份
    os.replace(tmp, path)
    _clear_pdf_cache()
    _audit("sys_settings", "CheckList 範本", f.filename[:200])
    db.session.commit()
    flash("已更新 CheckList 範本，之後產生的 PDF 都會使用新範本。", "success")
    return redirect(url_for("settings.pdf"))


@bp.route("/pdf/font", methods=["POST"])
def pdf_font():
    files = [f for f in request.files.getlist("fonts") if f and f.filename]
    if not files:
        flash("請選擇字型檔。", "error")
        return redirect(url_for("settings.pdf"))
    d = _font_dir()
    os.makedirs(d, exist_ok=True)
    saved = []
    for f in files:
        name = os.path.basename(f.filename).replace("/", "_")
        if not name.lower().endswith(FONT_EXT) or name.startswith("."):
            flash(f"{name} 不是字型檔（.ttf / .ttc / .otf），略過。", "error")
            continue
        head = f.stream.read(4)
        f.stream.seek(0)
        if head not in (b"\x00\x01\x00\x00", b"OTTO", b"ttcf", b"true"):
            flash(f"{name} 不是有效的字型檔，略過。", "error")
            continue
        f.save(os.path.join(d, name))
        saved.append(name)
    if saved:
        _clear_pdf_cache()
        _audit("sys_settings", "PDF 字型", "上傳 " + "、".join(saved)[:400])
        db.session.commit()
        flash(f"已上傳 {len(saved)} 個字型：{'、'.join(saved)}。", "success")
    return redirect(url_for("settings.pdf"))


@bp.route("/pdf/font/delete", methods=["POST"])
def pdf_font_delete():
    name = os.path.basename(request.form.get("name", ""))
    p = os.path.join(_font_dir(), name)
    if not name.lower().endswith(FONT_EXT) or not os.path.isfile(p):
        abort(404)
    os.remove(p)
    _clear_pdf_cache()
    _audit("sys_settings", "PDF 字型", f"刪除 {name}")
    db.session.commit()
    flash(f"已刪除 {name}。", "success")
    return redirect(url_for("settings.pdf"))


@bp.route("/pdf/upload", methods=["POST"])
def pdf_upload_limit():
    mb = _int("max_upload_mb", 1, sysconf.UPLOAD_CEILING_MB, 20)
    sysconf.put("max_upload_mb", mb, current_user)
    _audit("sys_settings", "上傳限制", f"{mb} MB")
    db.session.commit()
    flash(f"單次上傳上限已設為 {mb} MB。", "success")
    return redirect(url_for("settings.pdf"))


# ---------------------------------------------------------------- HTTPS 憑證


@bp.route("/tls", methods=["GET"])
def tls():
    return render_template("admin_tls.html", tab="tls", cert=tlsmod.current_cert(), st=tlsmod.state())


@bp.route("/tls/upload", methods=["POST"])
def tls_upload():
    cf, kf = request.files.get("cert"), request.files.get("key")
    cert = (cf.read(512 * 1024) if cf and cf.filename else b"") or request.form.get("cert_pem", "").encode()
    key = (kf.read(64 * 1024) if kf and kf.filename else b"") or request.form.get("key_pem", "").encode()
    if not cert.strip() or not key.strip():
        flash("請提供憑證與私鑰。", "error")
        return redirect(url_for("settings.tls"))
    info, err = tlsmod.validate(cert, key)
    if err:
        flash(err, "error")
        return redirect(url_for("settings.tls"))
    tlsmod.submit(cert, key, current_user)
    _audit("sys_settings", "HTTPS 憑證", f"上傳 {info['subject']}（到期 {info['not_after']:%Y-%m-%d}，簽發者 {info['issuer']}）")
    db.session.commit()
    flash(f"憑證檢查通過（{info['subject']}，到期 {info['not_after']:%Y-%m-%d}），正在套用到 nginx，約 10 秒後重新整理。",
          "success")
    return redirect(url_for("settings.tls"))


@bp.route("/tls/self-signed", methods=["POST"])
def tls_self_signed():
    tlsmod.submit(None, None, current_user)
    _audit("sys_settings", "HTTPS 憑證", "改回自簽憑證")
    db.session.commit()
    flash("正在改回自簽憑證，約 10 秒後重新整理。", "success")
    return redirect(url_for("settings.tls"))


# ---------------------------------------------------------------- 給帳號管理頁用


def local_accounts():
    return User.query.filter(User.role == "admin", User.password_hash.isnot(None)).order_by(User.username).all()


AUDIT_LABELS = {"delete_report": "刪除日報", "set_role": "變更角色", "set_admin": "系統管理員", "ai_settings": "AI 設定",
                "ai_provider_add": "新增 AI 服務", "ai_provider_edit": "修改 AI 服務", "ai_provider_delete": "刪除 AI 服務",
                "sys_settings": "系統設定", "local_account": "本機緊急帳號", "backup": "備份", "restore": "還原",
                "delegation": "代理權限"}


def recent_audit(limit=50):
    return AuditLog.query.order_by(AuditLog.id.desc()).limit(limit).all()


# ---------------------------------------------------------------- 發送與提醒（SharePoint、寄信、每日提醒）

DELIVERY_FORM = ("graph_tenant_id", "graph_client_id", "sp_site_url", "sp_library", "sp_folder", "sp_link_scope",
                 "mail_sender", "mail_to", "mail_cc", "mail_subject", "mail_body", "reminder_time", "deadline_time",
                 "reminder_extra")
LINK_SCOPES = {"organization": "公司內部（需登入公司帳號）", "anonymous": "任何有連結的人",
               "none": "不建立分享連結（沿用文件庫權限，只給檔案網址）"}
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _delivery_from_form():
    f = request.form
    c = {k: f.get(k, "").strip() for k in DELIVERY_FORM}
    c["sp_folder"] = c["sp_folder"].strip("/")
    c["delivery_enabled"] = f.get("delivery_enabled") == "1"
    c["reminder_enabled"] = f.get("reminder_enabled") == "1"
    c["sp_link_expire_days"] = _int("sp_link_expire_days", 0, 3650, 0)
    return c


def _check_delivery(c):
    from . import graph

    if c["sp_link_scope"] not in LINK_SCOPES:
        return "分享連結類型不正確"
    for k, label in (("reminder_time", "提醒時間"), ("deadline_time", "期限")):
        if not TIME_RE.match(c[k]):
            return f"{label}格式應為 HH:MM（24 小時制）"
    if c["reminder_time"] >= c["deadline_time"]:
        return "提醒時間必須早於期限"
    for k, label in (("mail_sender", "寄件信箱"), ("mail_to", "客戶收件人"), ("mail_cc", "副本"),
                     ("reminder_extra", "其他提醒收件人")):
        bad = [a for a in graph.addresses(c[k]) if not MAIL_RE.match(a)]
        if bad:
            return f"{label}：「{bad[0]}」不是 email 地址"
    if c["sp_site_url"] and not re.match(r"^https://[^/\s]+(/\S*)?$", c["sp_site_url"]):
        return "SharePoint 網站網址格式應為 https://公司.sharepoint.com/sites/網站名稱"
    return None


def _graph_config(c):
    """表單的值（還沒儲存）＋密碼：表單有填就用表單的，否則用已儲存的。"""
    from . import graph

    pw = request.form.get("graph_client_secret", "")
    return {**c, "client_secret": pw or graph.secret()}


def _delivery_page(c=None, steps=None, status=200):
    from . import delivery, graph

    saved = sysconf.many(DELIVERY_FORM + ("delivery_enabled", "reminder_enabled", "sp_link_expire_days"))
    return render_template("admin_delivery.html", tab="delivery", c=c or saved, steps=steps, scopes=LINK_SCOPES,
                           has_secret=bool(sysconf.get("graph_client_secret_enc")), secret_ok=graph.secret() is not None,
                           recipients=delivery.reminder_recipients(), reminder=sysconf.get("reminder_result"),
                           missing=graph.missing()), status


@bp.route("/delivery", methods=["GET", "POST"])
def delivery():
    from . import graph

    if request.method == "GET":
        return _delivery_page()
    c = _delivery_from_form()
    err = _check_delivery(c)
    if err:
        flash(err, "error")
        return _delivery_page(c, status=400)
    old = sysconf.many(tuple(c))
    for k, v in c.items():
        sysconf.put(k, v, current_user)
    changed = [k for k in c if c[k] != old.get(k)]
    pw = request.form.get("graph_client_secret", "")
    if pw or request.form.get("clear_secret") == "1":
        graph.set_secret(pw if pw else "", current_user)
        changed.append("用戶端密碼")
    if changed:
        _audit("sys_settings", "發送與提醒", "、".join(changed))
    db.session.commit()
    flash("已儲存發送與提醒設定。", "success")
    return redirect(url_for("settings.delivery"))


@bp.route("/delivery/test", methods=["POST"])
def delivery_test():
    """用表單上的值（不儲存）測試：取得權杖 → 找到網站與文件庫。不會上傳檔案。"""
    from . import graph

    c = _delivery_from_form()
    gc = _graph_config(c)
    steps = []
    miss = graph.missing(gc, mail=False)
    if miss:
        steps.append((False, "設定", "還沒填：" + "、".join(miss)))
        return _delivery_page(c, steps)
    try:
        graph.token(gc)
        steps.append((True, "取得權杖", f"租用戶 {gc['graph_tenant_id']}，應用程式 {gc['graph_client_id']}"))
        site_id, _, name = graph.site_and_drive(gc)
        steps.append((True, "SharePoint 網站與文件庫", f"{gc['sp_site_url']} → 文件庫「{name}」"))
        folder = graph.folder_for(gc, type("R", (), {"report_date": datetime.now().date()})())
        steps.append((None, "上傳位置", f"{name}/{folder}/（資料夾不存在時第一次上傳會自動建立）"))
    except graph.GraphError as e:
        steps.append((False, "連線", str(e)))
    return _delivery_page(c, steps)


@bp.route("/delivery/test-mail", methods=["POST"])
def delivery_test_mail():
    from . import graph

    c = _delivery_from_form()
    gc = _graph_config(c)
    to = graph.addresses(request.form.get("test_to", "")) or ([current_user.email] if current_user.email else [])
    miss = graph.missing(gc, sharepoint=False)
    if miss or not to:
        steps = [(False, "設定", "還沒填：" + "、".join(miss) if miss else "請填寫測試收件人")]
        return _delivery_page(c, steps)
    try:
        graph.send_mail(gc, to, "CFXD 日報系統測試信", "這是「系統設定 → 發送與提醒」寄出的測試信。")
        steps = [(True, "寄測試信", f"已由 {gc['mail_sender']} 寄給 {', '.join(to)}")]
    except graph.GraphError as e:
        steps = [(False, "寄測試信", str(e))]
    return _delivery_page(c, steps)

