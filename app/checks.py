"""日報檢查：直接用資料庫內容做一致性檢查，並與最近一份「已核准」日報比對。

規則沿用 DDoc_compare（RLY-ITO_DDoc_compare）的檢查項目，但不需要解析 PDF 或 OCR：
文件編號、表頭、頁碼、Reports Control 由系統產生，不會出錯，因此不再檢查。

嚴重度：錯誤（應退回）、警告（請確認）、提醒（參考）。
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field

from .helpers import SHEETS
from .models import ChecklistItem, ChecklistResult, Report, ScreenshotSection

ALARM_CATEGORY = {"vm": "Virtual Machines", "software": "Software", "physical": "Physical Devices", "pi": "PI"}
SEVERITY_ORDER = {"錯誤": 0, "警告": 1, "提醒": 2}
RESULT = {"N": "Normal", "W": "Warning", "F": "Failed", None: "未勾選"}


@dataclass
class Issue:
    severity: str
    area: str
    message: str
    ref: str = ""  # 項次 / 告警序號
    obj: str = ""  # 設備 / 欄位
    a: str = ""  # 依據
    b: str = ""  # 實際
    link: str = ""  # 修正位置（endpoint 名稱，畫面上轉成連結）


@dataclass
class CheckResult:
    issues: list = field(default_factory=list)
    baseline: Report | None = None
    gap_days: int = 0
    new_alarms: list = field(default_factory=list)
    cleared_alarms: list = field(default_factory=list)
    changed_alarms: list = field(default_factory=list)  # (今日, 基準, {欄位: (基準, 今日)})
    continued: int = 0
    status_changes: list = field(default_factory=list)  # (item, 基準結果, 今日結果, 基準備註, 今日備註)
    summary_diff: list = field(default_factory=list)  # (分頁, 分類, 基準 (N,W,F), 今日 (N,W,F))

    def add(self, *args, **kw):
        self.issues.append(Issue(*args, **kw))

    def counts(self):
        c = {"錯誤": 0, "警告": 0, "提醒": 0}
        for i in self.issues:
            c[i.severity] += 1
        return c

    def sorted_issues(self):
        return sorted(self.issues, key=lambda i: SEVERITY_ORDER[i.severity])


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _key(s):
    return re.sub(r"[^0-9a-z]", "", (s or "").lower())


def _alarm_key(row):
    # 同一設備、同一項次可能有多筆告警（例如 6.1 IPSec Tunnel 與 SD WAN），要連同 Description 一起比對
    return (_key(row.category), (row.item or "").strip(), _key(row.device_name), _key(row.description))


def baseline_for(report):
    """比對基準：日期早於本日報、最近一份已核准的日報。"""
    return (
        Report.query.filter(Report.report_date < report.report_date, Report.status == "approved")
        .order_by(Report.report_date.desc())
        .first()
    )


def _results(report):
    rows = (
        report.results.join(ChecklistItem)
        .with_entities(ChecklistResult, ChecklistItem)
        .order_by(ChecklistItem.sheet, ChecklistItem.order)
        .all()
    )
    return [(res, item) for res, item in rows]


def run_checks(report):
    r = CheckResult()
    results = _results(report)
    alarms = list(report.alarm_rows)

    _check_checklist(r, results)
    _check_alarm_table(r, alarms)
    _check_alarm_vs_checklist(r, results, alarms)
    _check_alarm_counts(r, report, alarms)
    _check_records(r, report)
    _check_basic(r, report)
    _check_screenshots(r, report)

    base = baseline_for(report)
    r.baseline = base
    if base is None:
        r.add("提醒", "跨日比對", "沒有日期較早的已核准日報，無法做跨日比對")
    else:
        r.gap_days = (report.report_date - base.report_date).days
        if r.gap_days > 1:
            r.add("提醒", "跨日比對", f"比對對象是 {base.report_date} 的日報，中間有 {r.gap_days - 1} 天沒有已核准日報",
                  obj=base.doc_no)
        _compare(r, report, results, alarms, base)
    return r


# ---------------------------------------------------------------- 單份日報


def _check_checklist(r, results):
    for res, item in results:
        obj = f"{item.device_name} {item.status_type}".strip()
        if res.result is None:
            r.add("錯誤", "勾選缺項", "Normal / Warning / Failed 都沒有勾選", ref=item.item_no, obj=obj,
                  link=f"checklist:{item.sheet}")
        elif res.result in ("W", "F") and not (res.note or "").strip():
            r.add("警告", "備註缺漏", f"勾選 {RESULT[res.result]} 但 Note 空白", ref=item.item_no, obj=obj,
                  a=f"勾選「{RESULT[res.result]}」", b="Note「(空白)」", link=f"checklist:{item.sheet}")


def _check_alarm_table(r, alarms):
    seen = defaultdict(list)
    for i, a in enumerate(alarms, 1):
        ref, obj = f"告警#{i}", f"{a.device_name} {a.description}".strip()
        if not (a.reason or "").strip():
            r.add("錯誤", "告警表", "缺 Reason", ref=ref, obj=obj, link="alarms")
        if not (a.category or "").strip() or not (a.device_name or "").strip():
            r.add("錯誤", "告警表", "缺 Alarm Category 或 Device Name", ref=ref, obj=obj, link="alarms")
        seen[(_key(a.device_name), _key(a.description), (a.item or "").strip())].append(i)
    for nos in seen.values():
        if len(nos) > 1:
            first = alarms[nos[0] - 1]
            r.add("警告", "告警表", f"Alarm Summary 重複項目 ×{len(nos)}", ref=", ".join(f"告警#{n}" for n in nos),
                  obj=f"{first.device_name} {first.description}", link="alarms")


def _match(alarm, item):
    """告警列對應到哪個檢查項目。設備名稱可能在 Device Name 或 Device Type 欄（Software 頁），
    項次可能寫細項（1.1）或大項編號（1）。"""
    if _key(alarm.category) != _key(ALARM_CATEGORY.get(item.sheet, "")):
        return False
    dev = _key(alarm.device_name)
    devices = {_key(item.device_name), _key(item.device_type)} - {""}
    dev_ok = not dev or dev in devices or any(dev in d and len(dev) > 5 for d in devices)
    if not dev_ok:
        return False
    it = (alarm.item or "").strip()
    desc = _key(alarm.description)
    desc_ok = bool(desc) and desc in (_key(item.status_type), _key(item.description))
    if it and it == item.item_no:
        return True
    if it and it == (item.no or "").strip():
        return desc_ok or not desc
    return desc_ok


def _check_alarm_vs_checklist(r, results, alarms):
    """Alarm Summary 必須與檢查表的 Warning / Failed 勾選一一對應（雙向）。"""
    abnormal = [(res, item) for res, item in results if res.result in ("W", "F")]
    matched = set()
    for i, a in enumerate(alarms, 1):
        hits = [k for k, (_, item) in enumerate(abnormal) if _match(a, item)]
        ref, obj = f"告警#{i}", f"{a.device_name} {a.description}".strip()
        if not hits:
            r.add("錯誤", "告警 vs 勾選", "Alarm Summary 有此告警，但檢查表沒有對應的 Warning/Failed 勾選",
                  ref=ref, obj=obj, a=f"Reason「{a.reason or ''}」", b="檢查表「找不到 / 已勾 Normal」", link="alarms")
            continue
        matched.update(hits)
        res, item = abnormal[hits[0]]
        note, reason = _norm(res.note), _norm(a.reason)
        if note and note != reason:
            if reason and (reason in note or note in reason):
                r.add("提醒", "告警 vs 備註", "告警 Reason 與檢查表 Note 部分相同（其中一方多了補充說明）", ref=ref, obj=obj,
                      a=f"Reason「{a.reason or ''}」", b=f"檢查表 Note「{res.note}」", link="alarms")
            else:
                r.add("警告", "告警 vs 備註", "告警 Reason 與檢查表 Note 不一致", ref=ref, obj=obj,
                      a=f"Reason「{a.reason or ''}」", b=f"檢查表 Note「{res.note}」", link="alarms")
    for k, (res, item) in enumerate(abnormal):
        if k not in matched:
            r.add("錯誤", "勾選 vs 告警", "檢查表勾選 Warning/Failed，但 Alarm Summary 沒有列出", ref=item.item_no,
                  obj=f"{item.device_name} {item.status_type}".strip(),
                  a=f"勾選「{RESULT[res.result]}」 Note「{res.note or '(空白)'}」", b="Alarm Summary「沒有」",
                  link="alarms")


def _check_alarm_counts(r, report, alarms):
    new, known, false = report.alarm_new or 0, report.alarm_known or 0, report.alarm_false or 0
    if new + known + false != len(alarms):
        r.add("錯誤", "告警數量", "告警計數與 Alarm Summary 項數不符", obj="4 Daily Alarm Inspection",
              a=f"New+Known+False = {new}+{known}+{false} = {new + known + false}", b=f"Alarm Summary {len(alarms)} 項",
              link="alarms")
    flagged = sum(1 for a in alarms if (a.false_alarm or "").upper() == "Y")
    if flagged != false:
        r.add("錯誤", "告警數量", "False Alarm 計數與表中標 Y 的數量不符", a=f"False Alarm「{false}」",
              b=f"表中標 Y {flagged} 項", link="alarms")
    if new > 0 and not report.true_alarms:
        r.add("錯誤", "True Alarm", "有 New Alarm，但 True Alarm Record 為 N/A", a=f"New Alarm「{new}」",
              b="True Alarm Record「N/A」", link="alarms")


NO_ISSUE = re.compile(r"\bno\b.*\bissue\b|沒有|無", re.I)
NO_ONSITE = re.compile(r"\bno\b.*\b(on-?site|work|update)\b|沒有|無", re.I)


def _check_records(r, report):
    says_none = bool(NO_ISSUE.search(report.inspection_summary or ""))
    if says_none and report.issues:
        r.add("錯誤", "Inspection Record", "說明寫沒有問題記錄，但 Issue Record 有資料",
              a=f"說明「{report.inspection_summary}」", b=f"Issue Record {len(report.issues)} 筆", link="records")
    elif not says_none and not report.issues:
        r.add("警告", "Inspection Record", "說明沒有寫「無問題記錄」，但 Issue Record 是 N/A",
              a=f"說明「{report.inspection_summary}」", b="Issue Record「N/A」", link="records")
    says_none = bool(NO_ONSITE.search(report.onsite_summary or ""))
    has = bool(report.onsite_records or report.system_updates)
    if says_none and has:
        r.add("錯誤", "On-site Record", "說明寫沒有現場作業 / 系統更新，但記錄有資料",
              a=f"說明「{report.onsite_summary}」",
              b=f"On-site {len(report.onsite_records)} 筆、System Update {len(report.system_updates)} 筆", link="records")
    elif not says_none and not has:
        r.add("警告", "On-site Record", "說明沒有寫「無現場作業」，但記錄是 N/A", a=f"說明「{report.onsite_summary}」",
              b="On-site / System Update「N/A」", link="records")


def _check_basic(r, report):
    if not (report.first_line_officer or "").strip():
        r.add("錯誤", "Shift Record", "First Line Duty officer 空白", link="basic")
    if not (report.second_line_support or "").strip():
        r.add("警告", "Shift Record", "Second Line Backup Support 空白", link="basic")
    if not report.approver_id and not (report.approver_name or "").strip():
        r.add("警告", "核准人", "未指定核准人（文件 Approval 欄會是空白）", link="basic")


def _check_screenshots(r, report):
    with_images = {i.section_id for i in report.images if i.kind == "screenshot"}
    sections = ScreenshotSection.query.order_by(ScreenshotSection.order).all()
    # 有子章節的大章節本身不放圖
    parents = {s.code.split(".")[0] for s in sections if "." in s.code}
    missing = [s for s in sections if s.id not in with_images and not (s.level == 1 and s.code in parents)]
    if missing:
        r.add("警告", "截圖", f"{len(missing)} 個章節沒有上傳截圖", obj="、".join(f"{s.code} {s.title}" for s in missing[:12])
              + ("…" if len(missing) > 12 else ""), link="screenshots")


# ---------------------------------------------------------------- 跨日比對


def _compare(r, report, results, alarms, base):
    T = {_alarm_key(a): a for a in alarms}
    Y = {_alarm_key(a): a for a in base.alarm_rows}
    for k, a in T.items():
        b = Y.get(k)
        if b is None:
            r.new_alarms.append(a)
            continue
        diff = {f: (getattr(b, f) or "", getattr(a, f) or "") for f in ("description", "reason", "false_alarm", "note")
                if _norm(getattr(a, f)) != _norm(getattr(b, f))}
        if diff:
            r.changed_alarms.append((a, b, diff))
        r.continued += 1
    r.cleared_alarms = [b for k, b in Y.items() if k not in T]

    if (report.alarm_new or 0) != len(r.new_alarms):
        r.add("錯誤", "跨日比對", "New Alarm 數與實際新增的告警數不同", obj="New Alarm",
              a=f"與 {base.report_date} 比對，實際新增 {len(r.new_alarms)} 項", b=f"填寫「{report.alarm_new or 0}」",
              link="alarms")
    if (report.alarm_false or 0) == 0 and (report.alarm_known or 0) != r.continued:
        r.add("錯誤", "跨日比對", "Known Alarm 數與延續自前一份日報的告警數不同", obj="Known Alarm",
              a=f"延續 {r.continued} 項", b=f"填寫「{report.alarm_known or 0}」", link="alarms")
    if r.cleared_alarms:
        r.add("提醒", "跨日比對", f"{len(r.cleared_alarms)} 項告警已解除（前一份有、這份沒有），請確認確實已排除",
              obj="、".join(f"{b.device_name} {b.description}" for b in r.cleared_alarms[:6]))

    # 檢查結果變化
    base_res = {item_id: (res, note) for item_id, res, note in base.results.with_entities(
        ChecklistResult.item_id, ChecklistResult.result, ChecklistResult.note)}
    for res, item in results:
        old = base_res.get(item.id)
        if old and (old[0] != res.result or _norm(old[1]) != _norm(res.note)):
            r.status_changes.append((item, old[0], res.result, old[1] or "", res.note or ""))

    # 分類統計變化
    def stats(rows):
        out = defaultdict(lambda: [0, 0, 0])
        for item, val in rows:
            if val in ("N", "W", "F"):
                out[(item.sheet, item.category or "")]["NWF".index(val)] += 1
        return out

    by_id = {item.id: item for _, item in results}
    today = stats((item, res.result) for res, item in results)
    before = stats((by_id[i], v[0]) for i, v in base_res.items() if i in by_id)
    for k in sorted(set(today) | set(before), key=lambda k: (list(SHEETS).index(k[0]), k[1])):
        if tuple(today[k]) != tuple(before[k]):
            r.summary_diff.append((SHEETS[k[0]], k[1] or "（整頁）", tuple(before[k]), tuple(today[k])))
