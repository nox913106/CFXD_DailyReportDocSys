"""系統設定（AD 驗證、上傳、備份、健康檢查）：存在資料庫 AppSetting，前端「系統設定」頁維護。

舊版安裝把 AD 設定寫在 .env：資料庫還沒有值時沿用 .env，管理員在設定頁儲存一次後就以資料庫為準。
每次讀取都查資料庫，所以網頁、OCR、AI、維護背景服務都不需要重啟就會套用新設定。

只有基礎設施（SECRET_KEY、資料庫位置、AUTH_MODE、反向代理 / Cookie）留在 .env，由安裝腳本管理。
"""

import json
import os
import re

from flask import current_app

from . import db

DEFAULT_BACKUP_DIR = "/var/backups/dailyreport"
UPLOAD_CEILING_MB = 200  # nginx client_max_body_size；設定頁的上傳上限不能超過

DEFAULTS = {
    # AD / LDAP
    "ldap_url": "",
    "ad_domain": "",
    "ldap_base_dn": "",
    "ldap_required_group": "",  # 群組 DN，可多個（一行一個；.env / 指令可用 ; 分隔）
    "ldap_ca_pem": "",  # LDAPS 驗證用的公司 CA 憑證（PEM）；空白 = 系統預設 CA
    "ldap_timeout": 5,
    # 上傳
    "max_upload_mb": 20,
    # 備份
    "backup_enabled": True,
    "backup_time": "02:30",
    "backup_keep": 14,
    "backup_dir": "",  # 空白 = DEFAULT_BACKUP_DIR
    "backup_include_env": True,
    # 健康檢查
    "health_enabled": True,
    "health_interval_hours": 6,
    # 結案後自動發送（Microsoft Graph）
    "delivery_enabled": False,  # 關閉時結案只產生正式 PDF，上傳與寄信標為「未啟用」
    "graph_tenant_id": "",
    "graph_client_id": "",
    "graph_client_secret_enc": "",
    "sp_site_url": "",  # https://公司.sharepoint.com/sites/網站
    "sp_library": "",  # 文件庫名稱；空白 = 網站預設文件庫
    "sp_folder": "CFXD Daily Report/{yyyy}/{mm}",
    "sp_link_scope": "organization",  # organization / anonymous / none（只給檔案網址）
    "sp_link_expire_days": 0,
    "mail_sender": "",
    "mail_to": "",
    "mail_cc": "",
    "mail_subject": "{doc_no} CFXD SLA Daily Work Report {date}",
    "mail_body": ("Dear all,<br><br>Please find the CFXD SLA Daily Work Report for {date} "
                  "({doc_no}, Rev. {revision}) at the link below:<br><a href=\"{link}\">{filename}</a><br><br>"
                  "MD5: {md5}<br><br>This message was sent automatically."),
    # 每日提醒：期限前還沒完成發送，寄信給二線人員
    "reminder_enabled": True,
    "reminder_time": "12:30",
    "deadline_time": "13:00",
    "reminder_extra": "",
    # 內部狀態（不在表單上）
    "reminder_last": "",
    "reminder_result": "",
    "backup_last_scheduled": "",
    "health_last": None,
    "health_refresh": "",  # 設定變更後要求維護服務盡快重新檢查（時間）
}

# 資料庫沒有值時沿用 .env（舊版安裝）
ENV_FALLBACK = {
    "ldap_url": "LDAP_URL",
    "ad_domain": "AD_DOMAIN",
    "ldap_base_dn": "LDAP_BASE_DN",
    "ldap_required_group": "LDAP_REQUIRED_GROUP",
}

LDAP_KEYS = ("ldap_url", "ad_domain", "ldap_base_dn", "ldap_required_group", "ldap_ca_pem", "ldap_timeout")


def _row(key):
    from .models import AppSetting

    return db.session.get(AppSetting, key)


def get(key):
    row = _row(key)
    if row is not None and row.value is not None:
        return json.loads(row.value)
    env = ENV_FALLBACK.get(key)
    if env:
        v = current_app.config.get(env)
        if isinstance(v, (set, list, tuple)):
            v = ",".join(sorted(v))
        if v:
            return v
    if key == "ldap_ca_pem":
        path = current_app.config.get("LDAP_CA_CERT")
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="ascii", errors="ignore") as fh:
                    return fh.read()
            except OSError:
                pass
    if key == "max_upload_mb":
        return int(current_app.config.get("MAX_CONTENT_LENGTH", 20 * 1024 * 1024) // (1024 * 1024))
    return DEFAULTS.get(key)


def source(key):
    """'db'：已在設定頁儲存；'env'：沿用 .env；'default'：預設值。"""
    row = _row(key)
    if row is not None and row.value is not None:
        return "db"
    env = ENV_FALLBACK.get(key)
    if env and current_app.config.get(env):
        return "env"
    return "default"


def put(key, value, user=None):
    from .models import AppSetting

    row = _row(key) or AppSetting(key=key)
    row.value = json.dumps(value, ensure_ascii=False)
    row.updated_by_id = user.id if user else None
    db.session.add(row)


def many(keys):
    return {k: get(k) for k in keys}


def ldap_config():
    c = many(LDAP_KEYS)
    c["ldap_urls"] = url_list(c["ldap_url"])
    c["required_groups"] = group_list(c["ldap_required_group"])
    return c


def url_list(value):
    """AD 伺服器清單：一行一台（也接受空白、逗號、; 分隔），依序嘗試；去除重複。"""
    out = []
    for u in re.split(r"[\s,;]+", value or ""):
        if u and u.lower() not in {x.lower() for x in out}:
            out.append(u)
    return out


def group_list(value):
    """群組 DN 清單：一行一個，也接受 ; 分隔（DN 本身用逗號，所以不能用逗號分隔）；去除空白與重複。"""
    out = []
    for part in re.split(r"(?<!\\);|\r?\n", value or ""):
        dn = part.strip()
        if dn and dn.lower() not in {d.lower() for d in out}:
            out.append(dn)
    return out


def ldap_configured(c=None):
    c = c or ldap_config()
    return bool(c["ldap_url"] and c["ad_domain"] and c["ldap_base_dn"]
                and "example" not in c["ldap_url"] and "example" not in c["ad_domain"])


def backup_dir():
    return get("backup_dir") or current_app.config.get("BACKUP_DIR") or DEFAULT_BACKUP_DIR


def max_upload_bytes():
    try:
        return max(1, int(get("max_upload_mb"))) * 1024 * 1024
    except (TypeError, ValueError):
        return 20 * 1024 * 1024
