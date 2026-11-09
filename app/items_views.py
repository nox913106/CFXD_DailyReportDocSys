"""檢查項目（CheckList 主檔）維護：新增、刪除、修改、拖曳調整順序。二線管理員以上。

- 修改後立即用在「可編輯」的日報（草稿、退回）與之後新增的日報；送審中、已核准、已結案的日報不變。
- 刪除 = 停用：舊日報的結果與文字保留。之後新增同一個編號時，會還原那個項目並更新內容。
- PDF 的 CheckList 頁由 Excel 範本畫出：範本沒有的項目不會出現在 PDF。頁面上方列出範本需要怎麼改，
  可下載修改清單；改好的範本由系統管理員在「系統設定 → PDF 與上傳」上傳。
"""

from datetime import datetime

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from . import db
from .helpers import SHEETS
from .models import REVIEWER_ROLES, AuditLog, ChecklistItem, ChecklistResult, Report

bp = Blueprint("items", __name__, url_prefix="/checklist-items")

FIELDS = [
    ("no", "No.", 6),
    ("device_type", "Device Type", 128),
    ("device_name", "Device Name", 128),
    ("group_item", "Group", 16),
    ("item_no", "Item", 16),
    ("location", "Location", 32),
    ("category", "統計分類", 128),
    ("status_type", "Status Type", 128),
    ("description", "Description", 2000),
]
EDITABLE_STATUSES = ("draft", "rejected")


def can_manage(user):
    return user.is_authenticated and (user.is_admin or user.role in REVIEWER_ROLES)


@bp.before_request
@login_required
def _guard():
    if not can_manage(current_user):
        abort(403)


def _sheet_or_404(sheet):
    if sheet not in SHEETS:
        abort(404)
    return sheet


@bp.route("/")
def index():
    return redirect(url_for("items.sheet", sheet="vm"))


@bp.route("/<sheet>", methods=["GET"])
def sheet(sheet):
    from .checklist_template import diff_rows, template_status

    _sheet_or_404(sheet)
    items = ChecklistItem.query.filter_by(sheet=sheet, active=True).order_by(ChecklistItem.order, ChecklistItem.id).all()
    deleted = ChecklistItem.query.filter_by(sheet=sheet, active=False).order_by(ChecklistItem.item_no).all()
    st = template_status()
    diffs = diff_rows(st)
    counts = {s: ChecklistItem.query.filter_by(sheet=s, active=True).count() for s in SHEETS}
    open_reports = Report.query.filter(Report.status.in_(EDITABLE_STATUSES)).count()
    return render_template("checklist_items.html", sheet=sheet, sheets=SHEETS, items=items, deleted=deleted,
                           fields=FIELDS, show_location=sheet == "physical", status=st, diffs=diffs,
                           sheet_diffs=[d for d in diffs if d[0] == _sheet_name(sheet)], counts=counts,
                           open_reports=open_reports)


def _sheet_name(sheet):
    from .checklist_template import CHECK_COLS

    return CHECK_COLS[sheet][0]


@bp.route("/<sheet>", methods=["POST"])
def save(sheet):
    _sheet_or_404(sheet)
    rows = _parse(request.form)
    err = _validate(sheet, rows)
    if err:
        flash(err, "error")
        return redirect(url_for("items.sheet", sheet=sheet))
    summary = apply(sheet, rows, current_user)
    if summary:
        flash("已儲存：" + "、".join(summary) + "。", "success")
    else:
        flash("沒有變更。", "success")
    return redirect(url_for("items.sheet", sheet=sheet))


def _parse(form):
    """item-<k>-<field>，依畫面順序（拖曳後的順序）。"""
    order = []
    for k in form:
        p = k.split("-")
        if len(p) == 3 and p[0] == "item" and p[1].isdigit() and p[1] not in order:
            order.append(p[1])
    rows = []
    for k in order:
        row = {f: " ".join(form.get(f"item-{k}-{f}", "").split()) for f, _, _ in FIELDS}
        row["description"] = form.get(f"item-{k}-description", "").strip()
        raw_id = form.get(f"item-{k}-id", "")
        row["id"] = int(raw_id) if raw_id.isdigit() else None
        if row["id"] is None and not any(row[f] for f, _, _ in FIELDS):
            continue  # 新增後沒填任何內容
        rows.append(row)
    return rows


