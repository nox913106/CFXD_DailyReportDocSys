"""戰情室：從日報內容自動整理「持續發生的事件」，不需要另外輸入。

- 事件來源（後台可關閉某些類型）：告警（Alarm Summary，排除誤報）、檢查表 Warning / Failed、問題紀錄、
  真實告警（沒有結束時間）、現場待辦（有 Further Action）。
- 同一件事只算一次：Alarm Summary 的告警對應到檢查表同一個項目的 Warning / Failed 時合併成一件（標示兩個來源）。
- 狀態：
  - 未上線：檢查表勾「未上線」（Not officially launched），或說明 / 原因 / 備註含已知狀態關鍵字 → 列在「已知狀態」，不算異常。
  - 已通報：檢查表勾「已通報」（Reported：處理中、待料）→ 仍算異常，標示已通報。
  - 其餘為未通報的異常。
- 依據：已核准、已結案的正式日報；最新一份若是「待審核」也列入，標示「審核中」。草稿、退回不算。檢視者只看正式日報。
- 事件是否還在：只看日報內容。最新一份還有 → 進行中；沒有了 → 已解除。
- 持續天數：從最新一份往回逐份比對，連續出現的第一份日報日期到最新日期（含）的天數；中斷就重新起算。
  說明 / 原因 / 備註寫了「Down date: YYYY/MM/DD」且更早時從那天起算；一路延續到系統最早的日報時顯示「≥」。
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

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
DEFAULT_KNOWN = ["Not officially launched", "end-of-life", "EOL", "Certificate Error"]
HISTORY_REPORTS = 120  # 最多往回看幾份日報
STRIP_DAYS = 14
TREND_DAYS = 30
DOWN_DATE = re.compile(r"down\s*date\s*[:：]?\s*(\d{4})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{1,2})", re.I)

_cache = {}


def enabled_types():
    v = sysconf.get("warroom_types")
    return [t for t in TYPES if t in v] if isinstance(v, list) and v else list(TYPES)


def thresholds():
    warn = int(sysconf.get("warroom_warn_days") or 3)
    alert = int(sysconf.get("warroom_alert_days") or 7)
    return warn, max(alert, warn + 1)


def known_keywords():
    v = sysconf.get("warroom_known_keywords")
    return [k.strip() for k in v if k and k.strip()] if isinstance(v, list) else list(DEFAULT_KNOWN)


def _keyword_re(words):
    if not words:
        return None
    # 英數開頭結尾的關鍵字要求是完整的字（EOL 不會比對到 geolocation）
    parts = [(r"\b" if re.match(r"\w", w) else "") + re.escape(w) + (r"\b" if re.search(r"\w$", w) else "") for w in words]
    return re.compile("|".join(parts), re.I)


def _k(s):
    return re.sub(r"[^0-9a-z一-鿿]", "", (s or "").lower())


def _join(*xs):
    out = []
    for x in xs:
        x = (x or "").strip()
        if x and x not in out:
            out.append(x)
    return " / ".join(out)


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
    sources: list = field(default_factory=list)  # 合併後的來源類型
    state: str = ""  # not_launched / known / reported / ""
    known_by: str = ""  # 判定為已知狀態的關鍵字
    first: object = None
    last: object = None
    days: int = 0
    at_least: bool = False  # 延續到系統最早的日報，實際可能更久
    down_since: object = None  # 從 Down date 推得的開始日
    reports: int = 0
    new: bool = False
    provisional: bool = False
    level: int = 0
    history: list = field(default_factory=list)  # [(date, report_id, status, note, state)] 新到舊
    strip: list = field(default_factory=list)

    @property
    def type_label(self):
        return TYPES[self.type][0]

    @property
    def source_labels(self):
        return [TYPES[t][0] for t in self.sources]

    @property
    def is_known(self):
        return self.state in ("not_launched", "known")


STATE_LABELS = {"not_launched": "未上線", "known": "已知狀態", "reported": "已通報", "": "未通報"}


def _match(alarm, item):
    from .checks import _match as m

    try:
        return m(alarm, item)
    except Exception:  # noqa: BLE001 - 比對不到就當成獨立告警
        return False


def _best_match(alarm, abnormal):
    """告警對應到哪一個 Warning / Failed 項目：同一設備同一項次有多列（例如 Disk 的 Space check 與 HA check）時，
    以說明（Status Type / Description）相符的優先。"""
    hits = [(res, it) for res, it in abnormal if _match(alarm, it)]
    if len(hits) <= 1:
        return hits[0] if hits else None
    desc = _k(alarm.description)
    item = (alarm.item or "").strip()

    def score(h):
        it = h[1]
        return ((2 if desc and desc in (_k(it.status_type), _k(it.description)) else 0)
                + (1 if item and item == it.item_no else 0))
    return max(hits, key=score)


def _extract(reports, types, known_re):
    """{report_id: {key: Event}}：一次查完所有日報，合併告警與檢查表，判斷狀態。"""
    ids = [r.id for r in reports]
    out = {i: {} for i in ids}
    if not ids:
        return out
    want_alarm, want_check = "alarm" in types, "check" in types
    abnormal = {i: [] for i in ids}  # report_id -> [(res, item)]
    if want_alarm or want_check:
        q = (db.session.query(ChecklistResult, ChecklistItem).join(ChecklistItem)
             .filter(ChecklistResult.report_id.in_(ids), ChecklistResult.result.in_(("W", "F"))))
        for res, it in q:
            abnormal[res.report_id].append((res, it))
            if want_check:
                key = ("check", it.id)
                title = " ".join(x for x in (it.device_name, it.status_type) if x) or it.item_no
                ev = Event(key, "check", title, it.description or "", res.note or "", it.category or it.device_type or "",
                           severity=res.result, sources=["check"])
                ev.state = "not_launched" if res.not_launched else ("reported" if res.reported else "")
                out[res.report_id][key] = ev
    if want_alarm:
        for a in AlarmSummaryRow.query.filter(AlarmSummaryRow.report_id.in_(ids)).order_by(AlarmSummaryRow.order):
            if (a.false_alarm or "").upper() == "Y":
                continue
            hit = _best_match(a, abnormal[a.report_id])
            reason = _join(a.reason, a.note)
            if hit and want_check:  # 同一件事：併入檢查表那一件
                ev = out[a.report_id][("check", hit[1].id)]
                if "alarm" not in ev.sources:
                    ev.sources.insert(0, "alarm")
                ev.note = _join(ev.note, reason)
                continue
            key = ("alarm", _k(a.category), (a.item or "").strip(), _k(a.device_name), _k(a.description))
            ev = Event(key, "alarm", a.device_name or a.item or "（未填設備）", a.description or "", reason,
                       a.category or "", sources=["alarm"])
            if hit:  # 檢查異常類型關閉時，仍沿用對應項目的未上線 / 已通報
                ev.state = "not_launched" if hit[0].not_launched else ("reported" if hit[0].reported else "")
            out[a.report_id][key] = ev
    if "issue" in types:
        for i in IssueRecord.query.filter(IssueRecord.report_id.in_(ids)):
            if not (i.jira or i.description or i.issue_type):
                continue
            key = ("issue", _k(i.jira) or _k(i.description)[:160])
            out[i.report_id][key] = Event(key, "issue", i.issue_type or "問題", i.description or "",
                                          _join(i.status, i.further_action), jira=i.jira or "", sources=["issue"])
    if "true_alarm" in types:
        for t in TrueAlarmRecord.query.filter(TrueAlarmRecord.report_id.in_(ids)):
            if (t.end_time or "").strip() or not (t.alarm_item or "").strip():
                continue
            key = ("true_alarm", _k(t.alarm_item))
            out[t.report_id][key] = Event(key, "true_alarm", t.alarm_item, f"開始 {t.start_time}" if t.start_time else "",
                                          _join(t.status, t.follow_up), sources=["true_alarm"])
    if "onsite" in types:
        for o in OnsiteRecord.query.filter(OnsiteRecord.report_id.in_(ids)):
            if not (o.further_action or "").strip() or not (o.title or "").strip():
                continue
            key = ("onsite", _k(o.title))
            out[o.report_id][key] = Event(key, "onsite", o.title, o.personnel or "",
                                          _join(o.status, o.further_action), jira=o.jira or "", sources=["onsite"])
    if known_re is not None:
        for evs in out.values():
            for ev in evs.values():
                if ev.state == "not_launched":
                    continue
                m = known_re.search(" ".join((ev.title, ev.detail, ev.note)))
                if m:
                    ev.state, ev.known_by = "known", m.group(0)
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
    return (str(db.engine.url), official_only, tuple(types), thresholds(), tuple(known_keywords()), tuple(m))


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


def _down_since(ev):
    m = DOWN_DATE.search(" ".join((ev.detail, ev.note)))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _build(official_only, types):
    reports = _source_reports(official_only)
    warn, alert = thresholds()
    data = {"latest": reports[0] if reports else None, "events": [], "known": [], "resolved": [], "trend": [],
            "types": types, "warn": warn, "alert": alert, "counts": {}, "keywords": known_keywords(),
            "STATE_LABELS": STATE_LABELS}
    if not reports:
        return data
    per = _extract(reports, types, _keyword_re(known_keywords()))
    latest = reports[0]
    oldest = reports[-1]
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
        ev.reports = len(run)
        ev.at_least = run[-1][0].id == oldest.id and len(reports) > 1
        ev.down_since = _down_since(ev)
        if ev.down_since and ev.down_since < ev.first and (ev.at_least or len(run) == len(reports)):
            ev.first, ev.at_least = ev.down_since, False
        ev.days = (ev.last - ev.first).days + 1
        ev.new = len(run) == 1 and len(reports) > 1
        ev.provisional = provisional and ev.new
        for r in reports[:TREND_DAYS]:
            e = per[r.id].get(key)
            if e is not None:
                ev.history.append((r.report_date, r.id, r.status, e.note, e.state))
        for i in range(STRIP_DAYS - 1, -1, -1):
            d = latest.report_date - timedelta(days=i)
            r = by_date.get(d)
            ev.strip.append(("none", d) if r is None else (("on" if key in per[r.id] else "off"), d))
        ev.level = 2 if ev.days > alert else (1 if ev.days >= warn else 0)
        events.append(ev)

    def order(e):
        return (e.state == "reported", -e.level, -e.days, list(TYPES).index(e.type), e.title)

    active = sorted((e for e in events if not e.is_known), key=order)
    data["events"] = active
    data["known"] = sorted((e for e in events if e.is_known), key=lambda e: (e.state, e.title))
    if len(reports) > 1:
        prev = reports[1]
        data["resolved"] = [e for k, e in per[prev.id].items() if k not in per[latest.id] and not e.is_known]
        data["prev"] = prev
    data["counts"] = {
        "active": len(active),
        "unreported": sum(1 for e in active if e.state != "reported"),
        "reported": sum(1 for e in active if e.state == "reported"),
        "new": sum(1 for e in active if e.new),
        "resolved": len(data["resolved"]),
        "long": sum(1 for e in active if e.days > alert),
        "known": len(data["known"]),
        "by_type": {t: sum(1 for e in active if t in e.sources) for t in types},
    }
    trend = []
    for r in reversed(reports[:TREND_DAYS]):
        c = {t: 0 for t in types}
        for e in per[r.id].values():
            if not e.is_known:
                c[e.type] += 1
        trend.append({"date": r.report_date, "status": r.status, "counts": c, "total": sum(c.values())})
    data["trend"] = trend
    data["trend_max"] = max([t["total"] for t in trend] + [1])
    return data
