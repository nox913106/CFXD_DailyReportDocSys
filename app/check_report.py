"""檢查結果.md：把 run_checks() 的結果寫成與 DDoc_compare `<DocNo>_比對報告.md` 相同結構的 Markdown。

- 章節編號沿用 DDoc_compare（一-1 ～ 一-8、二-1 ～ 二-4），舊的比對報告與這裡產生的檔案可以放在同一個 RAG 知識庫。
  二-5（檢查表列數差異）是 PDF 解析才有的問題，這裡不會發生；新增的二-6 是告警歷程。
- 檔案開頭有 YAML front matter（文件編號、日期、階段、錯誤 / 警告 / 提醒數），RAG 可依此過濾。
- 退回 / 核准時另有「三、審核結果」，記錄審核者的決定與意見，讓 AI 學到哪些問題會被退回。

送審、核准、退回時各存一份快照（CheckReport），並匯出到 CHECK_MD_DIR/<年>/<月>/。
"""

import os
import re
from collections import Counter
from datetime import datetime

from flask import current_app

from . import db
from .checks import RESULT, run_checks
from .models import CheckReport, ScreenshotSection

FORMAT_VERSION = 1
SEV_ORDER = {"錯誤": 0, "警告": 1, "提醒": 2}
# 一、今日文件內部一致性：分組與 DDoc_compare 相同（area -> 組）
GROUPS = [
    ("告警摘要 ↔ 檢查表內容", ("告警 vs 勾選", "勾選 vs 告警", "告警 vs 備註")),
    ("告警數量與告警表", ("告警數量", "告警表", "True Alarm")),
    ("統計表 ↔ 檢查表勾選", ("統計表", "統計圖", "統計 vs 勾選")),
    ("檢查表勾選與備註", ("勾選缺項", "勾選重複", "備註缺漏", "項次編號")),
    ("敘述 ↔ 記錄欄位", ("Inspection Record", "On-site Record")),
    ("文件資訊（值班人員／核准人）", ("Shift Record", "核准人")),
    ("截圖", ("截圖日期", "截圖")),
    ("其他", ()),
]
CROSS_AREAS = ("跨日比對",)
HISTORY_AREAS = ("告警歷程",)
ISSUE_HEADER = ["等級", "編號", "對象", "依據", "對照", "說明"]
FIELD_NAMES = dict(description="Description", reason="Reason", false_alarm="False Alarm", note="Note")


def md_escape(s):
    return str(s if s is not None else "").replace("|", "\\|").replace("\r", "").replace("\n", " ")


def table(L, header, rows, keep=()):
    """Markdown 表格；整欄空白的欄位不輸出（keep 內的欄位除外）。"""
    if not rows:
        return
    cols = [i for i, h in enumerate(header) if h in keep or any(str(r[i]).strip() for r in rows)]
    L.append("| " + " | ".join(header[i] for i in cols) + " |")
    L.append("|" + "---|" * len(cols))
    for r in rows:
        L.append("| " + " | ".join(md_escape(r[i]) for i in cols) + " |")
    L.append("")


def issue_table(L, issues):
    issues = sorted(issues, key=lambda i: SEV_ORDER[i.severity])
    table(L, ISSUE_HEADER, [[i.severity, i.ref or "—", i.obj or "—", i.a or "—", i.b or "—", i.message]
                            for i in issues], keep=ISSUE_HEADER)


def sev_counts(issues):
    c = Counter(i.severity for i in issues)
    return [c["錯誤"] or "", c["警告"] or "", c["提醒"] or ""]


def group_of(area):
    for n, (_, areas) in enumerate(GROUPS):
        if area in areas:
            return n
    return len(GROUPS) - 1


