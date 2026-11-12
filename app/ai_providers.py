"""AI 服務：預設範本、系統設定、金鑰加密、各家 API 的呼叫方式、計費與額度。

所有設定都在前端「系統設定」頁（只有系統管理員看得到），存在資料庫，背景服務每次產生前重新讀取，不需要重啟。

呼叫協定（kind）只有五種，涵蓋所有接入方式：
  openai        OpenAI 相容 Chat Completions：OpenAI、Mistral、DeepSeek、xAI、OpenRouter、Groq、Together、
                公司內部 AI 閘道（LiteLLM 等），以及地端的 Ollama、LM Studio、vLLM、llama.cpp、LocalAI
  azure_openai  Azure OpenAI（按量或 PTU 包月）
  anthropic     Anthropic Claude API（官方 SDK）
  gemini        Google Gemini API
  bedrock       AWS Bedrock Converse API（Bedrock API 金鑰；按量或 Provisioned Throughput 包月）

計費方式（billing）：
  token        按量計費：依 input / output token 單價計算費用，可設每期預算上限，用完就跳過、改用下一個服務
  monthly      包月：固定月費，可設每期 token 額度，用完就跳過
  self_hosted  自建主機：沒有按量費用，可填每月攤提成本供比較
"""

import base64
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime

from flask import current_app

from . import db

KINDS = {
    "openai": "OpenAI 相容 API",
    "azure_openai": "Azure OpenAI",
    "anthropic": "Anthropic Claude API",
    "gemini": "Google Gemini API",
    "bedrock": "AWS Bedrock",
}
BILLING = {"token": "按量計費（token）", "monthly": "包月（固定月費）", "self_hosted": "自建主機（地端）"}
TOKEN_PARAMS = {"max_tokens": "max_tokens", "max_completion_tokens": "max_completion_tokens", "none": "不傳送"}

# 預設範本：新增服務時帶入的預設值。價格會變動，除了 Claude 以外都留給管理員依合約填寫。
P = dict
PRESETS = {
    # ---- 按量計費（token）
    "openai": P(group="token", label="OpenAI API", kind="openai", base_url="https://api.openai.com/v1",
                token_param="max_completion_tokens", hint="模型名稱依 OpenAI 文件填寫"),
    "anthropic": P(group="token", label="Anthropic Claude API", kind="anthropic", base_url="",
                   model="claude-opus-5-5", price_in=4.0, price_out=20.0, send_temperature=False,
                   hint="預設 Claude Opus 5.5（每 100 萬 token：input US$4、output US$20）；也可填 claude-sonnet-5-5、claude-haiku-5-5"),
    "gemini": P(group="token", label="Google Gemini API", kind="gemini",
                base_url="https://generativelanguage.googleapis.com", hint="模型名稱依 Google AI Studio 文件填寫"),
    "azure_openai": P(group="token", label="Azure OpenAI（按量）", kind="azure_openai",
                      base_url="https://<資源名稱>.openai.azure.com", api_version="2024-10-21",
                      token_param="max_completion_tokens", hint="填部署名稱（deployment），模型名稱可留空"),
    "bedrock": P(group="token", label="AWS Bedrock（按量）", kind="bedrock", region="ap-northeast-1",
                 hint="填 Bedrock 模型 ID 或推論設定檔 ID；金鑰使用 Bedrock API 金鑰"),
    "mistral": P(group="token", label="Mistral AI", kind="openai", base_url="https://api.mistral.ai/v1"),
    "deepseek": P(group="token", label="DeepSeek", kind="openai", base_url="https://api.deepseek.com/v1"),
    "xai": P(group="token", label="xAI Grok", kind="openai", base_url="https://api.x.ai/v1"),
    "openrouter": P(group="token", label="OpenRouter（多家模型）", kind="openai", base_url="https://openrouter.ai/api/v1"),
    "groq": P(group="token", label="Groq", kind="openai", base_url="https://api.groq.com/openai/v1"),
    "together": P(group="token", label="Together AI", kind="openai", base_url="https://api.together.xyz/v1"),
    "custom_token": P(group="token", label="其他 OpenAI 相容服務（按量）", kind="openai", base_url=""),
    # ---- 包月（固定月費）
    "azure_ptu": P(group="monthly", label="Azure OpenAI PTU（預留容量）", kind="azure_openai",
                   base_url="https://<資源名稱>.openai.azure.com", api_version="2024-10-21",
                   token_param="max_completion_tokens", hint="Provisioned Throughput：按月計費，填月費與部署名稱"),
    "bedrock_pt": P(group="monthly", label="AWS Bedrock Provisioned Throughput", kind="bedrock",
                    region="ap-northeast-1", hint="模型 ID 填 Provisioned Throughput 的 ARN"),
    "gateway": P(group="monthly", label="公司 AI 閘道 / 包月方案（OpenAI 相容）", kind="openai", base_url="",
                 external=False, hint="例如 LiteLLM、企業 AI 平台；依合約填月費與每月 token 額度"),
    "anthropic_commit": P(group="monthly", label="Anthropic Claude（企業包月 / 預付）", kind="anthropic",
                          model="claude-opus-5-5", send_temperature=False,
                          hint="預付或承諾用量合約：填月費與額度；呼叫方式與 Claude API 相同"),
    # ---- 自建主機（地端，資料不離開公司）
    "ollama": P(group="self_hosted", label="Ollama", kind="openai", base_url="http://ai-host:11434/v1",
                external=False, hint="例如 qwen3:32b、llama3.3:70b"),
    "lmstudio": P(group="self_hosted", label="LM Studio", kind="openai", base_url="http://ai-host:1234/v1", external=False),
    "vllm": P(group="self_hosted", label="vLLM", kind="openai", base_url="http://ai-host:8000/v1", external=False),
    "llamacpp": P(group="self_hosted", label="llama.cpp server", kind="openai", base_url="http://ai-host:8080/v1",
                  external=False),
    "localai": P(group="self_hosted", label="LocalAI", kind="openai", base_url="http://ai-host:8080/v1", external=False),
    "custom_local": P(group="self_hosted", label="其他地端 OpenAI 相容服務", kind="openai", base_url="", external=False),
}
GROUP_LABELS = {"token": "按量計費（token）", "monthly": "包月（固定月費 / 預留容量）", "self_hosted": "自建主機（地端）"}

