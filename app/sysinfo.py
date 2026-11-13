"""系統資訊：硬體、作業系統、執行環境、服務、系統套件、Python 套件、字型與 OCR 模型。

每次開頁都即時讀取（/proc、/sys、systemctl、dpkg-query、importlib.metadata），不存任何固定資料。
以網站服務帳號（非 root）執行，讀不到的項目顯示「無法取得」而不是讓整頁失敗。
"""

import importlib.metadata as md
import os
import platform
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
from datetime import datetime, timedelta

from flask import current_app

from . import db

UNITS = ("dailyreport", "dailyreport-ocr", "dailyreport-ai", "dailyreport-maint", "dailyreport-tls.path", "nginx")
TOOLS = (  # (名稱, 指令)
    ("nginx", ["nginx", "-v"]),
    ("SQLite（指令）", ["sqlite3", "--version"]),
    ("OpenSSL（指令）", ["openssl", "version"]),
    ("git", ["git", "--version"]),
    ("curl", ["curl", "--version"]),
    ("systemd", ["systemctl", "--version"]),
    ("fontconfig", ["fc-list", "--version"]),
)


def _run(cmd, timeout=8):
    exe = shutil.which(cmd[0]) or next((p for p in (f"/usr/sbin/{cmd[0]}", f"/sbin/{cmd[0]}") if os.path.exists(p)), None)
    if not exe:
        return None
    try:
        r = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return (r.stdout or r.stderr).strip()


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def size(n):
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _duration(sec):
    d = timedelta(seconds=int(sec))
    days, rem = d.days, d.seconds
    return (f"{days} 天 " if days else "") + f"{rem // 3600} 小時 {rem % 3600 // 60} 分"


def _safe(fn):
    """單一區塊失敗只影響自己。"""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return [("無法取得", f"{type(e).__name__}: {str(e)[:200]}", "")]


# ---------------------------------------------------------------- 硬體


def hardware():
    rows = []
    cpu = _read("/proc/cpuinfo") or ""
    model = next((ln.split(":", 1)[1].strip() for ln in cpu.splitlines() if ln.lower().startswith(("model name", "hardware", "cpu model"))), None)
    phys = {ln.split(":", 1)[1].strip() for ln in cpu.splitlines() if ln.startswith("physical id")}
    cores = {ln.split(":", 1)[1].strip() for ln in cpu.splitlines() if ln.startswith("core id")}
    flags = next((ln.split(":", 1)[1].split() for ln in cpu.splitlines() if ln.startswith(("flags", "Features"))), [])
    rows.append(("CPU 型號", model or platform.processor() or "—", platform.machine()))
    try:
        usable = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        usable = os.cpu_count()
    rows.append(("CPU 數量", f"{os.cpu_count()} 執行緒" + (f"、{len(phys)} 顆 × {len(cores)} 核" if phys and cores else ""),
                 f"本程式可用 {usable} 個" if usable != os.cpu_count() else ""))
    ext = [f for f in ("sse4_2", "avx", "avx2", "avx512f", "fma", "neon", "asimd") if f in flags]
    if ext:
        rows.append(("CPU 指令集", "、".join(ext), "OCR（onnxruntime）會使用 AVX / NEON 加速"))
    mhz = [float(ln.split(":", 1)[1]) for ln in cpu.splitlines() if ln.startswith("cpu MHz")]
    if mhz:
        rows.append(("CPU 時脈", f"{sum(mhz) / len(mhz):.0f} MHz（目前平均）", ""))
    load = os.getloadavg() if hasattr(os, "getloadavg") else None
    if load:
        rows.append(("系統負載", " / ".join(f"{x:.2f}" for x in load), "1 / 5 / 15 分鐘平均"))
    mem = {}
    for ln in (_read("/proc/meminfo") or "").splitlines():
        k, _, v = ln.partition(":")
        if v.strip().endswith("kB"):
            mem[k] = int(v.split()[0]) * 1024
    if mem:
        total, avail = mem.get("MemTotal"), mem.get("MemAvailable")
        rows.append(("記憶體", f"{size(total)}，可用 {size(avail)}", f"已用 {(1 - avail / total) * 100:.0f}%" if total and avail else ""))
        if mem.get("SwapTotal"):
            rows.append(("Swap", f"{size(mem['SwapTotal'])}，可用 {size(mem.get('SwapFree'))}", ""))
        else:
            rows.append(("Swap", "未設定", ""))
    vendor, product = _read("/sys/class/dmi/id/sys_vendor"), _read("/sys/class/dmi/id/product_name")
    if vendor or product:
        rows.append(("主機型號", " ".join(x for x in (vendor, product) if x), _read("/sys/class/dmi/id/bios_version") and f"BIOS {_read('/sys/class/dmi/id/bios_version')}" or ""))
    virt = _run(["systemd-detect-virt"])
    rows.append(("虛擬化", {"none": "實體機", None: "—"}.get(virt, virt), ""))
    for name in sorted(os.listdir("/sys/block")) if os.path.isdir("/sys/block") else []:
        if name.startswith(("loop", "ram", "zram", "sr", "dm-")):
            continue
        sectors = _read(f"/sys/block/{name}/size")
        if not sectors or sectors == "0":
            continue
        rot = _read(f"/sys/block/{name}/queue/rotational")
        model_ = _read(f"/sys/block/{name}/device/model") or ""
        rows.append((f"磁碟 {name}", size(int(sectors) * 512), " ".join(x for x in (model_, {"0": "SSD", "1": "HDD"}.get(rot, "")) if x)))
    return rows


