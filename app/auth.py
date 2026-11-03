import ssl
from datetime import datetime

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import login_required, login_user, logout_user
from ldap3 import ALL, SUBTREE, Connection, Server, Tls
from werkzeug.security import check_password_hash
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from . import db, sysconf
from .models import User

bp = Blueprint("auth", __name__)


def _in_group_filter(username, group_dn):
    # 1.2.840.113556.1.4.1941 = 遞迴群組成員檢查（含巢狀群組）
    return (
        f"(&(sAMAccountName={escape_filter_chars(username)})"
        f"(memberOf:1.2.840.113556.1.4.1941:={escape_filter_chars(group_dn)}))"
    )


def _server(c):
    tls = None
    if c["ldap_url"].startswith("ldaps://"):
        pem = (c.get("ldap_ca_pem") or "").strip()
        tls = Tls(validate=ssl.CERT_REQUIRED, ca_certs_data=pem or None)
    return Server(c["ldap_url"], use_ssl=tls is not None, tls=tls, get_info=ALL,
                  connect_timeout=int(c.get("ldap_timeout") or 5))


def _lookup(conn, c, username):
    """回傳 (entry, is_approver)；不在允許群組內或找不到時 entry 為 None。"""
    group = c["ldap_required_group"]
    if group:
        search_filter = _in_group_filter(username, group)
    else:
        search_filter = f"(sAMAccountName={escape_filter_chars(username)})"
    conn.search(c["ldap_base_dn"], search_filter, SUBTREE, attributes=["displayName", "mail"])
    if not conn.entries:
        return None, False
    entry = conn.entries[0]
    is_approver = False
    if c["ldap_approver_group"]:
        conn.search(c["ldap_base_dn"], _in_group_filter(username, c["ldap_approver_group"]), SUBTREE)
        is_approver = bool(conn.entries)
    return entry, is_approver


def ad_authenticate(username, password, c=None):
    """以使用者自己的帳密 bind AD；成功回傳 (display_name, email, is_approver)，失敗回傳 None。

    設定來自「系統設定 → AD 驗證」（資料庫），每次登入重新讀取，不需要重啟。
    """
    c = c or sysconf.ldap_config()
    if not password or not sysconf.ldap_configured(c):  # 空密碼會變成匿名 bind 而「成功」，必須擋掉
        return None
    try:
        conn = Connection(_server(c), user=f"{username}@{c['ad_domain']}", password=password, auto_bind=True)
    except (LDAPException, ssl.SSLError, OSError) as e:
        current_app.logger.info("AD 登入失敗 %s: %s", username, e)
        return None

    try:
        entry, is_approver = _lookup(conn, c, username)
        if entry is None:
            current_app.logger.info("AD 帳號 %s 不在允許群組內或找不到", username)
            return None
        display_name = str(entry.displayName) if entry.displayName else username
        email = str(entry.mail) if entry.mail else None
        return display_name, email, is_approver
    except LDAPException as e:
        current_app.logger.warning("AD 查詢失敗 %s: %s", username, e)
        return None
    finally:
        conn.unbind()


