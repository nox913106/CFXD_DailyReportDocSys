"""AI 建議與提醒：依據「檢查結果.md」請 AI 給審核者建議。

- 要用哪些 AI 服務（按量、包月、地端）、提示詞、參數都在前端「系統設定」頁設定（ai_providers.py），
  依優先順序嘗試，失敗或本期額度 / 預算用完就換下一個。
- 由背景服務 `flask --app wsgi ai-worker` 處理，網頁不會等 AI 回應。
- AI 的依據只有檢查結果.md：本次的檢查結果，加上 retrieve_context() 找到的過去檢查結果.md（RAG）。
  retrieve_context() 目前用資料庫條件挑選（同一份日報先前被退回的版本、最近被退回的其他日報），
  日後改成向量檢索只要替換這個函式。
"""

import logging
import re
import time
from datetime import datetime, timedelta

from . import ai_providers as prov

log = logging.getLogger(__name__)
STALE_AFTER = timedelta(minutes=15)  # 背景服務中斷時，卡在 running 的工作重新排隊
OCR_WAIT = timedelta(minutes=30)  # 截圖還在辨識時先等，最多等這麼久
SYSTEM_PROMPT = prov.DEFAULT_SYSTEM_PROMPT


def enabled():
    return prov.ai_enabled()


def _strip_details(md):
    # 截圖逐張明細等 <details> 區塊很長，問題已經列在上面的表格，不送給 AI
    return re.sub(r"<details>.*?</details>\n?", "", md, flags=re.S)


def _clip(md, limit):
    md = _strip_details(md)
    return md if len(md) <= limit else md[:limit] + "\n\n…（內容過長，以下省略）\n"


def retrieve_context(snap, limit=None):
    """RAG：挑選與本次審核相關的過去檢查結果.md，回傳 [(標題, markdown)]。"""
    from .models import CheckReport, Report

    limit = prov.get_setting("ai_context_reports") if limit is None else limit
    if limit <= 0:
        return []
    out = []
    # 1. 同一份日報先前被退回的版本：這次是否已修正
    same = (CheckReport.query.filter(CheckReport.report_id == snap.report_id, CheckReport.stage == "reject",
                                     CheckReport.id < snap.id).order_by(CheckReport.id.desc()).limit(2).all())
    out += [(f"本日報 {s.report.doc_no} 先前被退回時的檢查結果（{s.created_at:%Y-%m-%d %H:%M}）", s) for s in same]
    # 2. 最近 60 天其他日報被退回的檢查結果：哪些問題常被退回
    since = snap.report.report_date - timedelta(days=60)
    others = (CheckReport.query.join(Report, Report.id == CheckReport.report_id)
              .filter(CheckReport.stage == "reject", CheckReport.report_id != snap.report_id,
                      Report.report_date >= since, Report.report_date < snap.report.report_date)
              .order_by(Report.report_date.desc(), CheckReport.id.desc()).limit(limit).all())
    out += [(f"{s.report.doc_no}（{s.report.report_date}）被退回時的檢查結果", s) for s in others]
    return [(title, s.markdown) for title, s in out[:limit]]


def build_messages(snap):
    max_chars = prov.get_setting("ai_max_chars")
    parts = [f"以下是 {snap.report.doc_no}（{snap.report.report_date}）的檢查結果.md：\n\n{_clip(snap.markdown, max_chars)}"]
    ctx = retrieve_context(snap)
    if ctx:
        parts.append("參考：過去的檢查結果（只用來比較，不是本次的檢查結果）")
        for title, md in ctx:
            parts.append(f"### {title}\n\n{_clip(md, max_chars // 3)}")
    system = prov.get_setting("ai_system_prompt") or SYSTEM_PROMPT
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(parts)}]


