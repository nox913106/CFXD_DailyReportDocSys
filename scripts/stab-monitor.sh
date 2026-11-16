#!/bin/bash
#
# 主機穩定性驗證：暫時性工具，不是系統正式功能（不列入版號）。驗證結束後 uninstall 即可完全移除。
#
#   安裝：sysstat（每分鐘資源）、journal 永久保存、每分鐘服務探測、每小時網頁報告、Netdata
#     curl -fsSL https://raw.githubusercontent.com/nox913106/CFXD_DailyReportDocSys/main/scripts/stab-monitor.sh | sudo bash
#   不裝 Netdata：
#     curl -fsSL .../stab-monitor.sh | sudo bash -s -- install --no-netdata
#   狀態 / 立即產生報告（/root/stab-report.html）/ 移除（加 --keep-data 保留資料）：
#     curl -fsSL .../stab-monitor.sh | sudo bash -s -- status
#     curl -fsSL .../stab-monitor.sh | sudo bash -s -- report
#     curl -fsSL .../stab-monitor.sh | sudo bash -s -- uninstall
#   安裝後也可以直接用：sudo stab-monitor status | report | uninstall
set -euo pipefail

SVCS="dailyreport dailyreport-ocr dailyreport-ai dailyreport-maint"
LIB=/usr/local/lib/stab-monitor
CSV=/var/log/stab-probe.csv
REPORT=/root/stab-report.html
CRON=/etc/cron.d/stab-monitor
TIMER_DROPIN=/etc/systemd/system/sysstat-collect.timer.d/stab-1min.conf
STATE=$LIB/state
URL="${STAB_URL:-https://raw.githubusercontent.com/nox913106/CFXD_DailyReportDocSys/main/scripts/stab-monitor.sh}"
BIN=/usr/local/sbin/stab-monitor