DEFAULT_SYSTEM_PROMPT = """你是 CFXD SLA 每日維運日報的審核助理，協助審核者決定核准或退回。
規則：
1. 只能根據使用者提供的「檢查結果.md」內容回答，不可以推測或編造檢查結果裡沒有的事實、設備或數字。
2. 每一點都要標出依據的檢查步驟編號（例如「一-2」「二-6」）和對象（設備或欄位）。
3. 錯誤代表確定有誤、應修正後再送審；警告需要人判斷；提醒僅供參考。不要把提醒說成錯誤。
4. 若提供了「參考：前幾份正式日報的檢查結果」，依其中的規則判斷每個異常連續幾天（全部都出現就寫「異常持續 >N 天」）；
   若提供了「本日報先前被退回時的檢查結果」，指出退回的問題這次是否已修正。引用時寫明是哪一份（文件編號）。
5. 用繁體中文，簡潔、條列，不要重複貼整張表格。

輸出格式（Markdown）：
## 建議
一句話：「建議退回修正」、「確認警告後可核准」或「可核准」，並說明主要理由。
## 需要處理
- [步驟編號] 對象：問題與建議的處理方式（沒有就寫「無」）
## 提醒
- 值得審核者注意但不影響核准的事項，例如持續很久或反覆發生的告警（沒有就寫「無」）
"""

DEFAULTS = {
    "ai_enabled": False,
    "ai_auto_on_submit": True,
    "ai_allow_external": False,  # 允許把檢查結果送到公司外部的 AI 服務
    "ai_context_reports": 7,  # 參考前 N 份正式日報判斷異常連續性；全部都出現就回答「異常持續 >N 天」
    "ai_max_chars": 24000,
    "ai_timeout": 180,
    "ai_max_output_tokens": 16000,
    "ai_temperature": 0.2,
    "ai_system_prompt": DEFAULT_SYSTEM_PROMPT,
    "check_md_dir": None,  # None = 使用 instance/check_reports
}


# ---------------------------------------------------------------- 系統設定


def get_setting(key):
    from .models import AppSetting

    row = db.session.get(AppSetting, key)
    if row is None or row.value is None:
        if key == "check_md_dir":
            return current_app.config.get("CHECK_MD_DIR")
        return DEFAULTS.get(key)
    return json.loads(row.value)


def set_setting(key, value, user=None):
    from .models import AppSetting

    row = db.session.get(AppSetting, key) or AppSetting(key=key)
    row.value = json.dumps(value, ensure_ascii=False)
    row.updated_by_id = user.id if user else None
    db.session.add(row)


def settings():
    return {k: get_setting(k) for k in DEFAULTS}


# ---------------------------------------------------------------- 金鑰加密