def suggest(snap, caller=None):
    """依優先順序嘗試可用的服務，回傳 (文字, 「服務名稱 / 模型」)。全部失敗時拋出 RuntimeError。"""
    providers = prov.available_providers()
    if not providers:
        raise RuntimeError("沒有可用的 AI 服務（請到「系統設定」新增或啟用，或確認本期額度 / 預算）")
    msgs = build_messages(snap)
    errors = []
    for p in providers:
        try:
            r = prov.call(p, msgs[0]["content"], msgs[1]["content"], check_report_id=snap.id, caller=caller)
            return r.text, f"{p.name} / {r.model}"
        except RuntimeError as e:
            errors.append(f"{p.name}：{e}")
    raise RuntimeError("；".join(errors))


# ---------------------------------------------------------------- 背景服務


def _ocr_waiting(report):
    return any(i.kind == "screenshot" and i.ocr_status in ("pending", "running") for i in report.images)


def claim_next(db, now=None):
    """取得下一份要產生 AI 建議的檢查結果（原子性，多個程序也不會重複處理）。"""
    from .models import CheckReport

    if not prov.get_setting("ai_enabled"):
        return None  # 總開關關閉：工作留在佇列，重新啟用後再處理
    now = now or datetime.now()
    for s in CheckReport.query.filter(CheckReport.ai_status == "running", CheckReport.ai_started_at < now - STALE_AFTER):
        s.ai_status = "pending"
    db.session.commit()
    for cand in CheckReport.query.filter_by(ai_status="pending").order_by(CheckReport.id).limit(20):
        if _ocr_waiting(cand.report) and cand.created_at > now - OCR_WAIT:
            continue  # 截圖還在辨識，等辨識完再產生，依據才完整
        n = (CheckReport.query.filter_by(id=cand.id, ai_status="pending")
             .update({"ai_status": "running", "ai_started_at": now}, synchronize_session=False))
        db.session.commit()
        if n:
            return db.session.get(CheckReport, cand.id)
    return None


def process_one(db, caller=None):
    from .check_report import refresh
    from .models import CheckReport

    snap = claim_next(db)
    if snap is None:
        return False
    try:
        latest = snap.report.check_reports.order_by(None).order_by(CheckReport.id.desc()).first()
        if snap.report.status == "submitted" and latest.id == snap.id:
            refresh(snap)  # 截圖辨識結果可能在送審後才出來
        text, model = suggest(snap, caller=caller)
        snap.ai_markdown, snap.ai_model, snap.ai_error, snap.ai_status = text, model, None, "done"
    except Exception as e:  # 單筆失敗不影響其他工作
        log.exception("檢查結果 %s 產生 AI 建議失敗", snap.id)
        snap.ai_status, snap.ai_error = "error", str(e)[:500]
    snap.ai_at = datetime.now()
    db.session.commit()
    return True


def run_worker(app, once=False, idle_seconds=5):
    from . import db

    with app.app_context():
        log.warning("AI 背景服務啟動（服務設定在「系統設定」頁，每次產生前重新讀取）")
        while True:
            if not process_one(db):
                if once:
                    return
                db.session.remove()
                time.sleep(idle_seconds)


# ---------------------------------------------------------------- 畫面


def md_to_html(text):
    """AI 回覆轉成 HTML。先跳脫所有 HTML（AI 輸出不可信），只支援標題、條列、粗體、行內程式碼、段落。"""
    from markupsafe import Markup, escape

    def inline(s):
        s = str(escape(s))
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        return re.sub(r"`([^`]+)`", r"<code>\1</code>", s)

    out, in_list = [], False
    for ln in (text or "").splitlines():
        s = ln.strip()
        m_h = re.match(r"(#{1,6})\s+(.*)", s)
        m_li = re.match(r"(?:[-*•]|\d+[.)])\s+(.*)", s)
        if m_li:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(m_li.group(1))}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if m_h:
            out.append(f"<h4>{inline(m_h.group(2))}</h4>")
        elif s:
            out.append(f"<p>{inline(s)}</p>")
    if in_list:
        out.append("</ul>")
    return Markup("\n".join(out))
