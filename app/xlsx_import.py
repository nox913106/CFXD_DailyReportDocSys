"""把過去已結案日報的 CheckList Excel（CFXD SLA - SCADA Daily Inspection CheckList_*.xlsx）匯入成已結案日報。

用途：跨日比對基準、告警歷程（365 天）、檢查結果.md / RAG 的歷史資料。

- 一個檔案 = 一天。日期取 Alarm Summary 的 Inspection Date（I2），讀不到時用檔名中的日期。
- 匯入的內容：Inspection Time、Revision、Recorded By、文件編號（Document ID）、Alarm Summary 每一列、
  四張檢查表每一項的 Normal / Warning / Failed 與 Note。
- Excel 裡沒有的（封面的值班 / 問題 / 現場 / 告警記錄、照片、截圖、核准人、正式 PDF）不會有。
- 檢查表依每列的文字對應到系統的檢查項目（checklist_template），舊版範本多出或少的項目列在警告中。
- 該日期已經有日報 → 略過（不覆蓋）；同一個檔案再匯入一次也會因此略過。
- New / Known / False Alarm：False = 標 Y 的列數；New = 前一份正式日報沒有的告警；Known = 其餘。
"""

import datetime as dt
import hashlib
import io
import json
import re
from dataclasses import dataclass, field

from . import db, docno
from .checklist_template import CHECK_COLS, align, item_key, sheet_rows
from .models import (
    OFFICIAL_STATUSES,
    AlarmSummaryRow,
    AuditLog,
    ChecklistItem,
    ChecklistResult,
    Report,
    ReviewLog,
    User,
)

REQUIRED_SHEETS = ["Alarm Summary"] + [v[0] for v in CHECK_COLS.values()]
ALARM_FIRST_ROW = 7
DOC_RE = re.compile(r"CFXD-SCA-RLY-DPR-\d{6}")
DATE_RES = [re.compile(r"(20\d\d)[-._](\d\d)[-._](\d\d)"), re.compile(r"(20\d\d)(\d\d)(\d\d)")]
MAX_BYTES = 30 * 1024 * 1024


class ImportError_(ValueError):
    pass


@dataclass
class Parsed:
    filename: str
    sha256: str
    report_date: dt.date = None
    inspection_time: str = "10:00 AM"
    revision: int = 0
    author: str = ""
    doc_no: str = ""
    alarms: list = field(default_factory=list)  # [dict]
    results: dict = field(default_factory=dict)  # item_id -> (N/W/F/None, note)
    counts: dict = field(default_factory=dict)  # sheet -> {"rows", "matched", "N", "W", "F", "blank"}
    warnings: list = field(default_factory=list)


def _date(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, str):
        return _date_from_text(v)
    return None


def _date_from_text(s):
    for rx in DATE_RES:
        m = rx.search(s or "")
        if m:
            try:
                return dt.date(int(m[1]), int(m[2]), int(m[3]))
            except ValueError:
                continue
    return None


