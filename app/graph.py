"""Microsoft Graph（SharePoint 上傳、分享連結、寄信），用應用程式權限（client credentials）。

需要公司 M365 管理員在 Entra ID 註冊應用程式：
  - SharePoint：Sites.Selected（只授權指定的網站，建議）或 Sites.ReadWrite.All
  - 寄信：Mail.Send（建議以 Application Access Policy 限制只能用指定的寄件信箱）
設定在「系統設定 → 發送與通知」；用 urllib 直接呼叫，遵守主機的 https_proxy 設定。
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import current_app

from . import sysconf

CHUNK = 320 * 1024 * 10  # 上傳分段大小必須是 320 KiB 的倍數
_tokens = {}


class GraphError(RuntimeError):
    pass


def _base():
    return current_app.config.get("GRAPH_BASE", "https://graph.microsoft.com/v1.0")


def _login_base():
    return current_app.config.get("GRAPH_LOGIN_BASE", "https://login.microsoftonline.com")


# ---------------------------------------------------------------- 設定與金鑰


def config():
    keys = ("graph_tenant_id", "graph_client_id", "sp_site_url", "sp_library", "sp_folder", "sp_link_scope",
            "sp_link_expire_days", "mail_sender", "mail_to", "mail_cc", "mail_tracking_url", "mail_subject", "mail_body")
    c = sysconf.many(keys)
    c["client_secret"] = secret()
    return c


def secret():
    from .ai_providers import _fernet

    enc = sysconf.get("graph_client_secret_enc") or ""
    if not enc:
        return ""
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(enc.encode()).decode()
    except InvalidToken:
        return None  # SECRET_KEY 換過：請重新輸入


def set_secret(plain, user=None):
    from .ai_providers import _fernet

    sysconf.put("graph_client_secret_enc", _fernet().encrypt(plain.encode()).decode() if plain else "", user)


def missing(c=None, mail=True, sharepoint=True):
    """尚未填寫的必要設定（顯示名稱）。mail = 日報 Mail 的寄件信箱。"""
    c = c or config()
    need = [("graph_tenant_id", "租用戶 ID"), ("graph_client_id", "應用程式 ID")]
    if sharepoint:
        need += [("sp_site_url", "SharePoint 網站網址")]
    if mail:
        need += [("mail_sender", "日報 Mail 寄件信箱")]
    out = [label for k, label in need if not (c.get(k) or "").strip()]
    if not c.get("client_secret"):
        out.append("用戶端密碼" + ("（無法解密，請重新輸入）" if c.get("client_secret") is None else ""))
    return out


# ---------------------------------------------------------------- HTTP


def _http(method, url, body=None, headers=None, timeout=60):
    data = body
    headers = dict(headers or {})
    if isinstance(body, (dict, list)):
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw and "json" in (r.headers.get("Content-Type") or "") else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            err = json.loads(raw)
            msg = err.get("error_description") or (err.get("error") or {}).get("message") or raw.decode()[:300]
        except ValueError:
            msg = raw.decode(errors="replace")[:300]
        raise GraphError(f"HTTP {e.code}：{msg}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GraphError(f"無法連線 {urllib.parse.urlsplit(url).netloc}：{getattr(e, 'reason', e)}") from None


def token(c):
    key = (c["graph_tenant_id"], c["graph_client_id"], c["client_secret"])
    tok, exp = _tokens.get(key, (None, 0))
    if tok and exp > time.time() + 60:
        return tok
    form = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": c["graph_client_id"],
                                   "client_secret": c["client_secret"], "scope": "https://graph.microsoft.com/.default"})
    _, data = _http("POST", f"{_login_base()}/{urllib.parse.quote(c['graph_tenant_id'])}/oauth2/v2.0/token",
                    form.encode(), {"Content-Type": "application/x-www-form-urlencoded"})
    if "access_token" not in data:
        raise GraphError("取得權杖失敗：回應沒有 access_token")
    _tokens[key] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
    return data["access_token"]


def call(c, method, path, body=None, headers=None, timeout=60):
    url = path if path.startswith("http") else _base() + path
    h = {"Authorization": f"Bearer {token(c)}", **(headers or {})}
    return _http(method, url, body, h, timeout)[1]


# ---------------------------------------------------------------- SharePoint


def site_and_drive(c):
    """(site_id, drive_id, drive 名稱)。sp_site_url 例：https://contoso.sharepoint.com/sites/CFXD"""
    u = urllib.parse.urlsplit((c["sp_site_url"] or "").strip())
    if u.scheme != "https" or not u.hostname:
        raise GraphError("SharePoint 網站網址格式應為 https://公司.sharepoint.com/sites/網站名稱")
    path = u.path.rstrip("/")
    site = call(c, "GET", f"/sites/{u.hostname}:{urllib.parse.quote(path)}" if path else f"/sites/{u.hostname}")
    lib = (c.get("sp_library") or "").strip()
    if not lib:
        d = call(c, "GET", f"/sites/{site['id']}/drive")
        return site["id"], d["id"], d.get("name", "")
    drives = call(c, "GET", f"/sites/{site['id']}/drives").get("value", [])
    for d in drives:
        if lib.lower() in ((d.get("name") or "").lower(), urllib.parse.unquote(d.get("webUrl", "")).rstrip("/").split("/")[-1].lower()):
            return site["id"], d["id"], d.get("name", "")
    names = "、".join(d.get("name", "") for d in drives) or "（沒有）"
    raise GraphError(f"網站中找不到文件庫「{lib}」。可用的文件庫：{names}")


