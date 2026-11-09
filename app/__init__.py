import os
import sqlite3

from dotenv import load_dotenv
from flask import Flask, Request, request
from sqlalchemy import event
from sqlalchemy.engine import Engine
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()
csrf = CSRFProtect()
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "請先登入。"


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    # WAL：網頁讀取（檢查、告警歷程）不會被 OCR 背景服務的寫入擋住；busy_timeout：同時寫入時等待而不是直接失敗
    if isinstance(dbapi_conn, sqlite3.Connection):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.close()


class _Request(Request):
    """單次上傳上限由「系統設定 → PDF 與上傳」決定（nginx 固定放寬到 sysconf.UPLOAD_CEILING_MB）。"""

    @property
    def max_content_length(self):
        from . import sysconf

        try:
            return sysconf.max_upload_bytes()
        except Exception:  # 資料庫還沒建立等情況
            return super().max_content_length


def create_app():
    load_dotenv()
    app = Flask(__name__, instance_relative_config=True)
    app.request_class = _Request
    os.makedirs(app.instance_path, exist_ok=True)

    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL", "sqlite:///dailyreport.db"),
        AUTH_MODE=os.environ.get("AUTH_MODE", "ldap"),
        LDAP_URL=os.environ.get("LDAP_URL", ""),
        AD_DOMAIN=os.environ.get("AD_DOMAIN", ""),
        LDAP_BASE_DN=os.environ.get("LDAP_BASE_DN", ""),
        LDAP_REQUIRED_GROUP=os.environ.get("LDAP_REQUIRED_GROUP", ""),
        LDAP_CA_CERT=os.environ.get("LDAP_CA_CERT", ""),
        UPLOAD_DIR=os.environ.get("UPLOAD_DIR", os.path.join(app.instance_path, "uploads")),
        MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_MB", "20")) * 1024 * 1024,
        PDF_TEMPLATE_XLSX=os.environ.get("PDF_TEMPLATE_XLSX")
        or os.path.join(app.instance_path, "templates", "checklist.xlsx"),
        FONT_DIR=os.environ.get("FONT_DIR") or os.path.join(app.instance_path, "fonts"),
        # 檢查結果.md 匯出位置的預設值；實際位置在「系統設定」頁設定
        CHECK_MD_DIR=os.environ.get("CHECK_MD_DIR", os.path.join(app.instance_path, "check_reports")),
        # 備份位置的預設值；實際位置在「系統設定 → 備份還原」設定
        BACKUP_DIR=os.environ.get("DR_BACKUP_DIR", ""),
        SESSION_COOKIE_HTTPONLY=True,
        # 部署在 HTTPS（nginx）後方時，cookie 只經 HTTPS 傳送
        PUBLIC_URL=os.environ.get("PUBLIC_URL", ""),  # 通知中的日報連結，例如 https://report.corp.local
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
        REMEMBER_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
        SESSION_COOKIE_SAMESITE="Lax",
    )
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
    if os.environ.get("BEHIND_PROXY") == "1":
        # nginx 反向代理：信任一層 X-Forwarded-For / X-Forwarded-Proto（取得真實 IP 與 https）
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # dev 模式會讓任何帳號登入，正式環境必須用 ldap
    if app.config["AUTH_MODE"] == "dev" and not app.debug and os.environ.get("FLASK_DEBUG") != "1":
        raise RuntimeError("AUTH_MODE=dev 只能在開發模式 (FLASK_DEBUG=1) 下使用")

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)

    from . import models
    from .auth import bp as auth_bp
    from .editors import bp as editors_bp
    from .images import bp as images_bp
    from .seed import seed_master_data
    from .views import bp as main_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(editors_bp)
    app.register_blueprint(images_bp)
    from .settings_views import bp as settings_bp

    app.register_blueprint(settings_bp)
    from .items_views import bp as items_bp
    from .import_views import bp as import_bp

    app.register_blueprint(items_bp)
    app.register_blueprint(import_bp)

    @app.before_request
    def _viewer_only_archive():
        """訪客（guest）只能看歡迎頁；檢視者（viewer）只能看正式文件下載頁、下載已結案的正式 PDF。其餘一律禁止。"""
        from flask import abort, redirect, url_for
        from flask_login import current_user

        if not getattr(current_user, "is_authenticated", False) or current_user.role not in ("viewer", "guest"):
            return None
        ep = request.endpoint or ""
        if current_user.role == "guest":
            if ep in ("static", "views.welcome") or ep.startswith("auth."):
                return None
            if request.method == "GET":
                return redirect(url_for("views.welcome"))
            abort(403)
        if ep in ("static", "views.archive", "views.final_pdf") or ep.startswith("auth."):
            return None
        if ep == "views.index":
            return redirect(url_for("views.archive"))
        abort(403)

    @app.after_request
    def _refresh_health(resp):
        """系統管理員在設定頁變更任何設定後，健康檢查結果跟著更新（由維護服務在背景執行）。"""
        if (request.method == "POST" and request.path.startswith("/admin/") and resp.status_code < 400
                and request.endpoint != "settings.health_run"):
            from . import maint

            try:
                maint.request_health()
                db.session.commit()
            except Exception:
                db.session.rollback()
        return resp

    from . import ai, ocr

    app.jinja_env.globals.update(STATUS_LABELS=models.STATUS_LABELS, RESULT_LABELS=models.RESULT_LABELS,
                                 REVIEW_ACTION_LABELS=models.REVIEW_ACTION_LABELS,
                                 ocr_status=ocr.status_for, ocr_clock=ocr.clock_of)
    app.jinja_env.filters["ai_html"] = ai.md_to_html
    from .doctor import LEVEL_LABELS

    app.jinja_env.globals["LEVEL_LABELS"] = LEVEL_LABELS

    @app.context_processor
    def _admin_alert():
        """系統管理員每一頁都看得到最近一次自動健康檢查的失敗項目。"""
        from flask_login import current_user

        from . import sysconf

        if not getattr(current_user, "is_authenticated", False) or not current_user.is_admin:
            return {}
        try:
            last = sysconf.get("health_last") or {}
        except Exception:
            return {}
        severe = [i for i in last.get("items", []) if i.get("status") in ("alert", "danger", "fail")]
        return {"health_alert": {"at": last.get("at", ""), "items": severe} if severe else None}
    app.jinja_env.tests["ocr_is"] = lambda img, code, day: ocr.status_for(img, day)[0] == code

    with app.app_context():
        db.create_all()
        _add_missing_columns()
        _migrate_roles()
        seed_master_data()

    from flask_wtf.csrf import CSRFError

    @app.errorhandler(CSRFError)
    def _csrf_error(e):
        """頁面開太久（CSRF token 預設 1 小時過期）或登入已失效時送出表單。

        自動儲存收到 JSON，app.js 會換新 token 再送一次；一般送出顯示說明頁，
        畫面上的內容已暫存在瀏覽器，回到上一頁即可還原後再儲存。"""
        from flask import jsonify, render_template

        if request.headers.get("X-Autosave") == "1" or request.accept_mimetypes.best == "application/json":
            return jsonify(ok=False, error="csrf", detail=e.description), 400
        return render_template("csrf_expired.html", reason=e.description), 400

    @app.get("/csrf-token")
    def csrf_token_refresh():
        """自動儲存遇到 token 過期時取得新的 token（需要登入；未登入時回 401，不轉到登入頁）。"""
        from flask import jsonify
        from flask_login import current_user
        from flask_wtf.csrf import generate_csrf

        if not getattr(current_user, "is_authenticated", False):
            return jsonify(ok=False, error="login"), 401
        return jsonify(ok=True, csrf=generate_csrf())

    @app.get("/healthz")
    def healthz():
        """部署檢查用：不需登入，只回報資料庫是否可用，不含任何資料。"""
        from sqlalchemy import text

        try:
            db.session.execute(text("SELECT 1"))
            return {"status": "ok"}
        except Exception:
            return {"status": "error"}, 503

    from .cli import register_cli

    register_cli(app)

    return app


def _add_missing_columns():
    """create_all 不會替既有資料表加欄位；版本更新新增的可空欄位在此補上。"""
    from sqlalchemy import inspect, text

    insp = inspect(db.engine)
    for table in db.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in existing and col.nullable:
                ddl = col.type.compile(dialect=db.engine.dialect)
                with db.engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}'))


def _migrate_roles():
    """舊版只有 is_approver：轉成角色（審核者 -> 檔案管理員，其餘 -> 一線使用者）。"""
    from .models import User

    changed = False
    for u in User.query.filter((User.role.is_(None)) | (User.role == "")):
        u.set_role("file_manager" if u.is_approver else "first_line")
        changed = True
    if changed:
        db.session.commit()