info() { echo -e "\e[32m✓\e[0m $*"; }
warn() { echo -e "\e[33m!\e[0m $*"; }
die() { echo -e "\e[31m✗\e[0m $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "請用 sudo 執行"

app_port() {
  local p
  p=$(grep -rhoE 'proxy_pass http://127\.0\.0\.1:[0-9]+' /etc/nginx 2>/dev/null | head -1 | grep -oE '[0-9]+$' || true)
  echo "${p:-8000}"
}

write_files() {
  mkdir -p "$LIB"
  cat >"$LIB/probe.sh" <<EOF
#!/bin/bash
# 每分鐘：/healthz 回應碼與時間、各服務狀態 / 累計重啟次數 / 記憶體
line="\$(date '+%F %T'),\$(curl -s -o /dev/null -m 5 -w '%{http_code} %{time_total}s' http://127.0.0.1:$(app_port)/healthz)"
for s in $SVCS; do
  line+=",\$s \$(systemctl show \$s -p ActiveState,NRestarts,MemoryCurrent 2>/dev/null | paste -sd' ')"
done
echo "\$line" >> $CSV
EOF
  chmod +x "$LIB/probe.sh"
  write_report_py
}

install_sysstat() {
  apt-get install -y -qq sysstat >/dev/null
  grep -q sysstat_was_enabled "$STATE" || echo "sysstat_was_enabled=$(grep -q '^ENABLED="true"' /etc/default/sysstat && echo 1 || echo 0)" >>"$STATE"
  sed -i 's/^ENABLED=.*/ENABLED="true"/' /etc/default/sysstat
  sed -i 's/^HISTORY=.*/HISTORY=31/' /etc/sysstat/sysstat
  mkdir -p "$(dirname "$TIMER_DROPIN")"
  printf '[Timer]\nOnCalendar=\nOnCalendar=*:*:00\n' >"$TIMER_DROPIN"
  systemctl daemon-reload
  systemctl enable --now sysstat sysstat-collect.timer >/dev/null 2>&1
  info "sysstat：每分鐘記錄 CPU、記憶體、Swap、磁碟、負載，保存 31 天"
}

install_journal() {
  if [ -d /var/log/journal ]; then info "journal 已經是永久保存"; return; fi
  mkdir -p /var/log/journal
  systemd-tmpfiles --create --prefix /var/log/journal >/dev/null 2>&1 || true
  systemctl restart systemd-journald
  info "journal 改為永久保存（重開機後訊息不會消失）"
}

install_netdata() {
  if command -v netdata >/dev/null 2>&1; then
    info "Netdata 已安裝，只更新設定"
  else
    echo "安裝 Netdata（官方腳本，關閉遙測、不連雲端）…"
    curl -fsSL https://get.netdata.cloud/kickstart.sh -o /tmp/netdata-kickstart.sh
    sh /tmp/netdata-kickstart.sh --non-interactive --stable-channel --disable-telemetry >/tmp/netdata-install.log 2>&1 \
      || die "Netdata 安裝失敗，記錄在 /tmp/netdata-install.log（可改用 install --no-netdata）"
    echo "netdata_installed_by_us=1" >>"$STATE"
  fi
  local conf=/etc/netdata
  # 只聽本機，用 SSH 通道看
  touch $conf/netdata.conf
  if grep -q '^\[web\]' $conf/netdata.conf; then
    sed -i '/^\s*bind to\s*=/d' $conf/netdata.conf
    sed -i 's/^\[web\]/[web]\n    bind to = 127.0.0.1/' $conf/netdata.conf
  else
    printf '\n[web]\n    bind to = 127.0.0.1\n' >>$conf/netdata.conf
  fi
  mkdir -p $conf/go.d
  printf 'enabled: yes\ndefault_run: yes\nmodules:\n  systemdunits: yes\n' >$conf/go.d.conf
  cat >$conf/go.d/systemdunits.conf <<'EOF'
jobs:
  - name: dailyreport
    include:
      - 'dailyreport*'
EOF
  cat >$conf/go.d/httpcheck.conf <<EOF
jobs:
  - name: dailyreport_healthz
    url: http://127.0.0.1:$(app_port)/healthz
    status_accepted: [200]
    response_match: '"ok"'
    update_every: 30
EOF
  local crt; crt=$(grep -rhoE 'ssl_certificate\s+[^;]+' /etc/nginx/sites-enabled 2>/dev/null | awk '{print $2}' | head -1 || true)
  if [ -n "$crt" ] && [ -f "$crt" ]; then
    printf 'jobs:\n  - name: dailyreport_tls\n    source: file://%s\n' "$crt" >$conf/go.d/x509check.conf
  fi
  systemctl restart netdata
  info "Netdata：服務狀態、/healthz、各服務資源${crt:+、HTTPS 憑證到期}，只聽 127.0.0.1:19999"
}

cmd_install() {
  local netdata=1
  [ "${1:-}" = "--no-netdata" ] && netdata=0
  command -v systemctl >/dev/null || die "需要 systemd"
  mkdir -p "$LIB"
  if [ -f "$STATE" ]; then info "已經安裝過，更新程式與設定"; else echo "installed_at=$(date '+%F %T')" >"$STATE"; fi
  apt-get update -qq >/dev/null || warn "apt-get update 失敗，繼續嘗試安裝"
  install_sysstat
  install_journal
  command -v python3 >/dev/null || apt-get install -y -qq python3 >/dev/null
  write_files
  if curl -fsSL "$URL" -o "$BIN.tmp" 2>/dev/null && bash -n "$BIN.tmp"; then mv "$BIN.tmp" "$BIN"; chmod +x "$BIN"; else rm -f "$BIN.tmp"; fi
  cat >"$CRON" <<EOF
# stab-monitor：每分鐘探測服務，每小時產生報告
* * * * * root $LIB/probe.sh
7 * * * * root python3 $LIB/report.py -o $REPORT >/dev/null 2>&1
EOF
  "$LIB/probe.sh"
  info "服務探測：每分鐘寫入 $CSV（/healthz 用 port $(app_port)）"
  info "網頁報告：每小時更新 $REPORT，也可隨時執行 report"
  if [ $netdata = 1 ]; then install_netdata; fi
  echo
  echo "安裝完成。之後："
  echo "  看狀態     sudo stab-monitor status"
  echo "  產生報告   sudo stab-monitor report   → 下載 $REPORT 用瀏覽器開"
  if [ $netdata = 1 ]; then echo "  看 Netdata 在自己電腦執行  ssh -L 19999:127.0.0.1:19999 帳號@這台主機，再開 http://localhost:19999"; fi
  echo "  驗證結束   sudo stab-monitor uninstall"
}

cmd_status() {
  [ -f "$STATE" ] || die "尚未安裝"
  echo "安裝時間：$(grep installed_at "$STATE" | cut -d= -f2)"
  if [ -s "$CSV" ]; then
    echo "服務探測：$(wc -l <"$CSV") 筆，最新 $(tail -1 "$CSV" | cut -d, -f1-2)"
  else warn "服務探測還沒有資料"; fi
  systemctl is-active --quiet sysstat-collect.timer && info "sysstat 記錄中" || warn "sysstat 沒有在記錄"
  [ -d /var/log/journal ] && info "journal 永久保存" || warn "journal 沒有永久保存"
  if command -v netdata >/dev/null 2>&1; then
    systemctl is-active --quiet netdata && info "Netdata 運作中（127.0.0.1:19999）" || warn "Netdata 沒有運作"
  fi
  [ -f "$REPORT" ] && echo "最近一次報告：$REPORT（$(date -r "$REPORT" '+%F %T')）"
  echo "佔用空間：探測 $(du -sh "$CSV" 2>/dev/null | cut -f1)、sysstat $(du -sh /var/log/sysstat 2>/dev/null | cut -f1)、journal $(journalctl --disk-usage 2>/dev/null | grep -oE '[0-9.]+[KMGT]' | head -1)"
}

cmd_report() {
  [ -f "$STATE" ] || die "尚未安裝"
  write_report_py  # 用最新版的報告程式
  python3 "$LIB/report.py" -o "${1:-$REPORT}"
}

cmd_uninstall() {
  local keep=0; [ "${1:-}" = "--keep-data" ] && keep=1
  [ -f "$STATE" ] || warn "沒有安裝紀錄，仍嘗試清除"
  rm -f "$CRON"
  rm -f "$TIMER_DROPIN"; rmdir "$(dirname "$TIMER_DROPIN")" 2>/dev/null || true
  systemctl daemon-reload
  if grep -q 'sysstat_was_enabled=0' "$STATE" 2>/dev/null; then
    systemctl disable --now sysstat sysstat-collect.timer >/dev/null 2>&1 || true
    apt-get purge -y -qq sysstat >/dev/null 2>&1 || true
    [ $keep = 0 ] && rm -rf /var/log/sysstat
    info "已移除 sysstat"
  else
    info "sysstat 安裝前就有在用，保留"
  fi
  if grep -q 'netdata_installed_by_us=1' "$STATE" 2>/dev/null; then
    curl -fsSL https://get.netdata.cloud/kickstart.sh -o /tmp/netdata-kickstart.sh \
      && sh /tmp/netdata-kickstart.sh --uninstall --non-interactive >/tmp/netdata-uninstall.log 2>&1 \
      && info "已移除 Netdata" || warn "Netdata 移除失敗，記錄在 /tmp/netdata-uninstall.log"
  elif command -v netdata >/dev/null 2>&1; then
    rm -f /etc/netdata/go.d/systemdunits.conf /etc/netdata/go.d/httpcheck.conf /etc/netdata/go.d/x509check.conf
    info "Netdata 安裝前就有，只移除本工具加的設定"
  fi
  if [ $keep = 1 ]; then
    mkdir -p /root/stab-monitor-data
    cp -a "$CSV" "$REPORT" /root/stab-monitor-data/ 2>/dev/null || true
    info "資料保留在 /root/stab-monitor-data"
  fi
  rm -f "$CSV" "$REPORT"
  rm -rf "$LIB" "$BIN"
  info "移除完成（journal 永久保存保留，之後追問題用得到）"
}

write_report_py() {
cat >"$LIB/report.py" <<'PYEOF'
#!/usr/bin/env python3
"""穩定性驗證報告（網頁版）：把 stab-probe.csv、sysstat、journal 整理成一個 HTML 檔。

用法：sudo python3 stab-report.py [-o stab-report.html]
只用 Python 標準函式庫，產生的 HTML 不需要網路（沒有外部 JS / CSS）。
"""
import argparse
import calendar
import glob
import html
import json
import os
import re
import socket
import subprocess
from collections import defaultdict
from datetime import datetime, timezone

SVCS = ["dailyreport", "dailyreport-ocr", "dailyreport-ai", "dailyreport-maint"]
ENV = dict(os.environ, LC_ALL="C", S_TIME_FORMAT="ISO")


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, env=ENV, timeout=120).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def epoch(s):
    """本機時間字串 → 秒數（當成 UTC 處理，網頁也用 UTC 顯示，避免時區換算）。"""
    return calendar.timegm(datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").timetuple())


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def read_probe(path):
    t, code, rt = [], [], []
    svc = {s: {"up": [], "r": [], "mem": []} for s in SVCS}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.rstrip("\n").split(",")
            if len(parts) < 2:
                continue
            try:
                t.append(epoch(parts[0]))
            except ValueError:
                continue
            c, _, r = parts[1].partition(" ")
            code.append(c)
            rt.append(num(r.rstrip("s")))
            got = {}
            for p in parts[2:]:
                name, _, rest = p.partition(" ")
                got[name] = dict(kv.split("=", 1) for kv in rest.split() if "=" in kv)
            for s in SVCS:
                v = got.get(s, {})
                svc[s]["up"].append(None if not v else int(v.get("ActiveState") == "active"))
                svc[s]["r"].append(num(v.get("NRestarts")))
                m = num(v.get("MemoryCurrent"))
                svc[s]["mem"].append(round(m / 1048576, 1) if m else None)
    return {"t": t, "code": code, "rt": rt, "svc": svc}


def read_sar(t0, sadir):
    cols = {"-u": {"%idle": "cpu", "%iowait": "iow"}, "-r": {"%memused": "mem"},
            "-S": {"%swpused": "swp"}, "-q": {"ldavg-1": "ld"}}
    rows = defaultdict(dict)
    files = sorted(glob.glob(os.path.join(sadir, "sa[0-9]*")), key=os.path.getmtime)
    for f in files:
        for opt, want in cols.items():
            head = None
            for line in run(["sadf", "-d", "-t", f, "--", opt]).splitlines():
                if line.startswith("#"):
                    head = line[2:].split(";")
                    continue
                if not head:
                    continue
                v = dict(zip(head, line.split(";")))
                if v.get("CPU") not in (None, "-1", "all"):
                    continue
                try:
                    ts = epoch(v["timestamp"])
                except (KeyError, ValueError):
                    continue
                if ts < t0:
                    continue
                for k, key in want.items():
                    x = num(v.get(k))
                    if x is not None:
                        rows[ts][key] = round(100 - x, 2) if key == "cpu" else x
    ts = sorted(rows)
    return {"t": ts, **{k: [rows[x].get(k) for x in ts] for k in ("cpu", "iow", "mem", "swp", "ld")}}


def incidents(t, bad):
    """連續的異常分鐘合併成一件：(開始, 結束, 分鐘數)。"""
    out, start, prev = [], None, None
    for ts, b in zip(t, bad):
        if b and start is None:
            start = ts
        if not b and start is not None:
            out.append([start, prev, round((prev - start) / 60) + 1])
            start = None
        prev = ts
    if start is not None:
        out.append([start, prev, round((prev - start) / 60) + 1])
    return out


def restarts(rs):
    total, last = 0, None
    for r in rs:
        if r is None:
            continue
        if last is not None:
            total += r - last if r >= last else r  # 主機重開機後計數歸零
        last = r
    return int(total)


def journal(since):
    errs = []
    for line in run(["journalctl", "-u", "dailyreport*", "-p", "err", "--since", since, "-q", "-o", "json"]).splitlines()[-300:]:
        try:
            j = json.loads(line)
        except ValueError:
            continue
        msg = j.get("MESSAGE")
        if isinstance(msg, list):
            msg = bytes(msg).decode("utf-8", "replace")
        ts = datetime.fromtimestamp(int(j.get("__REALTIME_TIMESTAMP", 0)) / 1e6).strftime("%Y-%m-%d %H:%M:%S")
        errs.append([ts, j.get("_SYSTEMD_UNIT") or j.get("SYSLOG_IDENTIFIER", ""), str(msg)[:500]])
    warn = {s: len(run(["journalctl", "-u", s, "-p", "warning", "--since", since, "-q", "-o", "cat"]).splitlines()) for s in SVCS}
    oom = [l for l in run(["journalctl", "-k", "--since", since, "-q", "-o", "short-iso"]).splitlines()
           if re.search(r"out of memory|oom-kill", l, re.I)]
    boots = run(["journalctl", "--list-boots", "-q", "--no-pager"]).strip().splitlines()[-10:]
    return {"errors": errs, "warn": warn, "oom": oom[-50:], "boots": boots}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.environ.get("STAB_CSV", "/var/log/stab-probe.csv"))
    ap.add_argument("--sadir", default="/var/log/sysstat")
    ap.add_argument("-o", "--out", default="stab-report.html")
    a = ap.parse_args()

    p = read_probe(a.csv)
    if not p["t"]:
        raise SystemExit(f"{a.csv} 沒有資料")
    t = p["t"]
    first = datetime.fromtimestamp(t[0], timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sar = read_sar(t[0], a.sadir)
    j = journal(first)
    fails = incidents(t, [c != "200" for c in p["code"]])
    codes = {}
    for inc in fails:
        codes[inc[0]] = sorted({c for ts, c in zip(t, p["code"]) if inc[0] <= ts <= inc[1]})
    svc_sum = {}
    for s in SVCS:
        d = p["svc"][s]
        mem = [m for m in d["mem"] if m is not None]
        svc_sum[s] = {"restarts": restarts(d["r"]), "down": sum(1 for u in d["up"] if u == 0),
                      "inc": incidents(t, [u == 0 for u in d["up"]]), "mem_max": max(mem) if mem else None,
                      "mem_first": mem[0] if mem else None, "mem_last": mem[-1] if mem else None,
                      "warn": j["warn"].get(s, 0)}
    ok = sum(1 for c in p["code"] if c == "200")
    rts = [r for r in p["rt"] if r is not None]
    data = {
        "meta": {"host": socket.gethostname(), "boot": run(["uptime", "-s"]).strip(),
                 "made": datetime.now().strftime("%Y-%m-%d %H:%M"), "cores": os.cpu_count(),
                 "first": first, "last": datetime.fromtimestamp(t[-1], timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                 "n": len(t), "avail": round(ok * 100 / len(t), 3), "fail_min": len(t) - ok,
                 "rt_avg": round(sum(rts) / len(rts), 3) if rts else None, "rt_max": max(rts) if rts else None},
        "probe": p, "sar": sar, "fails": [f + [codes[f[0]]] for f in fails], "svc": svc_sum,
        "journal": j, "df": run(["df", "-hP", "/", "/var", "/opt"]).strip().splitlines(),
    }
    tpl = TEMPLATE
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(tpl.replace("__TITLE__", html.escape(data["meta"]["host"])).replace("__DATA__", payload))
    print(f"已產生 {os.path.abspath(a.out)}（{os.path.getsize(a.out) // 1024} KB）")


TEMPLATE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>穩定性驗證 __TITLE__</title>
<style>
:root{color-scheme:light;--bg:#f4f4f2;--surface:#fcfcfb;--line:#e4e3df;--grid:#ecebe7;--text:#0b0b0b;--text2:#52514e;--muted:#7a7975;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--good:#0ca30c;--good-bg:#d9f0d9;--warn:#fab219;--crit:#d03b3b;--crit-bg:#fbe3e3}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--bg:#111110;--surface:#1a1a19;--line:#2e2e2c;--grid:#262624;--text:#f0efec;--text2:#c3c2b7;--muted:#8d8c85;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--good-bg:#173317;--crit-bg:#3a1a1a}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#111110;--surface:#1a1a19;--line:#2e2e2c;--grid:#262624;--text:#f0efec;--text2:#c3c2b7;--muted:#8d8c85;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--good-bg:#173317;--crit-bg:#3a1a1a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 system-ui,-apple-system,"Segoe UI","Microsoft JhengHei","PingFang TC",sans-serif}
main{max-width:1180px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:22px;margin:0 0 2px}h2{font-size:16px;margin:0 0 10px}h3{font-size:14px;margin:0;color:var(--text)}
.sub{color:var(--text2);margin:0 0 16px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px;min-width:0}
.verdict{display:flex;gap:12px;align-items:center;padding:14px 16px;border-radius:10px;margin-bottom:16px;font-weight:600}
.verdict.ok{background:var(--good-bg)}.verdict.bad{background:var(--crit-bg)}
.verdict .ic{font-size:20px}.verdict small{display:block;font-weight:400;color:var(--text2)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:16px}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.kpi b{display:block;font-size:26px;font-variant-numeric:tabular-nums;line-height:1.2}
.kpi span{color:var(--text2);font-size:13px}
.tag{display:inline-flex;align-items:center;gap:4px;font-size:12px;font-weight:600;padding:1px 8px;border-radius:999px;margin-top:4px}
.tag.ok{background:var(--good-bg);color:var(--text)}.tag.bad{background:var(--crit-bg);color:var(--text)}
.tag.ok::before{content:"✓";color:var(--good)}.tag.bad::before{content:"!";color:var(--crit);font-weight:800}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;position:sticky;top:0;z-index:5;background:var(--bg);padding:8px 0;margin-bottom:8px}
.bar span{color:var(--text2);margin-right:4px}
.chip{border:1px solid var(--line);background:var(--surface);color:var(--text);border-radius:999px;padding:4px 12px;font:inherit;cursor:pointer}
.chip.on{background:var(--text);color:var(--surface);border-color:var(--text)}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(460px,1fr));gap:16px}
@media (max-width:560px){.grid2{grid-template-columns:1fr}}
.grid2 .card{margin-bottom:0}
.chart{position:relative;margin-top:8px}
.chart svg{display:block;width:100%;overflow:visible}
.chart .ax{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.chart .gl{stroke:var(--grid);stroke-width:1}
.chart .base{stroke:var(--line);stroke-width:1}
.chart .ref{stroke:var(--text2);stroke-width:1;stroke-dasharray:4 3}
.chart .reflbl{fill:var(--text2);font-size:11px}
.chart .xh{stroke:var(--text2);stroke-width:1}
.chart .dot{stroke:var(--surface);stroke-width:2}
.chart .fail{fill:var(--crit)}
.chart .dlbl{font-size:11px;fill:var(--text2)}
.tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:12px;box-shadow:0 4px 14px rgb(0 0 0/.12);white-space:nowrap;z-index:3}
.tip i{display:inline-block;width:10px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle}
.tip b{font-variant-numeric:tabular-nums}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--text2);margin-top:4px}
.legend i{display:inline-block;width:14px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle}
.hint{color:var(--muted);font-size:12px;margin:6px 0 0}
.tl{display:grid;grid-template-columns:150px 1fr;gap:6px 10px;align-items:center;margin-top:8px}
.tl .nm{font-size:13px;color:var(--text2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tl svg{display:block;width:100%;height:18px}
.tl .up{fill:var(--good);opacity:.35}.tl .dn{fill:var(--crit)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--text2);font-weight:600;white-space:nowrap}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.scroll{overflow-x:auto}
td.msg{font-family:ui-monospace,Consolas,monospace;font-size:12px;word-break:break-all}
.bad-t{color:var(--crit);font-weight:600}
pre{margin:0;font-size:12px;white-space:pre-wrap;color:var(--text2)}
details summary{cursor:pointer;color:var(--text2)}
</style>
</head>
<body>
<main>
<h1>穩定性驗證報告</h1>
<p class="sub" id="sub"></p>
<div id="verdict"></div>
<div class="kpis" id="kpis"></div>

<div class="bar" role="group" aria-label="時間範圍"><span>時間範圍</span>
  <button class="chip on" data-r="0">全部</button><button class="chip" data-r="604800">最近 7 天</button>
  <button class="chip" data-r="86400">最近 24 小時</button><button class="chip" data-r="21600">最近 6 小時</button></div>

<section class="card"><h2>服務運作時間軸</h2>
  <div class="tl" id="tl"></div>
  <p class="legend"><span><i style="background:var(--good);opacity:.5"></i>運作中</span><span><i style="background:var(--crit)"></i>停止或重啟中</span><span>空白：沒有取樣</span></p></section>

<div class="grid2">
  <section class="card"><h3>網站回應時間（/healthz，秒）</h3><div class="chart" id="c-rt"></div>
    <p class="hint">紅色直線：沒有回應 200 的時間點</p></section>
  <section class="card"><h3>各服務記憶體（MB）</h3><div class="chart" id="c-svcmem"></div></section>
  <section class="card"><h3>CPU 使用率（%）</h3><div class="chart" id="c-cpu"></div></section>
  <section class="card"><h3>I/O 等待（%）</h3><div class="chart" id="c-iow"></div>
    <p class="hint">常超過 20% 代表磁碟跟不上</p></section>
  <section class="card"><h3>記憶體使用率（%）</h3><div class="chart" id="c-mem"></div></section>
  <section class="card"><h3>Swap 使用率（%）</h3><div class="chart" id="c-swp"></div>
    <p class="hint">持續上升代表記憶體不夠</p></section>
  <section class="card"><h3>系統負載（1 分鐘平均）</h3><div class="chart" id="c-ld"></div>
    <p class="hint">長時間超過虛線（CPU 核心數）代表忙不過來</p></section>
  <section class="card"><h3>磁碟空間</h3><div class="scroll" id="df"></div></section>
</div>
<p class="hint" id="bucket-hint"></p>

<section class="card" style="margin-top:16px"><h2>每日摘要</h2><div class="scroll" id="daily"></div></section>
<section class="card"><h2>服務</h2><div class="scroll" id="svc"></div></section>
<section class="card"><h2>網站沒有回應的時段</h2><div class="scroll" id="fails"></div></section>
<section class="card"><h2>系統事件</h2><div id="events"></div></section>
<section class="card"><h2>錯誤訊息（最近 300 則）</h2><div class="scroll" id="errors"></div></section>
</main>
<script id="data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById("data").textContent);
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const p2 = (n) => String(n).padStart(2, "0");
const fmt = (t, sec) => { const d = new Date(t * 1000); return `${d.getUTCFullYear()}-${p2(d.getUTCMonth()+1)}-${p2(d.getUTCDate())} ${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}${sec ? ":" + p2(d.getUTCSeconds()) : ""}`; };
const fmtNum = (v, dp) => v == null ? "—" : (+v).toLocaleString("zh-TW", {maximumFractionDigits: dp ?? 1});
const M = D.meta, P = D.probe, S = D.sar, SV = D.svc;
const tEnd = Math.max(P.t[P.t.length - 1], S.t.length ? S.t[S.t.length - 1] : 0);
let range = 0;

// ---- 結論與 KPI ----
const restartsTotal = Object.values(SV).reduce((a, s) => a + s.restarts, 0);
const downTotal = Object.values(SV).reduce((a, s) => a + s.down, 0);
const swpMax = Math.max(0, ...S.swp.filter((v) => v != null));
const problems = [];
if (M.fail_min) problems.push(`網站有 ${M.fail_min} 分鐘沒有正常回應`);
if (restartsTotal) problems.push(`服務重啟 ${restartsTotal} 次`);
if (downTotal) problems.push(`服務停止共 ${downTotal} 分鐘`);
if (D.journal.oom.length) problems.push(`記憶體不足被系統殺掉 ${D.journal.oom.length} 次`);
const hours = (P.t[P.t.length - 1] - P.t[0]) / 3600;
$("sub").textContent = `${M.host} · ${M.first} ～ ${M.last}（${hours >= 48 ? (hours / 24).toFixed(1) + " 天" : hours.toFixed(1) + " 小時"}，${M.n.toLocaleString()} 次取樣）· 本次開機 ${M.boot} · 產生於 ${M.made}`;
$("verdict").innerHTML = problems.length
  ? `<div class="verdict bad"><span class="ic" aria-hidden="true">⚠</span><div>期間內有異常<small>${esc(problems.join("；"))}。往下看時間軸與錯誤訊息找原因。</small></div></div>`
  : `<div class="verdict ok"><span class="ic" aria-hidden="true">✓</span><div>期間內運作穩定<small>網站一直正常回應，服務沒有停止或重啟，沒有記憶體不足。</small></div></div>`;
const kpi = (v, label, ok, note) => `<div class="kpi"><b>${v}</b><span>${label}</span><br><span class="tag ${ok ? "ok" : "bad"}">${note ?? (ok ? "正常" : "要注意")}</span></div>`;
$("kpis").innerHTML = [
  kpi(M.avail.toFixed(2) + "%", "網站可用率", M.avail >= 100, M.avail >= 100 ? "正常" : `失敗 ${M.fail_min} 分鐘`),
  kpi(restartsTotal, "服務重啟次數", !restartsTotal),
  kpi(downTotal, "服務停止分鐘", !downTotal),
  kpi(D.journal.oom.length, "記憶體不足（OOM）", !D.journal.oom.length),
  kpi(fmtNum(M.rt_max, 3) + " 秒", "最慢回應", M.rt_max == null || M.rt_max < 3, M.rt_max < 3 ? "正常" : "偏慢"),
  kpi(fmtNum(swpMax) + "%", "Swap 最高", swpMax < 50),
].join("");

// ---- 圖表 ----
function niceMax(v) { if (!(v > 0)) return 1; const raw = v / 4, e = Math.pow(10, Math.floor(Math.log10(raw))); for (const m of [1, 2, 2.5, 5, 10]) if (m * e >= raw) return 4 * m * e; return 40 * e; }
function timeTicks(t0, t1, w) {
  const span = t1 - t0, want = Math.max(2, Math.floor(w / 90));
  const steps = [600, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800];
  const step = steps.find((s) => span / s <= want) || 604800;
  const out = []; for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) out.push(t);
  return {out, step};
}
function tickLabel(t, step) { const s = fmt(t); return step >= 86400 ? s.slice(5, 10) : (t % 86400 === 0 ? s.slice(5, 10) : s.slice(11, 16)); }

