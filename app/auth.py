import ssl
from datetime import datetime

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import login_required, login_user, logout_user
from ldap3 import ALL, SUBTREE, Connection, Server, Tls
from werkzeug.security import check_password_hash
from ldap3.core.exceptions import LDAPBindError, LDAPException
from ldap3.utils.conv import escape_filter_chars

from . import db, sysconf
from .models import User

bp = Blueprint("auth", __name__)


def _in_group_filter(username, groups):
    """帳號是 groups 任一群組的成員（含巢狀群組）。groups 可以是單一 DN 或清單。"""
    if isinstance(groups, str):
        groups = [groups]
    # 1.2.840.113556.1.4.1941 = 遞迴群組成員檢查（含巢狀群組）
    terms = "".join(f"(memberOf:1.2.840.113556.1.4.1941:={escape_filter_chars(g)})" for g in groups)
    return f"(&(sAMAccountName={escape_filter_chars(username)}){terms if len(groups) == 1 else f'(|{terms})'})"


def _one_server(c, url):
    tls = None
    if url.lower().startswith("ldaps://"):
        pem = (c.get("ldap_ca_pem") or "").strip()
        tls = Tls(validate=ssl.CERT_REQUIRED, ca_certs_data=pem or None)
    return Server(url, use_ssl=tls is not None, tls=tls, get_info=ALL,
                  connect_timeout=int(c.get("ldap_timeout") or 5))


def _connect(c, user, password, urls=None):
    """依序連線網域控制站並 bind：連不上（逾時、拒絕連線、DNS、TLS 憑證）就換下一台。
    帳號密碼錯誤（LDAPBindError）直接回報，不換台：每一台的答案都一樣，也避免重複嘗試造成帳號鎖定。
    全部連不上時丟出最後一台的錯誤。"""
    last = None
    for url in urls or sysconf.url_list(c["ldap_url"]):
        try:
            return Connection(_one_server(c, url), user=user, password=password, auto_bind=True)
        except LDAPBindError:
            raise
        except (LDAPException, ssl.SSLError, OSError) as e:
            current_app.logger.warning("AD 伺服器 %s 無法使用，改試下一台：%s", url, e)
            last = e
    raise last or LDAPException("沒有設定 AD 伺服器")


def _lookup(conn, c, username):
    """回傳 entry；不在允許群組內或找不到時為 None。"""
    groups = c.get("required_groups") or sysconf.group_list(c["ldap_required_group"])
    if groups:
        search_filter = _in_group_filter(username, groups)
    else:
        search_filter = f"(sAMAccountName={escape_filter_chars(username)})"
    conn.search(c["ldap_base_dn"], search_filter, SUBTREE, attributes=["displayName", "mail"])
    return conn.entries[0] if conn.entries else None


def _member_of(conn, c, username, groups):
    """測試用：逐一列出帳號屬於哪些群組。"""
    hit = []
    for g in groups:
        conn.search(c["ldap_base_dn"], _in_group_filter(username, g), SUBTREE)
        if conn.entries:
            hit.append(g)
    return hit