def filesystems():
    """程式、資料、上傳、備份所在的檔案系統。"""
    cfg = current_app.config
    from .backup import app_dir

    paths = [("程式", app_dir()), ("資料（instance）", current_app.instance_path), ("上傳", cfg.get("UPLOAD_DIR")), ("根目錄", "/")]
    mounts = []
    for ln in (_read("/proc/mounts") or "").splitlines():
        p = ln.split()
        if len(p) >= 3:
            mounts.append((p[1].replace("\\040", " "), p[0], p[2]))
    rows, seen = [], set()
    for label, path in paths:
        if not path or not os.path.exists(path):
            continue
        real = os.path.realpath(path)
        mp = max((m for m in mounts if real == m[0] or real.startswith(m[0].rstrip("/") + "/")), key=lambda m: len(m[0]), default=None)
        st = os.statvfs(real)
        total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
        key = mp[0] if mp else real
        rows.append({"label": label, "path": path, "mount": mp[0] if mp else "—", "device": mp[1] if mp else "—",
                     "fstype": mp[2] if mp else "—", "total": size(total), "free": size(free),
                     "used_pct": round((1 - free / total) * 100) if total else 0, "dup": key in seen})
        seen.add(key)
    return rows


# ---------------------------------------------------------------- 作業系統


def operating_system():
    rows = []
    osr = {}
    for ln in (_read("/etc/os-release") or "").splitlines():
        k, _, v = ln.partition("=")
        osr[k] = v.strip('"')
    rows.append(("作業系統", osr.get("PRETTY_NAME") or platform.platform(), osr.get("VERSION_CODENAME", "")))
    rows.append(("核心", platform.release(), platform.version()[:80]))
    rows.append(("主機名稱", socket.gethostname(), socket.getfqdn() if socket.getfqdn() != socket.gethostname() else ""))
    up = _read("/proc/uptime")
    if up:
        sec = float(up.split()[0])
        rows.append(("開機時間", (datetime.now() - timedelta(seconds=sec)).strftime("%Y-%m-%d %H:%M"), f"已運作 {_duration(sec)}"))
    tz = _read("/etc/timezone") or (os.path.realpath("/etc/localtime").split("zoneinfo/")[-1] if os.path.exists("/etc/localtime") else "")
    rows.append(("時區", tz or time.tzname[0], datetime.now().astimezone().strftime("UTC%z")))
    sync = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    ntp = _run(["timedatectl", "show-timesync", "-p", "ServerName", "--value"])
    if sync is not None:
        rows.append(("時間同步", "已同步" if sync == "yes" else "未同步", f"時間伺服器 {ntp}" if ntp else ""))
    rows.append(("語系", os.environ.get("LANG") or "—", ""))
    return rows


# ---------------------------------------------------------------- 執行環境


