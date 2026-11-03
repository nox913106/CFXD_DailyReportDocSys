import os

from dotenv import load_dotenv
from flask import Flask
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
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)

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

    app.jinja_env.globals.update(STATUS_LABELS=models.STATUS_LABELS, RESULT_LABELS=models.RESULT_LABELS)

    with app.app_context():
        db.create_all()
        seed_master_data()

    return app
