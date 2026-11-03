"""管理指令。

建立或重設系統管理員（本機帳號，不經 AD）：
    flask --app wsgi create-admin --username sysadmin
"""

import click
from werkzeug.security import generate_password_hash


def register_cli(app):
    @app.cli.command("create-admin")
    @click.option("--username", default="sysadmin", show_default=True, help="本機系統管理員帳號")
    @click.password_option(help="密碼（至少 12 個字元）")
    def create_admin(username, password):
        """建立或重設本機系統管理員帳號。"""
        from . import db
        from .models import AuditLog, User

        if len(password) < 12:
            raise click.ClickException("密碼至少需要 12 個字元。")
        username = username.strip().lower()
        user = User.query.filter_by(username=username).first()
        if user and not user.password_hash and user.last_login:
            raise click.ClickException(f"{username} 已是 AD 帳號，請換一個系統管理員帳號名稱。")
        if not user:
            user = User(username=username, display_name="System Administrator")
            db.session.add(user)
        user.set_role("admin")
        user.password_hash = generate_password_hash(password)
        db.session.add(AuditLog(action="set_admin", target=username, detail="create-admin 指令"))
        db.session.commit()
        click.echo(f"系統管理員 {username} 已設定。請妥善保管密碼，平時不要使用此帳號。")
