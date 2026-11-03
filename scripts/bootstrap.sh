#!/usr/bin/env bash
#
# CFXD 日報系統：一行安裝 / 健檢。不需要先手動 git clone。
#
#   安裝（全新主機，所有資源自動完成）：
#     curl -fsSL https://raw.githubusercontent.com/nox913106/CFXD_DailyReportDocSys/main/scripts/bootstrap.sh | sudo bash
#   帶參數安裝：
#     curl -fsSL .../bootstrap.sh | sudo bash -s -- install --hostname report.corp.local \
#         --ldap-url ldaps://dc01.corp.local:636 --ad-domain corp.local --base-dn DC=corp,DC=local
#   檢查部署結果：
#     curl -fsSL .../bootstrap.sh | sudo bash -s -- doctor
#   升級：
#     curl -fsSL .../bootstrap.sh | sudo bash -s -- upgrade
#
# 環境變數：DR_REPO（Git 來源）、DR_BRANCH（預設 main）、DR_DIR（安裝位置，預設 /opt/dailyreport）
set -euo pipefail

REPO="${DR_REPO:-https://github.com/nox913106/CFXD_DailyReportDocSys.git}"
BRANCH="${DR_BRANCH:-main}"
DIR="${DR_DIR:-/opt/dailyreport}"
CMD="${1:-install}"

# shellcheck disable=SC2154
trap 'rc=$?; [[ $rc -ne 0 ]] && echo -e "\n安裝 / 檢查中斷（代碼 $rc）。修正上面的錯誤後可以直接重跑同一行指令；已完成的步驟不會重做。" >&2' EXIT

if [[ "$(id -u)" -ne 0 ]]; then
  echo "[錯誤] 請用 root 執行：curl ... | sudo bash" >&2
  exit 1
fi

case "$CMD" in
  doctor|status|backup)
    # 只檢查 / 備份：不更新程式碼
    if [[ ! -x "$DIR/scripts/dailyreport.sh" && ! -f "$DIR/scripts/dailyreport.sh" ]]; then
      echo "[錯誤] $DIR 尚未安裝。先執行：curl -fsSL https://raw.githubusercontent.com/nox913106/CFXD_DailyReportDocSys/main/scripts/bootstrap.sh | sudo bash" >&2
      exit 1
    fi
    exec bash "$DIR/scripts/dailyreport.sh" "$@"
    ;;
esac

if ! command -v git >/dev/null 2>&1; then
  echo "[*] 安裝 git…"
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git ca-certificates >/dev/null
fi

if [[ -d "$DIR/.git" ]]; then
  if [[ "$CMD" == "install" ]]; then
    echo "[*] $DIR 已存在，更新到 origin/$BRANCH…"
    git config --global --add safe.directory "$DIR" 2>/dev/null || true
    git -C "$DIR" fetch -q origin "$BRANCH"
    git -C "$DIR" checkout -q "$BRANCH" 2>/dev/null || true
    git -C "$DIR" merge -q --ff-only "origin/$BRANCH" \
      || { echo "[警告] $DIR 有本機修改，改為與 origin/$BRANCH 一致"; git -C "$DIR" reset -q --hard "origin/$BRANCH"; }
  fi
  # upgrade 由 dailyreport.sh 自己先備份再更新
else
  echo "[*] 下載程式：$REPO → $DIR"
  git clone -q --branch "$BRANCH" "$REPO" "$DIR"
fi

exec bash "$DIR/scripts/dailyreport.sh" "$@"
