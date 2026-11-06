"""結案與發送流程（維護背景服務 dailyreport-maint 執行）。

結案（二線以上）→ 鎖檔 1 分鐘（檔案管理員可在這段時間取消結案）→
  1. 產生正式 PDF（只產生一次，存檔並計算 MD5 / SHA-256，之後下載、上傳都用這個檔案）
  2. 上傳 SharePoint 指定資料夾
  3. 取得檔案連結
  4. 寄出日報 Mail（含連結與 MD5）
任何一步失敗立即中止，畫面顯示原因，二線以上按「重試」從失敗的那一步繼續；已完成的步驟不會重做。
"""

import hashlib
import html
import logging
import os
import string
from datetime import datetime, timedelta

from flask import current_app

from . import db, sysconf
from .models import DELIVERY_STEPS, Delivery, Report, ReviewLog

log = logging.getLogger("dailyreport.delivery")
LOCK_SECONDS = 60


class StepError(RuntimeError):
    pass


def final_dir():
    return os.path.join(current_app.instance_path, "final_pdf")


def final_path(d):
    return os.path.join(current_app.instance_path, d.pdf_path) if d and d.pdf_path else None


def ocr_pending(report):
    """還沒辨識完成的截圖張數（送審與核准前必須是 0：審核者要能看到辨識結果）。"""
    return sum(1 for i in report.images if i.kind == "screenshot" and i.ocr_status in (None, "pending", "running"))


# ---------------------------------------------------------------- 使用者動作


def close(report, user):
    now = datetime.now()
    d = Delivery(report=report, state="scheduled", closed_at=now, closed_by_id=user.id,
                 run_after=now + timedelta(seconds=LOCK_SECONDS))
    report.status = "closed"
    report.updated_by_id = user.id
    db.session.add(d)
    db.session.add(ReviewLog(report_id=report.id, action="close", actor_id=user.id,
                             comment=f"鎖檔；{LOCK_SECONDS} 秒後產生正式 PDF 並發送"))
    return d


def cancel(report, user, reason):
    db.session.delete(report.delivery)
    report.delivery = None
    report.status = "approved"
    report.updated_by_id = user.id
    db.session.add(ReviewLog(report_id=report.id, action="close_cancel", actor_id=user.id, comment=reason))


def retry(report, user):
    d = report.delivery
    steps = d.step_map()
    failed = next((k for k, _ in DELIVERY_STEPS if steps[k]["status"] == "error"), None)
    if failed:
        d.set_step(failed, "pending", "等待重試")
    d.state = "queued"
    db.session.add(ReviewLog(report_id=report.id, action="delivery_retry", actor_id=user.id,
                             comment=f"從「{dict(DELIVERY_STEPS)[failed]}」繼續" if failed else ""))


# ---------------------------------------------------------------- 背景執行


def due(now=None):
    now = now or datetime.now()
    return (Delivery.query.filter(((Delivery.state == "scheduled") & (Delivery.run_after <= now))
                                  | (Delivery.state == "queued")).order_by(Delivery.closed_at).all())


def tick(now=None):
    did = False
    for d in due(now):
        run(d)
        did = True
    return did


def run(d):
    d.state = "running"
    db.session.commit()
    enabled = bool(sysconf.get("delivery_enabled"))
    for key, label in DELIVERY_STEPS:
        if d.step_map()[key]["status"] in ("done", "skipped"):
            continue
        if key != "pdf" and not enabled:
            d.set_step(key, "skipped", "未啟用自動發送（系統設定 → 發送與通知）")
            continue
        d.set_step(key, "running")
        db.session.commit()
        try:
            msg = STEP_FUNCS[key](d)
        except Exception as e:  # 失敗原因顯示在日報頁，立即中止
            log.exception("發送流程 %s 失敗（%s）", label, d.report.doc_no)
            db.session.rollback()
            d = db.session.get(Delivery, d.id)
            d.set_step(key, "error", str(e)[:1000])
            d.state = "error"
            db.session.commit()
            _notify(d, "delivery_failed", step=label, error=str(e)[:500])
            return False
        d.set_step(key, "done", msg or "")
        db.session.commit()
    d.state, d.finished_at = "done", datetime.now()
    db.session.commit()
    if enabled:
        _notify(d, "delivery_done")
    return True


