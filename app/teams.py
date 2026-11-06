"""內部狀態通知（Microsoft Teams 頻道）。日報本身一律用正式 Mail 寄出，見 delivery.py。

做法：在 Teams 頻道建立 Workflow「收到 webhook 要求時張貼到頻道」（Post to a channel when a webhook request is
received），把產生的網址填到「系統設定 → 發送與通知 → 內部通知」。系統送出 Adaptive Card，頻道就會出現訊息。
不需要 Entra ID 應用程式，寄信設定出問題（例如用戶端密碼過期）時 Teams 通知照樣送得出去。

每種事件都有自己的開關與模版（標題、內容），模版用 {欄位} 代入。
"""

import json
import logging
import string
from datetime import datetime

from flask import current_app

from . import db, sysconf

log = logging.getLogger("dailyreport.teams")

EVENTS = {
    "reminder": {
        "label": "每日提醒：今天的日報到提醒時間還沒結案",
        "title": "【提醒】{date} 日報尚未結案（期限 {deadline}）",
        "text": "{date} 的日報目前狀態：**{status}**。\n\n請在 {deadline} 前完成審核與結案，系統會自動上傳 SharePoint 並寄出日報 Mail。",
        "default_on": True,
    },
    "delivery_failed": {
        "label": "發送中止：結案後的發送流程有一步失敗（立即通知）",
        "title": "【發送中止】{doc_no}（{date}）",
        "text": "「{step}」失敗：{error}\n\n收件人還沒收到這份日報。請修正後在日報頁最下方按「重試」。",
        "default_on": True,
    },
    "status_change": {
        "label": "日報狀態變更：任何日報（不限日期）每次狀態改變都通知",
        "title": "【{date} 日報】{old_status} → {status}",
        "text": "{actor} {action}。{comment}",
        "default_on": True,
    },
    "health": {
        "label": "排程推送：系統健康檢查（不是正常的項目逐條列出）",
        "title": "【系統健康檢查】{summary}",
        "text": "{abnormal}\n\n儲存空間：\n{storage}",
        "default_on": True,
    },
    "delivery_done": {
        "label": "發送完成：已上傳 SharePoint 並寄出日報 Mail",
        "title": "【已寄出】{doc_no}（{date}）",
        "text": "已寄出：給 {mail_to}\n\n檔案：{filename}\n\nMD5：{md5}",
        "default_on": False,
    },
}
FIELDS = {
    "date": "日報日期", "doc_no": "文件編號", "status": "目前狀態", "deadline": "期限", "step": "失敗的步驟",
    "error": "失敗原因", "filename": "正式 PDF 檔名", "md5": "MD5", "share_link": "SharePoint 連結",
    "mail_to": "收件人", "closed_by": "結案人", "time": "通知時間",
    "old_status": "原狀態（狀態變更）", "action": "動作（狀態變更）", "actor": "操作者（狀態變更）",
    "comment": "意見 / 原因（狀態變更）", "summary": "各等級數量（健康檢查）",
    "abnormal": "不是正常的項目清單（健康檢查）", "storage": "各磁碟用量 GB（健康檢查）",
}
SAMPLE = {"date": "2026-11-10", "doc_no": "CFXD-SCA-RLY-DPR-000903", "status": "待審核", "deadline": "13:00",
          "step": "寄送日報 Mail", "error": "HTTP 401：用戶端密碼已過期", "filename": "CFXD-SCA-RLY-DPR-000903_0_IFI.pdf",
          "md5": "0123456789abcdef0123456789abcdef", "share_link": "https://contoso.sharepoint.com/:b:/s/CFXD/xyz",
          "mail_to": "jamie@rallytech.com.tw", "closed_by": "Alan Lin", "time": "12:30",
          "old_status": "待審核", "action": "退回", "actor": "Alan Lin", "comment": "原因：4.3 截圖日期不對",
          "summary": "正常 30、注意 1、告警 1",
          "abnormal": "• [告警] 儲存空間 / 磁碟 /：已用 181.2 GB / 共 200.0 GB（90.6%），剩餘 18.8 GB\n"
                      "• [注意] 系統 / 時間同步：NTP 未同步",
          "storage": "• /：已用 181.2 GB / 共 200.0 GB（90.6%），剩餘 18.8 GB"}


class TeamsError(RuntimeError):
    pass


def key(event, part):
    return f"teams_{event}_{part}"  # teams_reminder_on / _title / _text


