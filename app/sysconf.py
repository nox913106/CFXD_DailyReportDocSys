"""系統設定（AD 驗證、上傳、備份、健康檢查）：存在資料庫 AppSetting，前端「系統設定」頁維護。

舊版安裝把 AD 設定寫在 .env：資料庫還沒有值時沿用 .env，管理員在設定頁儲存一次後就以資料庫為準。
每次讀取都查資料庫，所以網頁、OCR、AI、維護背景服務都不需要重啟就會套用新設定。

只有基礎設施（SECRET_KEY、資料庫位置、AUTH_MODE、反向代理 / Cookie）留在 .env，由安裝腳本管理。
"""

import json
import os

from flask import current_app

from . import db

DEFAULT_BACKUP_DIR = "/var/backups/dailyreport"
UPLOAD_CEILING_MB = 200  # nginx client_max_body_size；設定頁的上傳上限不能超過

DEFAULTS = {
    # AD / LDAP
    "ldap_url": "",
    "ad_domain": "",
    "ldap_base_dn": "",
    "ldap_required_group": "",
    "ldap_approver_group": "",
    "approver_users": "",  # 首次登入就設為檔案管理員的帳號（逗號分隔）
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
    # 內部狀態（不在表單上）
    "backup_last_scheduled": "",
    "health_last": None,
}

# 資料庫沒有值時沿用 .env（舊版安裝）
ENV_FALLBACK = {
    "ldap_url": "LDAP_URL",
    "ad_domain": "AD_DOMAIN",
    "ldap_base_dn": "LDAP_BASE_DN",
    "ldap_required_group": "LDAP_REQUIRED_GROUP",
    "ldap_approver_group": "LDAP_APPROVER_GROUP",
    "approver_users": "APPROVER_USERS",
}

LDAP_KEYS = ("ldap_url", "ad_domain", "ldap_base_dn", "ldap_required_group", "ldap_approver_group",
             "approver_users", "ldap_ca_pem", "ldap_timeout")


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
    c["approver_users_set"] = {v.strip().lower() for v in (c["approver_users"] or "").split(",") if v.strip()}
    return c


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