def _front_matter(report, chk, stage, now):
    c = chk.counts()
    meta = {
        "type": "cfxd-daily-report-check",
        "format_version": FORMAT_VERSION,
        "doc_no": report.doc_no,
        "report_date": report.report_date.isoformat(),
        "revision": report.revision,
        "stage": stage,
        "report_status": report.status,
        "errors": c["錯誤"],
        "warnings": c["警告"],
        "infos": c["提醒"],
        "baseline_doc_no": chk.baseline.doc_no if chk.baseline else "",
        "baseline_date": chk.baseline.report_date.isoformat() if chk.baseline else "",
        "generated_at": now.isoformat(timespec="seconds"),
        "source": "CFXD_DailyReportDocSys",
    }

    def val(v):
        return str(v) if isinstance(v, int) else '"' + str(v).replace('"', "'") + '"'
    return ["---", *(f"{k}: {val(v)}" for k, v in meta.items()), "---", ""]


def _clocks(L, report):
    from .ocr import CLOCK_STATUS, OCR_STATE, clock_of, status_for

    sections = {s.id: s for s in ScreenshotSection.query}
    shots = sorted((i for i in report.images if i.kind == "screenshot"),
                   key=lambda i: (sections[i.section_id].order if i.section_id in sections else 0, i.order, i.id))
    if not shots:
        return
    doc = report.report_date.isoformat()
    rows, cnt = [], Counter()
    for n, img in enumerate(shots, 1):
        st, label = status_for(img, doc)
        cnt[st] += 1
        rec = clock_of(img) or {}
        sec = sections.get(img.section_id)
        rows.append([f"第{n}張", f"{sec.code} {sec.title}" if sec else "", ", ".join(rec.get("dates") or []) or "—",
                     rec.get("time") or "—", label + (f"（{rec['region']}）" if rec.get("region") else "")])
    order = [*CLOCK_STATUS, *OCR_STATE]
    labels = {**CLOCK_STATUS, **OCR_STATE}
    table(L, ["截圖總數", *(labels[k] for k in order if cnt[k])], [[len(shots), *(cnt[k] for k in order if cnt[k])]])
    L.append("<details><summary>截圖逐張明細</summary>\n")
    table(L, ["編號", "對象", "截圖日期", "截圖時間", "判定"], rows)
    L.append("</details>\n")