// 依畫面寬度分桶，每桶取最高值（保留尖峰）
function bucket(t, vals, t0, t1, n) {
  const w = (t1 - t0) / n || 1, out = new Array(n).fill(null);
  for (let i = 0; i < t.length; i++) {
    if (t[i] < t0 || t[i] > t1) continue;
    const v = vals[i]; if (v == null) continue;
    const k = Math.min(n - 1, Math.floor((t[i] - t0) / w));
    if (out[k] == null || v > out[k]) out[k] = v;
  }
  return {w, out};
}

const charts = [];
function lineChart(id, opt) {
  const el = $(id); charts.push(() => draw());
  const tip = document.createElement("div"); tip.className = "tip"; tip.hidden = true;
  function draw() {
    const W = el.clientWidth || 500, H = 190, L = 44, R = opt.series.length > 1 ? 110 : 12, T = 10, B = 24;
    const pw = W - L - R, ph = H - T - B;
    const t1 = tEnd, t0 = range ? Math.max(t1 - range, opt.t[0] ?? t1) : (opt.t[0] ?? t1);
    if (!opt.t.length) { el.innerHTML = `<p class="hint">沒有資料</p>`; return; }
    const n = Math.max(10, Math.min(Math.floor(pw / 2), Math.ceil((t1 - t0) / 60) || 1));
    const bs = opt.series.map((s) => bucket(opt.t, s.v, t0, t1, n));
    let vmax = opt.max ?? niceMax(Math.max(opt.ref ?? 0, ...bs.flatMap((b) => b.out.filter((v) => v != null))) * 1.05);
    const x = (k) => L + (k + 0.5) / n * pw, y = (v) => T + ph - Math.min(v, vmax) / vmax * ph;
    const tx = (t) => L + (t - t0) / (t1 - t0 || 1) * pw;
    let s = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="${esc(opt.label)}">`;
    for (let i = 0; i <= 4; i++) { const v = vmax * i / 4, yy = y(v); s += `<line class="${i ? "gl" : "base"}" x1="${L}" x2="${L + pw}" y1="${yy}" y2="${yy}"/><text class="ax" x="${L - 6}" y="${yy + 4}" text-anchor="end">${fmtNum(v, vmax < 2 ? 2 : vmax < 10 ? 1 : 0)}</text>`; }
    const tk = timeTicks(t0, t1, pw);
    tk.out.forEach((t) => { s += `<text class="ax" x="${tx(t)}" y="${H - 6}" text-anchor="middle">${tickLabel(t, tk.step)}</text>`; });
    if (opt.ref != null) s += `<line class="ref" x1="${L}" x2="${L + pw}" y1="${y(opt.ref)}" y2="${y(opt.ref)}"/><text class="reflbl" x="${L + pw - 4}" y="${y(opt.ref) - 4}" text-anchor="end">${esc(opt.refLabel)}</text>`;
    if (opt.marks) opt.marks.forEach((t) => { if (t >= t0 && t <= t1) s += `<rect class="fail" x="${tx(t) - 1}" y="${T}" width="2" height="${ph}" opacity=".35"/>`; });
    const ends = [];
    bs.forEach((b, si) => {
      let d = "", pen = false, last = null;
      b.out.forEach((v, k) => { if (v == null) { pen = false; return; } d += (pen ? "L" : "M") + x(k).toFixed(1) + "," + y(v).toFixed(1); pen = true; last = [x(k), y(v), v]; });
      s += `<path d="${d}" fill="none" stroke="var(${opt.series[si].c})" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
      if (last && opt.series.length > 1) ends.push({y: last[1], si, v: last[2]});
    });
    // 多條線時在右側直接標名稱（避免重疊）
    ends.sort((a, b) => a.y - b.y); let prevY = -99;
    ends.forEach((e) => { const yy = Math.max(e.y, prevY + 13); prevY = yy; s += `<text class="dlbl" x="${L + pw + 6}" y="${yy + 4}">${esc(opt.series[e.si].name)}</text>`; });
    s += `<line class="xh" x1="0" x2="0" y1="${T}" y2="${T + ph}" visibility="hidden"/><g class="dots"></g>`;
    s += `<rect x="${L}" y="${T}" width="${pw}" height="${ph}" fill="transparent" class="hit"/></svg>`;
    el.innerHTML = s;
    if (opt.series.length > 1) el.insertAdjacentHTML("beforeend", `<div class="legend">${opt.series.map((x) => `<span><i style="background:var(${x.c})"></i>${esc(x.name)}</span>`).join("")}</div>`);
    el.appendChild(tip);
    const svg = el.querySelector("svg"), xh = svg.querySelector(".xh"), dots = svg.querySelector(".dots");
    const move = (ev) => {
      const r = svg.getBoundingClientRect(), mx = (ev.clientX - r.left) * W / r.width;
      let k = Math.round((mx - L) / pw * n - 0.5); k = Math.max(0, Math.min(n - 1, k));
      const xx = x(k); xh.setAttribute("x1", xx); xh.setAttribute("x2", xx); xh.setAttribute("visibility", "visible");
      const tA = t0 + k * bs[0].w, tB = tA + bs[0].w;
      let rows = "", dd = "";
      bs.forEach((b, si) => { const v = b.out[k]; if (v != null) dd += `<circle class="dot" cx="${xx}" cy="${y(v)}" r="4" fill="var(${opt.series[si].c})"/>`;
        rows += `<div><i style="background:var(${opt.series[si].c})"></i>${esc(opt.series[si].name)} <b>${fmtNum(v, opt.dp)}</b>${opt.unit ?? ""}</div>`; });
      dots.innerHTML = dd;
      tip.innerHTML = `<div style="color:var(--text2)">${fmt(tA)}${bs[0].w > 90 ? " ～ " + fmt(tB).slice(11) + "（最高值）" : ""}</div>${rows}`;
      tip.hidden = false;
      const px = (xx / W) * r.width; tip.style.left = Math.min(px + 12, r.width - tip.offsetWidth - 4) + "px"; tip.style.top = "8px";
    };
    svg.addEventListener("pointermove", move); svg.addEventListener("pointerdown", move);
    svg.addEventListener("pointerleave", () => { tip.hidden = true; xh.setAttribute("visibility", "hidden"); dots.innerHTML = ""; });
    const bw = (t1 - t0) / n;
    $("bucket-hint").textContent = bw > 90 ? `圖表每一點為約 ${Math.round(bw / 60)} 分鐘內的最高值，尖峰不會被平均掉；縮小時間範圍可看到每分鐘的細節。滑鼠移到圖上看數值。` : "圖表為每分鐘取樣。滑鼠移到圖上看數值。";
  }
}
const failTimes = P.t.filter((t, i) => P.code[i] !== "200");
lineChart("c-rt", {label: "回應時間", t: P.t, series: [{name: "回應時間", v: P.rt, c: "--s1"}], unit: " 秒", dp: 3, marks: failTimes});
const svcColors = ["--s1", "--s2", "--s3", "--s4"];
lineChart("c-svcmem", {label: "各服務記憶體", t: P.t, unit: " MB", series: Object.keys(P.svc).map((s, i) => ({name: s.replace("dailyreport-", "").replace(/^dailyreport$/, "web"), v: P.svc[s].mem, c: svcColors[i]}))});
lineChart("c-cpu", {label: "CPU", t: S.t, max: 100, unit: "%", series: [{name: "CPU", v: S.cpu, c: "--s1"}]});
lineChart("c-iow", {label: "I/O 等待", t: S.t, unit: "%", ref: 20, refLabel: "20%", series: [{name: "I/O 等待", v: S.iow, c: "--s1"}]});
lineChart("c-mem", {label: "記憶體", t: S.t, max: 100, unit: "%", series: [{name: "記憶體", v: S.mem, c: "--s1"}]});
lineChart("c-swp", {label: "Swap", t: S.t, unit: "%", series: [{name: "Swap", v: S.swp, c: "--s1"}]});
lineChart("c-ld", {label: "負載", t: S.t, ref: M.cores, refLabel: `${M.cores} 核心`, dp: 2, series: [{name: "負載", v: S.ld, c: "--s1"}]});