def defaults():
    out = {"teams_webhook_enc": "", "teams_mention": True, "teams_last_result": "",
           "teams_health_times": "08:30,17:30", "teams_health_only_abnormal": False, "teams_health_last": ""}
    for ev, d in EVENTS.items():
        out[key(ev, "on")] = d["default_on"]
        out[key(ev, "title")] = d["title"]
        out[key(ev, "text")] = d["text"]
    return out


# ---------------------------------------------------------------- 網址（等同密碼，加密保存）


def webhook():
    from .ai_providers import _fernet

    enc = sysconf.get("teams_webhook_enc") or ""
    if not enc:
        return ""
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(enc.encode()).decode()
    except InvalidToken:
        return None


def set_webhook(url, user=None):
    from .ai_providers import _fernet

    sysconf.put("teams_webhook_enc", _fernet().encrypt(url.encode()).decode() if url else "", user)


def masked(url):
    if not url:
        return ""
    from urllib.parse import urlsplit

    return f"https://{urlsplit(url).hostname}/…（已設定）"


# ---------------------------------------------------------------- 內容


class _Fmt(string.Formatter):
    def get_value(self, k, args, kwargs):
        return kwargs.get(k, "{" + str(k) + "}")


def render(event, values, title=None, text=None):
    title = title if title is not None else sysconf.get(key(event, "title"))
    text = text if text is not None else sysconf.get(key(event, "text"))
    v = {k: str(x) for k, x in values.items() if x is not None}
    return _Fmt().format(title or "", **v), _Fmt().format(text or "", **v)


