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

    @app.cli.command("ai-worker")
    @click.option("--once", is_flag=True, help="處理完目前排隊的工作就結束")
    def ai_worker(once):
        """AI 建議與提醒背景服務：依據檢查結果.md 呼叫地端 AI（由 systemd 以單一程序執行）。"""
        from .ai import run_worker

        run_worker(app, once=once)

    @app.cli.command("check-md-backfill")
    @click.option("--since", help="只處理這一天（YYYY-MM-DD）之後的日報")
    def check_md_backfill(since):
        """替還沒有檢查結果.md 的已核准日報補產生一份（建立 RAG 知識庫用，不產生 AI 建議）。"""
        import datetime as dt

        from . import db
        from .check_report import snapshot
        from .models import OFFICIAL_STATUSES, CheckReport, Report

        q = Report.query.filter(Report.status.in_(OFFICIAL_STATUSES), ~Report.check_reports.any())
        if since:
            q = q.filter(Report.report_date >= dt.date.fromisoformat(since))
        n = 0
        for r in q.order_by(Report.report_date):
            snapshot(r, "backfill")
            n += 1
            if n % 20 == 0:
                db.session.commit()
        db.session.commit()
        click.echo(f"已補產生 {n} 份檢查結果.md（共 {CheckReport.query.count()} 份）。")

    @app.cli.command("import-check-md")
    @click.argument("folder", type=click.Path(exists=True, file_okay=False))
    def import_check_md(folder):
        """把 DDoc_compare 產生的 <DocNo>_比對報告.md 複製到檢查結果.md 匯出位置的 ddoc_compare/ 下（保留子資料夾結構）。"""
        import os
        import shutil

        from .ai_providers import get_setting

        root = get_setting("check_md_dir")
        if not root:
            raise click.ClickException("「系統設定」沒有設定檢查結果.md 匯出位置。")
        dest_root = os.path.join(root, "ddoc_compare")
        n = skipped = 0
        for base, _, files in os.walk(folder):
            for f in files:
                if not f.endswith(".md"):
                    continue
                src = os.path.join(base, f)
                dest = os.path.join(dest_root, os.path.relpath(src, folder))
                if os.path.exists(dest) and os.path.getsize(dest) == os.path.getsize(src):
                    skipped += 1
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(src, dest)
                n += 1
        click.echo(f"已匯入 {n} 份，略過 {skipped} 份已存在的檔案 → {dest_root}")

    @app.cli.command("maint-worker")
    @click.option("--once", is_flag=True, help="做完目前的工作與到期的排程就結束")
    def maint_worker(once):
        """維護背景服務：備份、還原、自動健康檢查（由 systemd 以單一程序執行）。"""
        from .maint import run_worker

        run_worker(app, once=once)

    @app.cli.command("backup")
    @click.option("--no-prune", is_flag=True, help="不刪除超過保留份數的舊備份（還原前的保險備份用）")
    def backup_cmd(no_prune):
        """立即備份到「系統設定 → 備份還原」設定的位置（以服務帳號執行）。"""
        from .backup import create

        name, size = create("cli", prune=not no_prune)
        click.echo(f"{name}\t{size}")

    @app.cli.command("config-get")
    @click.argument("key")
    def config_get(key):
        """讀取一項系統設定（安裝腳本用）。"""
        from . import sysconf

        v = sysconf.backup_dir() if key == "backup_dir" else sysconf.get(key)
        click.echo("" if v is None else v)

    @app.cli.command("config-set")
    @click.argument("key")
    @click.argument("value")
    def config_set(key, value):
        """寫入一項系統設定（安裝腳本帶入 --ldap-url 等選項時用；平常請在系統設定頁修改）。"""
        from . import db, sysconf
        from .models import AuditLog

        if key not in sysconf.DEFAULTS:
            raise click.ClickException(f"不認得的設定 {key}")
        default = sysconf.DEFAULTS[key]
        v = value
        if isinstance(default, bool):
            v = value.lower() in ("1", "true", "yes", "on")
        elif isinstance(default, int):
            v = int(value)
        sysconf.put(key, v)
        db.session.add(AuditLog(action="sys_settings", target=key, detail=f"{key}={v}（指令）"[:500]))
        db.session.commit()
        click.echo(f"{key} 已設定。")

    @app.cli.command("doctor-json")
    def doctor_json():
        """部署健檢（應用程式內部項目），輸出 JSON 給 scripts/dailyreport.sh doctor。"""
        import json

        from .doctor import run

        click.echo(json.dumps(run(), ensure_ascii=False))

    @app.cli.command("grant-admin")
    @click.option("--username", required=True, help="要指派為系統管理員的帳號（只能是二線管理員）")
    def grant_admin(username):
        """指派系統管理員（初次設定用；平常請在「帳號管理」頁操作）。只能指派二線管理員。"""
        from . import db
        from .models import ADMIN_ELIGIBLE_ROLES, AuditLog, User

        user = User.query.filter_by(username=username.strip().lower()).first()
        if user is None:
            raise click.ClickException("找不到這個帳號（AD 帳號要先登入一次）。")
        if user.role not in ADMIN_ELIGIBLE_ROLES:
            raise click.ClickException("只有二線管理員可以被指派為系統管理員（檔案管理員與一線使用者不行）。")
        user.sys_admin = True
        db.session.add(AuditLog(action="set_admin", target=user.username, detail="指派為系統管理員（指令）"))
        db.session.commit()
        click.echo(f"{user.username} 已指派為系統管理員。")

    @app.cli.command("create-admin")
    @click.option("--username", default="sysadmin", show_default=True, help="本機系統管理員帳號")
    @click.password_option(help="密碼（至少 12 個字元）；也可用環境變數 DR_ADMIN_PASSWORD 傳入", envvar="DR_ADMIN_PASSWORD")
    @click.option("--if-missing", is_flag=True, help="已經有本機緊急帳號時不做任何事（安裝腳本用）")
    @click.option("--rename-from", default="", help="把這個還沒登入過的本機帳號改名（安裝後引導用）")
    def create_admin(username, password, if_missing, rename_from):
        """建立或重設本機系統管理員帳號。"""
        from . import db
        from .admin_views import MIN_PASSWORD, USERNAME_RE
        from .models import AuditLog, User

        if if_missing and User.query.filter_by(role="admin").filter(User.password_hash.isnot(None)).first():
            click.echo("已有本機緊急帳號，略過。")
            return
        if len(password) < MIN_PASSWORD:
            raise click.ClickException(f"密碼至少需要 {MIN_PASSWORD} 個字元。")
        username = username.strip().lower()
        if not USERNAME_RE.match(username):
            raise click.ClickException("帳號名稱需 3～64 個字元，只能用小寫英數字與 . _ -（開頭必須是英數字）。")
        user = User.query.filter_by(username=username).first()
        if user and not user.password_hash and user.last_login:
            raise click.ClickException(f"{username} 已是 AD 帳號，請換一個系統管理員帳號名稱。")
        old = User.query.filter_by(username=rename_from.strip().lower()).first() if rename_from else None
        if old and old.username != username:
            if user:
                raise click.ClickException(f"{username} 已存在，請換一個名稱。")
            if not old.password_hash or old.last_login:
                raise click.ClickException(f"{old.username} 已經使用過，不能改名；請用新名稱另外建立。")
            old.username = username
            user = old
        if not user:
            user = User(username=username, display_name="System Administrator")
            db.session.add(user)
        user.set_role("admin")
        user.password_hash = generate_password_hash(password)
        db.session.add(AuditLog(action="set_admin", target=username, detail="create-admin 指令"))
        db.session.commit()
        click.echo(f"系統管理員 {username} 已設定。請妥善保管密碼，平時不要使用此帳號。")
