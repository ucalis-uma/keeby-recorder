#!/usr/bin/env bash
# ============================================================
# GitHub Actions ランナー用の依存セットアップ
#
# 旧VPS手順 (再構築.md / 再構築.txt) を Ubuntu ランナー向けに置き換えたもの。
# apt で入るものは apt、yt-dlp と deno は必ず /usr/local/bin に置く
# （apt版は古く、cron/Actionsからの呼び出しを安定させるため手動インストール）
# ============================================================
set -euo pipefail

echo "[INFO] === 依存パッケージのインストール ==="
# 一時的なミラー不調で apt が落ちるのを避ける（既定は再試行なし）。
sudo apt-get -o Acquire::Retries=3 update
sudo apt-get -o Acquire::Retries=3 install -y \
  python3 \
  python3-pip \
  python3-venv \
  curl \
  git \
  ffmpeg \
  unzip \
  ca-certificates

echo "[INFO] === yt-dlp（nightly）を /usr/local/bin へ ==="
# 安定版→nightly の二重取得は不要。nightly を1回だけ取得する。
# （YouTube の仕様変更追従のため nightly を常用）
YTDLP_URL="https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/latest/download/yt-dlp"

install_ytdlp() {
  # -f は必須。付けないと 504 等のエラーページ(HTML)をそのまま保存して
  # curl が成功扱いになり、次の --version で
  # 「syntax error near unexpected token `<`」という分かりにくい失敗になる。
  # --retry-all-errors は 5xx も再試行対象にする（既定は接続系のみ）。
  sudo curl -fL --retry 5 --retry-all-errors --retry-delay 3 --connect-timeout 20 \
    "$YTDLP_URL" -o /usr/local/bin/yt-dlp
  sudo chmod a+rx /usr/local/bin/yt-dlp
  /usr/local/bin/yt-dlp --version
}

if ! install_ytdlp; then
  echo "[WARN] yt-dlp nightly の取得に失敗。5秒後に再試行します。" >&2
  sleep 5
  if ! install_ytdlp; then
    # GitHub Releases が落ちているとき用の保険。PyPI の安定版で代替する。
    echo "[WARN] 再試行も失敗。PyPI の安定版 yt-dlp で代替します。" >&2
    sudo python3 -m pip install --break-system-packages --upgrade yt-dlp
    /usr/local/bin/yt-dlp --version
  fi
fi

echo "[INFO] === Deno（JSランタイム）を /usr/local/bin へ ==="
# 2025年11月以降、YouTube の対応にJSランタイムが必須になる。
# yt-dlp が deno / node を自動検出する。/usr/local への書き込みには root 権限が
# 必要なため、sudo 側に DENO_INSTALL を渡す（sudo の env_reset 対策で env 経由）。
curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 \
  https://deno.land/install.sh | sudo env DENO_INSTALL=/usr/local sh -s -- -y
export PATH="/usr/local/bin:$PATH"
command -v deno && deno --version

echo "[INFO] === rclone を /usr/local/bin へ ==="
# `curl | sudo bash` は sudo の secure_path 環境で curl が見えない場合があるため、
# 先にダウンロードしてから sudo bash で実行する方式にする。
curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 \
  https://rclone.org/install.sh -o /tmp/rclone-install.sh
sudo bash /tmp/rclone-install.sh
# ここを `| head -n 1` にすると、head が先に閉じて rclone が SIGPIPE を受け、
# `set -o pipefail` によりパイプライン全体が 141 で失敗する（実際に発生した）。
# sed は最後まで読み切るので SIGPIPE にならない。
rclone version | sed -n '1p'

echo "[INFO] === Python 依存 (venv に統一) ==="
# ubuntu-24.04 (ubuntu-latest) のシステム Python は PEP 668
# (externally-managed-environment) のため、`pip install --user` は失敗する。
# そのためリポジトリ直下に .venv を作り、venv の pip で入れる。
# requirements.txt が唯一の依存定義（YouTube 用 + Twitch 用 chat-downloader を含む）。
python3 -m venv .venv
.venv/bin/pip install --upgrade --retries 5 --timeout 30 pip
.venv/bin/pip install --retries 5 --timeout 30 -r requirements.txt
export PATH="$PWD/.venv/bin:$PATH"

echo "[INFO] === バージョン確認 ==="
.venv/bin/python -c "import googleapiclient; print('googleapiclient OK')"
.venv/bin/python -c "import requests; print('requests OK')"
if [ -x .venv/bin/chat_downloader ]; then
  echo "chat_downloader OK"
else
  echo "chat_downloader: 未検出（動画のみ記録）"
fi

echo "[INFO] === セットアップ完了 ==="