def _notify(d, event, **extra):
    """Teams 內部通知；失敗只記錄，不影響發送流程。"""
    from . import teams

    r = d.report
    try:
        teams.notify(event, {"date": r.report_date.isoformat(), "doc_no": r.doc_no, "filename": d.pdf_name or "",
                             "md5": d.pdf_md5 or "", "share_link": d.share_url or "", "mail_to": d.mail_to or "",
                             "closed_by": d.closed_by.name if d.closed_by else "", **extra}, report=r)
    except Exception:
        log.exception("Teams 通知失敗")
        db.session.rollback()


def _step_pdf(d):
    """正式 PDF 只產生一次：重試時檔案已存在而且檢查碼相符就沿用。"""
    from .pdf.report_pdf import pdf_filename, render_pdf

    path = final_path(d)
    if path and os.path.isfile(path) and d.pdf_md5 and _md5(path) == d.pdf_md5:
        return f"沿用已產生的正式 PDF（MD5 {d.pdf_md5}）"
    r = d.report
    name = pdf_filename(r)
    rel = os.path.join("final_pdf", f"{r.report_date:%Y}", f"{r.report_date:%m}", name)
    full = os.path.join(current_app.instance_path, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    data = render_pdf(r, preview=False)
    tmp = full + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, full)
    d.pdf_path, d.pdf_name, d.pdf_size = rel, name, len(data)
    d.pdf_md5, d.pdf_sha256 = hashlib.md5(data).hexdigest(), hashlib.sha256(data).hexdigest()
    return f"{name}（{len(data) / 1e6:.1f} MB），MD5 {d.pdf_md5}"


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _config(mail=False, sharepoint=True):
    from . import graph

    c = graph.config()
    miss = graph.missing(c, mail=mail, sharepoint=sharepoint)
    if miss:
        raise StepError("發送設定不完整：" + "、".join(miss) + "（系統設定 → 發送與通知）")
    return c


def _step_upload(d):
    from . import graph

    c = _config()
    path = final_path(d)
    if not path or not os.path.isfile(path) or _md5(path) != d.pdf_md5:
        raise StepError("正式 PDF 檔案不存在或檢查碼不符，請聯絡系統管理員")
    folder = graph.folder_for(c, d.report)
    item = graph.upload(c, path, folder, d.pdf_name)
    if int(item.get("size") or d.pdf_size) != d.pdf_size:
        raise StepError(f"SharePoint 上的檔案大小 {item.get('size')} 與正式 PDF {d.pdf_size} 不符")
    d.sp_item_id = f"{item['drive_id']}/{item['id']}"
    d.sp_web_url = item.get("webUrl", "")
    return f"{folder}/{d.pdf_name}"


def _step_link(d):
    from . import graph

    c = _config()
    drive_id, item_id = d.sp_item_id.split("/", 1)
    d.share_url = graph.share_link(c, drive_id, item_id, d.sp_web_url)
    return d.share_url


class _Fmt(string.Formatter):
    def get_value(self, key, args, kwargs):
        return kwargs.get(key, "{" + str(key) + "}")


def mail_values(d, escape=False):
    r = d.report
    d0 = r.report_date
    nxt = d0.replace(day=28) + timedelta(days=4)
    month_end = nxt - timedelta(days=nxt.day)  # 當月最後一天
    v = {"doc_no": r.doc_no, "contractor_doc_no": r.contractor_doc_no, "date": r.report_date.isoformat(),
         "yyyymmdd": f"{d0:%Y%m%d}", "month_range": f"{d0:%Y%m}01-{month_end:%m%d}",
         "revision": r.revision, "filename": d.pdf_name or "", "md5": d.pdf_md5 or "", "sha256": d.pdf_sha256 or "",
         "link": d.share_url or "", "approver": r.approver_name or ""}
    return {k: html.escape(str(x), quote=True) for k, x in v.items()} if escape else v


def render_mail(d, c):
    subject = _Fmt().format(c["mail_subject"] or "", **mail_values(d))
    body = _Fmt().format(c["mail_body"] or "", **mail_values(d, escape=True))
    return subject, body


def _step_mail(d):
    from . import graph

    c = _config(mail=True, sharepoint=False)
    to, cc = graph.addresses(c["mail_to"]), graph.addresses(c["mail_cc"])
    if not to:
        raise StepError("沒有設定收件人（系統設定 → 發送與通知）")
    subject, body = render_mail(d, c)
    graph.send_mail(c, to, subject, body, cc)
    d.mail_to = ", ".join(to + [f"cc {a}" for a in cc])
    d.mail_sent_at = datetime.now()
    return d.mail_to


STEP_FUNCS = {"pdf": _step_pdf, "upload": _step_upload, "link": _step_link, "mail": _step_mail}


# ---------------------------------------------------------------- 流程檢核表（日報頁最下方）