def runtime():
    from .backup import app_dir, display_version

    rows = [("程式版本", display_version(), app_dir())]
    rows.append(("Python", f"{platform.python_implementation()} {platform.python_version()}", sys.executable))
    venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    rows.append(("虛擬環境", sys.prefix if venv else "未使用", f"基底 {sys.base_prefix}" if venv else ""))
    try:
        import pwd

        user = pwd.getpwuid(os.geteuid()).pw_name
    except (ImportError, KeyError):
        user = str(os.geteuid()) if hasattr(os, "geteuid") else "—"
    server = os.environ.get("SERVER_SOFTWARE", "")
    rows.append(("網站程序", f"PID {os.getpid()}，帳號 {user}", server or ("gunicorn" if "gunicorn" in " ".join(sys.argv) else " ".join(sys.argv)[:80])))
    rows.append(("OpenSSL（Python）", ssl.OPENSSL_VERSION, ""))
    rows.append(("SQLite 函式庫", sqlite3.sqlite_version, "Python 內建 sqlite3 模組連結的版本"))
    url = db.engine.url
    if url.get_backend_name() == "sqlite" and url.database:
        from .backup import db_file

        path = db_file()
        conn = db.session.connection()
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        pages = conn.exec_driver_sql("PRAGMA page_count").scalar()
        psize = conn.exec_driver_sql("PRAGMA page_size").scalar()
        files = [path, path + "-wal"]
        total = sum(os.path.getsize(f) for f in files if os.path.exists(f))
        rows.append(("資料庫", f"SQLite，{size(total)}", f"{path}；journal {mode}；{pages} 頁 × {psize} B"))
    else:
        rows.append(("資料庫", url.get_backend_name(), ""))
    for label, cmd in TOOLS:
        out = _run(cmd)
        line = (out or "").splitlines()[0] if out else None
        if line:
            line = re.sub(r"^nginx version: ", "", line)
        rows.append((label, line or "未安裝或無法執行", ""))
    return rows


# ---------------------------------------------------------------- 服務


def services():
    rows = []
    props = "LoadState,ActiveState,SubState,UnitFileState,MainPID,ActiveEnterTimestamp,MemoryCurrent,NRestarts,ExecMainStartTimestamp"
    for unit in UNITS:
        out = _run(["systemctl", "show", unit, "-p", props])
        if out is None:
            return [{"unit": u, "state": "無法查詢（不是以 systemd 執行）", "ok": None} for u in UNITS]
        p = dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)
        if not p.get("ActiveState"):  # 有 systemctl 但不是由 systemd 管理（例如容器）
            return [{"unit": u, "state": "無法查詢（不是以 systemd 執行）", "ok": None} for u in UNITS]
        mem = p.get("MemoryCurrent", "")
        since = p.get("ActiveEnterTimestamp", "")
        rows.append({
            "unit": unit,
            "state": f"{p.get('ActiveState', '?')}（{p.get('SubState', '?')}）" if p.get("LoadState") != "not-found" else "未安裝",
            "ok": p.get("ActiveState") == "active",
            "enabled": p.get("UnitFileState", ""),
            "pid": p.get("MainPID") if p.get("MainPID") not in (None, "0") else "",
            "since": since.split(" ", 1)[1] if " " in since else since,
            "memory": size(int(mem)) if mem.isdigit() else "",
            "restarts": p.get("NRestarts", ""),
        })
    return rows


# ---------------------------------------------------------------- 系統套件


def _install_list():
    """安裝腳本實際安裝的系統套件（讀 scripts/dailyreport.sh，和腳本保持一致）。"""
    from .backup import app_dir

    text = _read(os.path.join(app_dir(), "scripts", "dailyreport.sh")) or ""
    m = re.search(r"install_packages\(\)\s*\{.*?apt-get install[^\n]*\\\n(.*?)>/dev/null", text, re.S)
    if not m:
        return []
    return [w for w in m.group(1).replace("\\", " ").split() if re.match(r"^[a-z0-9][a-z0-9.+-]+$", w)]


def system_packages():
    """(使用中的套件列表, 全部已安裝的套件數)。"""
    wanted = _install_list() + ["systemd-timesyncd", "python3-minimal", "libsqlite3-0", "libssl3", "libssl3t64"]
    out = _run(["dpkg-query", "-W", "-f", "${Package}\t${Version}\t${db:Status-Abbrev}\t${binary:Summary}\n"], timeout=20)
    if out is None:
        return None, 0
    allpk = {}
    for ln in out.splitlines():
        p = ln.split("\t")
        if len(p) >= 3 and p[2].startswith("ii"):
            allpk[p[0].split(":")[0]] = (p[1], p[3] if len(p) > 3 else "")
    rows = []
    for name in dict.fromkeys(wanted):
        if name in allpk:
            rows.append({"name": name, "version": allpk[name][0], "summary": allpk[name][1], "installed": True})
        elif name in _install_list():
            rows.append({"name": name, "version": "未安裝", "summary": "", "installed": False})
    return rows, len(allpk)