// 服務時間軸
function timeline() {
  const t1 = tEnd, t0 = range ? Math.max(t1 - range, P.t[0]) : P.t[0], W = 1000, sx = (t) => (t - t0) / (t1 - t0 || 1) * W;
  $("tl").innerHTML = Object.keys(P.svc).map((s) => {
    const up = P.svc[s].up; let r = "";
    for (let i = 0; i < P.t.length; i++) {
      if (P.t[i] < t0 || up[i] == null) continue;
      const a = sx(P.t[i]), b = sx(Math.min(P.t[i + 1] ?? P.t[i] + 60, P.t[i] + 90));
      r += `<rect class="${up[i] ? "up" : "dn"}" x="${a.toFixed(2)}" width="${Math.max(up[i] ? 0 : 2, b - a).toFixed(2)}" y="${up[i] ? 4 : 0}" height="${up[i] ? 10 : 18}"><title>${fmt(P.t[i])} ${up[i] ? "運作中" : "停止"}</title></rect>`;
    }
    const sv = SV[s];
    return `<div class="nm" title="${s}">${s}${sv.down ? ` <span class="bad-t">✕ ${sv.inc.length} 次</span>` : ""}</div><svg viewBox="0 0 ${W} 18" preserveAspectRatio="none" role="img" aria-label="${s} 運作時間軸">${r}</svg>`;
  }).join("");
}
charts.push(timeline);
const redraw = () => charts.forEach((f) => f());
document.querySelectorAll(".chip[data-r]").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".chip[data-r]").forEach((x) => x.classList.toggle("on", x === b)); range = +b.dataset.r; redraw();
}));
let rz; window.addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(redraw, 150); });
redraw();

