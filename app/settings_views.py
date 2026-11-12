"""系統設定（只有系統管理員）。

這個檔案：AI 建議（服務、參數、提示詞、檢查結果.md 匯出位置、用量）。
admin_views.py：系統狀態與健康檢查、AD 驗證、本機緊急帳號、備份還原、PDF 與上傳、HTTPS 憑證。
帳號管理在 views.users（/admin/users），共用同一組分頁。
"""

import json
import os
from datetime import datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from . import ai_providers as prov
from . import approval as perm
from . import db
from .models import AIProvider, AIUsage, AuditLog

bp = Blueprint("settings", __name__, url_prefix="/admin/settings")

PROVIDER_TEXT = ("name", "base_url", "model", "deployment", "api_version", "region", "currency", "note", "extra_body")
PROVIDER_FLOAT = ("price_in", "price_out", "monthly_budget", "monthly_fee")


@bp.before_request
@login_required
def _require_admin():
    if not perm.can_manage_settings(current_user):
        abort(403)


def _audit(action, target, detail=""):
    db.session.add(AuditLog(action=action, actor_id=current_user.id, target=target[:128], detail=detail[:500]))


TYPE_LABELS = {"X": "核心架構", "Y": "功能", "Z": "修正", "—": "初版"}


def changelog():
    """讀程式根目錄的 CHANGELOG.md 版本表：[{version, date, commit, type, text}]（新到舊）。"""
    import re

    from .backup import app_dir

    rows = []
    try:
        with open(os.path.join(app_dir(), "CHANGELOG.md"), encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"^\|\s*(v\d+\.\d+\.\d+)\s*\|\s*([^|]*)\|\s*([^|]*)\|\s*([^|]*)\|\s*(.*)\|\s*$", line)
                if m:
                    v, d, c, t, text = (x.strip() for x in m.groups())
                    rows.append({"version": v, "date": d, "commit": c.strip("`"), "type": t, "text": text})
    except OSError:
        pass
    return rows


@bp.route("/versions")
def versions():
    """版本紀錄：全部版號（vX.Y.Z）、日期、commit、類型與內容；標出目前執行的版本。"""
    from .backup import semver, version

    rows = changelog()
    current = f"v{semver()}" if semver() else ""
    counts = {k: sum(1 for r in rows if r["type"] == k) for k in ("X", "Y", "Z")}
    return render_template("admin_versions.html", tab="versions", rows=rows, current=current,
                           commit=(version() or "")[:7], counts=counts, type_labels=TYPE_LABELS)


@bp.route("/ai", methods=["GET"])
def ai():
    providers = AIProvider.query.order_by(AIProvider.priority, AIProvider.id).all()
    s = prov.settings()
    rows = []
    for p in providers:
        u = prov.period_usage(p)
        rows.append((p, u, prov.blocked_reason(p, s["ai_allow_external"], u)))
    recent = AIUsage.query.order_by(AIUsage.id.desc()).limit(30).all()
    return render_template("settings.html", tab="ai", s=s, rows=rows, recent=recent, presets=prov.PRESETS,
                           groups=prov.GROUP_LABELS, billing=prov.BILLING, kinds=prov.KINDS,
                           default_prompt=prov.DEFAULT_SYSTEM_PROMPT, usable=sum(1 for *_, b in rows if not b))


def _int(name, lo, hi, default):
    try:
        return max(lo, min(hi, int(request.form.get(name, default))))
    except (TypeError, ValueError):
        return default