def render(report, chk=None, stage="manual", review=None, now=None):
    """review：(動作, 審核者, 代簽對象, 意見)，核准 / 退回時寫入「三、審核結果」。"""
    chk = chk or run_checks(report)
    now = now or datetime.now()
    L = _front_matter(report, chk, stage, now)
    c = chk.counts()
    L.append(f"# 日報檢查結果 — {report.doc_no} ({report.report_date})\n")
    base = chk.baseline
    info = [["文件編號", report.doc_no, base.doc_no if base else "—"],
            ["日期", report.report_date, base.report_date if base else "—"],
            ["版次", report.revision, base.revision if base else "—"],
            ["撰寫人", report.author_name, base.author_name if base else "—"],
            ["告警項數", len(report.alarm_rows), len(base.alarm_rows) if base else "—"],
            ["New / Known / False Alarm", f"{report.alarm_new or 0} / {report.alarm_known or 0} / {report.alarm_false or 0}",
             f"{base.alarm_new or 0} / {base.alarm_known or 0} / {base.alarm_false or 0}" if base else "—"]]
    table(L, ["項目", "今日", "比對基準（前一份已核准）"], info)
    L.append(f"- 產生時機：{dict(submit='送審', approve='核准', reject='退回', manual='手動重新產生', backfill='補產生').get(stage, stage)}"
             f"（{now:%Y-%m-%d %H:%M}）")
    if c["錯誤"]:
        L.append(f"- 結論：有 {c['錯誤']} 項錯誤需要修正")
    elif c["警告"]:
        L.append(f"- 結論：沒有錯誤，有 {c['警告']} 項警告請確認")
    else:
        L.append("- 結論：沒有發現錯誤或警告")
    L.append(f"- 錯誤 {c['錯誤']}、警告 {c['警告']}、提醒 {c['提醒']}\n")

    internal = [i for i in chk.issues if i.area not in CROSS_AREAS + HISTORY_AREAS]
    cross = [i for i in chk.issues if i.area in CROSS_AREAS]
    hist = [i for i in chk.issues if i.area in HISTORY_AREAS]
    grouped = [[] for _ in GROUPS]
    for i in internal:
        grouped[group_of(i.area)].append(i)

    L.append("## 總覽\n")
    ov = [[f"一-{n + 1}", name, *sev_counts(g)] for n, ((name, _), g) in enumerate(zip(GROUPS, grouped))]
    ov.append(["二-1", "跨日一致性", *sev_counts(cross)])
    if base:
        ov.append(["二-2", f"告警差異：新增 {len(chk.new_alarms)}、解除 {len(chk.cleared_alarms)}、"
                           f"內容變更 {len(chk.changed_alarms)}、延續 {chk.continued}", "", "", ""])
    if chk.history:
        ov.append(["二-6", "告警歷程（持續天數／上次發生）", *sev_counts(hist)])
    table(L, ["步驟", "檢查", "錯誤", "警告", "提醒"], ov, keep=("錯誤", "警告", "提醒"))
    L.append("空白 = 沒有發現問題。\n")

    L.append("## 一、今日文件內部一致性\n")
    for n, ((name, _), g) in enumerate(zip(GROUPS, grouped)):
        L.append(f"### 一-{n + 1} {name}\n")
        if g:
            issue_table(L, g)
        else:
            L.append("沒有發現問題。\n")
        if name == "截圖":
            _clocks(L, report)

    L.append("## 二、與前一份已核准日報比對\n")
    if not base:
        L.append("### 二-1 跨日一致性\n")
        issue_table(L, cross)
    else:
        L.append(f"比對基準：{base.doc_no}（{base.report_date}），間隔 {chk.gap_days} 天。\n")
        L.append("### 二-1 跨日一致性\n")
        if cross:
            issue_table(L, cross)
        else:
            L.append("沒有發現問題。\n")

        L.append("### 二-2 告警差異\n")
        L.append(f"新增 {len(chk.new_alarms)} 項、解除 {len(chk.cleared_alarms)} 項、內容變更 {len(chk.changed_alarms)} 項、"
                 f"延續 {chk.continued} 項。\n")
        q = lambda label, v: f"{label}「{v}」" if v not in (None, "") else f"{label}「(空白)」"
        rows = [["新增", f"告警#{a.order}", f"{a.device_name} {a.description}", "—", q("Reason", a.reason)]
                for a in chk.new_alarms]
        rows += [["解除", f"前一份告警#{b.order}", f"{b.device_name} {b.description}", q("Reason", b.reason), "—"]
                 for b in chk.cleared_alarms]
        for a, b, diff in chk.changed_alarms:
            for f, (old, new) in diff.items():
                rows.append(["變更", f"告警#{a.order}", f"{a.device_name} {a.description}",
                             q(FIELD_NAMES.get(f, f), old), q(FIELD_NAMES.get(f, f), new)])
        if rows:
            table(L, ["變化", "編號", "對象", "前一份", "今日"], rows)
        else:
            L.append("沒有差異。\n")

        L.append("### 二-3 統計數字變化\n")
        if chk.summary_diff:
            table(L, ["分頁", "分類", "前一份 N/W/F", "今日 N/W/F"],
                  [[s, cat, "/".join(map(str, old)), "/".join(map(str, new))] for s, cat, old, new in chk.summary_diff])
        else:
            L.append("沒有變化。\n")

        L.append("### 二-4 檢查表勾選／備註變化\n")
        if chk.status_changes:
            table(L, ["分頁", "編號", "對象", "前一份", "今日"],
                  [[item.sheet, item.item_no, f"{item.device_name} {item.status_type}".strip(),
                    f"勾選「{RESULT.get(old, old)}」 {q('Note', onote)}", f"勾選「{RESULT.get(new, new)}」 {q('Note', nnote)}"]
                   for item, old, new, onote, nnote in chk.status_changes])
        else:
            L.append("沒有變化。\n")

    if chk.history:
        L.append(f"### 二-6 告警歷程（回溯 {chk.history_days} 天，只計已核准日報）\n")
        if hist:
            issue_table(L, hist)
        today = report.report_date
        table(L, ["編號", "Category", "對象", "持續", "這次開始", "上次發生"],
              [[f"告警#{h.alarm.order}", h.alarm.category, f"{h.alarm.device_name} {h.alarm.item} {h.alarm.description}",
                f"超過 {chk.history_days} 天" if h.truncated else ("今日新增" if h.is_new else f"第 {h.days} 天"),
                "—" if h.truncated else h.since,
                f"{h.last_seen}（{h.days_ago(today)} 天前，{h.last_doc_no}）" if h.last_seen
                else ("—" if h.truncated else f"{chk.history_days} 天內沒有")] for h in chk.history])

    if review:
        action, actor, principal, comment = review
        L.append("## 三、審核結果\n")
        table(L, ["項目", "內容"], [
            ["決定", {"approve": "核准", "reject": "退回"}.get(action, action)],
            ["簽核者（文件顯示）", (principal or actor).name],
            ["實際操作", f"{actor.name} 代簽" if principal else "本人"],
            ["意見", comment or "（無）"],
            ["審核當下", f"錯誤 {c['錯誤']}、警告 {c['警告']}、提醒 {c['提醒']}"],
        ])
    return "\n".join(L).rstrip() + "\n"