def _time(v):
    if isinstance(v, dt.datetime):
        v = v.time()
    if isinstance(v, dt.time):
        return v.strftime("%I:%M %p")
    if isinstance(v, (int, float)) and 0 <= v < 1:  # Excel 時間序號
        minutes = round(v * 24 * 60)
        return dt.time(minutes // 60 % 24, minutes % 60).strftime("%I:%M %p")
    if isinstance(v, str) and v.strip():
        return v.strip()[:16]
    return "10:00 AM"


def _text(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "Y" if v else "N"
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()[:10]
    return str(v).strip()


def parse(filename, data):
    """解析一個 Excel；格式不對時 raise ImportError_。"""
    import openpyxl

    if len(data) > MAX_BYTES:
        raise ImportError_("檔案超過 30 MB")
    if not data.startswith(b"PK"):
        raise ImportError_("不是 Excel 檔（.xlsx / .xlsm）")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    except Exception as e:  # noqa: BLE001
        raise ImportError_(f"無法開啟：{e}") from e
    missing = [s for s in REQUIRED_SHEETS if s not in wb.sheetnames]
    if missing:
        raise ImportError_("缺少工作表：" + "、".join(missing) + "（不是 CheckList 日報）")

    p = Parsed(filename=filename, sha256=hashlib.sha256(data).hexdigest())
    ws = wb["Alarm Summary"]
    p.report_date = _date(ws["I2"].value) or _date_from_text(filename)
    if not p.report_date:
        raise ImportError_("讀不到日期（Alarm Summary 的 I2 或檔名中的日期）")
    file_day = _date_from_text(filename)
    if file_day and file_day != p.report_date:
        p.warnings.append(f"檔名日期 {file_day} 與 Inspection Date {p.report_date} 不同，以 Inspection Date 為準")
    p.inspection_time = _time(ws["I3"].value)
    try:
        p.revision = max(0, int(float(ws["I4"].value or 0)))
    except (TypeError, ValueError):
        p.revision = 0
    p.author = _text(ws["I5"].value)[:128]
    for sheet in wb.worksheets:
        for row in sheet.iter_rows(max_row=8):
            for c in row:
                if isinstance(c.value, str) and (m := DOC_RE.search(c.value)):
                    p.doc_no = p.doc_no or m[0]
    p.doc_no = p.doc_no or docno.doc_no(p.report_date)

    for r in range(ALARM_FIRST_ROW, ws.max_row + 1):
        vals = {c: _text(ws[f"{c}{r}"].value) for c in "CDEFGHI"}
        if not any(vals[c] for c in "CDEFG"):
            continue
        p.alarms.append({
            "category": vals["C"][:64], "item": vals["D"][:32], "device_name": vals["E"][:128],
            "description": vals["F"], "reason": vals["G"],
            "false_alarm": "Y" if vals["H"].upper().startswith("Y") else "N", "note": vals["I"],
        })

    items_all = ChecklistItem.query.order_by(ChecklistItem.sheet, ChecklistItem.order, ChecklistItem.id).all()
    for sheet, (name, cn, cw, cf, cnote) in CHECK_COLS.items():
        sws = wb[name]
        active = [i for i in items_all if i.sheet == sheet and i.active]
        inactive = {}
        for i in items_all:
            if i.sheet == sheet and not i.active:
                inactive.setdefault(item_key(i), i)
        rows = sheet_rows(sws, sheet, {item_key(i) for i in active} | set(inactive))
        m = align(sheet, rows, active)
        cnt = {"rows": len(rows), "matched": 0, "N": 0, "W": 0, "F": 0, "blank": 0}
        unmatched = []
        for row in rows:
            it = m.pairs.get(row.row) or (inactive.get(row.key) if row.key else None)
            if it is None:
                unmatched.append(row)
                continue
            marks = [code for code, col in (("F", cf), ("W", cw), ("N", cn)) if sws[f"{col}{row.row}"].value is True]
            if len(marks) > 1:
                p.warnings.append(f"{name} 第 {row.row} 列（{it.item_no}）同時勾了 {'/'.join(marks)}，取最嚴重的 {marks[0]}")
            result = marks[0] if marks else None
            note = _text(sws[f"{cnote}{row.row}"].value)
            p.results[it.id] = (result, note)
            cnt["matched"] += 1
            cnt[result or "blank"] += 1
        p.counts[sheet] = cnt
        if m.mode == "position":
            p.warnings.append(f"{name}：文字對不到系統的檢查項目，依順序對應（請抽查結果是否正確）")
        if unmatched:
            p.warnings.append(f"{name}：{len(unmatched)} 列對應不到系統的檢查項目，未匯入（例：第 {unmatched[0].row} 列 "
                              f"{unmatched[0].label[:80]}）")
        if m.changed:
            p.warnings.append(f"{name}：{len(m.changed)} 列文字與系統項目不同，依位置對應（例：第 {m.changed[0][0].row} 列"
                              f" → {m.changed[0][1].item_no}）")
        not_in_file = sum(1 for i in active if i.id not in p.results)
        if not_in_file:
            p.warnings.append(f"{name}：系統有 {not_in_file} 個項目這份 Excel 沒有（當時還沒有這些項目），結果留空")
        if cnt["blank"]:
            p.warnings.append(f"{name}：{cnt['blank']} 項沒有勾選")
    return p


def _alarm_key(a):
    k = lambda s: " ".join((s or "").split()).casefold()  # noqa: E731
    return (k(a["category"]), (a["item"] or "").strip(), k(a["device_name"]), k(a["description"]))


def _prev_keys(day, exclude_id=None):
    q = Report.query.filter(Report.report_date < day, Report.status.in_(OFFICIAL_STATUSES))
    if exclude_id:
        q = q.filter(Report.id != exclude_id)
    prev = q.order_by(Report.report_date.desc()).first()
    if not prev:
        return None
    return {_alarm_key({c: getattr(r, c) for c in ("category", "item", "device_name", "description")})
            for r in prev.alarm_rows}


def _counts(report_date, alarms, exclude_id=None):
    false = sum(1 for a in alarms if a["false_alarm"] == "Y")
    prev = _prev_keys(report_date, exclude_id)
    new = 0 if prev is None else sum(1 for a in alarms if a["false_alarm"] != "Y" and _alarm_key(a) not in prev)
    return new, len(alarms) - false - new, false


def _match_author(name):
    if not name:
        return None
    low = name.casefold()
    for u in User.query.filter(User.role != "admin"):
        if low in {(u.display_name or "").casefold(), (u.username or "").casefold()}:
            return u
    return None


def import_one(filename, data, user, dry_run=False):
    """回傳 dict：status = imported / would_import / skipped / error，以及日期、文件編號、統計、警告。"""
    try:
        p = parse(filename, data)
    except ImportError_ as e:
        return {"file": filename, "status": "error", "message": str(e)}
    out = {"file": filename, "date": p.report_date.isoformat(), "doc_no": p.doc_no, "author": p.author,
           "alarms": len(p.alarms), "counts": p.counts, "warnings": p.warnings}
    existing = Report.query.filter_by(report_date=p.report_date).first()
    if existing:
        out.update(status="skipped", message=f"{p.report_date} 已有日報（{existing.doc_no}，{existing.status_label}），不覆蓋",
                   report_id=existing.id)
        return out
    dup = Report.query.filter(db.func.lower(Report.doc_no) == p.doc_no.lower()).first()
    if dup:
        out.update(status="skipped", message=f"文件編號 {p.doc_no} 已用在 {dup.report_date} 的日報")
        return out
    if not sum(c["matched"] for c in p.counts.values()):
        out.update(status="error", message="檢查表一個項目都對應不到，可能不是這個系統的 CheckList")
        return out
    if dry_run:
        out.update(status="would_import", message="檢查通過，可以匯入")
        return out

    author = _match_author(p.author)
    if p.author and not author:
        p.warnings.append(f"Recorded By「{p.author}」不是系統帳號，撰寫人記為匯入者 {user.name}（原名保留在匯入資訊）")
    now = dt.datetime.now()
    new, known, false = _counts(p.report_date, p.alarms)
    report = Report(
        report_date=p.report_date, doc_no=p.doc_no, contractor_doc_no=docno.contractor_doc_no(p.report_date),
        revision=p.revision, inspection_time=p.inspection_time, author_id=(author or user).id,
        first_line_officer=p.author, status="closed", updated_by_id=user.id,
        alarm_new=new, alarm_known=known, alarm_false=false,
        import_meta=json.dumps({"file": filename, "sha256": p.sha256, "author": p.author, "warnings": p.warnings,
                                "counts": p.counts, "at": now.isoformat(timespec="seconds"), "by": user.name},
                               ensure_ascii=False),
    )
    for i, a in enumerate(p.alarms, 1):
        report.alarm_rows.append(AlarmSummaryRow(order=i, **a))
    db.session.add(report)
    db.session.flush()
    ids = set(p.results)
    for (iid,) in db.session.query(ChecklistItem.id).filter_by(active=True):
        ids.add(iid)
    for iid in ids:
        res, note = p.results.get(iid, (None, ""))
        db.session.add(ChecklistResult(report_id=report.id, item_id=iid, result=res, note=note))
    db.session.add(ReviewLog(report_id=report.id, action="import", actor_id=user.id,
                             comment=f"從 {filename} 匯入（SHA-256 {p.sha256[:12]}…）"
                                     + (f"；{len(p.warnings)} 則警告" if p.warnings else "")))
    db.session.add(AuditLog(action="import_report", actor_id=user.id, target=p.doc_no,
                            detail=f"{p.report_date} 從 {filename} 匯入"))
    db.session.flush()
    _recount_next(p.report_date)
    db.session.commit()
    out.update(status="imported", report_id=report.id, warnings=p.warnings,
               message=f"已匯入：告警 {len(p.alarms)} 筆（New {new} / Known {known} / False {false}）")
    return out


def _recount_next(day):
    """匯入順序不一定照日期：下一份匯入的日報以這份為基準，重算它的 New / Known。"""
    nxt = (Report.query.filter(Report.report_date > day, Report.status.in_(OFFICIAL_STATUSES))
           .order_by(Report.report_date).first())
    if nxt and nxt.imported:
        alarms = [{c: getattr(r, c) for c in ("category", "item", "device_name", "description", "false_alarm")}
                  for r in nxt.alarm_rows]
        nxt.alarm_new, nxt.alarm_known, nxt.alarm_false = _counts(nxt.report_date, alarms, exclude_id=nxt.id)