def _fernet():
    from cryptography.fernet import Fernet

    key = hashlib.sha256(("cfxd-ai-key:" + current_app.config["SECRET_KEY"]).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_key(plain):
    return _fernet().encrypt(plain.encode()).decode() if plain else None


def decrypt_key(provider):
    """解不開（例如 SECRET_KEY 換過）時回傳 None，畫面會提示重新輸入。"""
    if not provider.api_key_enc:
        return ""
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(provider.api_key_enc.encode()).decode()
    except InvalidToken:
        return None


# ---------------------------------------------------------------- 計費與額度


def period_start(provider, today=None):
    """本期起算日：每月 reset_day 號。"""
    today = today or date.today()
    day = min(max(provider.reset_day or 1, 1), 28)
    if today.day >= day:
        return datetime(today.year, today.month, day)
    y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return datetime(y, m, day)


def period_usage(provider):
    from sqlalchemy import func

    from .models import AIUsage

    row = (db.session.query(func.count(AIUsage.id), func.coalesce(func.sum(AIUsage.input_tokens), 0),
                            func.coalesce(func.sum(AIUsage.output_tokens), 0), func.coalesce(func.sum(AIUsage.cost), 0.0),
                            func.coalesce(func.sum(db.case((AIUsage.ok, 1), else_=0)), 0))
           .filter(AIUsage.provider_id == provider.id, AIUsage.created_at >= period_start(provider)).one())
    calls, tin, tout, cost, ok = row
    return {"calls": calls, "ok": ok, "input": tin, "output": tout, "tokens": tin + tout, "cost": float(cost)}


def cost_of(provider, tin, tout):
    if provider.billing != "token":
        return 0.0
    return round((tin * (provider.price_in or 0) + tout * (provider.price_out or 0)) / 1_000_000, 6)


def blocked_reason(provider, allow_external, usage=None):
    """這個服務現在不能用的原因；可以用則回傳空字串。"""
    if not provider.enabled:
        return "已停用"
    if provider.external and not allow_external:
        return "外部服務，尚未允許將檢查結果送到公司外部"
    if decrypt_key(provider) is None:
        return "金鑰無法解密（SECRET_KEY 可能已變更），請重新輸入"
    usage = usage or period_usage(provider)
    if provider.billing == "token" and provider.monthly_budget and usage["cost"] >= provider.monthly_budget:
        return f"本期費用 {usage['cost']:.2f} 已達預算上限 {provider.monthly_budget:g} {provider.currency}"
    if provider.billing == "monthly" and provider.monthly_token_quota and usage["tokens"] >= provider.monthly_token_quota:
        return f"本期已用 {usage['tokens']:,} token，達到額度 {provider.monthly_token_quota:,}"
    return ""


def available_providers():
    from .models import AIProvider

    allow = get_setting("ai_allow_external")
    return [p for p in AIProvider.query.order_by(AIProvider.priority, AIProvider.id) if not blocked_reason(p, allow)]


def ai_enabled():
    """AI 建議是否啟用：總開關打開，而且至少有一個可用的服務。"""
    return bool(get_setting("ai_enabled")) and bool(available_providers())


# ---------------------------------------------------------------- 呼叫


@dataclass
class Reply:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


def _post(url, body, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}：{e.read()[:300].decode('utf-8', 'replace')}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"無法連線：{e.reason}") from e


def _extra(p):
    if not (p.extra_body or "").strip():
        return {}
    try:
        v = json.loads(p.extra_body)
    except ValueError as e:
        raise RuntimeError(f"額外參數不是有效的 JSON：{e}") from e
    if not isinstance(v, dict):
        raise RuntimeError("額外參數必須是 JSON 物件")
    return v


def _openai_body(p, system, user, opts):
    body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "stream": False}
    if p.send_temperature:
        body["temperature"] = opts["temperature"]
    if p.token_param in ("max_tokens", "max_completion_tokens"):
        body[p.token_param] = opts["max_output_tokens"]
    return body