def ad_authenticate(username, password, c=None):
    """以使用者自己的帳密 bind AD；成功回傳 (display_name, email)，失敗回傳 None。

    設定來自「系統設定 → AD 驗證」（資料庫），每次登入重新讀取，不需要重啟。
    """
    c = c or sysconf.ldap_config()
    if not password or not sysconf.ldap_configured(c):  # 空密碼會變成匿名 bind 而「成功」，必須擋掉
        return None
    try:
        conn = _connect(c, f"{username}@{c['ad_domain']}", password)
    except (LDAPException, ssl.SSLError, OSError) as e:
        current_app.logger.info("AD 登入失敗 %s: %s", username, e)
        return None

    try:
        entry = _lookup(conn, c, username)
        if entry is None:
            current_app.logger.info("AD 帳號 %s 不在允許群組內或找不到", username)
            return None
        display_name = str(entry.displayName) if entry.displayName else username
        email = str(entry.mail) if entry.mail else None
        return display_name, email
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
    urls = sysconf.url_list(c["ldap_url"])
    if not urls or not c["ad_domain"] or not c["ldap_base_dn"]:
        return [(False, "設定", "伺服器網址、網域、搜尋起點（Base DN）都必須填寫")]
    timeout = int(c.get("ldap_timeout") or 5)
    ok_urls = []
    for i, url in enumerate(urls):
        u = urlparse(url)
        if u.scheme not in ("ldap", "ldaps") or not u.hostname:
            return [(False, "設定", f"{url}：格式應為 ldaps://主機:636 或 ldap://主機:389")]
        port = u.port or (636 if u.scheme == "ldaps" else 389)
        label = "網路連線" if len(urls) == 1 else f"網路連線（第 {i + 1} 台）"
        try:
            socket.create_connection((u.hostname, port), timeout=timeout).close()
        except OSError as e:
            steps.append((False, label, f"{u.hostname}:{port} 無法連線（{e}）。確認 DNS、防火牆與網域控制站"))
            continue
        try:
            conn = Connection(_one_server(c, url), auto_bind=True)  # 匿名 bind：只驗證連線與 TLS
            conn.unbind()
        except (LDAPException, ssl.SSLError, OSError) as e:
            steps.append((False, label, f"{u.hostname}:{port} 可連線，但 LDAP 失敗：{_tls_hint(e)}"))
            continue
        ok_urls.append(url)
        steps.append((True, label, f"{u.hostname}:{port} 可連線，" + ("TLS 憑證驗證通過" if u.scheme == "ldaps"
                                                                    else "LDAP 服務回應正常")))
    if not ok_urls:
        return steps
    if len(urls) > 1:
        first = urlparse(ok_urls[0]).netloc
        steps.append((True if len(ok_urls) == len(urls) else None, "自動切換",
                      f"{len(urls)} 台中 {len(ok_urls)} 台可用；登入時依序嘗試，目前會使用 {first}"))
    if any(urlparse(x).scheme == "ldap" for x in ok_urls):
        steps.append((None, "加密", "使用 ldap://（未加密），密碼會以明文送到網域控制站，建議改用 ldaps://"))
    if not username:
        steps.append((None, "帳號驗證", "填入測試帳號與密碼可以進一步確認登入與群組設定"))
        return steps
    username = username.split("\\")[-1].split("@")[0].lower()
    try:
        conn = _connect(c, f"{username}@{c['ad_domain']}", password, ok_urls)
        steps.append((True, "帳號驗證", f"{username}@{c['ad_domain']} 登入成功"))
    except (LDAPException, ssl.SSLError, OSError) as e:
        steps.append((False, "帳號驗證", _tls_hint(e)))
        return steps
    try:
        try:
            entry = _lookup(conn, c, username)
        except LDAPException as e:
            steps.append((False, "搜尋帳號", f"{e}（確認搜尋起點 Base DN 與群組 DN）"))
            return steps
        if entry is None:
            steps.append((False, "搜尋帳號", "在搜尋起點下找不到這個帳號，或不是允許登入群組的成員"
                          if c["ldap_required_group"] else "在搜尋起點下找不到這個帳號，確認 Base DN"))
            return steps
        name = str(entry.displayName) if entry.displayName else username
        req = sysconf.group_list(c["ldap_required_group"])
        hit = _member_of(conn, c, username, req) if len(req) > 1 else req
        steps.append((True, "搜尋帳號", f"{name}" + (f"（{entry.mail}）" if entry.mail else "")
                      + (f"，是允許登入群組的成員（{_short(hit)}）" if req else "")))
    finally:
        conn.unbind()
    return steps


def _short(dns):
    """CN=MIS,OU=…  →  MIS"""
    return "、".join(d.split(",", 1)[0].split("=", 1)[-1] for d in dns)


def _tls_hint(e):
    msg = str(e)
    if "certificate" in msg.lower() or isinstance(e, ssl.SSLError):
        return f"{msg}。網域控制站的憑證不受信任：在下方貼上公司 CA 憑證（PEM）"
    if "invalidCredentials" in msg or "52e" in msg:
        return "帳號或密碼錯誤"
    return msg


def verify_password(username, password):
    """重新確認身分（刪除正式文件等高風險操作）：本機帳號比對本機密碼，其他用 AD 驗證。
    回傳 User 或 None。不登入、不改 session。"""
    username = clean_username(username)
    if not username or not password:
        return None
    user = User.query.filter_by(username=username).first()
    if user is None:
        return None
    if user.password_hash:
        return user if check_password_hash(user.password_hash, password) else None
    if current_app.config["AUTH_MODE"] == "dev":
        return user
    return user if ad_authenticate(username, password) else None


def _next_url(default=None):
    next_url = request.args.get("next", "")
    # 只允許站內相對路徑，避免開放式重新導向
    if not next_url.startswith("/") or next_url.startswith("//") or next_url.startswith("/\\"):
        next_url = default or url_for("views.index")
    return next_url


