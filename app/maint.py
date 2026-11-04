"""維護背景服務（flask maint-worker，systemd：dailyreport-maint）。

- 執行系統設定頁排入的備份 / 還原工作
- 依「系統設定 → 備份還原」的時間自動備份（錯過時間，例如當時關機，啟動後會補做）
- 結案後的發送流程（正式 PDF → SharePoint → 連結 → 寄信）與每日提醒
- 依「系統設定 → 健康檢查」的間隔自動檢查，結果顯示在設定頁；有失敗時系統管理員每一頁都會看到提醒

所有設定每一輪重新讀取，不需要重啟。
"""

import logging
import time
from datetime import datetime, timedelta

from . import db, sysconf

log = logging.getLogger("dailyreport.maint")
POLL_SECONDS = 10
HEARTBEAT_SECONDS = 60


def scheduled_at(now=None):
    """今天的排程時間（datetime），設定格式錯誤時回傳 None。"""
    now = now or datetime.now()
    try:
        h, m = (int(x) for x in str(sysconf.get("backup_time")).split(":"))
        return now.replace(hour=h, minute=m, second=0, microsecond=0)
    except (TypeError, ValueError):
        return None


def backup_due(now=None):
    now = now or datetime.now()
    if not sysconf.get("backup_enabled"):
        return False
    at = scheduled_at(now)
    if at is None:
        return False
    if now < at:
        at -= timedelta(days=1)  # 還沒到今天的時間：看昨天那一次有沒有做
    last = sysconf.get("backup_last_scheduled") or ""
    return last < at.isoformat()


def next_backup(now=None):
    now = now or datetime.now()
    at = scheduled_at(now)
    if at is None or not sysconf.get("backup_enabled"):
        return None
    return at if at > now else at + timedelta(days=1)


def request_health():
    """設定變更後呼叫：維護服務下一輪（約 10 秒內）重新做健康檢查，系統狀態頁就不會顯示變更前的結果。"""
    sysconf.put("health_refresh", datetime.now().isoformat(timespec="seconds"))


def health_due(now=None):
    if sysconf.get("health_refresh"):
        return True
    if not sysconf.get("health_enabled"):
        return False
    last = sysconf.get("health_last") or {}
    if not last.get("at"):
        return True
    hours = max(1, int(sysconf.get("health_interval_hours") or 6))
    return datetime.fromisoformat(last["at"]) <= (now or datetime.now()) - timedelta(hours=hours)


def run_health(trigger="scheduled"):
    """執行健康檢查並儲存結果（設定頁顯示、頁首提醒）。"""
    from .doctor import run

    items = run(system=True)
    summary = {"at": datetime.now().isoformat(timespec="seconds"), "trigger": trigger,
               "ok": sum(1 for i in items if i["status"] == "ok"),
               "warn": sum(1 for i in items if i["status"] == "warn"),
               "fail": sum(1 for i in items if i["status"] == "fail"), "items": items}
    sysconf.put("health_last", summary)
    sysconf.put("health_refresh", "")
    db.session.commit()
    return summary


def enqueue(kind, user=None, trigger="manual", param=""):
    from .models import MaintJob

    if MaintJob.query.filter(MaintJob.status.in_(("pending", "running"))).count():
        return None
    job = MaintJob(kind=kind, trigger=trigger, param=param, requested_by_id=user.id if user else None)
    db.session.add(job)
    db.session.commit()
    return job


def worker_alive():
    hb = sysconf.get("maint_heartbeat")
    return bool(hb) and datetime.fromisoformat(hb) > datetime.now() - timedelta(seconds=HEARTBEAT_SECONDS * 3)


def _run_job(job):
    from . import backup
    from .models import AuditLog, MaintJob, User

    job.status, job.started_at = "running", datetime.now()
    db.session.commit()
    info = {"kind": job.kind, "trigger": job.trigger, "param": job.param, "requested_by_id": job.requested_by_id,
            "created_at": job.created_at, "started_at": job.started_at}
    user = db.session.get(User, job.requested_by_id) if job.requested_by_id else None
    try:
        if job.kind == "backup":
            name, size = backup.create(job.trigger, user)
            msg, ok = f"{name}（{size / 1e6:.1f} MB）", True
        elif job.kind == "restore":
            username = user.username if user else ""
            pre = backup.restore(job.param, user)
            # 資料庫已換成備份當時的內容：這筆工作與稽核紀錄要重新寫入
            msg, ok = f"已還原 {job.param}；還原前的資料已備份為 {pre}", True
            actor = User.query.filter_by(username=username).first() if username else None
            info["requested_by_id"] = actor.id if actor else None
            # 備份當時正在執行的工作（至少有產生那份備份的工作本身）也一起回來了，標成結束
            MaintJob.query.filter(MaintJob.status.in_(("pending", "running"))).update(
                {"status": "error", "message": "備份當時尚未完成的工作（已因還原結束）", "finished_at": datetime.now()},
                synchronize_session=False)
            job = MaintJob(**info)
            db.session.add(job)
            db.session.add(AuditLog(action="restore", actor_id=info["requested_by_id"], target=info["param"],
                                    detail=f"還原前備份 {pre}"))
        else:
            raise RuntimeError(f"不認得的工作 {job.kind}")
    except Exception as e:  # 失敗原因顯示在設定頁
        log.exception("維護工作失敗")
        db.session.rollback()
        job = db.session.get(MaintJob, job.id) if job.id else job
        msg, ok = str(e)[:1000], False
    job.status = "done" if ok else "error"
    job.message, job.finished_at = msg, datetime.now()
    db.session.add(job)
    db.session.commit()
    return ok


def tick(now=None):
    """一輪：排程備份 → 排隊中的工作 → 健康檢查。回傳是否做了事。"""
    from .models import MaintJob

    now = now or datetime.now()
    did = False
    if backup_due(now):
        sysconf.put("backup_last_scheduled", now.isoformat(timespec="seconds"))
        db.session.commit()
        enqueue("backup", trigger="scheduled")
    job = MaintJob.query.filter_by(status="pending").order_by(MaintJob.id).first()
    if job:
        _run_job(job)
        did = True
    from . import delivery

    try:
        did = delivery.tick(now) or did  # 結案後的正式 PDF、SharePoint、寄信
        if delivery.reminder_due(now):
            delivery.send_reminder(now)
            did = True
    except Exception:
        log.exception("發送流程 / 每日提醒錯誤")
        db.session.rollback()
    if health_due(now):
        try:
            run_health()
        except Exception:
            log.exception("健康檢查失敗")
            db.session.rollback()
        did = True
    return did


def run_worker(app, once=False):
    from .models import MaintJob

    with app.app_context():
        log.warning("維護背景服務啟動（備份、還原、健康檢查；設定在「系統設定」頁）")
        # 上次服務中斷時留下的「執行中」工作
        for j in MaintJob.query.filter_by(status="running"):
            j.status, j.message, j.finished_at = "error", "維護服務中斷，工作未完成", datetime.now()
        db.session.commit()
        last_hb = 0.0
        while True:
            if time.monotonic() - last_hb > HEARTBEAT_SECONDS:
                sysconf.put("maint_heartbeat", datetime.now().isoformat(timespec="seconds"))
                db.session.commit()
                last_hb = time.monotonic()
            try:
                did = tick()
            except Exception:
                log.exception("維護背景服務錯誤")
                db.session.rollback()
                did = False
            if once and not did:
                return
            db.session.remove()
            if not did:
                time.sleep(POLL_SECONDS)