def folder_for(c, report):
    d = report.report_date
    tpl = (c.get("sp_folder") or "").strip().strip("/")
    return tpl.replace("{yyyy}", f"{d:%Y}").replace("{mm}", f"{d:%m}").replace("{dd}", f"{d:%d}")


def upload(c, local_path, folder, name):
    """分段上傳（大檔也可以）；資料夾不存在會自動建立；同名檔案以這份取代（重試時內容相同）。回傳 driveItem。"""
    import os

    _, drive_id, _ = site_and_drive(c)
    rel = "/".join(urllib.parse.quote(p) for p in [*filter(None, folder.split("/")), name])
    sess = call(c, "POST", f"/drives/{drive_id}/root:/{rel}:/createUploadSession",
                {"item": {"@microsoft.graph.conflictBehavior": "replace"}})
    url, size = sess["uploadUrl"], os.path.getsize(local_path)
    item = None
    with open(local_path, "rb") as fh:
        start = 0
        while start < size:
            chunk = fh.read(CHUNK)
            end = start + len(chunk) - 1
            # uploadUrl 已含授權，不能再帶 Authorization
            _, item = _http("PUT", url, chunk, {"Content-Length": str(len(chunk)),
                                                "Content-Range": f"bytes {start}-{end}/{size}"}, timeout=300)
            start = end + 1
    if not item or "id" not in item:
        raise GraphError("上傳完成但沒有收到檔案資訊")
    item["drive_id"] = drive_id
    return item


def share_link(c, drive_id, item_id, web_url):
    scope = c.get("sp_link_scope") or "organization"
    if scope == "none":
        return web_url  # 沿用文件庫權限，只給檔案網址
    body = {"type": "view", "scope": scope}
    days = int(c.get("sp_link_expire_days") or 0)
    if scope == "anonymous" and days > 0:
        from datetime import datetime, timedelta, timezone

        body["expirationDateTime"] = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = call(c, "POST", f"/drives/{drive_id}/items/{item_id}/createLink", body)
    url = (data.get("link") or {}).get("webUrl")
    if not url:
        raise GraphError("建立分享連結失敗：回應沒有連結")
    return url


# ---------------------------------------------------------------- 寄信


def addresses(text):
    import re

    return [a for a in re.split(r"[\s,;]+", text or "") if a]


def send_mail(c, to, subject, html, cc=()):
    if not to:
        raise GraphError("沒有收件人")
    msg = {"subject": subject, "body": {"contentType": "HTML", "content": html},
           "toRecipients": [{"emailAddress": {"address": a}} for a in to],
           "ccRecipients": [{"emailAddress": {"address": a}} for a in cc]}
    call(c, "POST", f"/users/{urllib.parse.quote(c['mail_sender'].strip())}/sendMail",
         {"message": msg, "saveToSentItems": True})
