#!/usr/bin/env bash
#
# CFXD 日報系統管理腳本：安裝、升級、健檢、備份、還原。
#
#   sudo bash /opt/dailyreport/scripts/dailyreport.sh install [選項]   全新安裝（可重跑，不會覆蓋資料與 .env）
#   sudo bash /opt/dailyreport/scripts/dailyreport.sh upgrade           備份 → 更新程式 → 相依套件 → 重啟 → 健檢
#   sudo bash /opt/dailyreport/scripts/dailyreport.sh doctor            檢查部署結果，失敗項目附修正方式
#   sudo bash /opt/dailyreport/scripts/dailyreport.sh backup            立即備份（平常由系統設定頁排程）
#   sudo bash /opt/dailyreport/scripts/dailyreport.sh restore <資料夾>  停止服務後從備份還原（網頁無法開啟時用）
#   sudo bash /opt/dailyreport/scripts/dailyreport.sh template <xlsx>   放入 CheckList 範本（也可以在系統設定頁上傳）
#   sudo bash /opt/dailyreport/scripts/dailyreport.sh restart | status
#
# 日常設定（AD、帳號、備份、健康檢查、範本、HTTPS 憑證、AI）都在網頁「系統設定」，不需要登入主機。
# 這個腳本只負責安裝、升級（升級後自動 doctor 驗證）與網頁無法開啟時的救援。
#
# install 選項（都可省略，之後在系統設定頁設定）：
#   --hostname <名稱>        網址主機名稱（預設本機 FQDN）
#   --tls self-signed|custom|none   預設 self-signed（自動產生自簽憑證）；custom 需搭配 --cert / --key
#   --cert <檔案> --key <檔案>
#   --ldap-url <ldaps://…> --ad-domain <網域> --base-dn <DN> --required-group <DN> --approver-group <DN>
#   --approver-users <帳號,帳號>
#   --template <CheckList.xlsx>
#   --workers <數量>         gunicorn worker 數（預設依 CPU 與記憶體自動決定）
#   --admin-user <帳號>      本機緊急帳號名稱（預設 sysadmin）
#   --skip-ocr-model         不預先下載 OCR 模型（離線安裝時用，之後再跑 install 補上）
set -euo pipefail
# 1b42019 以前的 backup 會把 umask 改成 077 且沒有還原，升級時由舊版 exec 過來會繼承：一律重設
umask 022

DIR="${DR_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BRANCH="${DR_BRANCH:-main}"
APP_USER=dailyreport
APP_GROUP=www-data
ETC=/etc/dailyreport
BACKUP_DIR="${DR_BACKUP_DIR:-/var/backups/dailyreport}"
VENV="$DIR/.venv"
ENV_FILE="$DIR/.env"
INSTANCE="$DIR/instance"
PORT=8000
SERVICES=(dailyreport dailyreport-ocr dailyreport-ai dailyreport-maint)
UPLOAD_CEILING_MB=200   # 與 app/sysconf.py 相同；實際上限在系統設定頁設定
BOOTSTRAP_URL="https://raw.githubusercontent.com/nox913106/CFXD_DailyReportDocSys/main/scripts/bootstrap.sh"

if [[ -t 1 ]]; then G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; B=$'\e[1m'; D=$'\e[2m'; N=$'\e[0m'; else G='' Y='' R='' B='' D='' N=''; fi
step() { echo "${B}==> $*${N}"; }
info() { echo "    $*"; }
warn() { echo "${Y}[警告]${N} $*" >&2; }
die() { echo "${R}[錯誤]${N} $*" >&2; exit 1; }
need_root() { [[ "$(id -u)" -eq 0 ]] || die "請用 root 執行（sudo）。"; }
as_app() { runuser -u "$APP_USER" -- env -C "$DIR" "$@"; }
flask_app() { as_app "$VENV/bin/flask" --app wsgi "$@"; }

# ---------------------------------------------------------------- 安裝設定（記住主機名稱、TLS 等，升級時沿用）

load_conf() {
  HOST="" TLS="self-signed" CERT="" KEY="" WORKERS="" ADMIN_USER="sysadmin"
  # shellcheck disable=SC1091
  [[ -f "$ETC/install.conf" ]] && source "$ETC/install.conf"
  return 0
}

save_conf() {
  install -d -m 750 "$ETC"
  cat > "$ETC/install.conf" <<EOF
# 由 dailyreport.sh install 產生；升級時沿用。改完後執行 dailyreport.sh install 套用。
HOST='$HOST'
TLS='$TLS'
CERT='$CERT'
KEY='$KEY'
WORKERS='$WORKERS'
ADMIN_USER='$ADMIN_USER'
EOF
  chmod 640 "$ETC/install.conf"
}

set_env() {  # set_env KEY VALUE：更新或新增 .env 的一行（值可含任何字元）
  local k="$1" v="$2" tmp
  tmp="$(mktemp)"
  K="$k" V="$v" awk 'BEGIN{k=ENVIRON["K"]; v=ENVIRON["V"]; done=0}
    $0 ~ "^"k"=" {print k"="v; done=1; next} {print}
    END{if(!done) print k"="v}' "$ENV_FILE" > "$tmp"
  cat "$tmp" > "$ENV_FILE"
  rm -f "$tmp"
}

get_env() { [[ -f "$ENV_FILE" ]] && sed -n "s/^$1=//p" "$ENV_FILE" | tail -n1 || true; }

