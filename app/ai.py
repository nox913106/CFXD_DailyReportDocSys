"""AI 建議與提醒：依據「檢查結果.md」請地端 AI 給審核者建議。

- 只呼叫 OpenAI 相容的 Chat Completions API（Ollama、LM Studio、llama.cpp server、vLLM 都支援），
  AI_BASE_URL 指向公司內部的 AI 主機，資料不會送到外部。沒有設定 AI_BASE_URL 時整個功能關閉。
- 由背景服務 `flask --app wsgi ai-worker` 處理，網頁不會等 AI 回應（地端模型一次可能要數十秒）。
- AI 的依據只有檢查結果.md：本次的檢查結果，加上 retrieve_context() 找到的過去檢查結果.md（RAG）。
  retrieve_context() 目前用資料庫條件挑選（同一份日報先前被退回的版本、最近被退回的其他日報），
  日後改成向量檢索只要替換這個函式。
"""

import json
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from flask import current_app

log = logging.getLogger(__name__)
STALE_AFTER = timedelta(minutes=15)  # 背景服務中斷時，卡在 running 的工作重新排隊
OCR_WAIT = timedelta(minutes=30)  # 截圖還在辨識時先等，最多等這麼久

SYSTEM_PROMPT = """你是 CFXD SLA 每日維運日報的審核助理，協助審核者決定核准或退回。
規則：
1. 只能根據使用者提供的「檢查結果.md」內容回答，不可以推測或編造檢查結果裡沒有的事實、設備或數字。
2. 每一點都要標出依據的檢查步驟編號（例如「一-2」「二-6」）和對象（設備或欄位）。
3. 錯誤代表確定有誤、應修正後再送審；警告需要人判斷；提醒僅供參考。不要把提醒說成錯誤。
4. 若提供了「參考：過去的檢查結果」，可以指出相同問題是否反覆發生、先前退回的問題這次是否已修正，
   但必須寫明是哪一份（文件編號）。
5. 用繁體中文，簡潔、條列，不要重複貼整張表格。

輸出格式（Markdown）：
## 建議
一句話：「建議退回修正」、「確認警告後可核准」或「可核准」，並說明主要理由。
## 需要處理
- [步驟編號] 對象：問題與建議的處理方式（沒有就寫「無」）
## 提醒
- 值得審核者注意但不影響核准的事項，例如持續很久或反覆發生的告警（沒有就寫「無」）
"""


def enabled():
    return bool(current_app.config.get("AI_BASE_URL"))


def _strip_details(md):
    # 截圖逐張明細等 <details> 區塊很長，問題已經列在上面的表格，不送給 AI
    return re.sub(r"<details>.*?</details>\n?", "", md, flags=re.S)


def _clip(md, limit):
    md = _strip_details(md)
    return md if len(md) <= limit else md[:limit] + "\n\n…（內容過長，以下省略）\n"


def retrieve_context(snap, limit=None):
    """RAG：挑選與本次審核相關的過去檢查結果.md，回傳 [(標題, markdown)]。"""
    from .models import CheckReport, Report

    limit = current_app.config.get("AI_CONTEXT_REPORTS", 3) if limit is None else limit
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
    max_chars = current_app.config.get("AI_MAX_CHARS", 24000)
    parts = [f"以下是 {snap.report.doc_no}（{snap.report.report_date}）的檢查結果.md：\n\n{_clip(snap.markdown, max_chars)}"]
    ctx = retrieve_context(snap)
    if ctx:
        parts.append("參考：過去的檢查結果（只用來比較，不是本次的檢查結果）")
        for title, md in ctx:
            parts.append(f"### {title}\n\n{_clip(md, max_chars // 3)}")
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "\n\n".join(parts)}]


def chat(messages):
    """呼叫 OpenAI 相容 API，回傳 (文字, 模型名稱)。"""
    cfg = current_app.config
    url = cfg["AI_BASE_URL"].rstrip("/") + "/chat/completions"
    body = json.dumps({"model": cfg.get("AI_MODEL") or "default", "messages": messages,
                       "temperature": 0.2, "stream": False}).encode()
    headers = {"Content-Type": "application/json"}
    if cfg.get("AI_API_KEY"):
        headers["Authorization"] = f"Bearer {cfg['AI_API_KEY']}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=cfg.get("AI_TIMEOUT", 180)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"AI 服務回應 HTTP {e.code}：{e.read()[:300].decode('utf-8', 'replace')}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"無法連線到 AI 服務：{e.reason}") from e
    text = data["choices"][0]["message"].get("content") or ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()  # 推理模型的思考過程不顯示
    if not text:
        raise RuntimeError("AI 服務沒有回傳內容")
    return text, data.get("model") or cfg.get("AI_MODEL") or ""


# ---------------------------------------------------------------- 背景服務


def _ocr_waiting(report):
    return any(i.kind == "screenshot" and i.ocr_status in ("pending", "running") for i in report.images)


def claim_next(db, now=None):
    """取得下一份要產生 AI 建議的檢查結果（原子性，多個程序也不會重複處理）。"""
    from .models import CheckReport

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


def process_one(db, llm=None):
    from .check_report import refresh
    from .models import CheckReport

    snap = claim_next(db)
    if snap is None:
        return False
    llm = llm or chat
    try:
        latest = snap.report.check_reports.order_by(None).order_by(CheckReport.id.desc()).first()
        if snap.report.status == "submitted" and latest.id == snap.id:
            refresh(snap)  # 截圖辨識結果可能在送審後才出來
        text, model = llm(build_messages(snap))
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
        if not enabled():
            raise SystemExit("沒有設定 AI_BASE_URL，AI 建議功能未啟用。")
        log.warning("AI 背景服務啟動：%s（%s）", app.config["AI_BASE_URL"], app.config.get("AI_MODEL"))
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