# ---------------------------------------------------------------- 快照與匯出


def _safe(s):
    return re.sub(r"[^0-9A-Za-z._-]+", "_", s or "report").strip("_") or "report"


def export_path(snap):
    r = snap.report
    return os.path.join(f"{r.report_date:%Y}", f"{r.report_date:%m}",
                        f"{_safe(r.doc_no)}_R{r.revision}_{snap.stage}_{snap.created_at:%Y%m%d%H%M%S}_檢查結果.md")


def export(snap, root=None):
    """寫到 CHECK_MD_DIR（先寫 .part 再改名，RAG 收錄程式不會讀到寫一半的檔案）。失敗不影響審核流程。"""
    if root is None:
        from .ai_providers import get_setting

        root = get_setting("check_md_dir")
    if not root:
        return None
    rel = export_path(snap)
    path = os.path.join(root, rel)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".part", "w", encoding="utf-8", newline="\n") as f:
            f.write(snap.markdown)
        os.replace(path + ".part", path)
    except OSError:
        current_app.logger.exception("檢查結果.md 匯出失敗：%s", path)
        return None
    snap.file_path = rel
    return path


def snapshot(report, stage, user=None, review=None, ai=False):
    """建立檢查結果快照並匯出；ai=True 時排入 AI 建議。呼叫端負責 commit。"""
    from .ai_providers import ai_enabled, get_setting

    queue = ai and ai_enabled() and (stage != "submit" or get_setting("ai_auto_on_submit"))
    root = get_setting("check_md_dir")
    chk = run_checks(report)
    now = datetime.now()
    c = chk.counts()
    snap = CheckReport(report=report, stage=stage, created_at=now, created_by_id=user.id if user else None,
                       markdown=render(report, chk, stage, review, now),
                       errors=c["錯誤"], warnings=c["警告"], infos=c["提醒"], ai_status="pending" if queue else None)
    db.session.add(snap)
    export(snap, root)
    return snap


def refresh(snap):
    """送審中內容已鎖定，只有截圖 OCR 結果會改變；AI 產生前重新產生 .md，讓依據是完整的辨識結果。"""
    r = snap.report
    chk = run_checks(r)
    c = chk.counts()
    snap.markdown = render(r, chk, snap.stage, None, snap.created_at)
    snap.errors, snap.warnings, snap.infos = c["錯誤"], c["警告"], c["提醒"]
    if snap.file_path:
        export(snap)
