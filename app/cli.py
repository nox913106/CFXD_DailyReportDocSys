"""管理指令。

建立或重設系統管理員（本機帳號，不經 AD）：
    flask --app wsgi create-admin --username sysadmin
"""

import click
from werkzeug.security import generate_password_hash


def register_cli(app):
    @app.cli.command("ocr-worker")
    @click.option("--once", is_flag=True, help="處理完目前排隊的截圖就結束")
    def ocr_worker(once):
        """截圖系統時間 OCR 背景服務（由 systemd 以單一程序執行）。"""
        from . import db
        from .models import ReportImage
        from .ocr import run_worker

        # 尚未辨識過的舊截圖一併排入
        n = ReportImage.query.filter(ReportImage.kind == "screenshot", ReportImage.ocr_status.is_(None)) \
            .update({"ocr_status": "pending"}, synchronize_session=False)
        db.session.commit()
        if n:
            click.echo(f"排入 {n} 張尚未辨識的截圖。")
        run_worker(app, once=once)

    @app.cli.command("ocr-requeue")
    @click.option("--date", "day", help="只重新辨識這一天的日報（YYYY-MM-DD），預設全部")
    def ocr_requeue(day):
        """把截圖重新排入 OCR（例如更新辨識規則後）。"""
        import datetime as dt

        from . import db
        from .models import Report, ReportImage

        q = ReportImage.query.filter_by(kind="screenshot")
        if day:
            r = Report.query.filter_by(report_date=dt.date.fromisoformat(day)).first()
            if not r:
                raise click.ClickException(f"找不到 {day} 的日報")
            q = q.filter_by(report_id=r.id)
        n = q.update({"ocr_status": "pending", "ocr_result": None}, synchronize_session=False)
        db.session.commit()
        click.echo(f"已排入 {n} 張截圖。")

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
