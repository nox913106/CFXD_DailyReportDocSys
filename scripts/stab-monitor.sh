#!/bin/bash
#
# CFXD 日報系統：主機穩定性驗證。暫時性工具，不是系統正式功能（不列入版號）。
#
# 每分鐘記錄本系統的 5 個服務（dailyreport、-ocr、-ai、-maint、nginx）與主機資源，
# 網頁 http://主機IP:19999 即時查看（只允許內網），資料保留 31 天。
#
#   安裝 / 更新：curl -fsSL https://raw.githubusercontent.com/nox913106/CFXD_DailyReportDocSys/main/scripts/stab-monitor.sh | sudo bash
#   狀態：      curl -fsSL .../stab-monitor.sh | sudo bash -s -- status
#   移除：      curl -fsSL .../stab-monitor.sh | sudo bash -s -- uninstall
set -euo pipefail

NAME=stab-monitor
LIB=/usr/local/lib/$NAME
DATA=/var/lib/$NAME
UNIT=/etc/systemd/system/$NAME.service
WEB_PORT=19999

info() { echo -e "\e[32m✓\e[0m $*"; }
warn() { echo -e "\e[33m!\e[0m $*"; }
die() { echo -e "\e[31m✗\e[0m $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "請用 sudo 執行"
command -v systemctl >/dev/null || die "需要 systemd"
command -v python3 >/dev/null || die "需要 python3"

app_port() {
  local p
  p=$(grep -rhoE 'proxy_pass http://127\.0\.0\.1:[0-9]+' /etc/nginx 2>/dev/null | head -1 | grep -oE '[0-9]+$' || true)
  echo "${p:-8000}"
}
host_ip() { hostname -I 2>/dev/null | awk '{print $1}'; }
ufw_on() { command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; }

# 舊版（sysstat、Netdata、cron、報告檔）的殘留一併清掉
cleanup_old() {
  local st=$LIB/state
  [ -f $st ] || [ -f /etc/cron.d/stab-monitor ] || return 0
  rm -f /etc/cron.d/stab-monitor /var/log/stab-probe.csv /root/stab-report.html /usr/local/sbin/stab-monitor
  if [ -f /etc/systemd/system/sysstat-collect.timer.d/stab-1min.conf ]; then
    rm -f /etc/systemd/system/sysstat-collect.timer.d/stab-1min.conf
    rmdir /etc/systemd/system/sysstat-collect.timer.d 2>/dev/null || true
    systemctl daemon-reload
  fi
  if grep -q 'sysstat_was_enabled=0' $st 2>/dev/null; then
    systemctl disable --now sysstat sysstat-collect.timer >/dev/null 2>&1 || true
    DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq sysstat </dev/null >/dev/null 2>&1 || true
    rm -rf /var/log/sysstat
  fi
  if grep -q 'netdata_installed_by_us=1' $st 2>/dev/null; then
    echo "移除舊版安裝的 Netdata…"
    if curl -fsSL -m 60 https://get.netdata.cloud/kickstart.sh -o /tmp/netdata-kickstart.sh; then
      sh /tmp/netdata-kickstart.sh --uninstall --non-interactive </dev/null >/tmp/netdata-uninstall.log 2>&1 \
        || warn "Netdata 移除失敗，記錄在 /tmp/netdata-uninstall.log"
    fi
  elif [ -d /etc/netdata/go.d ]; then
    rm -f /etc/netdata/go.d/systemdunits.conf /etc/netdata/go.d/httpcheck.conf /etc/netdata/go.d/x509check.conf
  fi
  for d in /usr/share/netdata/web /opt/netdata/usr/share/netdata/web; do rm -f "$d/stab-report.html"; done
  if grep -q '^ufw_opened=1' $st 2>/dev/null && ufw_on; then ufw delete allow 19999/tcp >/dev/null 2>&1 || true; fi
  rm -f $st $LIB/probe.sh $LIB/report.py
  info "已清除舊版（sysstat / Netdata / 排程報告）"
}

cmd_install() {
  echo "主機穩定性驗證：安裝"
  cleanup_old
  mkdir -p $LIB $DATA
  write_monitor
  cat >$UNIT <<EOF
[Unit]
Description=CFXD 日報系統主機穩定性驗證（暫時性）
After=network.target

[Service]
Environment=STAB_APP_PORT=$(app_port) STAB_WEB_PORT=$WEB_PORT STAB_DATA=$DATA
ExecStart=/usr/bin/env python3 $LIB/monitor.py
Restart=always
RestartSec=5
Nice=10
MemoryMax=300M

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable -q $NAME
  systemctl restart $NAME
  # journal 永久保存：重開機後服務訊息與錯誤不會消失
  if [ ! -d /var/log/journal ]; then
    mkdir -p /var/log/journal
    systemd-tmpfiles --create --prefix /var/log/journal >/dev/null 2>&1 || true
    systemctl restart systemd-journald
    info "journal 改為永久保存"
  fi
  if ufw_on && ! ufw status | grep -qE "^$WEB_PORT/tcp"; then
    ufw allow $WEB_PORT/tcp >/dev/null && touch $LIB/ufw_opened && info "防火牆 ufw 開放 $WEB_PORT/tcp"
  fi
  sleep 2
  systemctl is-active --quiet $NAME || die "服務沒有啟動：journalctl -u $NAME -n 30"
  echo
  info "安裝完成，每分鐘記錄一次，保留 31 天"
  echo "  網頁：http://$(host_ip):$WEB_PORT （只允許內網；連不到請檢查主機前面的防火牆）"
  echo "  移除：curl -fsSL https://raw.githubusercontent.com/nox913106/CFXD_DailyReportDocSys/main/scripts/stab-monitor.sh | sudo bash -s -- uninstall"
}

cmd_status() {
  [ -f $UNIT ] || die "尚未安裝"
  systemctl is-active --quiet $NAME && info "運作中：http://$(host_ip):$WEB_PORT" || warn "沒有運作：journalctl -u $NAME -n 30"
  if [ -s $DATA/samples.jsonl ]; then
    echo "已記錄 $(wc -l <$DATA/samples.jsonl) 筆（$(du -h $DATA/samples.jsonl | cut -f1)）"
  fi
}

cmd_uninstall() {
  cleanup_old
  if [ -f $UNIT ]; then
    systemctl disable --now -q $NAME 2>/dev/null || true
    rm -f $UNIT
    systemctl daemon-reload
  fi
  if [ -f $LIB/ufw_opened ] && ufw_on; then ufw delete allow $WEB_PORT/tcp >/dev/null 2>&1 || true; info "防火牆 ufw 關閉 $WEB_PORT/tcp"; fi
  rm -rf $LIB $DATA
  info "已移除（journal 永久保存保留）"
}

write_monitor() {
cat >$LIB/monitor.py <<'PYEOF'
#!/usr/bin/env python3
"""CFXD 日報系統：主機穩定性驗證（暫時性工具）。

每分鐘記錄一次本系統的服務與主機狀態，並提供一個網頁（預設 :19999，只允許內網）。
只用 Python 標準函式庫。資料：/var/lib/stab-monitor/samples.jsonl，保留 31 天。
"""
import calendar
import ipaddress
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SERVICES = ["dailyreport", "dailyreport-ocr", "dailyreport-ai", "dailyreport-maint", "nginx"]
APP_PORT = int(os.environ.get("STAB_APP_PORT", "8000"))
WEB_PORT = int(os.environ.get("STAB_WEB_PORT", "19999"))
DATA_DIR = os.environ.get("STAB_DATA", "/var/lib/stab-monitor")
APP_DIR = os.environ.get("STAB_APP_DIR", "/opt/dailyreport")
KEEP_DAYS = 31
INTERVAL = 60
FILE = os.path.join(DATA_DIR, "samples.jsonl")
ENV = dict(os.environ, LC_ALL="C")

samples = []  # [t, code, rt, {svc: [up, restarts, mem_mb, cpu_pct]}, [cpu, iow, mem, swp, load, disk]]
lock = threading.Lock()


def now_local():
    """本機時間當成 UTC 的秒數：網頁用 UTC 顯示，就是主機的本地時間，不受瀏覽器時區影響。"""
    return calendar.timegm(time.localtime())


def fmt(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, env=ENV, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


# ---------------- 取樣 ----------------
_prev_cpu = None
_prev_svc = {}


def host_stats():
    global _prev_cpu
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:9]]
    total, idle, iow = sum(v), v[3] + v[4], v[4]
    cpu = iowp = None
    if _prev_cpu:
        dt = total - _prev_cpu[0]
        if dt > 0:
            cpu = round(100 * (1 - (idle - _prev_cpu[1]) / dt), 1)
            iowp = round(100 * (iow - _prev_cpu[2]) / dt, 1)
    _prev_cpu = (total, idle, iow)
    mi = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, rest = line.partition(":")
            mi[k] = int(rest.split()[0])
    mem = round(100 * (1 - mi.get("MemAvailable", 0) / mi["MemTotal"]), 1)
    swp = round(100 * (1 - mi.get("SwapFree", 0) / mi["SwapTotal"]), 1) if mi.get("SwapTotal") else 0
    disk = shutil.disk_usage(APP_DIR if os.path.isdir(APP_DIR) else "/")
    return [cpu, iowp, mem, swp, round(os.getloadavg()[0], 2), round(100 * disk.used / disk.total, 1)]


def service_stats(mono):
    out = run(["systemctl", "show", "-p", "ActiveState,NRestarts,MemoryCurrent,CPUUsageNSec", *SERVICES])
    blocks = out.strip().split("\n\n")
    res = {}
    for name, block in zip(SERVICES, blocks):
        kv = dict(l.split("=", 1) for l in block.splitlines() if "=" in l)
        up = 1 if kv.get("ActiveState") == "active" else 0
        restarts = int(kv["NRestarts"]) if kv.get("NRestarts", "").isdigit() else None
        mem = round(int(kv["MemoryCurrent"]) / 1048576, 1) if kv.get("MemoryCurrent", "").isdigit() else None
        cpu = None
        ns = int(kv["CPUUsageNSec"]) if kv.get("CPUUsageNSec", "").isdigit() else None
        prev = _prev_svc.get(name)
        if ns is not None and prev and ns >= prev[0] and mono > prev[1]:
            cpu = round((ns - prev[0]) / 1e9 / (mono - prev[1]) * 100, 1)
        _prev_svc[name] = (ns, mono) if ns is not None else None
        res[name] = [up, restarts, mem, cpu]
    return res


def healthz():
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{APP_PORT}/healthz", timeout=5) as r:
            r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception:
        return 0, None
    return code, round(time.monotonic() - t0, 3)


def sample():
    mono = time.monotonic()
    code, rt = healthz()
    return [now_local(), code, rt, service_stats(mono), host_stats()]


def load():
    os.makedirs(DATA_DIR, exist_ok=True)
    cut = now_local() - KEEP_DAYS * 86400
    if os.path.exists(FILE):
        with open(FILE) as f:
            for line in f:
                try:
                    s = json.loads(line)
                except ValueError:
                    continue
                if s[0] >= cut:
                    samples.append(s)
        prune()


def prune():
    """超過保存期限的資料從檔案與記憶體移除。"""
    cut = now_local() - KEEP_DAYS * 86400
    with lock:
        keep = [s for s in samples if s[0] >= cut]
        if len(keep) == len(samples) and os.path.exists(FILE):
            return
        samples[:] = keep
        tmp = FILE + ".tmp"
        with open(tmp, "w") as f:
            for s in keep:
                f.write(json.dumps(s, separators=(",", ":")) + "\n")
        os.replace(tmp, FILE)


def sampler():
    host_stats()  # 第一次只建立 CPU 基準
    service_stats(time.monotonic())
    last_prune = time.time()
    while True:
        time.sleep(INTERVAL - time.time() % INTERVAL)  # 對齊整分鐘
        try:
            s = sample()
            with lock:
                samples.append(s)
                with open(FILE, "a") as f:
                    f.write(json.dumps(s, separators=(",", ":")) + "\n")
            if time.time() - last_prune > 86400:
                prune()
                last_prune = time.time()
        except Exception as e:  # 取樣失敗不影響下一次
            print("sample failed:", e, flush=True)


# ---------------- 統計 ----------------
def incidents(ts, bad):
    out, start, prev = [], None, None
    for t, b in zip(ts, bad):
        if b and start is None:
            start = t
        if not b and start is not None:
            out.append([start, prev, round((prev - start) / 60) + 1])
            start = None
        prev = t
    if start is not None:
        out.append([start, prev, round((prev - start) / 60) + 1])
    return out


def restart_count(rs):
    total, last = 0, None
    for r in rs:
        if r is None:
            continue
        if last is not None:
            total += r - last if r >= last else r  # 主機重開機後計數歸零
        last = r
    return total


_jcache = {"at": 0, "since": None, "data": None}


def journal(since):
    if _jcache["data"] is not None and _jcache["since"] == since and time.time() - _jcache["at"] < 60:
        return _jcache["data"]
    s = fmt(since)
    errs = []
    for line in run(["journalctl", "-u", "dailyreport*", "-p", "err", "--since", s, "-q", "-o", "json"]).splitlines()[-300:]:
        try:
            j = json.loads(line)
        except ValueError:
            continue
        msg = j.get("MESSAGE")
        if isinstance(msg, list):
            msg = bytes(msg).decode("utf-8", "replace")
        ts = datetime.fromtimestamp(int(j.get("__REALTIME_TIMESTAMP", 0)) / 1e6).strftime("%Y-%m-%d %H:%M:%S")
        errs.append([ts, j.get("_SYSTEMD_UNIT") or j.get("SYSLOG_IDENTIFIER", ""), str(msg)[:500]])
    oom = [l for l in run(["journalctl", "-k", "--since", s, "-q", "-o", "short-iso"]).splitlines()
           if re.search(r"out of memory|oom-kill", l, re.I)]
    boots = run(["journalctl", "--list-boots", "-q", "--no-pager"]).strip().splitlines()[-10:]
    data = {"errors": errs, "oom": oom[-50:], "boots": boots}
    _jcache.update(at=time.time(), since=since, data=data)
    return data


def build(range_s):
    with lock:
        rows = list(samples)
    if not rows:
        return {"empty": True, "meta": meta(rows)}
    if range_s:
        rows = [r for r in rows if r[0] >= rows[-1][0] - range_s] or rows[-1:]
    ts = [r[0] for r in rows]
    ok = [r[1] == 200 for r in rows]
    rts = [r[2] for r in rows if r[2] is not None]

    svc = {}
    for s in SERVICES:
        v = [r[3].get(s, [None] * 4) for r in rows]
        mem = [x[2] for x in v if x[2] is not None]
        svc[s] = {"restarts": restart_count([x[1] for x in v]), "down": sum(1 for x in v if x[0] == 0),
                  "inc": incidents(ts, [x[0] == 0 for x in v])[-100:],
                  "mem_first": mem[0] if mem else None, "mem_last": mem[-1] if mem else None,
                  "mem_max": max(mem) if mem else None}
    fails = incidents(ts, [not o for o in ok])
    for f in fails:
        f.append(sorted({str(r[1]) for r in rows if f[0] <= r[0] <= f[1]}))

    # 每日摘要（原始資料）
    days = {}
    for r in rows:
        d = days.setdefault(fmt(r[0])[:10], {"n": 0, "ok": 0, "rt": 0, "r": 0, "dn": 0, "cpu": [], "iow": 0, "mem": 0, "swp": 0, "disk": 0})
        d["n"] += 1
        d["ok"] += r[1] == 200
        d["rt"] = max(d["rt"], r[2] or 0)
        d["dn"] += sum(1 for s in SERVICES if r[3].get(s, [1])[0] == 0)
        h = r[4]
        if h[0] is not None:
            d["cpu"].append(h[0])
        for i, k in ((1, "iow"), (2, "mem"), (3, "swp"), (5, "disk")):
            d[k] = max(d[k], h[i] or 0)
    prev = {}
    for r in rows:
        d = days[fmt(r[0])[:10]]
        for s in SERVICES:
            x = r[3].get(s, [None, None])[1]
            if x is not None and prev.get(s) is not None:
                d["r"] += x - prev[s] if x >= prev[s] else x
            if x is not None:
                prev[s] = x
    daily = []
    for k in sorted(days):
        d = days[k]
        daily.append([k, round(100 * d["ok"] / d["n"], 2), d["rt"], d["r"], d["dn"],
                      round(sum(d["cpu"]) / len(d["cpu"])) if d["cpu"] else None, max(d["cpu"]) if d["cpu"] else None,
                      d["iow"], d["mem"], d["swp"], d["disk"]])

    # 圖表：每段取最高值（保留尖峰），最多約 1500 點
    step = max(INTERVAL, (ts[-1] - ts[0]) // 1500 // INTERVAL * INTERVAL)
    buckets = {}
    for r in rows:
        buckets.setdefault((r[0] - ts[0]) // step, []).append(r)

    def mx(vals):
        vals = [v for v in vals if v is not None]
        return max(vals) if vals else None

    series = {"t": [], "rt": [], "bad": [], "cpu": [], "iow": [], "mem": [], "swp": [], "disk": [],
              "svc": {s: {"up": [], "mem": [], "cpu": []} for s in SERVICES}}
    for k in sorted(buckets):
        b = buckets[k]
        series["t"].append(b[0][0])
        series["rt"].append(mx(r[2] for r in b))
        series["bad"].append(sum(1 for r in b if r[1] != 200))
        for i, key in ((0, "cpu"), (1, "iow"), (2, "mem"), (3, "swp"), (5, "disk")):
            series[key].append(mx(r[4][i] for r in b))
        for s in SERVICES:
            v = [r[3].get(s) for r in b if r[3].get(s)]
            series["svc"][s]["up"].append(min(x[0] for x in v) if v else None)
            series["svc"][s]["mem"].append(mx(x[2] for x in v))
            series["svc"][s]["cpu"].append(mx(x[3] for x in v))

    m = meta(rows)
    m.update(first=fmt(ts[0]), last=fmt(ts[-1]), n=len(rows), avail=round(100 * sum(ok) / len(ok), 3),
             fail_min=len(ok) - sum(ok), rt_avg=round(sum(rts) / len(rts), 3) if rts else None,
             rt_max=max(rts) if rts else None, step=step,
             now=[rows[-1][1], {s: rows[-1][3].get(s, [None])[0] for s in SERVICES}, rows[-1][4]])
    return {"meta": m, "series": series, "svc": svc, "fails": fails[-200:], "daily": daily, "journal": journal(ts[0])}


def meta(rows):
    return {"host": os.uname().nodename, "cores": os.cpu_count(), "services": SERVICES, "app_port": APP_PORT,
            "boot": run(["uptime", "-s"]).strip(), "started": fmt(samples[0][0]) if samples else None,
            "made": fmt(now_local()), "keep": KEEP_DAYS}


# ---------------- 網頁 ----------------
def allowed(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_loopback or a.is_private or a.is_link_local


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if not allowed(self.client_address[0]):
            return self.send(403, "只允許內網連線", "text/plain; charset=utf-8")
        u = urlparse(self.path)
        if u.path == "/":
            return self.send(200, PAGE, "text/html; charset=utf-8")
        if u.path == "/data.json":
            try:
                rng = int(parse_qs(u.query).get("range", ["86400"])[0])
            except ValueError:
                rng = 86400
            return self.send(200, json.dumps(build(rng), ensure_ascii=False, separators=(",", ":")), "application/json")
        self.send(404, "not found", "text/plain")


PAGE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>主機穩定性</title>
<style>
:root{color-scheme:light;--bg:#f4f4f2;--surface:#fcfcfb;--line:#e4e3df;--grid:#ecebe7;--text:#0b0b0b;--text2:#52514e;--muted:#7a7975;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--good:#0ca30c;--good-bg:#d9f0d9;--crit:#d03b3b;--crit-bg:#fbe3e3}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--bg:#111110;--surface:#1a1a19;--line:#2e2e2c;--grid:#262624;--text:#f0efec;--text2:#c3c2b7;--muted:#8d8c85;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--good-bg:#173317;--crit-bg:#3a1a1a}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#111110;--surface:#1a1a19;--line:#2e2e2c;--grid:#262624;--text:#f0efec;--text2:#c3c2b7;--muted:#8d8c85;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--good-bg:#173317;--crit-bg:#3a1a1a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 system-ui,-apple-system,"Segoe UI","Microsoft JhengHei","PingFang TC",sans-serif}
main{max-width:1180px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:22px;margin:0 0 2px}h2{font-size:16px;margin:0 0 10px}h3{font-size:14px;margin:0}
.sub{color:var(--text2);margin:0 0 16px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px;min-width:0}
.verdict{display:flex;gap:12px;align-items:center;padding:14px 16px;border-radius:10px;margin-bottom:16px;font-weight:600}
.verdict.ok{background:var(--good-bg)}.verdict.bad{background:var(--crit-bg)}
.verdict .ic{font-size:20px}.verdict small{display:block;font-weight:400;color:var(--text2)}
.svcs{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:16px}
.sv{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.sv b{display:block;font-size:15px}.sv span{color:var(--text2);font-size:13px;display:block}
.tag{display:inline-flex;align-items:center;gap:4px;font-size:12px;font-weight:600;padding:1px 8px;border-radius:999px;margin:4px 0}
.tag.ok{background:var(--good-bg)}.tag.bad{background:var(--crit-bg)}
.tag.ok::before{content:"✓";color:var(--good)}.tag.bad::before{content:"✕";color:var(--crit)}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;position:sticky;top:0;z-index:5;background:var(--bg);padding:8px 0;margin-bottom:8px}
.bar span{color:var(--text2);margin-right:4px}
.chip{border:1px solid var(--line);background:var(--surface);color:var(--text);border-radius:999px;padding:4px 12px;font:inherit;cursor:pointer}
.chip.on{background:var(--text);color:var(--surface);border-color:var(--text)}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(460px,1fr));gap:16px;margin-bottom:16px}
@media (max-width:560px){.grid2{grid-template-columns:1fr}}
.grid2 .card{margin-bottom:0}
.chart{position:relative;margin-top:8px}
.chart svg{display:block;width:100%;overflow:visible}
.ax{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.gl{stroke:var(--grid)}.base{stroke:var(--line)}
.ref{stroke:var(--text2);stroke-dasharray:4 3}.reflbl{fill:var(--text2);font-size:11px}
.xh{stroke:var(--text2)}.dot{stroke:var(--surface);stroke-width:2}.fail{fill:var(--crit)}
.dlbl{font-size:11px;fill:var(--text2)}
.tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:12px;box-shadow:0 4px 14px rgb(0 0 0/.12);white-space:nowrap;z-index:3}
.tip i,.legend i{display:inline-block;width:12px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--text2);margin-top:4px}
.hint{color:var(--muted);font-size:12px;margin:6px 0 0}
.tl{display:grid;grid-template-columns:150px 1fr;gap:6px 10px;align-items:center;margin-top:8px}
.tl .nm{font-size:13px;color:var(--text2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tl svg{display:block;width:100%;height:18px}
.tl .up{fill:var(--good);opacity:.35}.tl .dn{fill:var(--crit)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--text2);font-weight:600;white-space:nowrap}
.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.scroll{overflow-x:auto}
.msg{font-family:ui-monospace,Consolas,monospace;font-size:12px;word-break:break-all}
.bad-t{color:var(--crit);font-weight:600}
pre{margin:0;font-size:12px;white-space:pre-wrap;color:var(--text2)}
details summary{cursor:pointer;color:var(--text2)}
</style>
</head>
<body>
<main>
<h1>主機穩定性</h1>
<p class="sub" id="sub">載入中…</p>
<div id="verdict"></div>
<div class="svcs" id="svcs"></div>
<div class="bar" role="group" aria-label="時間範圍"><span>時間範圍</span>
  <button class="chip" data-r="21600">6 小時</button><button class="chip on" data-r="86400">24 小時</button>
  <button class="chip" data-r="604800">7 天</button><button class="chip" data-r="0">全部</button></div>
<section class="card"><h2>服務運作時間軸</h2><div class="tl" id="tl"></div>
  <p class="legend"><span><i style="background:var(--good);opacity:.5"></i>運作中</span><span><i style="background:var(--crit)"></i>停止或重啟中</span></p></section>
<div class="grid2">
  <section class="card"><h3>網站回應時間（秒）</h3><div class="chart" id="c-rt"></div><p class="hint">紅線：沒有正常回應的時間點</p></section>
  <section class="card"><h3>服務記憶體（MB）</h3><div class="chart" id="c-smem"></div></section>
  <section class="card"><h3>服務 CPU（%，100% = 1 核）</h3><div class="chart" id="c-scpu"></div></section>
  <section class="card"><h3>主機 CPU 與 I/O 等待（%）</h3><div class="chart" id="c-cpu"></div><p class="hint">I/O 等待常超過 20% 代表磁碟跟不上</p></section>
  <section class="card"><h3>主機記憶體與 Swap（%）</h3><div class="chart" id="c-mem"></div><p class="hint">Swap 持續上升代表記憶體不夠</p></section>
  <section class="card"><h3>系統磁碟使用率（%）</h3><div class="chart" id="c-disk"></div></section>
</div>
<section class="card"><h2>每日摘要</h2><div class="scroll" id="daily"></div></section>
<section class="card"><h2>服務</h2><div class="scroll" id="svc"></div></section>
<section class="card"><h2>網站沒有回應的時段</h2><div class="scroll" id="fails"></div></section>
<section class="card"><h2>系統事件</h2><div id="events"></div></section>
<section class="card"><h2>錯誤訊息（最近 300 則）</h2><div class="scroll" id="errors"></div></section>
<p class="hint" id="foot"></p>
</main>
<script>
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const p2 = (n) => String(n).padStart(2, "0");
const fmt = (t) => { const d = new Date(t * 1000); return `${d.getUTCFullYear()}-${p2(d.getUTCMonth()+1)}-${p2(d.getUTCDate())} ${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}`; };
const num = (v, dp = 1) => v == null ? "—" : (+v).toLocaleString("zh-TW", {maximumFractionDigits: dp});
const bad = (c, v) => c ? `<span class="bad-t">${v}</span>` : v;
const COLORS = ["--s1", "--s2", "--s3", "--s4", "--s5"];
const short = (s) => s === "dailyreport" ? "web" : s.replace("dailyreport-", "");
const table = (head, rows, nc = []) => rows.length ? `<table><thead><tr>${head.map((h, i) => `<th class="${nc.includes(i) ? "n" : ""}">${h}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c, i) => `<td class="${nc.includes(i) ? "n" : ""}">${c}</td>`).join("")}</tr>`).join("")}</tbody></table>` : `<p class="hint">沒有</p>`;
let range = 86400, D = null;
try { range = +(localStorage.getItem("stab-range") ?? 86400); } catch (e) {}