def _validate(sheet, rows):
    seen = {}
    for n, row in enumerate(rows, 1):
        if not row["item_no"]:
            return f"第 {n} 列沒有 Item 編號。"
        for f, label, limit in FIELDS:
            if len(row[f]) > limit:
                return f"第 {n} 列（{row['item_no']}）的 {label} 最多 {limit} 個字。"
        if not (row["status_type"] or row["description"]):
            return f"第 {n} 列（{row['item_no']}）的 Status Type 與 Description 至少要填一個。"
        if row["item_no"] in seen:
            return f"Item 編號 {row['item_no']} 重複（第 {seen[row['item_no']]} 列與第 {n} 列）。"
        seen[row["item_no"]] = n
    ids = [r["id"] for r in rows if r["id"]]
    valid = {i for (i,) in db.session.query(ChecklistItem.id).filter(ChecklistItem.sheet == sheet,
                                                                      ChecklistItem.active.is_(True))}
    if set(ids) - valid:
        return "畫面上的項目已被其他人修改或刪除，請重新整理後再改。"
    # 已刪除項目的編號：只有「新增」可以沿用（= 還原），修改既有項目的編號不能用
    deleted = {it.item_no: it for it in ChecklistItem.query.filter_by(sheet=sheet, active=False)}
    for r in rows:
        if r["id"] and r["item_no"] in deleted:
            return (f"Item 編號 {r['item_no']} 是已刪除的項目使用過的編號，不能改成這個編號；"
                    "要恢復那個項目，請新增一列並填這個編號。")
    return None


def apply(sheet, rows, user):
    """套用到主檔與可編輯的日報。回傳變更摘要（list[str]）。"""
    now = datetime.now()
    current = {it.id: it for it in ChecklistItem.query.filter_by(sheet=sheet, active=True)}
    deleted = {it.item_no: it for it in ChecklistItem.query.filter_by(sheet=sheet, active=False)}
    keep = {r["id"] for r in rows if r["id"]}
    added, restored, changed, removed = [], [], [], []
    before = [it.id for it in sorted(current.values(), key=lambda i: (i.order, i.id))]

    # 編號互換時避免唯一性衝突：先把要改編號的項目換成暫時編號
    renum = [r for r in rows if r["id"] and current[r["id"]].item_no != r["item_no"]]
    for r in renum:
        current[r["id"]].item_no = f"~{r['id']}"
    db.session.flush()

    new_items = []
    for pos, r in enumerate(rows, 1):
        values = {f: r[f] for f, _, _ in FIELDS}
        touched = True
        if r["id"]:
            it = current[r["id"]]
            touched = r in renum or any((getattr(it, f) or "") != v for f, v in values.items())
            if touched:
                changed.append(values["item_no"])
        elif values["item_no"] in deleted:
            it = deleted.pop(values["item_no"])
            it.active = True
            restored.append(values["item_no"])
            new_items.append(it)
        else:
            it = ChecklistItem(sheet=sheet, active=True)
            db.session.add(it)
            added.append(values["item_no"])
            new_items.append(it)
        for f, v in values.items():
            setattr(it, f, v)
        if touched or it.order != pos:
            it.updated_at = now
        it.order = pos
    for iid, it in current.items():
        if iid not in keep:
            it.active = False
            it.updated_at = now
            removed.append(it)
    db.session.flush()

    reordered = [r["id"] for r in rows if r["id"]] != [i for i in before if i in keep]
    open_reports = Report.query.filter(Report.status.in_(EDITABLE_STATUSES)).all()
    for rep in open_reports:
        have = {i for (i,) in db.session.query(ChecklistResult.item_id).filter_by(report_id=rep.id)}
        for it in new_items:
            if it.id not in have:
                db.session.add(ChecklistResult(report_id=rep.id, item_id=it.id, result=None, note=""))
        if removed:
            ChecklistResult.query.filter(ChecklistResult.report_id == rep.id,
                                         ChecklistResult.item_id.in_([it.id for it in removed])).delete(
                synchronize_session=False)
    summary = []
    if added:
        summary.append(f"新增 {len(added)} 項（{', '.join(added[:10])}{'…' if len(added) > 10 else ''}）")
    if restored:
        summary.append(f"還原 {len(restored)} 項（{', '.join(restored[:10])}）")
    if changed:
        summary.append(f"修改 {len(changed)} 項（{', '.join(changed[:10])}{'…' if len(changed) > 10 else ''}）")
    if removed:
        names = [it.item_no for it in removed]
        summary.append(f"刪除 {len(names)} 項（{', '.join(names[:10])}{'…' if len(names) > 10 else ''}）")
    if reordered:
        summary.append("調整順序")
    if summary:
        if open_reports and (new_items or removed):
            summary.append(f"已套用到 {len(open_reports)} 份可編輯的日報")
        db.session.add(AuditLog(action="checklist_items", actor_id=user.id, target=_sheet_name(sheet),
                                detail="；".join(summary)))
    db.session.commit()
    return summary


@bp.route("/template-diff.csv")
def template_diff_csv():
    from .checklist_template import diff_rows, template_status

    rows = ["工作表,動作,Excel 列,目前內容,說明"]
    for r in diff_rows(template_status()):
        rows.append(",".join('"' + str(v).replace('"', '""') + '"' for v in r))
    return Response("﻿" + "\r\n".join(rows) + "\r\n", mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="checklist_template_changes.csv"'})
