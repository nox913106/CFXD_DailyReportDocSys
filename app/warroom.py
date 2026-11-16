"""戰情室：從日報內容自動整理「持續發生的事件」，不需要另外輸入。

- 事件來源（後台可關閉某些類型）：告警（Alarm Summary，排除誤報）、檢查表 Warning / Failed、問題紀錄、
  真實告警（沒有結束時間）、現場待辦（有 Further Action）。
- 依據：已核准、已結案的正式日報；最新一份若是「待審核」也列入，標示「審核中」。草稿、退回不算。
  檢視者只看正式日報。
- 事件是否還在：只看日報內容。這份日報還有 → 進行中；最新一份沒有了 → 已解除。不另外判斷狀態文字。
- 持續天數：從最新一份往回逐份比對，連續出現的第一份日報日期到最新日期（含）的天數；中斷就重新起算。
"""

import re
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func

from . import db, sysconf
from .models import (AlarmSummaryRow, ChecklistItem, ChecklistResult, IssueRecord, OnsiteRecord, Report,
                     TrueAlarmRecord)

TYPES = {  # 代碼: (名稱, 說明)
    "alarm": ("告警", "Alarm Summary（排除誤報）"),
    "check": ("檢查異常", "檢查表勾 Warning / Failed 的項目"),
    "issue": ("問題紀錄", "值班的問題紀錄（Issue Record）"),
    "true_alarm": ("真實告警", "True Alarm 沒有結束時間的"),
    "onsite": ("現場待辦", "現場紀錄有 Further Action 的"),
}
HISTORY_REPORTS = 120  # 最多往回看幾份日報
STRIP_DAYS = 14
TREND_DAYS = 30

_cache = {}


def enabled_types():
    v = sysconf.get("warroom_types")
    return [t for t in TYPES if t in v] if isinstance(v, list) and v else list(TYPES)


def thresholds():
    warn = int(sysconf.get("warroom_warn_days") or 3)
    alert = int(sysconf.get("warroom_alert_days") or 7)
    return warn, max(alert, warn + 1)


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _k(s):
    return re.sub(r"[^0-9a-z一-鿿]", "", (s or "").lower())


@dataclass
class Event:
    key: tuple
    type: str
    title: str
    detail: str = ""
    note: str = ""
    category: str = ""
    jira: str = ""
    severity: str = ""  # check：F / W
    first: object = None  # 連續出現的第一份日報日期
    last: object = None
    days: int = 0
    reports: int = 0
    new: bool = False
    provisional: bool = False  # 只出現在審核中的那一份
    history: list = field(default_factory=list)  # [(date, report_id, status, note)] 新到舊
    strip: list = field(default_factory=list)  # 最近 14 天：'on' / 'off' / 'none'（那天沒有日報）

    @property
    def type_label(self):
        return TYPES[self.type][0]


def _extract(reports, types):
    """{report_id: {key: Event(基本欄位)}}，一次查完所有日報。"""
    ids = [r.id for r in reports]
    out = {i: {} for i in ids}
    if not ids:
        return out
    if "alarm" in types:
        for a in AlarmSummaryRow.query.filter(AlarmSummaryRow.report_id.in_(ids)).order_by(AlarmSummaryRow.order):
            if (a.false_alarm or "").upper() == "Y":
                continue
            key = ("alarm", _k(a.category), (a.item or "").strip(), _k(a.device_name), _k(a.description))
            out[a.report_id][key] = Event(key, "alarm", a.device_name or a.item or "（未填設備）", a.description or "",
                                          " / ".join(x for x in (a.reason, a.note) if x), a.category or "")
    if "check" in types:
        q = (db.session.query(ChecklistResult, ChecklistItem).join(ChecklistItem)
             .filter(ChecklistResult.report_id.in_(ids), ChecklistResult.result.in_(("W", "F"))))
        for res, it in q:
            key = ("check", it.id)
            title = " ".join(x for x in (it.device_name, it.status_type) if x) or it.item_no
            out[res.report_id][key] = Event(key, "check", title, it.description or "", res.note or "",
                                            it.category or it.device_type or "", severity=res.result)
    if "issue" in types:
        for i in IssueRecord.query.filter(IssueRecord.report_id.in_(ids)):
            if not (i.jira or i.description or i.issue_type):
                continue
            key = ("issue", _k(i.jira) or _k(i.description)[:160])
            out[i.report_id][key] = Event(key, "issue", i.issue_type or "問題", i.description or "",
                                          " / ".join(x for x in (i.status, i.further_action) if x), jira=i.jira or "")
    if "true_alarm" in types:
        for t in TrueAlarmRecord.query.filter(TrueAlarmRecord.report_id.in_(ids)):
            if (t.end_time or "").strip() or not (t.alarm_item or "").strip():
                continue
            key = ("true_alarm", _k(t.alarm_item))
            out[t.report_id][key] = Event(key, "true_alarm", t.alarm_item, f"開始 {t.start_time}" if t.start_time else "",
                                          " / ".join(x for x in (t.status, t.follow_up) if x))
    if "onsite" in types:
        for o in OnsiteRecord.query.filter(OnsiteRecord.report_id.in_(ids)):
            if not (o.further_action or "").strip() or not (o.title or "").strip():
                continue
            key = ("onsite", _k(o.title))
            out[o.report_id][key] = Event(key, "onsite", o.title, o.personnel or "",
                                          " / ".join(x for x in (o.status, o.further_action) if x), jira=o.jira or "")
    return out


