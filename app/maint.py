"""維護背景服務（flask maint-worker，systemd：dailyreport-maint）。

- 執行系統設定頁排入的備份 / 還原工作
- 依「系統設定 → 備份還原」的時間自動備份（錯過時間，例如當時關機，啟動後會補做）
- 結案後的發送流程（正式 PDF → SharePoint → 連結 → 寄信）與每日提醒
- 依「系統設定 → 健康檢查」的間隔自動檢查，結果顯示在設定頁；有失敗時系統管理員每一頁都會看到提醒

所有設定每一輪重新讀取，不需要重啟。
"""

import logging
import os
import time
from datetime import datetime, timedelta

from . import db, sysconf

log = logging.getLogger("dailyreport.maint")
POLL_SECONDS = 10
HEARTBEAT_SECONDS = 60


WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")


def _hm(value):
    try:
        h, m = (int(x) for x in str(value).split(":"))
        return h, m
    except (TypeError, ValueError):
        return None


def scheduled_at(now=None):
    """今天的完整備份時間（datetime），設定格式錯誤時回傳 None。"""
    now = now or datetime.now()
    hm = _hm(sysconf.get("backup_time"))
    return now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0) if hm else None


def _full_day(d):
    freq = sysconf.get("backup_full_freq") or "daily"
    if freq == "weekly":
        return d.weekday() == int(sysconf.get("backup_full_weekday") or 0)
    if freq == "monthly":
        return d.day == int(sysconf.get("backup_full_monthday") or 1)
    return True


def _last_full_slot(now):
    at = scheduled_at(now)
    if at is None:
        return None
    if now < at:
        at -= timedelta(days=1)
    for _ in range(40):
        if _full_day(at):
            return at
        at -= timedelta(days=1)
    return None


def backup_due(now=None):
    """完整備份：依頻率（每天 / 每週某天 / 每月某日）的時間到了、而且那一次還沒做。"""
    now = now or datetime.now()
    if not sysconf.get("backup_enabled"):
        return False
    slot = _last_full_slot(now)
    return slot is not None and (sysconf.get("backup_last_scheduled") or "") < slot.isoformat()


def next_backup(now=None):
    now = now or datetime.now()
    at = scheduled_at(now)
    if at is None or not sysconf.get("backup_enabled"):
        return None
    if at <= now:
        at += timedelta(days=1)
    for _ in range(40):
        if _full_day(at):
            return at
        at += timedelta(days=1)
    return None


def incr_mode():
    mode = sysconf.get("backup_incr_mode") or "off"
    if mode == "off" and int(sysconf.get("backup_db_hours") or 0):
        return "hours"  # v4.42.0 設過「資料庫快速備份」：沿用為每幾小時增量
    return mode


def incr_hours():
    if (sysconf.get("backup_incr_mode") or "off") == "off" and int(sysconf.get("backup_db_hours") or 0):
        return int(sysconf.get("backup_db_hours"))
    return int(sysconf.get("backup_incr_hours") or 6)


def _incr_last():
    try:
        return datetime.fromisoformat(sysconf.get("backup_incr_last") or sysconf.get("backup_db_last") or "")
    except ValueError:
        return None


def incr_due(now=None):
    """增量備份：每天固定時間，或每幾小時一次。"""
    now = now or datetime.now()
    mode, last = incr_mode(), _incr_last()
    if mode == "hours":
        return last is None or now - last >= timedelta(hours=incr_hours()) - timedelta(minutes=1)
    if mode == "daily":
        hm = _hm(sysconf.get("backup_incr_time"))
        if not hm:
            return False
        at = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        if now < at:
            at -= timedelta(days=1)
        return last is None or last < at
    return False


def next_incr(now=None):
    now = now or datetime.now()
    mode, last = incr_mode(), _incr_last()
    if mode == "hours":
        return max(now, last + timedelta(hours=incr_hours())) if last else now
    if mode == "daily":
        hm = _hm(sysconf.get("backup_incr_time"))
        if not hm:
            return None
        at = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        return at if at > now else at + timedelta(days=1)
    return None


def schedule_text():
    """健康檢查與設定頁用的排程說明。"""
    freq = sysconf.get("backup_full_freq") or "daily"
    t = sysconf.get("backup_time")
    full = {"daily": f"每天 {t}", "weekly": f"每週{WEEKDAYS[int(sysconf.get('backup_full_weekday') or 0)]} {t}",
            "monthly": f"每月 {sysconf.get('backup_full_monthday')} 日 {t}"}.get(freq, t)
    full = f"完整備份 {full}，保留 {sysconf.get('backup_keep')} 份" if sysconf.get("backup_enabled") else "完整備份未啟用"
    mode = incr_mode()
    incr = {"off": "增量備份關閉", "daily": f"增量備份每天 {sysconf.get('backup_incr_time')}",
            "hours": f"增量備份每 {incr_hours()} 小時"}.get(mode, "")
    if mode != "off":
        incr += f"，保留 {sysconf.get('backup_incr_keep')} 份"
    return f"{full}；{incr}"


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
    from .doctor import summarize

    summary = {"at": datetime.now().isoformat(timespec="seconds"), "trigger": trigger, **summarize(items),
               "items": items}
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