// ---- 表格 ----
const table = (head, rows, numCols = []) => rows.length ? `<table><thead><tr>${head.map((h, i) => `<th class="${numCols.includes(i) ? "n" : ""}">${h}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c, i) => `<td class="${numCols.includes(i) ? "n" : ""}">${c}</td>`).join("")}</tr>`).join("")}</tbody></table>` : `<p class="hint">沒有</p>`;

// 每日摘要
const days = {};
const day = (t) => fmt(t).slice(0, 10);
const D0 = (k) => days[k] ??= {n: 0, ok: 0, rt: 0, cpu: [], iow: 0, mem: 0, swp: 0, ld: 0, r: 0, dn: 0};
P.t.forEach((t, i) => { const d = D0(day(t)); d.n++; if (P.code[i] === "200") d.ok++; if (P.rt[i] > d.rt) d.rt = P.rt[i];
  Object.keys(P.svc).forEach((s) => { if (P.svc[s].up[i] === 0) d.dn++; const a = P.svc[s].r[i - 1], b = P.svc[s].r[i]; if (i && a != null && b != null) d.r += b >= a ? b - a : b; }); });
S.t.forEach((t, i) => { const d = D0(day(t)); if (S.cpu[i] != null) d.cpu.push(S.cpu[i]); d.iow = Math.max(d.iow, S.iow[i] ?? 0); d.mem = Math.max(d.mem, S.mem[i] ?? 0); d.swp = Math.max(d.swp, S.swp[i] ?? 0); d.ld = Math.max(d.ld, S.ld[i] ?? 0); });
const bad = (cond, v) => cond ? `<span class="bad-t">${v}</span>` : v;
$("daily").innerHTML = table(["日期", "網站可用率", "最慢回應", "重啟", "停止分鐘", "CPU 平均 / 最高", "I/O 等待最高", "記憶體最高", "Swap 最高", "負載最高"],
  Object.keys(days).sort().map((k) => { const d = days[k], cpuAvg = d.cpu.length ? d.cpu.reduce((a, b) => a + b, 0) / d.cpu.length : null;
    return [k, d.n ? bad(d.ok < d.n, (d.ok * 100 / d.n).toFixed(2) + "%") : "—", fmtNum(d.rt, 3) + " 秒", bad(d.r, d.r), bad(d.dn, d.dn),
      d.cpu.length ? `${fmtNum(cpuAvg, 0)}% / ${fmtNum(Math.max(...d.cpu), 0)}%` : "—", bad(d.iow > 20, fmtNum(d.iow) + "%"), bad(d.mem > 90, fmtNum(d.mem) + "%"), bad(d.swp > 50, fmtNum(d.swp) + "%"), bad(d.ld > M.cores, fmtNum(d.ld, 2))]; }),
  [1, 2, 3, 4, 5, 6, 7, 8, 9]);