def timeline(report):
    """[(狀態 done/error/running/pending/skipped, 階段, 說明)]，從建立到寄出。"""
    from .ocr import status_for

    r = report
    shots = [i for i in r.images if i.kind == "screenshot"]
    pend = ocr_pending(r)
    t = [("done", "建立日報", f"{r.author.name if r.author else ''}，{r.created_at:%Y-%m-%d %H:%M}")]
    if not shots:
        t.append(("pending", "截圖辨識完成", "尚未上傳截圖"))
    elif pend:
        t.append(("running", "截圖辨識完成", f"{len(shots)} 張中還有 {pend} 張辨識中"))
    else:
        bad = sum(1 for i in shots if status_for(i, r.report_date.isoformat())[0] not in ("ok",))
        t.append(("done", "截圖辨識完成", f"{len(shots)} 張" + (f"，{bad} 張日期需要確認" if bad else "，日期全部正確")))
    submitted = r.status in ("submitted", "approved", "closed")
    t.append(("done" if submitted else ("error" if r.status == "rejected" else "pending"), "送審",
              f"{r.submitted_by.name}，{r.submitted_at:%Y-%m-%d %H:%M}" if submitted and r.submitted_by and r.submitted_at
              else ("已退回修正：" + (r.review_comment or "") if r.status == "rejected" else "")))
    approved = r.status in ("approved", "closed")
    who = ""
    if approved and r.reviewed_by:
        who = r.reviewed_by.name + (f"（代 {r.reviewed_on_behalf_of.name}）" if r.reviewed_on_behalf_of else "")
        who += f"，{r.reviewed_at:%Y-%m-%d %H:%M}" if r.reviewed_at else ""
    t.append(("done" if approved else "pending", "審核通過", who))
    d = r.delivery
    if r.status == "voided":
        t.append(("error", "結案", "已作廢"))
        return t
    if not d:
        t.append(("pending", "結案（線上確認排版後）", "核准後由二線以上確認預覽沒有問題再結案" if approved else ""))
        for _, label in DELIVERY_STEPS:
            t.append(("pending", label, ""))
        return t
    t.append(("done", "結案（鎖檔）", f"{d.closed_by.name if d.closed_by else ''}，{d.closed_at:%Y-%m-%d %H:%M:%S}"
              + (f"；{d.run_after:%H:%M:%S} 開始發送" if d.state == "scheduled" else "")))
    steps = d.step_map()
    for key, label in DELIVERY_STEPS:
        s = steps[key]
        st = s["status"]
        if st == "pending" and d.state == "scheduled":
            msg = "鎖檔中"
        else:
            msg = s.get("message", "")
        if st == "done" and key == "link" and d.share_url:
            msg = d.share_url
        t.append((st, label, msg))
    return t


# ---------------------------------------------------------------- 每日提醒


def _at(hhmm, now):
    try:
        h, m = (int(x) for x in str(hhmm).split(":"))
        return now.replace(hour=h, minute=m, second=0, microsecond=0)
    except (TypeError, ValueError):
        return None


def today_state(day):
    """今天的日報走到哪裡；已結案回傳 None（不需要提醒）。發送失敗另由首頁與健康檢查提示。"""
    r = Report.query.filter_by(report_date=day).first()
    if not r:
        return None, "尚未建立"
    if r.status == "closed":
        return r, None
    return r, r.status_label


def reminder_due(now=None):
    now = now or datetime.now()
    at = _at(sysconf.get("reminder_time"), now)
    return (bool(sysconf.get("reminder_enabled")) and at is not None and now >= at
            and sysconf.get("reminder_last") != now.date().isoformat())


def send_reminder(now=None):
    """到了提醒時間今天的日報還沒結案，就在 Teams 頻道發內部提醒（@二線管理員）；一天一次。回傳結果文字。"""
    from . import teams

    now = now or datetime.now()
    day = now.date()
    sysconf.put("reminder_last", day.isoformat())
    r, state = today_state(day)
    if state is None:
        result = f"{now:%Y-%m-%d %H:%M} 今日日報已結案，不需要提醒"
    else:
        ok, detail = teams.notify("reminder", {"date": day.isoformat(), "doc_no": r.doc_no if r else "", "status": state,
                                       "deadline": sysconf.get("deadline_time") or "13:00"}, report=r, force=True)
        result = (f"{now:%Y-%m-%d %H:%M} 已在 Teams 提醒（{state}）" if ok
                  else f"{now:%Y-%m-%d %H:%M} 提醒失敗：{detail}")
    sysconf.put("reminder_result", result)
    db.session.commit()
    return result