def _source_reports(official_only):
    statuses = ("approved", "closed") if official_only else ("approved", "closed", "submitted")
    rows = (Report.query.filter(Report.status.in_(statuses)).order_by(Report.report_date.desc())
            .limit(HISTORY_REPORTS + 1).all())
    if not official_only:
        # 「審核中」只採用最新那一份；更早的待審核（還沒核准、可能被退回）不列入歷史
        rows = [r for i, r in enumerate(rows) if r.status != "submitted" or i == 0]
    return rows[:HISTORY_REPORTS]


def _signature(official_only, types):
    m = db.session.query(func.count(Report.id), func.max(Report.updated_at), func.max(Report.id)).one()
    return (str(db.engine.url), official_only, tuple(types), thresholds(), tuple(m))


def build(official_only=False):
    """戰情室資料（依日報最後更新時間快取）。"""
    types = enabled_types()
    sig = _signature(official_only, types)
    hit = _cache.get(official_only)
    if hit and hit[0] == sig:
        return hit[1]
    data = _build(official_only, types)
    _cache[official_only] = (sig, data)
    return data


def _build(official_only, types):
    reports = _source_reports(official_only)
    warn, alert = thresholds()
    data = {"latest": reports[0] if reports else None, "events": [], "resolved": [], "trend": [],
            "types": types, "warn": warn, "alert": alert, "counts": {}}
    if not reports:
        return data
    per = _extract(reports, types)
    latest = reports[0]
    provisional = latest.status == "submitted"
    by_date = {r.report_date: r for r in reports}

    events = []
    for key, ev in per[latest.id].items():
        run = []
        for r in reports:
            e = per[r.id].get(key)
            if e is None:
                break
            run.append((r, e))
        ev.first, ev.last = run[-1][0].report_date, latest.report_date
        ev.days = (ev.last - ev.first).days + 1
        ev.reports = len(run)
        ev.new = len(run) == 1 and len(reports) > 1
        ev.provisional = provisional and ev.new
        # 歷程：最近 30 份裡出現過的（含中斷前），新到舊
        for r in reports[:TREND_DAYS]:
            e = per[r.id].get(key)
            if e is not None:
                ev.history.append((r.report_date, r.id, r.status, e.note))
        for i in range(STRIP_DAYS - 1, -1, -1):
            d = latest.report_date - timedelta(days=i)
            r = by_date.get(d)
            ev.strip.append(("none", d) if r is None else (("on" if key in per[r.id] else "off"), d))
        events.append(ev)

    def level(e):
        return 2 if e.days > alert else (1 if e.days >= warn else 0)

    events.sort(key=lambda e: (-level(e), -e.days, list(TYPES).index(e.type), e.title))
    for e in events:
        e.level = level(e)
    data["events"] = events
    if len(reports) > 1:
        prev = reports[1]
        data["resolved"] = [e for k, e in per[prev.id].items() if k not in per[latest.id]]
        data["prev"] = prev
    data["counts"] = {
        "active": len(events),
        "new": sum(1 for e in events if e.new),
        "resolved": len(data["resolved"]),
        "long": sum(1 for e in events if e.days > alert),
        "by_type": {t: sum(1 for e in events if e.type == t) for t in types},
    }
    # 趨勢：近 30 份日報，每份的事件數（依類型）
    trend = []
    for r in reversed(reports[:TREND_DAYS]):
        c = {t: 0 for t in types}
        for k in per[r.id]:
            c[k[0]] += 1
        trend.append({"date": r.report_date, "status": r.status, "counts": c, "total": sum(c.values())})
    data["trend"] = trend
    data["trend_max"] = max([t["total"] for t in trend] + [1])
    return data