def _openai_reply(data, fallback_model):
    choice = (data.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    u = data.get("usage") or {}
    return Reply(text, data.get("model") or fallback_model, u.get("prompt_tokens", 0), u.get("completion_tokens", 0))


def call_openai(p, key, system, user, opts):
    body = {"model": p.model, **_openai_body(p, system, user, opts), **_extra(p)}
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    data = _post(p.base_url.rstrip("/") + "/chat/completions", body, headers, opts["timeout"])
    return _openai_reply(data, p.model)


def call_azure(p, key, system, user, opts):
    url = (f"{p.base_url.rstrip('/')}/openai/deployments/{urllib.parse.quote(p.deployment)}/chat/completions"
           f"?api-version={urllib.parse.quote(p.api_version or '2024-10-21')}")
    body = {**_openai_body(p, system, user, opts), **_extra(p)}
    data = _post(url, body, {"api-key": key}, opts["timeout"])
    return _openai_reply(data, p.model or p.deployment)


# 這些 Claude 模型在 Claude API 上支援伺服器端 fallback（被安全機制拒絕時自動改用其他模型）
CLAUDE_FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")


def call_anthropic(p, key, system, user, opts):
    import anthropic

    client = anthropic.Anthropic(api_key=key or None, base_url=p.base_url or None, timeout=opts["timeout"],
                                 max_retries=1)
    kwargs = dict(model=p.model, max_tokens=opts["max_output_tokens"], system=system,
                  messages=[{"role": "user", "content": user}], **_extra(p))
    if p.send_temperature:  # 目前的 Claude 模型不接受 temperature，預設不傳
        kwargs["temperature"] = opts["temperature"]
    try:
        if p.model in CLAUDE_FALLBACK_MODELS and not p.base_url:
            resp = client.beta.messages.create(**kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        else:
            resp = client.messages.create(**kwargs)
    except anthropic.AuthenticationError as e:
        raise RuntimeError("Claude API 金鑰無效") from e
    except anthropic.RateLimitError as e:
        raise RuntimeError("Claude API 超過速率限制，請稍後再試") from e
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Claude API HTTP {e.status_code}：{e.message}") from e
    except anthropic.APIConnectionError as e:
        raise RuntimeError(f"無法連線到 Claude API：{e}") from e
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude 拒絕回覆這次的內容")
    text = "".join(b.text for b in resp.content if b.type == "text")
    u = resp.usage
    tin = (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) + \
        (getattr(u, "cache_creation_input_tokens", 0) or 0)
    return Reply(text, resp.model, tin, u.output_tokens or 0)


def call_gemini(p, key, system, user, opts):
    url = f"{(p.base_url or 'https://generativelanguage.googleapis.com').rstrip('/')}/v1beta/models/" \
          f"{urllib.parse.quote(p.model)}:generateContent"
    cfg = {"maxOutputTokens": opts["max_output_tokens"]}
    if p.send_temperature:
        cfg["temperature"] = opts["temperature"]
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": cfg, **_extra(p)}
    data = _post(url, body, {"x-goog-api-key": key}, opts["timeout"])
    parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    u = data.get("usageMetadata") or {}
    return Reply("".join(x.get("text", "") for x in parts), data.get("modelVersion") or p.model,
                 u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0))


def call_bedrock(p, key, system, user, opts):
    base = p.base_url or f"https://bedrock-runtime.{p.region}.amazonaws.com"
    url = f"{base.rstrip('/')}/model/{urllib.parse.quote(p.model, safe='')}/converse"
    cfg = {"maxTokens": opts["max_output_tokens"]}
    if p.send_temperature:
        cfg["temperature"] = opts["temperature"]
    body = {"system": [{"text": system}], "messages": [{"role": "user", "content": [{"text": user}]}],
            "inferenceConfig": cfg, **_extra(p)}
    data = _post(url, body, {"Authorization": f"Bearer {key}"}, opts["timeout"])
    content = ((data.get("output") or {}).get("message") or {}).get("content") or []
    u = data.get("usage") or {}
    return Reply("".join(x.get("text", "") for x in content), p.model, u.get("inputTokens", 0), u.get("outputTokens", 0))


CALLERS = {"openai": call_openai, "azure_openai": call_azure, "anthropic": call_anthropic, "gemini": call_gemini,
           "bedrock": call_bedrock}


def call(provider, system, user, opts=None, purpose="suggest", check_report_id=None, caller=None):
    """呼叫一個服務並記錄用量；失敗時記錄後拋出 RuntimeError。"""
    import re

    from .models import AIUsage

    s = settings()
    opts = opts or {"timeout": s["ai_timeout"], "max_output_tokens": s["ai_max_output_tokens"],
                    "temperature": s["ai_temperature"]}
    use = AIUsage(provider_id=provider.id, provider_name=provider.name, check_report_id=check_report_id,
                  purpose=purpose, model=provider.model or provider.deployment, currency=provider.currency)
    t0 = time.monotonic()
    try:
        key = decrypt_key(provider)
        if key is None:
            raise RuntimeError("金鑰無法解密，請重新輸入")
        reply = (caller or CALLERS[provider.kind])(provider, key, system, user, opts)
        reply.text = re.sub(r"<think>.*?</think>", "", reply.text or "", flags=re.S).strip()  # 推理模型的思考過程
        if not reply.text:
            raise RuntimeError("服務沒有回傳內容")
        use.ok, use.model = True, reply.model or use.model
        use.input_tokens, use.output_tokens = reply.input_tokens, reply.output_tokens
        use.cost = cost_of(provider, reply.input_tokens, reply.output_tokens)
        return reply
    except Exception as e:
        use.error = str(e)[:500]
        raise RuntimeError(str(e)) from e
    finally:
        use.latency_ms = int((time.monotonic() - t0) * 1000)
        db.session.add(use)
        db.session.commit()
