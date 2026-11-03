"""備份與還原（系統設定頁 → 備份還原；由維護背景服務 maint-worker 執行）。

格式與 scripts/dailyreport.sh backup 相同，兩邊產生的備份可以互相還原：
    <備份位置>/<YYYYmmdd-HHMMSS-xxxx>/
        dailyreport.db      SQLite 線上備份（不需停服務）
        instance.tar.gz     截圖、範本、字型、檢查結果.md（不含 PDF 快取與資料庫）
        env                 .env（含 SECRET_KEY；AI 金鑰以它加密，換主機還原時需要）
        VERSION             程式版本（git commit）
        meta.json           誰、何時、手動或排程
        SHA256SUMS          檢查碼（sha256sum -c 格式）
"""

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import string
import tarfile
from contextlib import contextmanager
from datetime import datetime

from flask import current_app

from . import db, sysconf

NAME_RE = re.compile(r"^20\d{6}-\d{6}-[A-Za-z0-9]{4,12}$")
DOWNLOADABLE = ("dailyreport.db", "instance.tar.gz")  # env 含 SECRET_KEY，不提供下載
EXCLUDE_DIRS = {"pdf_cache", "tls_request"}


class BackupError(RuntimeError):
    pass


def app_dir():
    return os.path.dirname(current_app.root_path)


def db_file():
    path = db.engine.url.database
    if not path:
        raise BackupError("只支援 SQLite 資料庫")
    return path if os.path.isabs(path) else os.path.join(current_app.instance_path, path)


def version():
    """讀 .git 取得目前 commit（不呼叫 git：程式目錄屬於 root，git 會拒絕其他使用者）。"""
    git = os.path.join(app_dir(), ".git")
    try:
        with open(os.path.join(git, "HEAD")) as fh:
            head = fh.read().strip()
        if not head.startswith("ref: "):
            return head
        ref = head[5:]
        p = os.path.join(git, ref)
        if os.path.exists(p):
            with open(p) as fh:
                return fh.read().strip()
        with open(os.path.join(git, "packed-refs")) as fh:
            for line in fh:
                if line.rstrip().endswith(" " + ref):
                    return line.split()[0]
    except OSError:
        pass
    return ""


@contextmanager
def _lock(root):
    os.makedirs(root, mode=0o700, exist_ok=True)
    fh = open(os.path.join(root, ".lock"), "w")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("另一個備份或還原正在進行") from None
        yield
    finally:
        fh.close()


def check_dir(root):
    """備份位置可以寫入嗎？回傳錯誤訊息或 None。"""
    if not os.path.isabs(root):
        return "必須是絕對路徑"
    try:
        os.makedirs(root, mode=0o700, exist_ok=True)
        probe = os.path.join(root, f".probe-{secrets.token_hex(4)}")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
    except OSError as e:
        return f"無法寫入（{e.strerror or e}）。若是 NAS，請先掛載並讓 dailyreport 帳號可以寫入"
    return None


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _tar_filter(instance, root):
    root_real = os.path.realpath(root)

    def f(ti):
        rel = ti.name.split("/", 1)[1] if "/" in ti.name else ""
        top = rel.split("/", 1)[0]
        if top in EXCLUDE_DIRS or re.search(r"\.db(-wal|-shm|-journal)?$", rel) or rel.endswith(".lock"):
            return None
        full = os.path.realpath(os.path.join(instance, rel)) if rel else instance
        if full == root_real or full.startswith(root_real + os.sep):  # 備份位置在 instance 底下時不要備份自己
            return None
        return ti

    return f


def create(trigger="manual", user=None, prune=True):
    """建立一份備份，回傳 (名稱, 大小 bytes)。"""
    root = sysconf.backup_dir()
    err = check_dir(root)
    if err:
        raise BackupError(f"備份位置 {root} {err}")
    with _lock(root):
        old = os.umask(0o077)  # 備份含全部資料與 SECRET_KEY，只有服務帳號可讀
        try:
            name = datetime.now().strftime("%Y%m%d-%H%M%S-") + "".join(
                secrets.choice(string.ascii_letters + string.digits) for _ in range(4))
            dest = os.path.join(root, name)
            os.mkdir(dest, 0o700)
            try:
                src = sqlite3.connect(db_file())
                dst = sqlite3.connect(os.path.join(dest, "dailyreport.db"))
                with dst:
                    src.backup(dst)
                dst.close()
                src.close()
                instance = current_app.instance_path
                with tarfile.open(os.path.join(dest, "instance.tar.gz"), "w:gz") as tar:
                    tar.add(instance, arcname="instance", filter=_tar_filter(instance, root))
                env = os.path.join(app_dir(), ".env")
                if sysconf.get("backup_include_env") and os.access(env, os.R_OK):
                    shutil.copyfile(env, os.path.join(dest, "env"))
                with open(os.path.join(dest, "VERSION"), "w") as fh:
                    fh.write(version() + "\n")
                from .models import Report

                meta = {"trigger": trigger, "by": user.username if user else "", "created_at": datetime.now().isoformat(),
                        "version": version(), "reports": Report.query.count()}
                with open(os.path.join(dest, "meta.json"), "w") as fh:
                    json.dump(meta, fh, ensure_ascii=False)
                files = sorted(f for f in os.listdir(dest) if f != "SHA256SUMS")
                with open(os.path.join(dest, "SHA256SUMS"), "w") as fh:
                    for f in files:
                        fh.write(f"{_sha256(os.path.join(dest, f))}  ./{f}\n")
            except Exception:
                shutil.rmtree(dest, ignore_errors=True)
                raise
        finally:
            os.umask(old)
        if prune:
            _prune(root, int(sysconf.get("backup_keep") or 14), protect={name})
        return name, _size(dest)


