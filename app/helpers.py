from collections import OrderedDict

from datetime import datetime

from flask import abort, flash, jsonify, redirect, request, url_for

from . import db
from .models import ChecklistItem, ChecklistResult, Report

SHEETS = OrderedDict(
    [
        ("vm", "VMs, System and Service Status"),
        ("software", "Software"),
        ("physical", "Physical devices"),
        ("pi", "PI System"),
    ]
)


def get_report(report_id):
    return db.session.get(Report, report_id) or abort(404)


def is_autosave():
    """編輯頁每分鐘在背景送出的自動儲存（app.js 加上 X-Autosave 標頭）：回傳 JSON，不留 flash 訊息。"""
    return request.headers.get("X-Autosave") == "1"


def autosave_reply(error=None, status=200, report=None):
    from flask_wtf.csrf import generate_csrf

    if error:
        return jsonify(ok=False, error=error, csrf=generate_csrf()), status
    # 每次回傳新的 CSRF token：頁面開很久也不會因為 token 過期而無法儲存
    updated = report.updated_at.isoformat() if report is not None and report.updated_at else ""
    return jsonify(ok=True, saved_at=datetime.now().strftime("%H:%M:%S"), csrf=generate_csrf(), updated=updated)


def saved(report, message, endpoint, **kw):
    """編輯頁儲存完成：自動儲存回 JSON，一般儲存顯示訊息後回到原頁。"""
    if is_autosave():
        return autosave_reply(report=report)
    flash(message, "success")
    return redirect(url_for(endpoint, report_id=report.id, **kw))


def locked_redirect(report, endpoint="views.detail"):
    """報告已送審/核准時不可修改；回傳 redirect，可編輯時回傳 None。"""
    if report.editable:
        return None
    msg = f"此日報目前為「{report.status_label}」，不能修改。"
    if is_autosave():
        return autosave_reply(msg, 409)
    flash(msg, "error")
    return redirect(url_for(endpoint, report_id=report.id))


def parse_rows(form, prefix, fields):
    """把 prefix-<n>-<field> 形式的表單欄位整理成 list[dict]；整列空白就略過。
    順序依表單欄位出現的先後（= 畫面上的順序，拖曳調整過的列也照新順序），不是 n 的大小。"""
    indexes = []
    for k in form:
        parts = k.split("-")
        if len(parts) >= 3 and parts[0] == prefix and parts[1].isdigit() and int(parts[1]) not in indexes:
            indexes.append(int(parts[1]))
    rows = []
    for i in indexes:
        row = {f: form.get(f"{prefix}-{i}-{f}", "").strip() for f in fields}
        if any(row.values()):
            rows.append(row)
    return rows


def checklist_stats(report):
    """回傳 {sheet: {"N","W","F","blank","total", "categories": {名稱: {...}}}}。"""
    rows = (
        db.session.query(ChecklistItem.sheet, ChecklistItem.category, ChecklistResult.result)
        .join(ChecklistResult, ChecklistResult.item_id == ChecklistItem.id)
        .filter(ChecklistResult.report_id == report.id)
        .all()
    )
    stats = OrderedDict((s, _empty()) for s in SHEETS)
    for sheet, category, result in rows:
        for bucket in (stats[sheet], stats[sheet]["categories"].setdefault(category or "", _empty(False))):
            bucket["total"] += 1
            bucket[result or "blank"] += 1
    return stats


def _empty(with_categories=True):
    d = {"N": 0, "W": 0, "F": 0, "blank": 0, "total": 0}
    if with_categories:
        d["categories"] = OrderedDict()
    return d