def card(title, text, link=None, mentions=()):
    """Teams Workflows 接受的訊息：Adaptive Card；mentions = [(名稱, UPN / email)] 會在卡片最後 @ 這些人。"""
    body = [{"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True},
            {"type": "TextBlock", "text": text, "wrap": True}]
    entities = []
    if mentions:
        tags = []
        for name, upn in mentions:
            tag = f"<at>{name}</at>"
            tags.append(tag)
            entities.append({"type": "mention", "text": tag, "mentioned": {"id": upn, "name": name}})
        body.append({"type": "TextBlock", "text": " ".join(tags), "wrap": True})
    content = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
               "version": "1.4", "body": body, "msteams": {"width": "Full"}}
    if entities:
        content["msteams"]["entities"] = entities
    if link:
        content["actions"] = [{"type": "Action.OpenUrl", "title": "開啟日報", "url": link}]
    return {"type": "message",
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": content}]}


def mention_list():
    """要 @ 的二線管理員（AD 帳號的 email 通常就是 Teams 的登入名稱）。"""
    from .models import User

    return [(u.name, u.email) for u in User.query.filter_by(role="second_line").order_by(User.username) if u.email]


def report_link(report):
    base = (current_app.config.get("PUBLIC_URL") or "").rstrip("/")
    return f"{base}/reports/{report.id}" if base and report is not None else None


# ---------------------------------------------------------------- 傳送


def post(url, payload):
    from .graph import GraphError, _http

    try:
        _http("POST", url, payload, timeout=20)
    except GraphError as e:
        raise TeamsError(str(e)) from None


def notify(event, values, report=None, force=False):
    """依設定傳送一則內部通知。回傳 (成功, 說明)；未啟用回傳 (None, "")。失敗不丟例外（不影響主流程），結果記在設定頁。"""
    if not force and not sysconf.get(key(event, "on")):
        return None, ""
    url = webhook()
    now = datetime.now()
    values = {"time": f"{now:%H:%M}", **values}
    try:
        if not url:
            raise TeamsError("尚未設定 Teams Workflow 網址" if url == "" else "Teams 網址無法解密，請重新輸入")
        title, text = render(event, values)
        mentions = mention_list() if sysconf.get("teams_mention") and event == "reminder" else []
        post(url, card(title, text, report_link(report), mentions))
        ok, detail = True, f"已傳送「{title}」"
    except TeamsError as e:
        log.warning("Teams 通知失敗（%s）：%s", event, e)
        ok, detail = False, f"{EVENTS[event]['label'].split('：')[0]}傳送失敗：{e}"
    sysconf.put("teams_last_result", f"{now:%Y-%m-%d %H:%M} {detail}")
    db.session.commit()
    return ok, detail


# ---------------------------------------------------------------- 佇列（網頁上的動作不等 Teams 回應；維護服務送出）


def enqueue(event, values, report=None):
    """排入一則通知（未啟用就不排）。由維護背景服務在 10 秒內送出。"""
    from .models import TeamsMessage

    if not sysconf.get(key(event, "on")):
        return None
    m = TeamsMessage(event=event, values=json.dumps(values, ensure_ascii=False, default=str),
                     report_id=report.id if report is not None else None)
    db.session.add(m)
    return m


def flush(limit=20):
    """送出排隊中的通知；每則最多試 3 次。回傳送出（含失敗）的數量。"""
    from .models import Report, TeamsMessage

    n = 0
    for m in (TeamsMessage.query.filter(TeamsMessage.sent_at.is_(None), TeamsMessage.attempts < 3)
              .order_by(TeamsMessage.id).limit(limit).all()):
        m.attempts += 1
        report = db.session.get(Report, m.report_id) if m.report_id else None
        ok, detail = notify(m.event, json.loads(m.values or "{}"), report=report, force=True)
        m = db.session.get(TeamsMessage, m.id)
        m.result = detail[:500]
        if ok:
            m.sent_at = datetime.now()
        db.session.commit()
        n += 1
    return n


# ---------------------------------------------------------------- 日報狀態變更


ACTIONS = {"create": "建立", "submit": "送審", "approve": "核准", "reject": "退回", "return": "退回修正",
           "close": "結案（1 分鐘後寄出日報 Mail）", "close_cancel": "取消結案", "delete": "刪除",
           "retry": "重試發送"}


def status_changed(report, old_status, action, actor, comment="", new_status=None):
    """任何日報（不限日期）狀態改變時排入 Teams 通知。發送完成／中止另有通知事件。"""
    from .models import STATUS_LABELS

    new = new_status or ("deleted" if action == "delete" else report.status)
    labels = {**STATUS_LABELS, "new": "（新建）", "deleted": "已刪除",
              "delivery_error": "已結案（發送中止）", "delivery_retry": "已結案（重新發送）"}
    return enqueue("status_change", {
        "date": report.report_date.isoformat(), "doc_no": report.doc_no, "old_status": labels.get(old_status, old_status),
        "status": labels.get(new, new), "action": ACTIONS.get(action, action),
        "actor": getattr(actor, "name", "") or "", "comment": f"意見：{comment}" if comment else ""},
        report=None if action == "delete" else report)


# ---------------------------------------------------------------- 排程推送：系統健康檢查


def health_slots(now):
    out = []
    for t in str(sysconf.get("teams_health_times") or "").replace("，", ",").split(","):
        try:
            h, m = (int(x) for x in t.strip().split(":"))
            out.append(now.replace(hour=h, minute=m, second=0, microsecond=0))
        except ValueError:
            continue
    return sorted(out)


def health_due(now=None):
    """今天已到、還沒推送過的最近一個時段（錯過的時段只補推最近的一次）。"""
    now = now or datetime.now()
    if not sysconf.get(key("health", "on")):
        return None
    past = [s for s in health_slots(now) if s <= now]
    if not past:
        return None
    last = sysconf.get("teams_health_last") or ""
    return past[-1] if last < past[-1].isoformat() else None


def health_values(summary):
    from .doctor import GB, LEVEL_LABELS, LEVELS, abnormal_items, storage

    items = summary.get("items", [])
    bad = abnormal_items(items)
    counts = "、".join(f"{LEVEL_LABELS[k]} {summary.get(k, 0)}" for k in LEVELS if k == "ok" or summary.get(k))
    lines = [f"• [{LEVEL_LABELS.get(i['status'], i['status'])}] {i['group']} / {i['name']}：{i['detail']}" for i in bad]
    disks = [f"• {s['mount']}：已用 {s['used'] / GB:.1f} GB / 共 {s['total'] / GB:.1f} GB（{s['pct']:.1f}%），"
             f"剩餘 {s['free'] / GB:.1f} GB（{LEVEL_LABELS[s['level']]}）" for s in storage()]
    return {"summary": counts, "abnormal": "\n".join(lines) if lines else "全部正常。",
            "storage": "\n".join(disks), "date": datetime.now().date().isoformat()}, bool(bad)


def push_health(now=None):
    """執行健康檢查並推送到 Teams（設定「只在有異常時推送」時，全部正常就不推）。"""
    from .maint import run_health

    now = now or datetime.now()
    slot = health_due(now)
    sysconf.put("teams_health_last", (slot or now).isoformat())
    db.session.commit()
    summary = run_health(trigger="scheduled")
    values, abnormal = health_values(summary)
    if not abnormal and sysconf.get("teams_health_only_abnormal"):
        return None, "全部正常，未推送"
    return notify("health", values, force=True)