def _size(path):
    return sum(os.path.getsize(os.path.join(path, f)) for f in os.listdir(path)
               if os.path.isfile(os.path.join(path, f)))


def _prune(root, keep, protect=()):
    names = sorted(n for n in os.listdir(root) if NAME_RE.match(n) and os.path.isdir(os.path.join(root, n)))
    for n in names[:-keep] if keep > 0 else []:
        if n not in protect:
            shutil.rmtree(os.path.join(root, n), ignore_errors=True)


def path_of(name):
    if not NAME_RE.match(name or ""):
        raise BackupError("備份名稱不正確")
    p = os.path.join(sysconf.backup_dir(), name)
    if not os.path.isdir(p):
        raise BackupError(f"找不到備份 {name}")
    return p


def list_backups():
    root = sysconf.backup_dir()
    out = []
    try:
        names = sorted((n for n in os.listdir(root) if NAME_RE.match(n)), reverse=True)
    except OSError:
        return out
    for n in names:
        p = os.path.join(root, n)
        if not os.path.isdir(p):
            continue
        meta = {}
        try:
            with open(os.path.join(p, "meta.json")) as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            meta = {"trigger": "cli"}  # dailyreport.sh 指令產生的備份
        try:
            size = _size(p)
            readable = os.access(os.path.join(p, "dailyreport.db"), os.R_OK)
        except OSError:
            size, readable = 0, False
        out.append({"name": n, "time": datetime.strptime(n[:15], "%Y%m%d-%H%M%S"), "size": size, "meta": meta,
                    "readable": readable, "has_env": os.path.exists(os.path.join(p, "env"))})
    return out


def verify(path):
    sums = os.path.join(path, "SHA256SUMS")
    if not os.path.isfile(sums):
        raise BackupError("沒有 SHA256SUMS")
    with open(sums) as fh:
        for line in fh:
            digest, _, f = line.strip().partition("  ")
            f = f.removeprefix("./")
            if "/" in f or not f:
                raise BackupError("SHA256SUMS 格式不正確")
            fp = os.path.join(path, f)
            if not os.path.isfile(fp) or _sha256(fp) != digest:
                raise BackupError(f"{f} 檢查碼不符，備份可能已損壞")
    if not os.path.isfile(os.path.join(path, "dailyreport.db")):
        raise BackupError("備份中沒有資料庫")


def restore(name, user=None):
    """還原：先檢查碼 → 自動備份目前資料 → 線上替換資料庫 → 解開檔案 → 補齊新版欄位。回傳還原前備份的名稱。

    資料庫用 SQLite backup API 寫回原檔案：其他服務的連線會直接看到新資料，不需要停服務。
    """
    from . import _add_missing_columns, _migrate_roles

    src_dir = path_of(name)
    verify(src_dir)
    with sqlite3.connect(f"file:{os.path.join(src_dir, 'dailyreport.db')}?mode=ro", uri=True) as chk:
        if chk.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("備份中的資料庫損壞")
    pre, _ = create("pre_restore", user, prune=False)
    root = sysconf.backup_dir()
    with _lock(root):
        db.session.remove()
        db.engine.dispose()
        src = sqlite3.connect(f"file:{os.path.join(src_dir, 'dailyreport.db')}?mode=ro", uri=True)
        dst = sqlite3.connect(db_file(), timeout=60)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        instance = current_app.instance_path
        tgz = os.path.join(src_dir, "instance.tar.gz")
        if os.path.isfile(tgz):
            with tarfile.open(tgz, "r:gz") as tar:
                members = [m for m in tar.getmembers()
                           if m.name.startswith("instance/") and not re.search(r"\.db(-wal|-shm)?$", m.name)]
                for m in members:
                    m.name = m.name[len("instance/"):]
                tar.extractall(instance, members=[m for m in members if m.name], filter="data")
        shutil.rmtree(os.path.join(instance, "pdf_cache"), ignore_errors=True)
        os.makedirs(os.path.join(instance, "pdf_cache"), exist_ok=True)
    db.create_all()
    _add_missing_columns()
    _migrate_roles()
    return pre


def delete(name):
    shutil.rmtree(path_of(name))


def last_backup():
    items = list_backups()
    return items[0] if items else None