function niceMax(v) { if (!(v > 0)) return 1; const raw = v / 4, e = Math.pow(10, Math.floor(Math.log10(raw))); for (const m of [1, 2, 2.5, 5, 10]) if (m * e >= raw) return 4 * m * e; return 40 * e; }
function ticks(t0, t1, w) { const want = Math.max(2, Math.floor(w / 90)); const step = [600, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800].find((s) => (t1 - t0) / s <= want) || 604800; const out = []; for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) out.push(t); return {out, step}; }
const tlabel = (t, step) => step >= 86400 || t % 86400 === 0 ? fmt(t).slice(5, 10) : fmt(t).slice(11, 16);

const charts = [];
function lineChart(id, get) {
  const el = $(id), tip = document.createElement("div"); tip.className = "tip"; tip.hidden = true;
  function draw() {
    const o = get(), t = D.series.t, n = t.length;
    if (n < 2) { el.innerHTML = `<p class="hint">資料還不夠，幾分鐘後再看</p>`; return; }
    const W = el.clientWidth || 500, H = 190, L = 44, R = 12, T = 10, B = 24, pw = W - L - R, ph = H - T - B;
    const t0 = t[0], t1 = t[n - 1], x = (tt) => L + (tt - t0) / (t1 - t0) * pw;
    const vmax = o.max ?? niceMax(Math.max(o.ref ?? 0, ...o.series.flatMap((s) => s.v.filter((v) => v != null))) * 1.05);
    const y = (v) => T + ph - Math.min(v, vmax) / vmax * ph;
    let s = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="${esc(o.label)}">`;
    for (let i = 0; i <= 4; i++) { const v = vmax * i / 4; s += `<line class="${i ? "gl" : "base"}" x1="${L}" x2="${L + pw}" y1="${y(v)}" y2="${y(v)}"/><text class="ax" x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${num(v, vmax < 2 ? 2 : vmax < 10 ? 1 : 0)}</text>`; }
    const tk = ticks(t0, t1, pw); tk.out.forEach((tt) => { s += `<text class="ax" x="${x(tt)}" y="${H - 6}" text-anchor="middle">${tlabel(tt, tk.step)}</text>`; });
    if (o.ref != null) s += `<line class="ref" x1="${L}" x2="${L + pw}" y1="${y(o.ref)}" y2="${y(o.ref)}"/><text class="reflbl" x="${L + pw - 4}" y="${y(o.ref) - 4}" text-anchor="end">${esc(o.refLabel)}</text>`;
    if (o.marks) o.marks.forEach((m, i) => { if (m) s += `<rect class="fail" x="${x(t[i]) - 1}" y="${T}" width="2" height="${ph}" opacity=".35"/>`; });
    o.series.forEach((se, si) => {
      let d = "", pen = false;
      se.v.forEach((v, i) => { if (v == null || (i && t[i] - t[i - 1] > D.meta.step * 3)) pen = false; if (v == null) return; d += (pen ? "L" : "M") + x(t[i]).toFixed(1) + "," + y(v).toFixed(1); pen = true; });
      s += `<path d="${d}" fill="none" stroke="var(${se.c})" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    });
    s += `<line class="xh" x1="0" x2="0" y1="${T}" y2="${T + ph}" visibility="hidden"/><g class="dots"></g><rect x="${L}" y="${T}" width="${pw}" height="${ph}" fill="transparent"/></svg>`;
    el.innerHTML = s + (o.series.length > 1 ? `<div class="legend">${o.series.map((se) => `<span><i style="background:var(${se.c})"></i>${esc(se.name)}</span>`).join("")}</div>` : "");
    el.appendChild(tip);
    const svg = el.querySelector("svg"), xh = svg.querySelector(".xh"), dots = svg.querySelector(".dots");
    const move = (ev) => {
      const r = svg.getBoundingClientRect(), mx = (ev.clientX - r.left) * W / r.width, tt = t0 + (mx - L) / pw * (t1 - t0);
      let k = 0; for (let i = 1; i < n; i++) if (Math.abs(t[i] - tt) < Math.abs(t[k] - tt)) k = i;
      const xx = x(t[k]); xh.setAttribute("x1", xx); xh.setAttribute("x2", xx); xh.setAttribute("visibility", "visible");
      dots.innerHTML = o.series.map((se) => se.v[k] == null ? "" : `<circle class="dot" cx="${xx}" cy="${y(se.v[k])}" r="4" fill="var(${se.c})"/>`).join("");
      const step = D.meta.step;
      tip.innerHTML = `<div style="color:var(--text2)">${fmt(t[k])}${step > 60 ? ` 起 ${Math.round(step / 60)} 分鐘內最高` : ""}</div>` +
        o.series.map((se) => `<div><i style="background:var(${se.c})"></i>${esc(se.name)} <b>${num(se.v[k], o.dp)}</b>${o.unit ?? ""}</div>`).join("") +
        (o.marks && o.marks[k] ? `<div class="bad-t">${o.marks[k]} 分鐘沒有正常回應</div>` : "");
      tip.hidden = false; const px = xx / W * r.width; tip.style.left = Math.max(0, Math.min(px + 12, r.width - tip.offsetWidth - 4)) + "px"; tip.style.top = "8px";
    };
    svg.addEventListener("pointermove", move); svg.addEventListener("pointerdown", move);
    svg.addEventListener("pointerleave", () => { tip.hidden = true; xh.setAttribute("visibility", "hidden"); dots.innerHTML = ""; });
  }
  charts.push(draw);
}
const svcSeries = (key) => D.meta.services.map((s, i) => ({name: short(s), v: D.series.svc[s][key], c: COLORS[i]}));
lineChart("c-rt", () => ({label: "網站回應時間", unit: " 秒", dp: 3, marks: D.series.bad, series: [{name: "回應時間", v: D.series.rt, c: "--s1"}]}));
lineChart("c-smem", () => ({label: "服務記憶體", unit: " MB", dp: 0, series: svcSeries("mem")}));
lineChart("c-scpu", () => ({label: "服務 CPU", unit: "%", series: svcSeries("cpu")}));
lineChart("c-cpu", () => ({label: "主機 CPU", unit: "%", max: 100, series: [{name: "CPU", v: D.series.cpu, c: "--s1"}, {name: "I/O 等待", v: D.series.iow, c: "--s2"}]}));
lineChart("c-mem", () => ({label: "主機記憶體", unit: "%", max: 100, series: [{name: "記憶體", v: D.series.mem, c: "--s1"}, {name: "Swap", v: D.series.swp, c: "--s2"}]}));
lineChart("c-disk", () => ({label: "磁碟", unit: "%", max: 100, ref: 85, refLabel: "85%", series: [{name: "磁碟", v: D.series.disk, c: "--s1"}]}));
charts.push(() => {
  const t = D.series.t, n = t.length; if (n < 2) { $("tl").innerHTML = ""; return; }
  const W = 1000, t0 = t[0], t1 = t[n - 1] + D.meta.step, sx = (v) => (v - t0) / (t1 - t0) * W;
  $("tl").innerHTML = D.meta.services.map((s) => {
    const up = D.series.svc[s].up; let r = "";
    for (let i = 0; i < n; i++) { if (up[i] == null) continue; const a = sx(t[i]), b = sx(Math.min(t[i + 1] ?? t1, t[i] + D.meta.step));
      r += `<rect class="${up[i] ? "up" : "dn"}" x="${a.toFixed(2)}" width="${Math.max(up[i] ? 0 : 3, b - a).toFixed(2)}" y="${up[i] ? 4 : 0}" height="${up[i] ? 10 : 18}"><title>${fmt(t[i])} ${up[i] ? "運作中" : "停止"}</title></rect>`; }
    const inc = D.svc[s].inc.length;
    return `<div class="nm" title="${s}">${s}${inc ? ` <span class="bad-t">✕ ${inc} 次</span>` : ""}</div><svg viewBox="0 0 ${W} 18" preserveAspectRatio="none" role="img" aria-label="${s} 運作時間軸">${r}</svg>`;
  }).join("");
});

