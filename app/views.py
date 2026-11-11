from datetime import date, datetime, timedelta

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from . import db, docno, teams
import os

from flask import current_app

from . import approval as perm
from .approval import approvers, pending_for
from .ai_providers import ai_enabled
from .check_report import render as render_check_md
from .check_report import snapshot
from .checks import run_checks
from .helpers import get_report
from .models import (
    ADMIN_ELIGIBLE_ROLES,
    OFFICIAL_STATUSES,
    REVIEWER_ROLES,
    ASSIGNABLE_ROLES,
    ROLE_LABELS,
    AlarmSummaryRow,
    ApprovalDelegation,
    AuditLog,
    CheckReport,
    ChecklistItem,
    ChecklistResult,
    IssueRecord,
    OnsiteRecord,
    Report,
    ReportImage,
    ReviewLog,
    SystemUpdateRecord,
    TrueAlarmRecord,
    User,
)

bp = Blueprint("views", __name__)


@bp.before_request
@login_required
def _require_login():
    pass


@bp.route("/welcome")
def welcome():
    """訪客（AD 首次登入、尚未開通）唯一能看到的頁面：請系統管理員設定權限。"""
    if current_user.role != "guest":
        return redirect(url_for("views.index"))
    admins = [u for u in User.query.order_by(User.display_name, User.username) if u.is_admin and not u.is_local_admin
              and "#" not in u.username]
    return render_template("welcome.html", admins=admins)