$("svc").innerHTML = table(["服務", "重啟次數", "停止分鐘", "停止次數", "記憶體 開始 → 最後 / 最高（MB）", "警告以上訊息"],
  Object.entries(SV).map(([s, v]) => [s, bad(v.restarts, v.restarts), bad(v.down, v.down), bad(v.inc.length, v.inc.length),
    `${fmtNum(v.mem_first, 0)} → ${fmtNum(v.mem_last, 0)} / ${fmtNum(v.mem_max, 0)}`, v.warn]), [1, 2, 3, 4, 5]) +
  Object.entries(SV).filter(([, v]) => v.inc.length).map(([s, v]) => `<details style="margin-top:8px"><summary>${s} 停止時段（${v.inc.length}）</summary>${table(["開始", "結束", "分鐘"], v.inc.slice(-100).map((i) => [fmt(i[0]), fmt(i[1]), i[2]]), [2])}</details>`).join("") +
  `<p class="hint">記憶體一天比一天高、沒有回落，可能有記憶體洩漏。</p>`;

$("fails").innerHTML = table(["開始", "結束", "分鐘", "回應碼"], D.fails.slice(-200).reverse().map((f) => [fmt(f[0]), fmt(f[1]), f[2], f[3].map((c) => c === "000" ? "000（沒有回應）" : c).join("、")]), [2]);

