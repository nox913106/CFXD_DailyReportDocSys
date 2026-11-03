"""HTTPS 憑證（系統設定頁 → HTTPS 憑證）。

網頁服務沒有 root 權限，不能直接改 nginx。流程：
  1. 設定頁檢查上傳的憑證與私鑰（格式、配對、有效期限），寫到 instance/tls_request/，最後寫 request.json
  2. systemd 的 dailyreport-tls.path 偵測到 request.json，以 root 執行 `dailyreport.sh apply-tls`
  3. 腳本再檢查一次、安裝到 /etc/dailyreport/tls/、nginx -t 通過才 reload，失敗就還原；結果寫回 result.json
"""

import json
import os
import socket
import ssl
from datetime import datetime, timezone

from flask import current_app

REQUEST_FILES = ("cert.pem", "key.pem", "request.json")


def request_dir():
    return os.path.join(current_app.instance_path, "tls_request")


def _describe(cert):
    from cryptography import x509
    from cryptography.x509.oid import NameOID

    def cn(name):
        attrs = name.get_attributes_for_oid(NameOID.COMMON_NAME)
        return attrs[0].value if attrs else name.rfc4514_string()

    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [str(v) for v in san.get_values_for_type(x509.DNSName)] + \
                [str(v) for v in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        names = []
    end = cert.not_valid_after_utc
    return {"subject": cn(cert.subject), "issuer": cn(cert.issuer), "names": names,
            "not_before": cert.not_valid_before_utc.astimezone().replace(tzinfo=None),
            "not_after": end.astimezone().replace(tzinfo=None),
            "days_left": (end - datetime.now(timezone.utc)).days,
            "self_signed": cert.subject == cert.issuer}


def current_cert(host="127.0.0.1", port=443):
    """nginx 目前提供的憑證（直接連線讀取，不需要讀 /etc 底下的檔案）。沒有 HTTPS 時回傳 None。"""
    from cryptography import x509

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=3) as sock, ctx.wrap_socket(sock) as tls:
            der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError):
        return None
    return _describe(x509.load_der_x509_certificate(der)) if der else None


def validate(cert_pem, key_pem):
    """回傳 (憑證資訊, 錯誤訊息)。憑證可含中繼憑證鏈（伺服器憑證放第一個）。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    try:
        certs = x509.load_pem_x509_certificates(cert_pem)
    except ValueError:
        return None, "憑證不是 PEM 格式（應以 -----BEGIN CERTIFICATE----- 開頭；.cer / .crt 若是二進位，請先轉成 PEM）"
    try:
        key = serialization.load_pem_private_key(key_pem, password=None)
    except TypeError:
        return None, "私鑰有密碼保護，nginx 無法自動載入。請提供未加密的私鑰"
    except ValueError:
        return None, "私鑰不是 PEM 格式（應以 -----BEGIN PRIVATE KEY----- 或 -----BEGIN RSA PRIVATE KEY----- 開頭）"
    pub = serialization.PublicFormat.SubjectPublicKeyInfo
    enc = serialization.Encoding.DER
    if certs[0].public_key().public_bytes(enc, pub) != key.public_key().public_bytes(enc, pub):
        return None, "私鑰與憑證不配對（第一個憑證必須是這台伺服器的憑證）"
    info = _describe(certs[0])
    if info["days_left"] < 0:
        return None, f"憑證已在 {info['not_after']:%Y-%m-%d} 過期"
    if info["not_before"] > datetime.now():
        return None, f"憑證要到 {info['not_before']:%Y-%m-%d} 才生效"
    info["chain"] = len(certs)
    return info, None


def submit(cert_pem, key_pem, user):
    d = request_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    for f in REQUEST_FILES + ("result.json",):
        try:
            os.remove(os.path.join(d, f))
        except FileNotFoundError:
            pass
    old = os.umask(0o077)
    try:
        if cert_pem is not None:
            with open(os.path.join(d, "cert.pem"), "wb") as fh:
                fh.write(cert_pem)
            with open(os.path.join(d, "key.pem"), "wb") as fh:
                fh.write(key_pem)
        action = "custom" if cert_pem is not None else "self-signed"
        tmp = os.path.join(d, "request.json.part")
        with open(tmp, "w") as fh:
            json.dump({"action": action, "by": user.username, "at": datetime.now().isoformat(timespec="seconds")}, fh)
        os.rename(tmp, os.path.join(d, "request.json"))  # 最後才出現 request.json，觸發 systemd
    finally:
        os.umask(old)


def state():
    """{'pending': bool, 'result': dict | None}"""
    d = request_dir()
    result = None
    try:
        with open(os.path.join(d, "result.json")) as fh:
            result = json.load(fh)
    except (OSError, ValueError):
        pass
    return {"pending": os.path.exists(os.path.join(d, "request.json")), "result": result}