def ldap_test(c, username="", password=""):
    """設定頁的連線測試：逐步回報 [(ok, 步驟, 說明)]。不會建立帳號，也不記錄密碼。"""
    import socket
    from urllib.parse import urlparse

    steps = []
    if not c["ldap_url"] or not c["ad_domain"] or not c["ldap_base_dn"]:
        return [(False, "設定", "伺服器網址、網域、搜尋起點（Base DN）都必須填寫")]
    u = urlparse(c["ldap_url"])
    if u.scheme not in ("ldap", "ldaps") or not u.hostname:
        return [(False, "設定", "伺服器網址格式應為 ldaps://主機:636 或 ldap://主機:389")]
    port = u.port or (636 if u.scheme == "ldaps" else 389)
    timeout = int(c.get("ldap_timeout") or 5)
    try:
        socket.create_connection((u.hostname, port), timeout=timeout).close()
        steps.append((True, "網路連線", f"{u.hostname}:{port} 可連線"))
    except OSError as e:
        steps.append((False, "網路連線", f"{u.hostname}:{port} 無法連線（{e}）。確認 DNS、防火牆與網域控制站"))
        return steps
    if u.scheme == "ldap":
        steps.append((None, "加密", "使用 ldap://（未加密），密碼會以明文送到網域控制站，建議改用 ldaps://"))
    server = _server(c)
    if not username:
        try:
            conn = Connection(server, auto_bind=True)  # 匿名 bind：只驗證連線與 TLS
            conn.unbind()
            steps.append((True, "LDAP 連線", "TLS 憑證驗證通過" if u.scheme == "ldaps" else "LDAP 服務回應正常"))
        except (LDAPException, ssl.SSLError, OSError) as e:
            steps.append((False, "LDAP 連線", _tls_hint(e)))
        steps.append((None, "帳號驗證", "填入測試帳號與密碼可以進一步確認登入與群組設定"))
        return steps
    username = username.split("\\")[-1].split("@")[0].lower()
    try:
        conn = Connection(server, user=f"{username}@{c['ad_domain']}", password=password, auto_bind=True)
        steps.append((True, "帳號驗證", f"{username}@{c['ad_domain']} 登入成功"))
    except (LDAPException, ssl.SSLError, OSError) as e:
        steps.append((False, "帳號驗證", _tls_hint(e)))
        return steps
    try:
        try:
            entry, is_approver = _lookup(conn, c, username)
        except LDAPException as e:
            steps.append((False, "搜尋帳號", f"{e}（確認搜尋起點 Base DN 與群組 DN）"))
            return steps
        if entry is None:
            steps.append((False, "搜尋帳號", "在搜尋起點下找不到這個帳號，或不是允許登入群組的成員"
                          if c["ldap_required_group"] else "在搜尋起點下找不到這個帳號，確認 Base DN"))
            return steps
        name = str(entry.displayName) if entry.displayName else username
        steps.append((True, "搜尋帳號", f"{name}" + (f"（{entry.mail}）" if entry.mail else "")
                      + ("，是允許登入群組的成員" if c["ldap_required_group"] else "")))
        if c["ldap_approver_group"]:
            steps.append((True, "檔案管理員群組", "是成員（首次登入會設為檔案管理員）" if is_approver else "不是成員"))
    finally:
        conn.unbind()
    return steps


def _tls_hint(e):
    msg = str(e)
    if "certificate" in msg.lower() or isinstance(e, ssl.SSLError):
        return f"{msg}。網域控制站的憑證不受信任：在下方貼上公司 CA 憑證（PEM）"
    if "invalidCredentials" in msg or "52e" in msg:
        return "帳號或密碼錯誤"
    return msg


def _next_url():
    next_url = request.args.get("next", "")
    # 只允許站內相對路徑，避免開放式重新導向
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = url_for("views.index")
    return next_url


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        # 接受 DOMAIN\user 或 user@domain，統一成 sAMAccountName
        username = username.split("\\")[-1].split("@")[0].lower()
        password = request.form.get("password", "")

        local = User.query.filter_by(username=username).first()
        if local is not None and local.password_hash:
            # 本機帳號（系統管理員）：不經 AD，只比對本機密碼
            if password and check_password_hash(local.password_hash, password):
                local.last_login = datetime.now()
                db.session.commit()
                login_user(local)
                current_app.logger.warning("本機帳號 %s 登入", username)
                return redirect(_next_url())
            flash("帳號或密碼錯誤，或您沒有使用此系統的權限。", "error")
            return render_template("login.html")

        if current_app.config["AUTH_MODE"] == "dev":
            result = (username, None, False) if username else None
        else:
            result = ad_authenticate(username, password)

        if result:
            display_name, email, in_approver_group = result
            user = User.query.filter_by(username=username).first()
            if not user:
                # 首次登入：預設一線使用者；設定檔 / AD 群組指定的帳號預設為檔案管理員。
                # 之後角色一律由系統管理員在「帳號管理」設定，登入時不再變更。
                user = User(username=username)
                bootstrap = in_approver_group or username in sysconf.ldap_config()["approver_users_set"]
                user.set_role("file_manager" if bootstrap else "first_line")
                db.session.add(user)
            user.display_name = display_name
            user.email = email
            user.last_login = datetime.now()
            db.session.commit()
            login_user(user)
            return redirect(_next_url())

        flash("帳號或密碼錯誤，或您沒有使用此系統的權限。", "error")

    return render_template("login.html")


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