@bp.route("/general", methods=["POST"])
def general():
    f = request.form
    old = prov.settings()
    new = {
        "ai_enabled": f.get("ai_enabled") == "1",
        "ai_auto_on_submit": f.get("ai_auto_on_submit") == "1",
        "ai_allow_external": f.get("ai_allow_external") == "1",
        "ai_context_reports": _int("ai_context_reports", 0, 10, 7),
        "ai_max_chars": _int("ai_max_chars", 2000, 400000, 24000),
        "ai_timeout": _int("ai_timeout", 10, 900, 180),
        "ai_max_output_tokens": _int("ai_max_output_tokens", 256, 128000, 16000),
    }
    try:
        new["ai_temperature"] = max(0.0, min(2.0, float(f.get("ai_temperature", 0.2))))
    except ValueError:
        new["ai_temperature"] = 0.2
    md_dir = f.get("check_md_dir", "").strip()
    if md_dir and not os.path.isabs(md_dir):
        flash("檢查結果.md 匯出位置必須是絕對路徑。", "error")
        return redirect(url_for("settings.ai"))
    new["check_md_dir"] = md_dir  # 空字串 = 只存在資料庫，不匯出檔案
    if new["ai_allow_external"] and not old["ai_allow_external"] and f.get("external_ack") != "1":
        flash("允許外部 AI 服務前，請勾選確認資料會送到公司外部。", "error")
        return redirect(url_for("settings.ai"))
    for k, v in new.items():
        prov.set_setting(k, v, current_user)
    changed = [k for k in new if new[k] != old.get(k)]
    if changed:
        _audit("ai_settings", "一般設定", "、".join(f"{k}={new[k]}" for k in changed))
    db.session.commit()
    flash("已儲存設定。", "success")
    return redirect(url_for("settings.ai"))


@bp.route("/prompt", methods=["POST"])
def prompt():
    text = request.form.get("ai_system_prompt", "").strip()
    if request.form.get("reset") == "1" or not text:
        text = prov.DEFAULT_SYSTEM_PROMPT
    prov.set_setting("ai_system_prompt", text, current_user)
    _audit("ai_settings", "提示詞", f"{len(text)} 字")
    db.session.commit()
    flash("已儲存提示詞。", "success")
    return redirect(url_for("settings.ai") + "#prompt")


@bp.route("/providers/new", methods=["GET", "POST"])
@bp.route("/providers/<int:pid>", methods=["GET", "POST"])
def provider(pid=None):
    p = db.session.get(AIProvider, pid) if pid else None
    if pid and p is None:
        abort(404)
    if p is None:
        key = request.values.get("preset", "custom_local")
        if key not in prov.PRESETS:
            abort(400)
        d = prov.PRESETS[key]
        p = AIProvider(preset=key, kind=d["kind"], name=d["label"], base_url=d.get("base_url", ""),
                       model=d.get("model", ""), api_version=d.get("api_version", ""), region=d.get("region", ""),
                       billing=d["group"], external=d.get("external", True),
                       send_temperature=d.get("send_temperature", True),
                       token_param=d.get("token_param", "max_tokens"), price_in=d.get("price_in", 0.0),
                       price_out=d.get("price_out", 0.0), currency="USD", reset_day=1, enabled=True,
                       priority=(db.session.query(db.func.max(AIProvider.priority)).scalar() or 0) + 10)
    if request.method == "POST":
        return _save_provider(p)
    return render_template("settings_provider.html", p=p, preset=prov.PRESETS.get(p.preset, {}), kinds=prov.KINDS,
                           billing=prov.BILLING, token_params=prov.TOKEN_PARAMS,
                           key_broken=p.id is not None and prov.decrypt_key(p) is None)