@bp.route("/")
def index():
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    try:
        first = datetime.strptime(month, "%Y-%m").date()
    except ValueError:
        abort(400)
    next_first = date(first.year + first.month // 12, first.month % 12 + 1, 1)
    reports = (
        Report.query.filter(Report.report_date >= first, Report.report_date < next_first)
        .order_by(Report.report_date.desc())
        .all()
    )
    pending = pending_for(current_user)
    pending_checks = {r.id: run_checks(r, history=False).counts() for r in pending}
    return render_template(
        "index.html", reports=reports, month=month, pending=pending,
        pending_checks=pending_checks,
        today=date.today().isoformat(), can_create=perm.can_create(current_user),
        official=last_official(date.today() + timedelta(days=1)), **_desk(),
        guests=User.query.filter_by(role="guest").count() if current_user.is_admin else 0,
    )


def _desk():
    """二線以上首頁：待結案（已核准）與發送中止的日報。"""
    from .models import REVIEWER_ROLES, Delivery

    if not (current_user.is_admin or current_user.role in REVIEWER_ROLES):
        return {"to_close": [], "delivery_errors": []}
    return {"to_close": Report.query.filter_by(status="approved").order_by(Report.report_date).all(),
            "delivery_errors": Report.query.join(Delivery).filter(Delivery.state == "error")
            .order_by(Report.report_date).all()}


def last_official(d):
    """d 之前最近一份已核准（正式）的日報。"""
    return (Report.query.filter(Report.report_date < d, Report.status.in_(OFFICIAL_STATUSES))
            .order_by(Report.report_date.desc()).first())


def _copy_cols(obj, model, skip=("id", "report_id")):
    return {c.name: getattr(obj, c.name) for c in model.__table__.columns if c.name not in skip}


COPY_REPORT_FIELDS = ("issue_purpose", "inspection_time", "inspection_summary", "second_line_support", "onsite_summary",
                      "alarm_false")


def _copy_official(report, src):
    """複製正式日報的內容：封面、紀錄、告警、檢查表（另外處理）。不複製：日期、文件編號、版次、撰寫人、
    值班人員（今天的建立者）、送審 / 核准 / 作廢狀態與截圖。"""
    for f in COPY_REPORT_FIELDS:
        setattr(report, f, getattr(src, f))
    report.alarm_new = 0  # 內容與前一份相同時沒有新增；有新告警再改
    for rel, model in (("issues", IssueRecord), ("onsite_records", OnsiteRecord),
                       ("system_updates", SystemUpdateRecord), ("true_alarms", TrueAlarmRecord)):
        for row in getattr(src, rel):
            getattr(report, rel).append(model(**_copy_cols(row, model)))


def _copy_photos(report, src):
    """現場照片、告警照片連同檔案一起複製（截圖不複製）。"""
    import shutil
    import uuid

    root = current_app.config["UPLOAD_DIR"]
    os.makedirs(os.path.join(root, str(report.id)), exist_ok=True)
    for img in src.images:
        if img.kind not in ("onsite", "alarm"):  # 截圖（含暫存區）不複製
            continue
        path = os.path.join(root, str(src.id), img.filename)
        if not os.path.isfile(path):
            continue
        name = f"{uuid.uuid4().hex}{os.path.splitext(img.filename)[1]}"
        shutil.copyfile(path, os.path.join(root, str(report.id), name))
        db.session.add(ReportImage(report_id=report.id, kind=img.kind, filename=name, original_name=img.original_name,
                                   order=img.order, uploaded_by_id=current_user.id))


@bp.route("/reports/new", methods=["POST"])
def create():
    if not perm.can_create(current_user):
        abort(403)
    try:
        d = datetime.strptime(request.form.get("report_date", ""), "%Y-%m-%d").date()
    except ValueError:
        flash("日期格式錯誤。", "error")
        return redirect(url_for("views.index"))

    existing = Report.query.filter_by(report_date=d).first()
    if existing:
        flash(f"{d} 的日報已經存在。", "error")
        return redirect(url_for("views.detail", report_id=existing.id))

    # 預設複製上一份已核准（正式）日報的全部內容，截圖除外；取消勾選時只沿用值班、核准人與已知告警
    shown = request.form.get("copy_shown") == "1"
    copy = request.form.get("copy_prev") == "1" if shown else True
    official = last_official(d) if copy else None
    prev = official or Report.query.filter(Report.report_date < d).order_by(Report.report_date.desc()).first()
    report = Report(
        report_date=d,
        doc_no=docno.doc_no(d),
        contractor_doc_no=docno.contractor_doc_no(d),
        author_id=current_user.id,
        updated_by_id=current_user.id,
        first_line_officer=current_user.name,
    )
    if not prev or (prev.approver_id is None and not prev.approver_name):
        only = approvers()
        if len(only) == 1:  # 只有一位核准人時預設指定給他
            report.approver_id, report.approver_name = only[0].id, only[0].name
    if prev:
        # 固定資料與已知告警清單；告警計數滾動：昨天的新增今天變成已知
        report.second_line_support = prev.second_line_support
        report.approver_id = prev.approver_id
        report.approver_name = prev.approver_name
        report.inspection_time = prev.inspection_time
        report.alarm_known = (prev.alarm_known or 0) + (prev.alarm_new or 0)
        for row in prev.alarm_rows:
            report.alarm_rows.append(AlarmSummaryRow(**_copy_cols(row, AlarmSummaryRow)))
    if official:
        _copy_official(report, official)
    db.session.add(report)
    db.session.flush()

    prev_results = {}
    if prev:
        q = db.session.query(ChecklistResult.item_id, ChecklistResult.result, ChecklistResult.note).filter(
            ChecklistResult.report_id == prev.id)
        prev_results = {i: (res if official else None, note or "") for i, res, note in q}
    for (item_id,) in db.session.query(ChecklistItem.id).filter_by(active=True):
        res, note = prev_results.get(item_id, (None, ""))
        db.session.add(ChecklistResult(report_id=report.id, item_id=item_id, result=res, note=note))
    if official:
        _copy_photos(report, official)
    teams.status_changed(report, "new", "create", current_user)
    db.session.commit()
    flash(f"已建立 {d} 日報 {report.doc_no}"
          + (f"，已複製 {official.report_date} 已核准日報的內容（截圖除外），請逐項確認今天的狀況。" if official else "。"),
          "success")
    return redirect(url_for("editors.basic", report_id=report.id))


@bp.route("/reports/<int:report_id>")
def detail(report_id):
    """完整日報檢視：嵌入與原始範本相同排版的 PDF，並提供送審/審核操作。"""
    report = get_report(report_id)
    u = current_user
    review = perm.can_review(u, report)
    snaps = report.check_reports.order_by(None).order_by(CheckReport.id.desc()).all()
    from . import delivery

    return render_template(
        "report_detail.html", report=report, can_review=review.allowed, principal=review.principal,
        checks=run_checks(report) if report.status not in ("voided", "closed") else None,
        timeline=delivery.timeline(report), ocr_pending=delivery.ocr_pending(report),
        shot_groups=_shot_groups(report)
        if review.allowed or ((u.is_admin or u.role in REVIEWER_ROLES) and report.status == "approved") else [],
        snaps=snaps, ai_snap=next((s for s in snaps if s.ai_status), None),
        ai_enabled=ai_enabled(),
        can={
            "ai": _can_request_ai(u, report),
            "edit": perm.can_edit(u, report), "submit": perm.can_submit(u, report),
            "delete": perm.can_delete(u, report),
            "close": perm.can_close(u, report), "return": perm.can_return(u, report),
            "cancel_close": perm.can_cancel_close(u, report), "retry": perm.can_retry_delivery(u, report),
        },
    )


@bp.route("/reports/<int:report_id>/submit", methods=["POST"])
def submit(report_id):
    report = get_report(report_id)
    d = perm.can_submit(current_user, report)
    if not d:
        flash(d.reason, "error")
        return redirect(url_for("views.detail", report_id=report.id))

    missing = report.results.filter(ChecklistResult.result.is_(None)).count()
    if missing:
        flash(f"檢查表還有 {missing} 個項目未勾選，請全部勾選後再送審。", "error")
        return redirect(url_for("editors.checklist_overview", report_id=report.id))
    from .delivery import ocr_pending
    from .shot_batch import staged_count

    if (n := staged_count(report)):
        flash(f"截圖批次上傳的暫存區還有 {n} 張沒有放入報告，請放入或移除後再送審。", "error")
        return redirect(url_for("editors.shot_batch", report_id=report.id))
    if (n := ocr_pending(report)):
        flash(f"還有 {n} 張截圖正在辨識系統時間，辨識完成後才能送審（審核者需要看到辨識結果）。", "error")
        return redirect(url_for("views.detail", report_id=report.id))

    old_status = report.status
    report.status = "submitted"
    report.submitted_at = datetime.now()
    report.submitted_by_id = current_user.id
    report.updated_by_id = current_user.id
    snapshot(report, "submit", current_user, ai=True)  # 檢查結果.md，並排入 AI 建議
    teams.status_changed(report, old_status, "submit", current_user)
    db.session.commit()
    flash("已送出審核。", "success")
    return redirect(url_for("views.detail", report_id=report.id))


@bp.route("/reports/<int:report_id>/review", methods=["POST"])
def review(report_id):
    report = get_report(report_id)
    d = perm.can_review(current_user, report)
    if not d:
        own = d.reason.startswith("不能審核自己")  # 職責分離：撰寫人 / 送審人
        if current_user.role == "first_line" or (report.status == "submitted" and not own):
            abort(403)
        flash(d.reason, "error")
        return redirect(url_for("views.detail", report_id=report.id))
    principal = d.principal

    action = request.form.get("action")
    comment = request.form.get("comment", "").strip()
    if action not in ("approve", "reject"):
        abort(400)
    if action == "reject" and not comment:
        flash("退回時請填寫原因。", "error")
        return redirect(url_for("views.detail", report_id=report.id))

    from .delivery import ocr_pending

    if action == "approve" and (n := ocr_pending(report)):
        flash(f"還有 {n} 張截圖正在辨識，辨識完成、確認截圖內容後才能核准。", "error")
        return redirect(url_for("views.detail", report_id=report.id))
    old_status = report.status
    signer = principal or current_user  # 文件上的簽核者：代簽時仍是核准人本人
    if report.approver_id is None:
        report.approver_id = signer.id
    if action == "approve":
        report.status = "approved"
        report.approver_name = signer.name
        msg = "已核准" + (f"（代 {signer.name} 簽核，文件顯示為 {signer.name}）。" if principal else "。")
    else:
        report.status = "rejected"
        msg = "已退回給撰寫人" + (f"（代 {signer.name}）。" if principal else "。")
    if action == "reject":
        from .models import ReportImage, ScreenshotSection

        marked = []
        for raw in request.form.getlist("misplaced"):
            img = ReportImage.query.filter_by(id=int(raw), report_id=report.id, kind="screenshot").first() if raw.isdigit() else None
            if img:
                img.misplaced = True
                sec = db.session.get(ScreenshotSection, img.section_id)
                marked.append(f"{sec.code if sec else '?'} {img.original_name}")
        if marked:
            comment += "\n標記放錯章節：" + "、".join(marked)
    report.reviewed_by_id = current_user.id
    report.reviewed_on_behalf_of_id = principal.id if principal else None
    report.reviewed_at = datetime.now()
    report.review_comment = comment
    db.session.add(ReviewLog(report_id=report.id, action=action, actor_id=current_user.id,
                             on_behalf_of_id=principal.id if principal else None, comment=comment))
    # 審核當下的檢查結果與決定，留給日後 RAG（哪些問題會被退回）
    snapshot(report, action, current_user, review=(action, current_user, principal, comment))
    teams.status_changed(report, old_status, action, current_user, comment)
    db.session.commit()
    flash(msg, "success")
    return redirect(url_for("views.detail", report_id=report.id))


def _shot_groups(report):
    """審核用：每個章節今天的截圖與前一份正式日報同章節的截圖並排。"""
    from .models import ScreenshotSection
    from .shot_batch import previous_official

    prev = previous_official(report)
    today, before = {}, {}
    for i in sorted((i for i in report.images if i.kind == "screenshot"), key=lambda i: (i.order, i.id)):
        today.setdefault(i.section_id, []).append(i)
    for i in sorted((i for i in (prev.images if prev else []) if i.kind == "screenshot"), key=lambda i: (i.order, i.id)):
        before.setdefault(i.section_id, []).append(i)
    return [(s, today.get(s.id, []), before.get(s.id, []), prev)
            for s in ScreenshotSection.query.order_by(ScreenshotSection.order) if s.id in today or s.id in before]


def _can_request_ai(u, report):
    """AI 建議：撰寫中（送審前自我檢查）或送審中，可編輯或可審核的人才能要求重新產生。"""
    if not ai_enabled() or report.status not in ("draft", "rejected", "submitted"):
        return False
    return bool(perm.can_edit(u, report) or perm.can_review(u, report) or u.is_admin or u.role == "file_manager")


@bp.route("/reports/<int:report_id>/ai", methods=["POST"])
def request_ai(report_id):
    report = get_report(report_id)
    if not _can_request_ai(current_user, report):
        abort(403)
    snapshot(report, "manual", current_user, ai=True)
    db.session.commit()
    flash("已依目前的檢查結果重新產生檢查結果.md，AI 建議產生中。", "success")
    return redirect(url_for("views.detail", report_id=report.id))


@bp.route("/reports/<int:report_id>/checks.md")
def checks_md(report_id):
    """下載檢查結果.md：指定快照（?snap=），或沒有指定時即時產生目前的檢查結果（不存檔）。"""
    report = get_report(report_id)
    sid = request.args.get("snap", type=int)
    if sid:
        snap = CheckReport.query.filter_by(id=sid, report_id=report.id).first_or_404()
        md, stamp = snap.markdown, f"{snap.stage}_{snap.created_at:%Y%m%d%H%M%S}"
    else:
        md, stamp = render_check_md(report, stage="manual"), "目前"
    from urllib.parse import quote

    name = f"{report.doc_no}_{stamp}_檢查結果.md"
    return Response(md, mimetype="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


# ---------------------------------------------------------------- 代簽設定


@bp.route("/delegations", methods=["GET", "POST"])
def delegations():
    """核准人設定代簽人；代簽人可看到自己被授權代誰簽核。"""
    if not (current_user.is_admin or current_user.role in ("file_manager", "second_line")):
        abort(403)
    if request.method == "POST":
        # 檔案管理員授權自己的代理人；系統管理員可替任何檔案管理員設定
        if current_user.is_admin:
            principal = db.session.get(User, request.form.get("principal_id", type=int) or 0)
            if not principal or principal.role != "file_manager":
                flash("請選擇要被代理的檔案管理員。", "error")
                return redirect(url_for("views.delegations"))
        elif current_user.role == "file_manager":
            principal = current_user
        else:
            abort(403)
        delegate = db.session.get(User, request.form.get("delegate_id", type=int) or 0)
        try:
            start = datetime.strptime(request.form.get("start_date", ""), "%Y-%m-%d").date()
            end_raw = request.form.get("end_date", "").strip()
            end = datetime.strptime(end_raw, "%Y-%m-%d").date() if end_raw else None
        except ValueError:
            flash("日期格式錯誤。", "error")
            return redirect(url_for("views.delegations"))
        if not delegate or delegate.id == principal.id or delegate.role != "second_line":
            flash("請選擇一位二線管理員作為代理人。", "error")
        elif end and end < start:
            flash("結束日期不能早於開始日期。", "error")
        else:
            db.session.add(ApprovalDelegation(
                principal_id=principal.id, delegate_id=delegate.id, start_date=start, end_date=end,
                note=request.form.get("note", "").strip()[:256], created_by_id=current_user.id,
            ))
            db.session.add(AuditLog(action="delegation", actor_id=current_user.id, target=delegate.username,
                                    detail=f"代理 {principal.name}（{start}～{end or '撤銷為止'}）"))
            db.session.commit()
            flash(f"已授權 {delegate.name} 代理 {principal.name}（{start}～{end or '撤銷為止'}）。文件上仍顯示 {principal.name}。",
                  "success")
        return redirect(url_for("views.delegations"))

    q = ApprovalDelegation.query.order_by(ApprovalDelegation.id.desc())
    mine = q.all() if current_user.is_admin else q.filter_by(principal_id=current_user.id).all()
    granted = (ApprovalDelegation.query.filter_by(delegate_id=current_user.id)
               .order_by(ApprovalDelegation.id.desc()).all())
    users = User.query.filter_by(role="second_line").order_by(User.display_name, User.username).all()
    managers = User.query.filter_by(role="file_manager").order_by(User.display_name, User.username).all()
    return render_template("delegations.html", mine=mine, granted=granted, users=users, managers=managers,
                           today=date.today().isoformat(), tab="delegations")


@bp.route("/delegations/<int:delegation_id>/revoke", methods=["POST"])
def revoke_delegation(delegation_id):
    d = db.session.get(ApprovalDelegation, delegation_id) or abort(404)
    if d.principal_id != current_user.id and not current_user.is_admin:
        abort(403)
    if d.revoked_at is None:
        d.revoked_at = datetime.now()
        db.session.add(AuditLog(action="delegation", actor_id=current_user.id, target=d.delegate.username,
                                detail=f"撤銷代理 {d.principal.name}"))
        db.session.commit()
        flash(f"已撤銷 {d.delegate.name} 的代簽權限。", "success")
    return redirect(url_for("views.delegations"))


# ---------------------------------------------------------------- 刪除


def _log(report, action, principal=None, comment=""):
    db.session.add(ReviewLog(report_id=report.id, action=action, actor_id=current_user.id,
                             on_behalf_of_id=principal.id if principal else None, comment=comment))


def _set_aside_final_pdf(report):
    """刪除已結案日報時，正式 PDF 不直接刪：移到 instance/final_pdf/_deleted/（保留 MD5 可查證），回傳相對路徑。"""
    from .delivery import final_path

    d = report.delivery
    path = final_path(d) if d and d.pdf_path else None
    if not path or not os.path.isfile(path):
        return None
    rel = os.path.join("final_pdf", "_deleted", f"{datetime.now():%Y%m%d-%H%M%S}_{os.path.basename(path)}")
    dst = os.path.join(current_app.instance_path, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.replace(path, dst)
    return rel + (f"（MD5 {d.pdf_md5}）" if d.pdf_md5 else "")


@bp.route("/reports/<int:report_id>/delete", methods=["POST"])
def delete(report_id):
    """刪除日報（含截圖檔）。刪除後文件編號釋放：同一天可重新建立，編號也可以再用在其他日報。"""
    report = get_report(report_id)
    d = perm.can_delete(current_user, report)
    if not d:
        if (report.status == "closed" and not report.imported and not current_user.is_admin) \
                or current_user.role in ("first_line", "viewer"):
            abort(403)
        flash(d.reason, "error")
        return redirect(url_for("views.detail", report_id=report.id))
    if request.form.get("confirm_doc_no", "").strip() != report.doc_no:
        flash("請輸入完整文件編號以確認刪除。", "error")
        return redirect(url_for("views.detail", report_id=report.id))
    folder = os.path.join(current_app.config["UPLOAD_DIR"], str(report.id))
    detail = f"{report.report_date} {report.status_label}"
    final = _set_aside_final_pdf(report)
    if final:
        detail += f"；正式 PDF 移到 {final}"
    dl = report.delivery
    if dl and (dl.sp_web_url or dl.mail_sent_at):
        detail += "；SharePoint 檔案與已寄出的 Mail 不受影響" + (f"（{dl.sp_web_url}）" if dl.sp_web_url else "")
    db.session.add(AuditLog(action="delete_report", actor_id=current_user.id, target=report.doc_no, detail=detail))
    if not report.imported:
        teams.status_changed(report, report.status, "delete", current_user)
    for log in report.review_logs:
        db.session.delete(log)
    db.session.delete(report)
    db.session.commit()
    if os.path.isdir(folder):
        import shutil

        shutil.rmtree(folder, ignore_errors=True)
    flash(f"已刪除 {report.report_date} 的日報，文件編號 {report.doc_no} 已釋放，可以再使用。", "success")
    return redirect(url_for("views.index"))


# ---------------------------------------------------------------- 帳號管理（系統管理員）


@bp.route("/admin/users", methods=["GET", "POST"])
def users():
    if not perm.can_manage_users(current_user):
        abort(403)
    if request.method == "POST" and request.form.get("sys_admin") is not None:
        return _set_sys_admin()
    if request.method == "POST":
        user = db.session.get(User, request.form.get("user_id", type=int) or 0) or abort(404)
        role = request.form.get("role")
        if user.id == current_user.id and user.is_admin and role not in ADMIN_ELIGIBLE_ROLES:
            flash("不能把自己改成一線使用者（會失去系統管理員權限）。請由其他系統管理員操作。", "error")
            return redirect(url_for("views.users"))
        if user.role == "admin" or role not in ASSIGNABLE_ROLES:
            abort(400)
        if user.role != role:
            db.session.add(AuditLog(action="set_role", actor_id=current_user.id, target=user.username,
                                    detail=f"{ROLE_LABELS.get(user.role)} → {ROLE_LABELS[role]}"))
            was_admin = user.is_admin
            user.set_role(role)
            if was_admin and not user.is_admin:
                db.session.add(AuditLog(action="set_admin", actor_id=current_user.id, target=user.username,
                                        detail="改為一線使用者，系統管理員權限自動移除"))
            if role != "second_line":
                # 不再是二線管理員：撤銷其所有代理授權
                for dl in ApprovalDelegation.query.filter_by(delegate_id=user.id, revoked_at=None):
                    dl.revoked_at = datetime.now()
            db.session.commit()
            flash(f"{user.name} 已設為{ROLE_LABELS[role]}。", "success")
        return redirect(url_for("views.users"))
    from . import admin_views as adm

    all_users = User.query.order_by(User.role, User.display_name, User.username).all()
    all_users.sort(key=lambda u: u.role != "guest")  # 等待開通的訪客排最前面
    return render_template("users.html", tab="users", users=all_users, roles=ASSIGNABLE_ROLES, role_labels=ROLE_LABELS,
                           admin_roles=ADMIN_ELIGIBLE_ROLES,
                           local_accounts=adm.local_accounts(), min_pw=adm.MIN_PASSWORD,
                           matrix=perm.PERMISSION_MATRIX, matrix_roles=perm.MATRIX_ROLES, matrix_notes=perm.MATRIX_NOTES)


# ---------------------------------------------------------------- 稽核紀錄查詢（系統管理員）

AUDIT_PAGE = 200
AUDIT_CMD = "__cmd__"  # 操作者篩選：沒有登入帳號（主機上執行的指令）


def _audit_filters():
    """從網址參數取得篩選條件，回傳 (條件 dict, 錯誤訊息)。日期預設今天；區間最多 366 天。"""
    a = request.args
    today = date.today()
    err = None

    def day(key, default):
        v = (a.get(key) or "").strip()
        if not v:
            return default
        try:
            return date.fromisoformat(v)
        except ValueError:
            nonlocal err
            err = f"日期格式不正確：{v}（應為 YYYY-MM-DD）"
            return default

    f = {"date_from": day("date_from", today), "date_to": day("date_to", today),
         "action": (a.get("action") or "").strip(), "actor": (a.get("actor") or "").strip(),
         "target": (a.get("target") or "").strip(), "q": (a.get("q") or "").strip()}
    if f["date_from"] > f["date_to"]:
        f["date_from"], f["date_to"] = f["date_to"], f["date_from"]
    if (f["date_to"] - f["date_from"]).days > 366:
        err = "查詢區間最多 366 天，已改為結束日往前 366 天。"
        f["date_from"] = f["date_to"] - timedelta(days=366)
    return f, err


def _audit_query(f):
    q = AuditLog.query.filter(AuditLog.created_at >= datetime.combine(f["date_from"], datetime.min.time()),
                              AuditLog.created_at < datetime.combine(f["date_to"] + timedelta(days=1), datetime.min.time()))
    if f["action"]:
        q = q.filter(AuditLog.action == f["action"])
    if f["actor"] == AUDIT_CMD:
        q = q.filter(AuditLog.actor_id.is_(None))
    elif f["actor"].isdigit():
        q = q.filter(AuditLog.actor_id == int(f["actor"]))
    if f["target"]:
        q = q.filter(AuditLog.target.ilike(f"%{_like(f['target'])}%", escape="\\"))
    if f["q"]:
        q = q.filter(AuditLog.detail.ilike(f"%{_like(f['q'])}%", escape="\\"))
    return q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())


def _like(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@bp.route("/admin/audit")
def audit():
    """稽核紀錄查詢：以天為單位（起訖日期），可依動作、操作者、對象、內容篩選；可匯出 CSV。"""
    if not perm.can_manage_users(current_user):
        abort(403)
    from . import admin_views as adm

    f, err = _audit_filters()
    if err:
        flash(err, "error")
    q = _audit_query(f)
    total = q.count()
    page = max(1, request.args.get("page", 1, type=int))
    pages = max(1, (total + AUDIT_PAGE - 1) // AUDIT_PAGE)
    page = min(page, pages)
    logs = q.offset((page - 1) * AUDIT_PAGE).limit(AUDIT_PAGE).all()
    # 每天筆數（整個查詢結果，不只這一頁）
    per_day = {}
    for (ts,) in q.with_entities(AuditLog.created_at):
        per_day[ts.date()] = per_day.get(ts.date(), 0) + 1
    actions = dict(adm.AUDIT_LABELS)
    for (a,) in db.session.query(AuditLog.action).distinct():
        actions.setdefault(a, a)
    actor_ids = [i for (i,) in db.session.query(AuditLog.actor_id).distinct() if i]
    actors = sorted(User.query.filter(User.id.in_(actor_ids)).all(), key=lambda u: u.name.casefold()) if actor_ids else []
    from .maint import AUDIT_MAX_DAYS, AUDIT_MIN_DAYS, audit_retention_days

    keep = audit_retention_days()
    today = date.today()
    quick = [("今天", today, today), ("昨天", today - timedelta(days=1), today - timedelta(days=1)),
             ("近 7 天", today - timedelta(days=6), today), ("近 30 天", today - timedelta(days=29), today),
             ("本月", today.replace(day=1), today)]
    return render_template("audit.html", tab="audit", f=f, logs=logs, total=total, page=page, pages=pages,
                           per_day=sorted(per_day.items(), reverse=True), actions=actions, actors=actors,
                           cmd=AUDIT_CMD, quick=quick, audit_labels=adm.AUDIT_LABELS, keep=keep,
                           keep_min=AUDIT_MIN_DAYS, keep_max=AUDIT_MAX_DAYS, earliest=today - timedelta(days=keep))


@bp.route("/admin/audit/retention", methods=["POST"])
def audit_retention():
    """稽核紀錄保存天數：180–366。縮短時，超過的紀錄由維護服務在今天內清除。"""
    from . import sysconf
    from .maint import AUDIT_MAX_DAYS, AUDIT_MIN_DAYS, audit_retention_days

    if not perm.can_manage_users(current_user):
        abort(403)
    n = request.form.get("days", type=int)
    if n is None or not AUDIT_MIN_DAYS <= n <= AUDIT_MAX_DAYS:
        flash(f"保存天數必須在 {AUDIT_MIN_DAYS}–{AUDIT_MAX_DAYS} 天之間。", "error")
        return redirect(url_for("views.audit"))
    old = audit_retention_days()
    if n != old:
        sysconf.put("audit_retention_days", n)
        sysconf.put("audit_purge_last", "")  # 讓維護服務下一輪就套用
        db.session.add(AuditLog(action="audit_retention", actor_id=current_user.id, target=f"{n} 天",
                                detail=f"稽核紀錄保存天數 {old} → {n} 天"))
        db.session.commit()
        flash(f"稽核紀錄保存天數已改為 {n} 天。" + ("超過的紀錄會在維護服務下一輪清除。" if n < old else ""), "success")
    return redirect(url_for("views.audit"))


@bp.route("/admin/audit.csv")
def audit_csv():
    """目前篩選條件的全部結果（不分頁），UTF-8 BOM，Excel 可直接開啟。"""
    import csv
    import io

    if not perm.can_manage_users(current_user):
        abort(403)
    from . import admin_views as adm

    f, _ = _audit_filters()
    buf = io.StringIO()
    buf.write("\ufeff")
    w = csv.writer(buf)
    w.writerow(["時間", "動作", "動作代碼", "操作者", "操作者帳號", "對象", "內容"])
    for l in _audit_query(f):
        w.writerow([l.created_at.strftime("%Y-%m-%d %H:%M:%S"), adm.AUDIT_LABELS.get(l.action, l.action), l.action,
                    l.actor.name if l.actor else "指令", l.actor.username if l.actor else "", l.target, l.detail])
    name = f"audit_{f['date_from']:%Y%m%d}-{f['date_to']:%Y%m%d}.csv"
    db.session.add(AuditLog(action="audit_export", actor_id=current_user.id, target=name,
                            detail=", ".join(f"{k}={v}" for k, v in f.items() if v)))
    db.session.commit()
    return Response(buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _set_sys_admin():
    """指派 / 移除系統管理員。一線使用者強制排除；不能移除自己的權限（避免把自己鎖在外面）。"""
    user = db.session.get(User, request.form.get("user_id", type=int) or 0) or abort(404)
    on = request.form.get("sys_admin") == "1"
    if user.is_local_admin:
        abort(400)
    if on and user.role not in ADMIN_ELIGIBLE_ROLES:
        flash("一線使用者不能成為系統管理員，請先改為檔案管理員或二線管理員。", "error")
        return redirect(url_for("views.users"))
    if not on and user.id == current_user.id:
        flash("不能移除自己的系統管理員權限，請由其他系統管理員操作。", "error")
        return redirect(url_for("views.users"))
    if bool(user.sys_admin) != on:
        user.sys_admin = on
        db.session.add(AuditLog(action="set_admin", actor_id=current_user.id, target=user.username,
                                detail="指派為系統管理員" if on else "移除系統管理員"))
        db.session.commit()
        flash(f"{user.name} {'已指派為' if on else '已移除'}系統管理員。", "success")
    return redirect(url_for("views.users"))


@bp.route("/reports/<int:report_id>/pdf")
def pdf(report_id):
    """預覽 PDF（每頁浮水印，內容即時產生）。已結案且正式 PDF 已產生時改給正式檔案。?download=1 以附件下載。"""
    from .pdf.report_pdf import TemplateMissing, build_pdf, pdf_filename

    report = get_report(report_id)
    if report.delivery and report.delivery.pdf_md5:
        return final_pdf(report_id)
    if report.status == "closed" and not report.imported:  # 結案後只提供正式 PDF（產生中）；匯入的可先看預覽
        return Response("正式 PDF 產生中，請稍後再試。", status=409, mimetype="text/plain; charset=utf-8")
    try:
        data = build_pdf(report)
    except TemplateMissing as e:
        return Response(str(e), status=503, mimetype="text/plain; charset=utf-8")
    disposition = "attachment" if request.args.get("download") else "inline"
    return Response(
        data,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{pdf_filename(report)}"'},
    )


# ---------------------------------------------------------------- 結案與發送


@bp.route("/reports/<int:report_id>/close", methods=["POST"])
def close(report_id):
    from . import delivery

    report = get_report(report_id)
    d = perm.can_close(current_user, report)
    if not d:
        flash(d.reason, "error")
        return redirect(url_for("views.detail", report_id=report.id))
    # 一天只有一份正式日報：日期唯一，這裡再用條件更新防止兩個人同時結案
    n = Report.query.filter_by(id=report.id, status="approved").update({"status": "closing"}, synchronize_session=False)
    if not n:
        db.session.rollback()
        flash("這份日報剛剛已被其他人結案。", "error")
        return redirect(url_for("views.detail", report_id=report.id))
    delivery.close(report, current_user)
    teams.status_changed(report, "approved", "close", current_user)
    db.session.commit()
    flash(f"已結案並鎖檔。{delivery.LOCK_SECONDS} 秒後產生正式 PDF、上傳 SharePoint 並寄出日報 Mail；進度顯示在下方流程。",
          "success")
    return redirect(url_for("views.detail", report_id=report.id))


@bp.route("/reports/<int:report_id>/return", methods=["POST"])
def return_fix(report_id):
    """核准後、結案前發現問題：退回給撰寫人修正，修正後重新送審。"""
    report = get_report(report_id)
    d = perm.can_return(current_user, report)
    reason = request.form.get("reason", "").strip()
    if not d or not reason:
        flash(d.reason if not d else "請填寫退回原因。", "error")
        return redirect(url_for("views.detail", report_id=report.id))
    old_status = report.status
    report.status = "rejected"
    report.review_comment = reason
    report.updated_by_id = current_user.id
    db.session.add(ReviewLog(report_id=report.id, action="return", actor_id=current_user.id, comment=reason))
    teams.status_changed(report, old_status, "return", current_user, reason)
    db.session.commit()
    flash("已退回給撰寫人修正，修正後需要重新送審。", "success")
    return redirect(url_for("views.detail", report_id=report.id))


@bp.route("/reports/<int:report_id>/close/cancel", methods=["POST"])
def cancel_close(report_id):
    from . import delivery

    report = get_report(report_id)
    d = perm.can_cancel_close(current_user, report)
    reason = request.form.get("reason", "").strip()
    if not d or not reason:
        flash(d.reason if not d else "請填寫取消原因。", "error")
        return redirect(url_for("views.detail", report_id=report.id))
    delivery.cancel(report, current_user, reason)
    teams.status_changed(report, "closed", "close_cancel", current_user, reason)
    db.session.commit()
    flash("已取消結案，日報回到「已核准（待結案）」。", "success")
    return redirect(url_for("views.detail", report_id=report.id))


@bp.route("/reports/<int:report_id>/delivery/retry", methods=["POST"])
def retry_delivery(report_id):
    from . import delivery

    report = get_report(report_id)
    d = perm.can_retry_delivery(current_user, report)
    if not d:
        flash(d.reason, "error")
        return redirect(url_for("views.detail", report_id=report.id))
    delivery.retry(report, current_user)
    teams.status_changed(report, "delivery_error", "retry", current_user, new_status="delivery_retry")
    db.session.commit()
    flash("已排入重試，維護服務會在幾秒內從失敗的步驟繼續。", "success")
    return redirect(url_for("views.detail", report_id=report.id))


@bp.route("/reports/<int:report_id>/final.pdf")
def final_pdf(report_id):
    """結案後產生的正式 PDF（凍結的檔案，與上傳 SharePoint、寄出的日報 Mail 附的是同一份）。"""
    from flask import send_file

    from .delivery import final_path

    report = get_report(report_id)
    d = report.delivery
    path = final_path(d)
    if report.status != "closed" or not d or not d.pdf_md5 or not path or not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="application/pdf", as_attachment=bool(request.args.get("download")),
                     download_name=d.pdf_name, max_age=0, etag=d.pdf_md5)


@bp.route("/reports/<int:report_id>/original.xlsx")
def original_xlsx(report_id):
    """Excel 匯入的日報：匯入時保存的原始檔案（逐位元相同，SHA-256 與匯入紀錄一致）。"""
    from flask import send_file

    report = get_report(report_id)
    path = report.original_xlsx
    if not path:
        abort(404)
    m = report.import_info
    return send_file(path, as_attachment=True, download_name=m.get("file") or os.path.basename(path), max_age=0,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.route("/archive")
def archive():
    """正式文件下載：已結案日報的正式 PDF 與 MD5 / SHA-256。檢視者（viewer）只能看到這一頁。"""
    from .models import Delivery

    month = request.args.get("month") or ""
    q = (Report.query.join(Delivery).filter(Report.status == "closed", Delivery.pdf_md5.isnot(None))
         .order_by(Report.report_date.desc()))
    if month:
        try:
            first = datetime.strptime(month, "%Y-%m").date()
        except ValueError:
            abort(400)
        nxt = date(first.year + first.month // 12, first.month % 12 + 1, 1)
        q = q.filter(Report.report_date >= first, Report.report_date < nxt)
    return render_template("archive.html", reports=q.limit(400).all(), month=month)