$("df").innerHTML = (() => { const [h, ...rows] = D.df; if (!h) return `<p class="hint">沒有資料</p>`;
  return table(["磁碟", "容量", "已用", "可用", "使用率", "掛載點"], rows.filter((r, i, a) => a.findIndex((x) => x.split(/\s+/)[0] === r.split(/\s+/)[0]) === i).map((r) => { const c = r.split(/\s+/); return [esc(c[0]), c[1], c[2], c[3], bad(parseInt(c[4]) >= 85, c[4]), esc(c.slice(5).join(" "))]; }), [1, 2, 3, 4]); })();

const J = D.journal;
$("events").innerHTML = `<p>記憶體不足被系統殺掉（OOM）：<b class="${J.oom.length ? "bad-t" : ""}">${J.oom.length} 次</b></p>` +
  (J.oom.length ? `<details><summary>OOM 紀錄</summary><pre>${esc(J.oom.join("\n"))}</pre></details>` : "") +
  `<details open style="margin-top:8px"><summary>開機紀錄（最近 10 次）</summary><pre>${esc(J.boots.join("\n") || "沒有資料（journal 可能沒有永久保存）")}</pre></details>`;
$("errors").innerHTML = table(["時間", "服務", "訊息"], J.errors.slice().reverse().map((e) => [`<span style="white-space:nowrap">${esc(e[0])}</span>`, esc(e[1]), `<span class="msg">${esc(e[2])}</span>`]));
document.querySelectorAll("#errors td:nth-child(3)").forEach((td) => td.classList.add("msg"));
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
PYEOF
}

case "${1:-install}" in
  install) shift; cmd_install "$@" ;;
  status) cmd_status ;;
  report) shift; cmd_report "$@" ;;
  uninstall) shift; cmd_uninstall "$@" ;;
  *) echo "用法：install [--no-netdata] | status | report [檔名.html] | uninstall [--keep-data]"; exit 1 ;;
esac