def clean_username(raw):
    """接受 user、user@網域、DOMAIN\\user，統一成 sAMAccountName（小寫）。"""
    return (raw or "").strip().split("\\")[-1].split("@")[0].strip().lower()


def _fail_audit(username, why):
    from .models import AuditLog

    db.session.add(AuditLog(action="login_fail", actor_id=None, target=username[:128],
                            detail=f"{why}；來源 {request.remote_addr or '?'}"[:500]))
    db.session.commit()


def _local_login(username, password):
    """本機帳號（系統管理員緊急帳號）：不經 AD，只比對本機密碼。只能從後台登入頁使用。"""
    user = User.query.filter_by(username=username).first()
    if user is None or not user.password_hash or not password or not check_password_hash(user.password_hash, password):
        return None
    current_app.logger.warning("本機帳號 %s 登入（後台）", username)
    return user


def _ad_login(username, password):
    """AD 帳號：以本人帳密 bind AD；首次登入建立帳號（訪客）。本機帳號不接受 AD 登入。"""
    existing = User.query.filter_by(username=username).first()
    if existing is not None and existing.password_hash:
        return None
    if current_app.config["AUTH_MODE"] == "dev":
        result = (username, None) if username and password else None
    else:
        result = ad_authenticate(username, password)
    if not result:
        return None
    display_name, email = result
    user = existing
    if not user:
        # 首次登入：AD 帳號一律為訪客（只能看歡迎頁，等系統管理員在「帳號管理」設定角色）。
        # 開發模式沿用一線使用者。之後角色一律由系統管理員設定，登入時不再變更。
        user = User(username=username)
        user.set_role("first_line" if current_app.config["AUTH_MODE"] == "dev" else "guest")
        db.session.add(user)
    user.display_name = display_name
    user.email = email
    return user


def _finish(user):
    user.last_login = datetime.now()
    db.session.commit()
    login_user(user)


FAIL = "帳號或密碼錯誤，或您沒有使用此系統的權限。"


@bp.route("/login", methods=["GET", "POST"])
def login():
    """前台登入：只接受公司 AD 帳號；本機帳號一律拒絕（請由後台 /admin 登入）。"""
    suffix = sysconf.login_suffix()
    if request.method == "POST":
        username = clean_username(request.form.get("username"))
        user = _ad_login(username, request.form.get("password", "")) if username else None
        if user:
            _finish(user)
            return redirect(_next_url())
        local = User.query.filter_by(username=username).first() if username else None
        if local is not None and local.password_hash:
            current_app.logger.warning("本機帳號 %s 嘗試從前台登入，已拒絕", username)
            _fail_audit(username, "本機帳號不能從前台登入")
        flash(FAIL, "error")
    return render_template("login.html", suffix=suffix)


@bp.route("/admin", methods=["GET", "POST"], strict_slashes=False)
def admin_login():
    """後台登入：可選 AD 帳號或本機帳號；只有系統管理員能登入，登入後進入系統設定。"""
    from flask_login import current_user

    if request.method == "GET" and current_user.is_authenticated and current_user.is_admin:
        return redirect(_next_url(default=url_for("settings.index")))
    suffix = sysconf.login_suffix()
    method = request.form.get("method") or request.args.get("method") or "ad"
    if method not in ("ad", "local"):
        method = "ad"
    if request.method == "POST":
        username = clean_username(request.form.get("username"))
        password = request.form.get("password", "")
        user = None
        if username:
            user = _local_login(username, password) if method == "local" else _ad_login(username, password)
        if user and user.is_admin:
            _finish(user)
            return redirect(_next_url(default=url_for("settings.index")))
        if user:  # 帳密正確但不是系統管理員：不登入
            db.session.rollback()
            _fail_audit(username, f"後台登入（{'本機' if method == 'local' else 'AD'}）：沒有系統管理員權限")
            flash("這個帳號沒有系統管理員權限，請由前台登入。", "error")
        else:
            if username:
                _fail_audit(username, f"後台登入（{'本機' if method == 'local' else 'AD'}）失敗")
            flash("帳號或密碼錯誤。", "error")
    return render_template("admin_login.html", suffix=suffix, method=method)


@bp.route("/logout")
@login_required
def logout():
    from flask_login import current_user

    local = bool(current_user.password_hash)
    logout_user()
    return redirect(url_for("auth.admin_login" if local else "auth.login"))