def _audit_since(name):
    """還原會把稽核紀錄一起換回備份當時：先記下目前資料庫中比備份新的紀錄（含要求還原、身分確認失敗），還原後再寫回。"""
    import sqlite3

    from . import backup
    from .models import AuditLog

    with sqlite3.connect(f"file:{os.path.join(backup.path_of(name), 'dailyreport.db')}?mode=ro", uri=True) as con:
        try:
            last = con.execute("SELECT MAX(id) FROM audit_log").fetchone()[0] or 0
        except sqlite3.Error:
            last = 0
    return [{"action": a.action, "actor": a.actor.username if a.actor else "", "target": a.target or "",
             "detail": a.detail or "", "created_at": a.created_at}
            for a in AuditLog.query.filter(AuditLog.id > last).order_by(AuditLog.id)]


def _put_back(rows):
    from .models import AuditLog, User

    for r in rows:
        actor = User.query.filter_by(username=r["actor"]).first() if r["actor"] else None
        detail = r["detail"] if actor or not r["actor"] else f"（操作者 {r['actor']}，備份中沒有這個帳號）{r['detail']}"
        db.session.add(AuditLog(action=r["action"], actor_id=actor.id if actor else None, target=r["target"],
                                detail=detail, created_at=r["created_at"]))


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
            if job.param == "db":
                name, size = backup.create(job.trigger, user, db_only=True)
            else:
                name, size = backup.create(job.trigger, user, mode="incr" if job.param == "incr" else "full")
            msg, ok = f"{name}（{size / 1e6:.1f} MB）", True
        elif job.kind == "restore":
            username = user.username if user else ""
            newer = _audit_since(job.param)
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
            _put_back(newer)
            db.session.add(AuditLog(action="restore", actor_id=info["requested_by_id"], target=info["param"],
                                    detail=f"還原前備份 {pre}；還原後保留 {len(newer)} 筆比備份新的稽核紀錄"))
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


AUDIT_MIN_DAYS, AUDIT_MAX_DAYS = 180, 366


def audit_retention_days():
    try:
        n = int(sysconf.get("audit_retention_days") or AUDIT_MAX_DAYS)
    except (TypeError, ValueError):
        n = AUDIT_MAX_DAYS
    return min(AUDIT_MAX_DAYS, max(AUDIT_MIN_DAYS, n))


def purge_audit(now=None):
    """每天一次：刪除超過保存天數（180–366）的稽核紀錄，並記一筆清除紀錄。回傳刪除筆數。"""
    from datetime import timedelta

    from .models import AuditLog

    now = now or datetime.now()
    if sysconf.get("audit_purge_last") == now.date().isoformat():
        return 0
    days = audit_retention_days()
    cutoff = datetime.combine(now.date() - timedelta(days=days), datetime.min.time())
    old = AuditLog.query.filter(AuditLog.created_at < cutoff)
    n = old.count()
    if n:
        oldest = old.order_by(AuditLog.created_at).first().created_at
        old.delete(synchronize_session=False)
        db.session.add(AuditLog(action="audit_purge", actor_id=None, target=f"{days} 天",
                                detail=f"清除 {cutoff:%Y-%m-%d} 以前的稽核紀錄 {n} 筆（最舊 {oldest:%Y-%m-%d}）"))
        log.warning("清除過期稽核紀錄 %d 筆（保存 %d 天）", n, days)
    sysconf.put("audit_purge_last", now.date().isoformat())
    db.session.commit()
    return n


def tick(now=None):
    """一輪：排程備份 → 排隊中的工作 → 健康檢查。回傳是否做了事。"""
    from .models import MaintJob

    now = now or datetime.now()
    did = False
    if backup_due(now):
        sysconf.put("backup_last_scheduled", now.isoformat(timespec="seconds"))
        db.session.commit()
        enqueue("backup", trigger="scheduled")
        if incr_due(now):  # 同一時間只做完整備份，這一次的增量就不用了
            sysconf.put("backup_incr_last", now.isoformat(timespec="seconds"))
            db.session.commit()
    elif incr_due(now) and enqueue("backup", trigger="scheduled", param="incr"):
        sysconf.put("backup_incr_last", now.isoformat(timespec="seconds"))  # 排得進去才算；有工作在跑時下一輪再試
        db.session.commit()
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
        from . import teams

        did = bool(teams.flush()) or did  # 網頁動作排入的 Teams 通知（日報狀態變更）
        if teams.health_due(now):
            teams.push_health(now)  # 排程推送健康檢查
            did = True
    except Exception:
        log.exception("發送流程 / 每日提醒錯誤")
        db.session.rollback()
    try:
        did = purge_audit(now) or did
    except Exception:
        log.exception("清除過期稽核紀錄失敗")
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
        from . import delivery

        try:
            delivery.recover_interrupted(all_running=True)  # 上次被終止時停在「執行中」的發送 → 中止，可重試
        except Exception:
            log.exception("檢查中斷的發送流程失敗")
            db.session.rollback()
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