backup_root() {  # 備份位置（系統設定頁設定）
  local d
  d="$(flask_app config-get backup_dir 2>/dev/null | tail -n1 || true)"
  echo "${d:-$BACKUP_DIR}"
}

db_path() {
  local url
  url="$(get_env DATABASE_URL)"
  url="${url:-sqlite:///dailyreport.db}"
  url="${url#sqlite:///}"
  [[ "$url" == /* ]] && echo "$url" || echo "$INSTANCE/$url"
}

# ---------------------------------------------------------------- install 步驟

preflight() {
  step "檢查主機環境"
  need_root
  [[ -d /run/systemd/system ]] || die "需要 systemd（容器內請改用完整虛擬機，或以 systemd 為 PID 1 的容器）。"
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID}:${VERSION_ID}" in
    ubuntu:24.04|ubuntu:26.04|debian:13) info "作業系統：$PRETTY_NAME" ;;
    *) if [[ "${DR_SKIP_OS_CHECK:-0}" == 1 ]]; then warn "未驗證的作業系統 $PRETTY_NAME（DR_SKIP_OS_CHECK=1）"
       else die "支援 Ubuntu 24.04 / 26.04、Debian 13（需要 Python 3.12 以上），目前是 $PRETTY_NAME。設定 DR_SKIP_OS_CHECK=1 可強制安裝。"; fi ;;
  esac
  case "$(uname -m)" in x86_64|aarch64) ;; *) die "不支援的 CPU 架構 $(uname -m)（需要 x86_64 或 aarch64）。" ;; esac
  local mem disk
  mem=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
  disk=$(df -Pm "$(dirname "$DIR")" | awk 'NR==2 {print $4}')
  info "記憶體 ${mem} MB、可用磁碟 ${disk} MB"
  (( mem >= 1800 )) || warn "記憶體少於 2 GB：截圖 OCR 約需 1 GB，建議 4 GB 以上。"
  (( disk >= 3000 )) || die "$(dirname "$DIR") 可用空間不足 3 GB。"
}

install_packages() {
  step "安裝系統套件"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends \
    python3 python3-venv python3-pip nginx sqlite3 git curl ca-certificates openssl fontconfig \
    fonts-crosextra-carlito fonts-liberation fonts-dejavu-core fonts-noto-cjk \
    libgl1 libglib2.0-0 libgomp1 >/dev/null
  local py
  py=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' || die "需要 Python 3.12 以上，目前是 $py。"
  info "Python $py、nginx $(nginx -v 2>&1 | sed 's/.*\///')"
}

create_user() {
  step "建立系統帳號 $APP_USER"
  if id "$APP_USER" >/dev/null 2>&1; then info "已存在"
  else useradd --system --home-dir "$DIR" --no-create-home --shell /usr/sbin/nologin --gid "$APP_GROUP" "$APP_USER"; info "已建立"; fi
}

setup_venv() {
  step "安裝 Python 套件（第一次約需數分鐘）"
  [[ -x "$VENV/bin/python" ]] || python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --disable-pip-version-check --upgrade pip
  "$VENV/bin/pip" install -q --disable-pip-version-check -r "$DIR/requirements.txt"
  info "$("$VENV/bin/python" --version)，$("$VENV/bin/pip" list 2>/dev/null | wc -l) 個套件"
}

setup_dirs() {
  step "建立資料夾與權限"
  install -d -o "$APP_USER" -g "$APP_GROUP" -m 750 "$INSTANCE" "$INSTANCE/uploads" "$INSTANCE/templates" \
    "$INSTANCE/fonts" "$INSTANCE/check_reports" "$INSTANCE/pdf_cache"
  chown -R "$APP_USER:$APP_GROUP" "$INSTANCE"
  # 程式碼屬於 root（應用程式只能讀），nginx 需要讀 static
  chmod 755 "$DIR"
  # 修正舊版升級時以 umask 077 寫出的程式檔（服務帳號讀不到會無法啟動）
  find "$DIR" \( -path "$INSTANCE" -o -path "$VENV" -o -path "$ENV_FILE" \) -prune -o -exec chmod go+rX {} +
  # 預設備份位置：由維護背景服務（服務帳號）寫入，其他人不能讀
  install -d -o "$APP_USER" -g root -m 700 "$BACKUP_DIR"
  chown -R "$APP_USER:root" "$BACKUP_DIR"
  info "資料：$INSTANCE（擁有者 $APP_USER）"
}

write_env() {
  step "設定檔 $ENV_FILE"
  if [[ ! -f "$ENV_FILE" ]]; then
    cat > "$ENV_FILE" <<EOF
# 由 dailyreport.sh install 產生的基礎設施設定，平常不需要修改。
# AD、上傳上限、備份、健康檢查、範本、HTTPS 憑證、AI 都在網頁「系統設定」設定（存在資料庫）。
SECRET_KEY=$(openssl rand -hex 32)
DATABASE_URL=sqlite:///$INSTANCE/dailyreport.db
AUTH_MODE=ldap

# nginx 反向代理與 HTTPS
BEHIND_PROXY=1
COOKIE_SECURE=1
EOF
    info "已產生（含隨機 SECRET_KEY）"
  else
    info "已存在，只更新這次指定的項目"
  fi
  set_env BEHIND_PROXY 1
  set_env COOKIE_SECURE "$([[ "$TLS" == none ]] && echo 0 || echo 1)"
  chown root:"$APP_GROUP" "$ENV_FILE"
  chmod 640 "$ENV_FILE"
}

apply_options() {  # install 帶入的 AD 選項寫進資料庫（與系統設定頁同一份設定）
  local pair key var n=0
  for pair in ldap_url=OPT_LDAP_URL ad_domain=OPT_AD_DOMAIN ldap_base_dn=OPT_BASE_DN \
              ldap_required_group=OPT_REQUIRED_GROUP ldap_approver_group=OPT_APPROVER_GROUP approver_users=OPT_APPROVER_USERS; do
    key="${pair%%=*}" var="${pair#*=}"
    [[ -n "${!var:-}" ]] || continue
    flask_app config-set "$key" "${!var}" >/dev/null
    n=$((n + 1))
  done
  (( n )) && info "AD 設定已寫入系統設定（$n 項）"
  if [[ "$(flask_app config-get ldap_url 2>/dev/null | tail -n1)" == "" ]]; then
    warn "尚未設定 AD：AD 帳號暫時無法登入。用本機緊急帳號登入後，在「系統設定 → AD 驗證」設定。"
  fi
  return 0
}

install_template() {
  local src="$1"
  [[ -f "$src" ]] || die "找不到範本 $src"
  install -o "$APP_USER" -g "$APP_GROUP" -m 640 "$src" "$INSTANCE/templates/checklist.xlsx"
  rm -rf "${INSTANCE:?}/pdf_cache/"*
  info "CheckList 範本：$INSTANCE/templates/checklist.xlsx"
}

init_app() {
  step "初始化資料庫與本機緊急帳號"
  local pw out
  pw="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 20)"
  out="$(as_app env DR_ADMIN_PASSWORD="$pw" "$VENV/bin/flask" --app wsgi create-admin --if-missing --username "$ADMIN_USER" 2>&1)" \
    || die "初始化失敗：$out"
  if [[ "$out" == *略過* ]]; then
    info "已有本機緊急帳號，不變更密碼"
    ADMIN_PW=""
  else
    install -d -m 700 "$ETC"
    umask 077
    printf '%s\n' "帳號：$ADMIN_USER" "密碼：$pw" > "$ETC/.admin-initial-password"
    umask 022
    ADMIN_PW="$pw"
    info "已建立 $ADMIN_USER（密碼存在 $ETC/.admin-initial-password，僅 root 可讀）"
  fi
}

download_ocr_model() {
  step "下載截圖 OCR 模型"
  if [[ "${OPT_SKIP_OCR:-0}" == 1 ]]; then warn "略過（--skip-ocr-model）"; return; fi
  "$VENV/bin/python" -c "from rapidocr import RapidOCR; RapidOCR()" >/dev/null 2>&1 \
    || die "OCR 模型下載失敗（需要連線到模型下載站）。可加 --skip-ocr-model 先完成安裝，之後再重跑 install。"
  info "完成"
}

setup_tls() {
  step "HTTPS 憑證"
  case "$TLS" in
    none) warn "未使用 HTTPS（--tls none），密碼會以明文傳送，只建議在測試環境使用。" ;;
    custom)
      [[ -f "$CERT" && -f "$KEY" ]] || die "--tls custom 需要 --cert 與 --key 檔案。"
      info "使用 $CERT" ;;
    self-signed)
      CERT="$ETC/tls/cert.pem" KEY="$ETC/tls/key.pem"
      if [[ -f "$CERT" ]]; then info "已有自簽憑證，沿用"
      else
        install -d -m 750 "$ETC/tls"
        local ip san
        ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
        san="DNS:$HOST,DNS:localhost,IP:127.0.0.1${ip:+,IP:$ip}"
        openssl req -x509 -newkey rsa:2048 -nodes -days 825 -subj "/CN=$HOST" -addext "subjectAltName=$san" \
          -keyout "$KEY" -out "$CERT" >/dev/null 2>&1
        chmod 600 "$KEY"
        info "已產生自簽憑證（$san，825 天）。正式使用建議換成公司 CA 簽發的憑證：--tls custom --cert … --key …"
      fi ;;
    *) die "--tls 只能是 self-signed、custom 或 none" ;;
  esac
}

setup_nginx() {
  step "設定 nginx"
  local conf=/etc/nginx/sites-available/dailyreport v6=""
  # 停用 IPv6 的主機不能 listen [::]
  [[ -s /proc/net/if_inet6 ]] && v6=1
  {
    echo "# 由 dailyreport.sh install 產生，重跑 install 會覆蓋。"
    if [[ "$TLS" != none ]]; then
      cat <<EOF
server {
    listen 80 default_server;
${v6:+    listen [::]:80 default_server;}
    server_name $HOST _;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl default_server;
${v6:+    listen [::]:443 ssl default_server;}
    ssl_certificate $CERT;
    ssl_certificate_key $KEY;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:dailyreport:10m;
EOF
      # 不送 HSTS：憑證可在系統設定頁改回自簽，HSTS 會讓瀏覽器無法略過自簽憑證的警告
    else
      cat <<EOF
server {
    listen 80 default_server;
${v6:+    listen [::]:80 default_server;}
EOF
    fi
    cat <<EOF
    server_name $HOST _;
    server_tokens off;
    client_max_body_size ${UPLOAD_CEILING_MB}m;   # 實際上限在系統設定頁設定

    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options SAMEORIGIN always;
    add_header Referrer-Policy same-origin always;

    location /static/ {
        alias $DIR/app/static/;
        expires 7d;
    }

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_read_timeout 180s;   # PDF 第一次產生約 20 秒
    }
}
EOF
  } > "$conf"
  ln -sf "$conf" /etc/nginx/sites-enabled/dailyreport
  # 系統預設站台也是 default_server，會衝突
  if [[ -L /etc/nginx/sites-enabled/default ]]; then rm -f /etc/nginx/sites-enabled/default; info "停用 nginx 預設站台"; fi
  nginx -t >/dev/null 2>&1 || { nginx -t; die "nginx 設定錯誤"; }
  systemctl enable -q nginx
  systemctl reload nginx 2>/dev/null || systemctl restart nginx
  info "$conf"
}

auto_workers() {
  local cpu mem
  cpu=$(nproc)
  mem=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
  local w=$(( cpu * 2 + 1 ))
  (( w > 5 )) && w=5
  (( mem < 4 && w > 2 )) && w=2
  echo "$w"
}

setup_systemd() {
  step "設定 systemd 服務"
  local w="${WORKERS:-$(auto_workers)}" f
  for f in "$DIR"/deploy/dailyreport*.service "$DIR"/deploy/dailyreport*.path; do
    sed -e "s#/opt/dailyreport#$DIR#g" -e "s#--workers [0-9]*#--workers $w#" "$f" > "/etc/systemd/system/$(basename "$f")"
  done
  # 舊版的備份排程：改由維護背景服務依系統設定頁的時間備份
  if [[ -f /etc/systemd/system/dailyreport-backup.timer ]]; then
    systemctl disable --now -q dailyreport-backup.timer 2>/dev/null || true
    rm -f /etc/systemd/system/dailyreport-backup.timer /etc/systemd/system/dailyreport-backup.service
    info "移除舊版備份排程（改在系統設定頁設定）"
  fi
  systemctl daemon-reload
  systemctl enable -q "${SERVICES[@]}" dailyreport-tls.path
  systemctl restart "${SERVICES[@]}"
  systemctl restart dailyreport-tls.path
  info "網頁 $w 個 worker、OCR、AI、維護（備份 / 健康檢查）背景服務、HTTPS 憑證套用"
}

wait_healthy() {
  step "等待服務啟動"
  local i
  for i in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then info "網頁服務已回應（${i} 秒）"; return 0; fi
    sleep 1
  done
  journalctl -u dailyreport -n 30 --no-pager >&2 || true
  die "網頁服務 60 秒內沒有回應，請看上面的紀錄。"
}

cmd_install() {
  load_conf
  # OPT_* 由 apply_options 以間接參照讀取
  # shellcheck disable=SC2034
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --hostname) HOST="$2"; shift 2 ;;
      --tls) TLS="$2"; shift 2 ;;
      --cert) CERT="$2"; TLS=custom; shift 2 ;;
      --key) KEY="$2"; shift 2 ;;
      --ldap-url) OPT_LDAP_URL="$2"; shift 2 ;;
      --ad-domain) OPT_AD_DOMAIN="$2"; shift 2 ;;
      --base-dn) OPT_BASE_DN="$2"; shift 2 ;;
      --required-group) OPT_REQUIRED_GROUP="$2"; shift 2 ;;
      --approver-group) OPT_APPROVER_GROUP="$2"; shift 2 ;;
      --approver-users) OPT_APPROVER_USERS="$2"; shift 2 ;;
      --template) OPT_TEMPLATE="$2"; shift 2 ;;
      --workers) WORKERS="$2"; shift 2 ;;
      --admin-user) ADMIN_USER="$2"; shift 2 ;;
      --skip-ocr-model) OPT_SKIP_OCR=1; shift ;;
      -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
      *) die "不認得的選項 $1（--help 查看說明）" ;;
    esac
  done
  HOST="${HOST:-$(hostname -f 2>/dev/null || hostname)}"
  local t0=$SECONDS
  preflight
  install_packages
  create_user
  setup_venv
  setup_dirs
  write_env
  [[ -n "${OPT_TEMPLATE:-}" ]] && { step "CheckList 範本"; install_template "$OPT_TEMPLATE"; }
  init_app
  apply_options
  download_ocr_model
  setup_tls
  save_conf
  setup_nginx
  setup_systemd
  wait_healthy
  local scheme=https; [[ "$TLS" == none ]] && scheme=http
  echo
  echo "${G}${B}安裝完成${N}（$(( SECONDS - t0 )) 秒）"
  echo "  網址：${B}$scheme://$HOST/${N}$([[ "$TLS" == self-signed ]] && echo "（自簽憑證，瀏覽器第一次會警告）")"
  if [[ -n "${ADMIN_PW:-}" ]]; then
    echo "  本機緊急帳號：${B}$ADMIN_USER${N}  密碼：${B}$ADMIN_PW${N}"
    echo "  ${D}（只顯示這一次；也存在 $ETC/.admin-initial-password，記下後可刪除）${N}"
  elif [[ -f "$ETC/.admin-initial-password" ]]; then
    echo "  本機緊急帳號的初始密碼：sudo cat $ETC/.admin-initial-password（記下後可刪除）"
  fi
  echo "  下一步（都在網頁「系統設定」完成，不需要再登入主機）："
  [[ -n "$(flask_app config-get ldap_url 2>/dev/null | tail -n1)" ]] || echo "   - AD 驗證：填入網域控制站，按「測試」確認後儲存"
  [[ -f "$INSTANCE/templates/checklist.xlsx" ]] || echo "   - PDF 與上傳：上傳 CheckList 範本"
  echo "   - 帳號管理：AD 帳號登入一次後，把檔案管理員指派為系統管理員；可另外新增本機緊急帳號"
  echo "   - 備份還原：確認備份時間與位置（建議設到 NAS）"
  [[ "$TLS" == self-signed ]] && echo "   - HTTPS 憑證：上傳公司 CA 簽發的憑證，瀏覽器就不會再警告"
  echo "   - AI 建議：設定 AI 服務（可不設定）"
  echo
  cmd_doctor || true
}

# ---------------------------------------------------------------- upgrade / restart / template

cmd_upgrade() {
  need_root
  load_conf
  [[ -d "$DIR/.git" ]] || die "$DIR 不是 git 安裝，無法升級。"
  step "升級前備份"
  cmd_backup --quiet
  step "更新程式碼"
  git config --global --add safe.directory "$DIR" 2>/dev/null || true
  local before after
  before="$(git -C "$DIR" rev-parse --short HEAD)"
  git -C "$DIR" fetch -q origin "$BRANCH"
  git -C "$DIR" merge -q --ff-only "origin/$BRANCH" \
    || { warn "有本機修改，改為與 origin/$BRANCH 一致"; git -C "$DIR" reset -q --hard "origin/$BRANCH"; }
  after="$(git -C "$DIR" rev-parse --short HEAD)"
  info "$before → $after"
  # 交給新版腳本完成後續步驟（新版可能有新的相依套件或設定）
  exec bash "$DIR/scripts/dailyreport.sh" _post_upgrade
}

cmd_post_upgrade() {
  load_conf
  HOST="${HOST:-$(hostname -f 2>/dev/null || hostname)}"
  install_packages
  setup_venv
  setup_dirs
  write_env
  setup_tls
  setup_nginx
  setup_systemd
  rm -rf "${INSTANCE:?}/pdf_cache/"*
  wait_healthy
  echo "${G}${B}升級完成${N}"
  cmd_doctor || true
}

cmd_restart() {
  need_root
  systemctl restart "${SERVICES[@]}"
  wait_healthy
}

cmd_template() {
  need_root
  [[ $# -ge 1 ]] || die "用法：dailyreport.sh template <CheckList.xlsx>"
  install_template "$1"
}

cmd_status() {
  local s
  for s in "${SERVICES[@]}" nginx dailyreport-tls.path; do
    printf '  %-26s %s\n' "$s" "$(systemctl is-active "$s" 2>/dev/null || true)"
  done
  curl -fsS "http://127.0.0.1:$PORT/healthz" 2>/dev/null && echo || echo "  healthz：沒有回應"
}

# ---------------------------------------------------------------- backup / restore

cmd_backup() {  # 與系統設定頁的「立即備份」相同（同一個位置、同一種格式），另外附上 install.conf
  need_root
  local quiet=0 args=() a out name root dest tmp
  for a in "$@"; do case "$a" in --quiet) quiet=1 ;; --no-prune) args+=(--no-prune) ;; esac; done
  out="$(flask_app backup "${args[@]}" 2>&1)" || die "備份失敗：$out"
  out="$(tail -n1 <<<"$out")"
  name="${out%%$'\t'*}"
  root="$(backup_root)"
  dest="$root/$name"
  [[ -n "$name" && -d "$dest" ]] || die "備份失敗：$out"
  if [[ -f "$ETC/install.conf" ]]; then
    install -o "$APP_USER" -m 600 "$ETC/install.conf" "$dest/install.conf"
    tmp="$(mktemp)"
    (cd "$dest" && find . -maxdepth 1 -type f ! -name SHA256SUMS -printf '%P\n' | sort | sed 's#^#./#' | xargs sha256sum) > "$tmp"
    install -o "$APP_USER" -m 600 "$tmp" "$dest/SHA256SUMS"
    rm -f "$tmp"
  fi
  [[ $quiet == 1 ]] || { echo "${G}備份完成${N}：$dest（$(du -sh "$dest" | cut -f1)）"; info "還原：系統設定 → 備份還原；網頁無法開啟時 sudo bash $DIR/scripts/dailyreport.sh restore $dest"; }
}

cmd_restore() {
  need_root
  local src="${1:-}"
  local root; root="$(backup_root)"
  [[ -d "$src" && -f "$src/dailyreport.db" ]] || die "用法：dailyreport.sh restore <備份資料夾>（例如 $(find "$root" -mindepth 1 -maxdepth 1 -type d -name '20*' 2>/dev/null | sort | tail -n1)）。網頁可以開啟時，請改用「系統設定 → 備份還原」。"
  (cd "$src" && sha256sum -c --quiet SHA256SUMS) || die "備份檔案檢查碼不符，可能已損壞。"
  src="$(cd "$src" && pwd)"
  step "還原前先備份目前的資料"
  cmd_backup --no-prune || warn "目前資料備份失敗（可能是資料庫已損壞），繼續還原"
  step "還原 $src"
  systemctl stop "${SERVICES[@]}"
  local db; db="$(db_path)"
  rm -f "$db-wal" "$db-shm"
  install -o "$APP_USER" -g "$APP_GROUP" -m 640 "$src/dailyreport.db" "$db"
  tar -C "$DIR" -xzf "$src/instance.tar.gz"
  chown -R "$APP_USER:$APP_GROUP" "$INSTANCE"
  rm -rf "${INSTANCE:?}/pdf_cache/"*
  info "設定檔 .env 未變更（備份中的設定檔在 $src/env）"
  systemctl start "${SERVICES[@]}"
  wait_healthy
  echo "${G}還原完成${N}"
}

# ---------------------------------------------------------------- 系統設定頁上傳的 HTTPS 憑證（dailyreport-tls.path 觸發）

cmd_apply_tls() {
  need_root
  load_conf
  HOST="${HOST:-$(hostname -f 2>/dev/null || hostname)}"
  local req="$INSTANCE/tls_request" action f a b
  [[ -f "$req/request.json" && ! -L "$req/request.json" ]] || { rm -f "$req/request.json"; return 0; }
  tls_result() {  # tls_result true|false 訊息：寫回設定頁看得到的 result.json，並清掉這次的要求
    MSG="$2" OK="$1" AT="$(date '+%Y-%m-%d %H:%M')" python3 -c \
      'import json, os; print(json.dumps({"ok": os.environ["OK"] == "true", "at": os.environ["AT"], "message": os.environ["MSG"]}, ensure_ascii=False))' \
      > "$req/result.json"
    chown "$APP_USER:$APP_GROUP" "$req/result.json"
    chmod 640 "$req/result.json"
    rm -f "$req/request.json" "$req/cert.pem" "$req/key.pem"
  }
  # shellcheck disable=SC2064
  trap "[[ -f '$req/request.json' ]] && tls_result false '套用時發生錯誤，請在主機查看 journalctl -u dailyreport-tls'" EXIT
  for f in cert.pem key.pem; do
    if [[ -L "$req/$f" ]]; then tls_result false "不接受符號連結"; return 0; fi
  done
  action="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("action", ""))' "$req/request.json" 2>/dev/null || true)"
  local old_tls="$TLS" old_cert="$CERT" old_key="$KEY"
  case "$action" in
    custom)
      [[ -f "$req/cert.pem" && -f "$req/key.pem" ]] || { tls_result false "缺少憑證或私鑰"; return 0; }
      a="$(openssl x509 -in "$req/cert.pem" -pubkey -noout 2>/dev/null | openssl sha256 2>/dev/null || true)"
      b="$(openssl pkey -in "$req/key.pem" -pubout 2>/dev/null | openssl sha256 2>/dev/null || true)"
      [[ -n "$a" && "$a" == "$b" ]] || { tls_result false "憑證格式錯誤，或私鑰與憑證不配對"; return 0; }
      openssl x509 -in "$req/cert.pem" -noout -checkend 0 >/dev/null 2>&1 || { tls_result false "憑證已過期"; return 0; }
      install -d -m 750 "$ETC/tls"
      for f in cert key; do
        [[ -f "$ETC/tls/custom-$f.pem" ]] && cp -p "$ETC/tls/custom-$f.pem" "$ETC/tls/custom-$f.pem.previous"
      done
      install -m 644 "$req/cert.pem" "$ETC/tls/custom-cert.pem"
      install -m 600 "$req/key.pem" "$ETC/tls/custom-key.pem"
      TLS=custom CERT="$ETC/tls/custom-cert.pem" KEY="$ETC/tls/custom-key.pem" ;;
    self-signed)
      TLS=self-signed
      setup_tls >/dev/null ;;
    *) tls_result false "不認得的要求"; return 0 ;;
  esac
  if ( setup_nginx ) >/dev/null 2>&1; then
    save_conf
    tls_result true "已套用：$(openssl x509 -in "$CERT" -noout -subject -enddate 2>/dev/null | tr '\n' ' ')"
  else
    # nginx 不接受：還原原本的憑證與設定
    for f in cert key; do
      [[ -f "$ETC/tls/custom-$f.pem.previous" ]] && mv -f "$ETC/tls/custom-$f.pem.previous" "$ETC/tls/custom-$f.pem"
    done
    TLS="$old_tls" CERT="$old_cert" KEY="$old_key"
    ( setup_nginx ) >/dev/null 2>&1 || true
    tls_result false "nginx 不接受這份憑證（nginx -t 失敗），已還原成原本的憑證"
  fi
  trap - EXIT
}

# ---------------------------------------------------------------- doctor

N_OK=0; N_WARN=0; N_FAIL=0; LAST_GROUP=""
item() {  # item 狀態 分組 名稱 說明 [修正方式]
  local st="$1" grp="$2" name="$3" detail="$4" fix="${5:-}" mark
  if [[ "$grp" != "$LAST_GROUP" ]]; then echo; echo "${B}$grp${N}"; LAST_GROUP="$grp"; fi
  case "$st" in
    ok) mark="${G}✔${N}"; N_OK=$((N_OK + 1)) ;;
    warn) mark="${Y}!${N}"; N_WARN=$((N_WARN + 1)) ;;
    *) mark="${R}✘${N}"; N_FAIL=$((N_FAIL + 1)) ;;
  esac
  printf '  %s %s%*s %s\n' "$mark" "$name" "$(( 18 - $(printf '%s' "$name" | LC_ALL=C.UTF-8 wc -L) ))" "" "$detail"
  [[ -n "$fix" && "$st" != ok ]] && printf '      %s→ %s%s\n' "$D" "$fix" "$N"
  return 0
}

cmd_doctor() {
  need_root
  N_OK=0; N_WARN=0; N_FAIL=0; LAST_GROUP=""
  echo "${B}CFXD 日報系統健檢${N}  $(date '+%Y-%m-%d %H:%M:%S')  $(hostname)"
  local g

  g="系統"
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID}:${VERSION_ID}" in
    ubuntu:24.04|ubuntu:26.04|debian:13) item ok "$g" "作業系統" "$PRETTY_NAME" ;;
    *) item warn "$g" "作業系統" "$PRETTY_NAME（未驗證）" ;;
  esac
  local mem disk
  mem=$(awk '/MemAvailable/ {printf "%d", $2/1024}' /proc/meminfo)
  item "$( ((mem >= 1024)) && echo ok || echo warn)" "$g" "可用記憶體" "${mem} MB" "OCR 約需 1 GB，建議加記憶體或 swap"
  disk=$(df -Pm "$DIR" 2>/dev/null | awk 'NR==2 {print $4}')
  item "$( ((disk >= 5000)) && echo ok || { ((disk >= 1000)) && echo warn || echo fail; })" "$g" "可用磁碟" "${disk} MB（$DIR）" "系統設定 → 備份還原：減少保留份數或改存 NAS；或擴充磁碟"
  if [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" == yes ]]; then item ok "$g" "時間同步" "NTP 已同步"
  else item warn "$g" "時間同步" "NTP 未同步（截圖日期檢查依賴正確的系統時間）" "sudo timedatectl set-ntp true"; fi

  g="程式"
  if [[ ! -f "$DIR/wsgi.py" ]]; then item fail "$g" "安裝位置" "$DIR 沒有程式" "curl -fsSL $BOOTSTRAP_URL | sudo bash"; summary_doctor; return 1; fi
  local rev behind=""
  rev="$(git -C "$DIR" log -1 --format='%h %cd' --date=format:'%Y-%m-%d' 2>/dev/null || echo 未知)"
  if timeout 15 git -C "$DIR" fetch -q origin "$BRANCH" 2>/dev/null; then
    behind="$(git -C "$DIR" rev-list --count "HEAD..origin/$BRANCH" 2>/dev/null || echo 0)"
  fi
  if [[ -z "$behind" ]]; then item ok "$g" "版本" "$rev（無法連線檢查新版）"
  elif [[ "$behind" == 0 ]]; then item ok "$g" "版本" "$rev（最新）"
  else item warn "$g" "版本" "$rev，有 $behind 個新版本" "sudo bash $DIR/scripts/dailyreport.sh upgrade"; fi
  if "$VENV/bin/python" -c "import flask, reportlab, openpyxl, rapidocr, onnxruntime, anthropic, cryptography" 2>/dev/null; then
    item ok "$g" "Python 套件" "$("$VENV/bin/python" --version)"
  else item fail "$g" "Python 套件" "缺少套件或虛擬環境損壞" "sudo bash $DIR/scripts/dailyreport.sh install"; fi
  local models
  models="$("$VENV/bin/python" -c "import pathlib, rapidocr; print(len(list(pathlib.Path(rapidocr.__file__).parent.rglob('*.onnx'))))" 2>/dev/null || echo 0)"
  if (( models >= 3 )); then item ok "$g" "OCR 模型" "$models 個模型檔"
  else item fail "$g" "OCR 模型" "尚未下載（截圖第一次辨識時會失敗）" "sudo $VENV/bin/python -c 'from rapidocr import RapidOCR; RapidOCR()'"; fi

  g="檔案與權限"
  if [[ -f "$ENV_FILE" ]]; then
    local perm owner
    perm="$(stat -c %a "$ENV_FILE")"; owner="$(stat -c %U:%G "$ENV_FILE")"
    if [[ "$perm" == 640 || "$perm" == 600 ]]; then item ok "$g" ".env" "$perm $owner"
    else item fail "$g" ".env" "權限 $perm（含 SECRET_KEY，不應讓其他人讀取）" "sudo chmod 640 $ENV_FILE && sudo chown root:$APP_GROUP $ENV_FILE"; fi
  else item fail "$g" ".env" "不存在" "sudo bash $DIR/scripts/dailyreport.sh install"; fi
  if [[ "$(stat -c %U "$INSTANCE" 2>/dev/null)" == "$APP_USER" ]]; then item ok "$g" "資料夾" "$INSTANCE"
  else item fail "$g" "資料夾" "$INSTANCE 擁有者不是 $APP_USER" "sudo chown -R $APP_USER:$APP_GROUP $INSTANCE"; fi

  # 應用程式內部檢查（設定、資料庫、PDF、背景工作）
  local json
  if json="$(as_app timeout 60 "$VENV/bin/flask" --app wsgi doctor-json 2>/dev/null | tail -n1)" && [[ "$json" == \[* ]]; then
    while IFS=$'\t' read -r st grp name detail fix; do
      item "$st" "$grp" "$name" "$detail" "$fix"
    done < <(printf '%s' "$json" | python3 -c '
import json, sys
for x in json.load(sys.stdin):
    print("\t".join(str(x[k]).replace("\t", " ").replace("\n", " ") for k in ("status", "group", "name", "detail", "fix")))')
  else
    item fail "應用程式" "啟動" "應用程式無法載入" "sudo -u $APP_USER env -C $DIR $VENV/bin/flask --app wsgi doctor-json 查看錯誤"
  fi

  g="服務"
  local s
  for s in "${SERVICES[@]}" nginx; do
    local active enabled errs restarts
    active="$(systemctl is-active "$s" 2>/dev/null || true)"
    enabled="$(systemctl is-enabled "$s" 2>/dev/null || true)"
    errs="$(journalctl -u "$s" -p err --since '-1h' -q --no-pager 2>/dev/null | wc -l)"
    restarts="$(systemctl show -p NRestarts --value "$s" 2>/dev/null || echo 0)"
    if [[ "$active" != active ]]; then item fail "$g" "$s" "$active" "sudo systemctl restart $s；sudo journalctl -u $s -n 50"
    elif [[ "$enabled" != enabled ]]; then item warn "$g" "$s" "執行中，但開機不會自動啟動" "sudo systemctl enable $s"
    elif (( errs > 0 || restarts > 3 )); then item warn "$g" "$s" "執行中；最近 1 小時錯誤 $errs 筆、重啟 $restarts 次" "sudo journalctl -u $s -p err --since -1h"
    else item ok "$g" "$s" "執行中"; fi
  done
  if [[ "$(systemctl is-active dailyreport-tls.path 2>/dev/null)" == active ]]; then item ok "$g" "dailyreport-tls.path" "等待系統設定頁上傳的憑證"
  else item warn "$g" "dailyreport-tls.path" "未啟用：系統設定頁上傳的 HTTPS 憑證不會套用" "sudo systemctl enable --now dailyreport-tls.path"; fi

  g="網路"
  load_conf
  if curl -fsS "http://127.0.0.1:$PORT/healthz" 2>/dev/null | grep -q '"ok"'; then item ok "$g" "網頁服務" "127.0.0.1:$PORT 回應正常"
  else item fail "$g" "網頁服務" "127.0.0.1:$PORT 沒有回應" "sudo journalctl -u dailyreport -n 50"; fi
  if nginx -t >/dev/null 2>&1; then item ok "$g" "nginx 設定" "語法正確"; else item fail "$g" "nginx 設定" "語法錯誤" "sudo nginx -t"; fi
  local scheme=https code hdr
  [[ "${TLS:-self-signed}" == none ]] && scheme=http
  code="$(curl -sk -o /dev/null -w '%{http_code}' "$scheme://127.0.0.1/login" 2>/dev/null || echo 000)"
  if [[ "$code" == 200 ]]; then item ok "$g" "登入頁" "$scheme://$HOST/login（HTTP $code）"
  else item fail "$g" "登入頁" "$scheme://127.0.0.1/login 回應 HTTP $code" "sudo nginx -t；sudo systemctl status nginx dailyreport"; fi
  if [[ "$scheme" == https ]]; then
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1/login" 2>/dev/null || echo 000)"
    item "$([[ "$code" == 301 ]] && echo ok || echo warn)" "$g" "HTTP 轉 HTTPS" "HTTP $code" "sudo bash $DIR/scripts/dailyreport.sh install"
    local end days
    if [[ -n "${CERT:-}" && -f "$CERT" ]]; then
      end="$(openssl x509 -enddate -noout -in "$CERT" | cut -d= -f2)"
      days=$(( ( $(date -d "$end" +%s) - $(date +%s) ) / 86400 ))
      item "$( ((days > 30)) && echo ok || { ((days > 0)) && echo warn || echo fail; })" "$g" "憑證" \
        "$( [[ "$TLS" == self-signed ]] && echo "自簽，" )剩 $days 天到期（$CERT）" "更新憑證後執行 sudo systemctl reload nginx"
    fi
  else
    item warn "$g" "HTTPS" "未使用 HTTPS，密碼以明文傳送" "sudo bash $DIR/scripts/dailyreport.sh install --tls self-signed"
  fi
  hdr="$(curl -skI "$scheme://127.0.0.1/login" 2>/dev/null | tr -d '\r')"
  if grep -qi '^x-content-type-options: nosniff' <<<"$hdr" && grep -qi '^x-frame-options' <<<"$hdr"; then item ok "$g" "安全標頭" "nosniff、X-Frame-Options"
  else item warn "$g" "安全標頭" "缺少" "sudo bash $DIR/scripts/dailyreport.sh install"; fi

  summary_doctor
}

summary_doctor() {
  echo
  echo "${B}結果${N}：${G}正常 $N_OK${N}、${Y}注意 $N_WARN${N}、${R}失敗 $N_FAIL${N}"
  if (( N_FAIL > 0 )); then echo "依照每一項下方 → 的指令修正後，再執行一次 doctor。"; return 1; fi
  return 0
}

# ---------------------------------------------------------------- main

cmd="${1:-install}"; shift || true
case "$cmd" in
  install) cmd_install "$@" ;;
  upgrade) cmd_upgrade "$@" ;;
  _post_upgrade) cmd_post_upgrade ;;
  apply-tls) cmd_apply_tls ;;
  doctor|check) cmd_doctor ;;
  backup) cmd_backup "$@" ;;
  restore) cmd_restore "$@" ;;
  template) cmd_template "$@" ;;
  restart) cmd_restart ;;
  status) cmd_status ;;
  -h|--help|help) sed -n '2,30p' "$0" ;;
  *) die "不認得的指令 $cmd（install / upgrade / doctor / backup / restore / template / restart / status）" ;;
esac