def all_system_packages():
    out = _run(["dpkg-query", "-W", "-f", "${Package}\t${Version}\t${db:Status-Abbrev}\n"], timeout=20) or ""
    return sorted((p[0], p[1]) for p in (ln.split("\t") for ln in out.splitlines()) if len(p) >= 3 and p[2].startswith("ii"))


# ---------------------------------------------------------------- Python 套件


def _requirements():
    from .backup import app_dir

    req = {}
    for ln in (_read(os.path.join(app_dir(), "requirements.txt")) or "").splitlines():
        m = re.match(r"^\s*([A-Za-z0-9_.\-\[\]]+)\s*==\s*([^\s;#]+)", ln)
        if m:
            req[_norm(m.group(1).split("[")[0])] = m.group(2)
    return req


def _norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def python_packages():
    req = _requirements()
    rows, seen = [], set()
    for dist in md.distributions():
        name = dist.metadata["Name"] or "?"
        key = _norm(name)
        if key in seen:
            continue
        seen.add(key)
        want = req.get(key)
        rows.append({"name": name, "version": dist.version, "summary": (dist.metadata["Summary"] or "")[:120],
                     "license": _license(dist), "direct": key in req, "required": want,
                     "mismatch": bool(want and want != dist.version)})
    for key, want in req.items():
        if key not in seen:
            rows.append({"name": key, "version": "未安裝", "summary": "", "license": "", "direct": True,
                         "required": want, "mismatch": True})
    rows.sort(key=lambda r: (not r["direct"], r["name"].lower()))
    return rows


def _license(dist):
    m = dist.metadata
    lic = m.get("License-Expression") or ""
    if not lic:
        lic = (m.get("License") or "").splitlines()[0] if m.get("License") else ""
    if not lic or len(lic) > 40:
        cls = [c.split("::")[-1].strip() for c in (m.get_all("Classifier") or []) if c.startswith("License ::")]
        lic = cls[0] if cls else lic[:40]
    return lic


# ---------------------------------------------------------------- 字型與 OCR 模型


def fonts_in_use():
    from .pdf import fonts

    dirs = [current_app.config.get("FONT_DIR")] + fonts.SYSTEM_DIRS
    rows = []
    for fam, styles in fonts.FAMILIES.items():
        path = fonts._load(f"SysInfo-{fam.replace(' ', '')}", styles[""], [d for d in dirs if d])
        uploaded = bool(path and current_app.config.get("FONT_DIR") and path.startswith(current_app.config["FONT_DIR"]))
        rows.append({"family": fam, "file": os.path.basename(path) if path else "找不到可用字型",
                     "path": path or "", "source": "上傳" if uploaded else ("系統" if path else ""), "ok": bool(path)})
    return rows


def ocr_models():
    try:
        dist = md.distribution("rapidocr")
    except md.PackageNotFoundError:
        return []
    base = os.path.join(str(dist.locate_file("")), "rapidocr", "models")
    if not os.path.isdir(base):
        return []
    return [{"name": f, "size": size(os.path.getsize(os.path.join(base, f)))}
            for f in sorted(os.listdir(base)) if f.endswith(".onnx")]


# ---------------------------------------------------------------- 全部


def collect():
    t0 = time.monotonic()
    try:
        pk, n_all = system_packages()
    except Exception:  # noqa: BLE001
        pk, n_all = None, 0
    info = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "hardware": _safe(hardware),
        "filesystems": _safe(filesystems),
        "os": _safe(operating_system),
        "runtime": _safe(runtime),
        "services": _safe(services),
        "system_packages": pk,
        "system_packages_total": n_all,
        "python_packages": _safe(python_packages),
        "fonts": _safe(fonts_in_use),
        "ocr_models": _safe(ocr_models),
    }
    info["elapsed"] = f"{time.monotonic() - t0:.2f}"
    return info
