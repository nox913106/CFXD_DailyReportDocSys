from collections import OrderedDict

from flask import abort, flash, redirect, url_for

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


def locked_redirect(report, endpoint="views.detail"):
    """報告已送審/核准時不可修改；回傳 redirect，可編輯時回傳 None。"""
    if report.editable:
        return None
    flash(f"此日報目前為「{report.status_label}」，不能修改。", "error")
    return redirect(url_for(endpoint, report_id=report.id))


def parse_rows(form, prefix, fields):
    """把 prefix-<n>-<field> 形式的表單欄位整理成 list[dict]；整列空白就略過。"""
    indexes = sorted(
        {int(k.split("-")[1]) for k in form if k.startswith(prefix + "-") and k.split("-")[1].isdigit()}
    )
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