function render() {
  const M = D.meta;
  if (D.empty) { $("sub").textContent = `${M.host} · 剛開始記錄，第一筆資料約 1 分鐘後出現`; return; }
  const SV = D.svc, svcs = M.services;
  const restarts = svcs.reduce((a, s) => a + SV[s].restarts, 0), down = svcs.reduce((a, s) => a + SV[s].down, 0), oom = D.journal.oom.length;
  const hours = (D.series.t[D.series.t.length - 1] - D.series.t[0]) / 3600;
  $("sub").textContent = `${M.host} · ${M.first} ～ ${M.last}（${hours >= 48 ? (hours / 24).toFixed(1) + " 天" : hours.toFixed(1) + " 小時"}）· 記錄自 ${M.started} 起，保留 ${M.keep} 天 · 本次開機 ${M.boot}`;
  const prob = [];
  if (M.fail_min) prob.push(`網站有 ${M.fail_min} 分鐘沒有正常回應`);
  if (restarts) prob.push(`服務重啟 ${restarts} 次`);
  if (down) prob.push(`服務停止共 ${down} 分鐘`);
  if (oom) prob.push(`記憶體不足被系統殺掉 ${oom} 次`);
  $("verdict").innerHTML = prob.length
    ? `<div class="verdict bad"><span class="ic" aria-hidden="true">⚠</span><div>這段期間有異常<small>${esc(prob.join("；"))}。往下看時間軸與錯誤訊息。</small></div></div>`
    : `<div class="verdict ok"><span class="ic" aria-hidden="true">✓</span><div>這段期間運作穩定<small>網站一直正常回應（可用率 ${M.avail}%），服務沒有停止或重啟，沒有記憶體不足。</small></div></div>`;
  const [code, nowUp] = M.now;
  $("svcs").innerHTML = `<div class="sv"><b>網站 /healthz</b><span class="tag ${code === 200 ? "ok" : "bad"}">${code === 200 ? "正常回應" : code ? "回應 " + code : "沒有回應"}</span><span>可用率 ${M.avail}% · 最慢 ${num(M.rt_max, 2)} 秒</span></div>` +
    svcs.map((s) => `<div class="sv"><b>${s}</b><span class="tag ${nowUp[s] ? "ok" : "bad"}">${nowUp[s] ? "運作中" : "停止"}</span><span>重啟 ${SV[s].restarts} 次 · 記憶體 ${num(SV[s].mem_last, 0)} MB</span></div>`).join("");
  charts.forEach((f) => f());
  $("daily").innerHTML = table(["日期", "網站可用率", "最慢回應", "重啟", "停止分鐘", "CPU 平均 / 最高", "I/O 等待最高", "記憶體最高", "Swap 最高", "磁碟"],
    D.daily.map((d) => [d[0], bad(d[1] < 100, d[1] + "%"), num(d[2], 3) + " 秒", bad(d[3], d[3]), bad(d[4], d[4]), d[5] == null ? "—" : `${d[5]}% / ${num(d[6], 0)}%`,
      bad(d[7] > 20, num(d[7]) + "%"), bad(d[8] > 90, num(d[8]) + "%"), bad(d[9] > 50, num(d[9]) + "%"), bad(d[10] >= 85, num(d[10]) + "%")]), [1, 2, 3, 4, 5, 6, 7, 8, 9]);
  $("svc").innerHTML = table(["服務", "重啟次數", "停止分鐘", "停止次數", "記憶體 開始 → 最後 / 最高（MB）"],
    svcs.map((s) => [s, bad(SV[s].restarts, SV[s].restarts), bad(SV[s].down, SV[s].down), bad(SV[s].inc.length, SV[s].inc.length),
      `${num(SV[s].mem_first, 0)} → ${num(SV[s].mem_last, 0)} / ${num(SV[s].mem_max, 0)}`]), [1, 2, 3, 4]) +
    svcs.filter((s) => SV[s].inc.length).map((s) => `<details style="margin-top:8px"><summary>${s} 停止時段（${SV[s].inc.length}）</summary>${table(["開始", "結束", "分鐘"], SV[s].inc.slice().reverse().map((i) => [fmt(i[0]), fmt(i[1]), i[2]]), [2])}</details>`).join("") +
    `<p class="hint">記憶體一天比一天高、沒有回落，可能有記憶體洩漏。</p>`;
  $("fails").innerHTML = table(["開始", "結束", "分鐘", "回應碼"], D.fails.slice().reverse().map((f) => [fmt(f[0]), fmt(f[1]), f[2], f[3].map((c) => c === "0" ? "沒有回應" : c).join("、")]), [2]);
  const J = D.journal;
  $("events").innerHTML = `<p>記憶體不足被系統殺掉（OOM）：<b class="${oom ? "bad-t" : ""}">${oom} 次</b></p>` +
    (oom ? `<details><summary>OOM 紀錄</summary><pre>${esc(J.oom.join("\n"))}</pre></details>` : "") +
    `<details style="margin-top:8px"><summary>開機紀錄（最近 10 次）</summary><pre>${esc(J.boots.join("\n") || "沒有資料")}</pre></details>`;
  $("errors").innerHTML = table(["時間", "服務", "訊息"], J.errors.slice().reverse().map((e) => [`<span style="white-space:nowrap">${esc(e[0])}</span>`, esc(e[1]), `<span class="msg">${esc(e[2])}</span>`]));
  $("foot").textContent = `每分鐘取樣，畫面每分鐘自動更新。${M.step > 60 ? `圖表每一點為 ${Math.round(M.step / 60)} 分鐘內的最高值，尖峰不會被平均掉。` : ""}最後更新 ${M.made}`;
}

async function refresh() {
  try { const r = await fetch("data.json?range=" + range, {cache: "no-store"}); D = await r.json(); render(); }
  catch (e) { $("sub").textContent = "讀取資料失敗：" + e.message; }
}
document.querySelectorAll(".chip[data-r]").forEach((b) => {
  b.classList.toggle("on", +b.dataset.r === range);
  b.addEventListener("click", () => { range = +b.dataset.r; try { localStorage.setItem("stab-range", range); } catch (e) {}
    document.querySelectorAll(".chip[data-r]").forEach((x) => x.classList.toggle("on", x === b)); refresh(); });
});
let rz; window.addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => D && !D.empty && charts.forEach((f) => f()), 150); });
refresh(); setInterval(refresh, 60000);
</script>
</body>
</html>
"""


def main():
    load()
    threading.Thread(target=sampler, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), Handler)
    print(f"stab-monitor：取樣中，網頁 :{WEB_PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
PYEOF
}

case "${1:-install}" in
  install) cmd_install ;;
  status) cmd_status ;;
  uninstall) cmd_uninstall ;;
  *) echo "用法：install | status | uninstall"; exit 1 ;;
esac