def _save_provider(p):
    f = request.form
    for k in PROVIDER_TEXT:
        setattr(p, k, f.get(k, "").strip())
    for k in PROVIDER_FLOAT:
        try:
            setattr(p, k, max(0.0, float(f.get(k) or 0)))
        except ValueError:
            flash(f"{k} 必須是數字。", "error")
            return redirect(request.url)
    p.monthly_token_quota = _int("monthly_token_quota", 0, 10**12, 0)
    p.reset_day = _int("reset_day", 1, 28, 1)
    p.kind = f.get("kind") if f.get("kind") in prov.KINDS else p.kind
    p.billing = f.get("billing") if f.get("billing") in prov.BILLING else p.billing
    p.token_param = f.get("token_param") if f.get("token_param") in prov.TOKEN_PARAMS else "max_tokens"
    p.enabled = f.get("enabled") == "1"
    p.external = f.get("external") == "1"
    p.send_temperature = f.get("send_temperature") == "1"
    p.currency = (p.currency or "USD").upper()[:8]
    if not p.name:
        flash("請填寫名稱。", "error")
        return redirect(request.url)
    if p.extra_body:
        try:
            if not isinstance(json.loads(p.extra_body), dict):
                raise ValueError
        except ValueError:
            flash("額外參數必須是 JSON 物件，例如 {\"top_p\": 0.9}。", "error")
            return redirect(request.url)
    need = {"openai": ("base_url", "model"), "azure_openai": ("base_url", "deployment"), "anthropic": ("model",),
            "gemini": ("model",), "bedrock": ("region", "model")}[p.kind]
    missing = [k for k in need if not getattr(p, k)]
    if missing:
        flash("請填寫：" + "、".join(missing), "error")
        return redirect(request.url)
    new_key = f.get("api_key", "")
    if f.get("clear_key") == "1":
        p.api_key_enc, p.api_key_hint = None, ""
    elif new_key.strip():
        p.api_key_enc = prov.encrypt_key(new_key.strip())
        p.api_key_hint = new_key.strip()[-4:]
    is_new = p.id is None
    db.session.add(p)
    db.session.flush()
    _audit("ai_provider_add" if is_new else "ai_provider_edit", p.name,
           f"{prov.KINDS[p.kind]}・{prov.BILLING[p.billing]}・{p.model or p.deployment}"
           + ("・更新金鑰" if new_key.strip() else "") + ("・清除金鑰" if f.get("clear_key") == "1" else ""))
    db.session.commit()
    flash(f"已儲存 {p.name}。", "success")
    return redirect(url_for("settings.ai") + "#providers")


@bp.route("/providers/<int:pid>/delete", methods=["POST"])
def delete_provider(pid):
    p = db.session.get(AIProvider, pid) or abort(404)
    AIUsage.query.filter_by(provider_id=p.id).update({"provider_id": None}, synchronize_session=False)
    _audit("ai_provider_delete", p.name)
    db.session.delete(p)
    db.session.commit()
    flash(f"已刪除 {p.name}（用量紀錄保留）。", "success")
    return redirect(url_for("settings.ai") + "#providers")


@bp.route("/providers/<int:pid>/move", methods=["POST"])
def move_provider(pid):
    rows = AIProvider.query.order_by(AIProvider.priority, AIProvider.id).all()
    idx = next((i for i, p in enumerate(rows) if p.id == pid), None)
    if idx is None:
        abort(404)
    j = idx - 1 if request.form.get("dir") == "up" else idx + 1
    if 0 <= j < len(rows):
        rows[idx], rows[j] = rows[j], rows[idx]
    for n, p in enumerate(rows, 1):
        p.priority = n * 10
    db.session.commit()
    return redirect(url_for("settings.ai") + "#providers")


@bp.route("/providers/<int:pid>/test", methods=["POST"])
def test_provider(pid):
    """連線測試：送一個很短的請求（會計入用量）。外部服務仍需先允許送出。"""
    p = db.session.get(AIProvider, pid) or abort(404)
    if p.external and not prov.get_setting("ai_allow_external"):
        flash("這是外部服務，請先在一般設定允許將資料送到公司外部，再測試。", "error")
        return redirect(url_for("settings.ai") + "#providers")
    s = prov.settings()
    opts = {"timeout": min(60, s["ai_timeout"]), "max_output_tokens": min(1024, s["ai_max_output_tokens"]),
            "temperature": s["ai_temperature"]}
    try:
        r = prov.call(p, "你是連線測試。", "請只回覆「OK」。", opts=opts, purpose="test")
        p.last_test_ok, p.last_test_msg = True, f"{r.model}：{r.text[:80]}（{r.input_tokens}+{r.output_tokens} token）"
        flash(f"{p.name} 連線成功：{p.last_test_msg}", "success")
    except RuntimeError as e:
        p.last_test_ok, p.last_test_msg = False, str(e)[:500]
        flash(f"{p.name} 連線失敗：{e}", "error")
    p.last_test_at = datetime.now()
    db.session.commit()
    return redirect(url_for("settings.ai") + "#providers")


from . import admin_views  # noqa: E402,F401  在同一個 blueprint 註冊其他分頁
