import os
import sqlite3

from dotenv import load_dotenv
from flask import Flask
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


def _split_list(value):
    return {v.strip().lower() for v in (value or "").split(",") if v.strip()}


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    # WAL：網頁讀取（檢查、告警歷程）不會被 OCR 背景服務的寫入擋住；busy_timeout：同時寫入時等待而不是直接失敗
    if isinstance(dbapi_conn, sqlite3.Connection):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.close()


def create_app():
    load_dotenv()
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)

    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL", "sqlite:///dailyreport.db"),
        AUTH_MODE=os.environ.get("AUTH_MODE", "ldap"),
        LDAP_URL=os.environ.get("LDAP_URL", ""),
        AD_DOMAIN=os.environ.get("AD_DOMAIN", ""),
        LDAP_BASE_DN=os.environ.get("LDAP_BASE_DN", ""),
        LDAP_REQUIRED_GROUP=os.environ.get("LDAP_REQUIRED_GROUP", ""),
        LDAP_APPROVER_GROUP=os.environ.get("LDAP_APPROVER_GROUP", ""),
        LDAP_CA_CERT=os.environ.get("LDAP_CA_CERT", ""),
        APPROVER_USERS=_split_list(os.environ.get("APPROVER_USERS")),
        UPLOAD_DIR=os.environ.get("UPLOAD_DIR", os.path.join(app.instance_path, "uploads")),
        MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_MB", "20")) * 1024 * 1024,
        PDF_TEMPLATE_XLSX=os.environ.get("PDF_TEMPLATE_XLSX")
        or os.path.join(app.instance_path, "templates", "checklist.xlsx"),
        FONT_DIR=os.environ.get("FONT_DIR") or os.path.join(app.instance_path, "fonts"),
        # 檢查結果.md 匯出位置的預設值；實際位置在「系統設定」頁設定
        CHECK_MD_DIR=os.environ.get("CHECK_MD_DIR", os.path.join(app.instance_path, "check_reports")),
        SESSION_COOKIE_HTTPONLY=True,
        # 部署在 HTTPS（nginx）後方時，cookie 只經 HTTPS 傳送
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

    from . import ai, ocr

    app.jinja_env.globals.update(STATUS_LABELS=models.STATUS_LABELS, RESULT_LABELS=models.RESULT_LABELS,
                                 REVIEW_ACTION_LABELS=models.REVIEW_ACTION_LABELS,
                                 ocr_status=ocr.status_for, ocr_clock=ocr.clock_of)
    app.jinja_env.filters["ai_html"] = ai.md_to_html
    app.jinja_env.tests["ocr_is"] = lambda img, code, day: ocr.status_for(img, day)[0] == code

    with app.app_context():
        db.create_all()
        _add_missing_columns()
        _migrate_roles()
        seed_master_data()

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
