import ssl
from datetime import datetime

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import login_required, login_user, logout_user
from ldap3 import ALL, SUBTREE, Connection, Server, Tls
from werkzeug.security import check_password_hash
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from . import db
from .models import User

bp = Blueprint("auth", __name__)


def _in_group_filter(username, group_dn):
    # 1.2.840.113556.1.4.1941 = 遞迴群組成員檢查（含巢狀群組）
    return (
        f"(&(sAMAccountName={escape_filter_chars(username)})"
        f"(memberOf:1.2.840.113556.1.4.1941:={escape_filter_chars(group_dn)}))"
    )


def ad_authenticate(username, password):
    """以使用者自己的帳密 bind AD；成功回傳 (display_name, email, is_approver)，失敗回傳 None。"""
    cfg = current_app.config
    if not password:  # 空密碼會變成匿名 bind 而「成功」，必須擋掉
        return None

    tls = None
    if cfg["LDAP_URL"].startswith("ldaps://"):
        tls = Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=cfg["LDAP_CA_CERT"] or None)
    server = Server(cfg["LDAP_URL"], use_ssl=tls is not None, tls=tls, get_info=ALL, connect_timeout=5)

    try:
        conn = Connection(server, user=f"{username}@{cfg['AD_DOMAIN']}", password=password, auto_bind=True)
    except LDAPException as e:
        current_app.logger.info("AD 登入失敗 %s: %s", username, e)
        return None

    try:
        group = cfg["LDAP_REQUIRED_GROUP"]
        if group:
            search_filter = _in_group_filter(username, group)
        else:
            search_filter = f"(sAMAccountName={escape_filter_chars(username)})"
        conn.search(cfg["LDAP_BASE_DN"], search_filter, SUBTREE, attributes=["displayName", "mail"])
        if not conn.entries:
            current_app.logger.info("AD 帳號 %s 不在允許群組內或找不到", username)
            return None
        entry = conn.entries[0]
        display_name = str(entry.displayName) if entry.displayName else username
        email = str(entry.mail) if entry.mail else None

        is_approver = False
        if cfg["LDAP_APPROVER_GROUP"]:
            conn.search(cfg["LDAP_BASE_DN"], _in_group_filter(username, cfg["LDAP_APPROVER_GROUP"]), SUBTREE)
            is_approver = bool(conn.entries)
        return display_name, email, is_approver
    finally:
        conn.unbind()


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
                bootstrap = in_approver_group or username in current_app.config["APPROVER_USERS"]
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
