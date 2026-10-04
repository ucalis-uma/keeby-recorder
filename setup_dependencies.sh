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
sudo apt-get update
sudo apt-get install -y \
  python3 \
  python3-pip \
  python3-venv \
  curl \
  git \
  ffmpeg \
  unzip \
  ca-certificates

echo "[INFO] === yt-dlp（nightly）を /usr/local/bin へ ==="
sudo curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
  -o /usr/local/bin/yt-dlp
sudo chmod a+rx /usr/local/bin/yt-dlp
# 実行時に version チェックが走るため git を要求されるAnnual版を使う
sudo curl -L https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/latest/download/yt-dlp \
  -o /usr/local/bin/yt-dlp
sudo chmod a+rx /usr/local/bin/yt-dlp
/usr/local/bin/yt-dlp --version

echo "[INFO] === Deno（JSランタイム）を /usr/local/bin へ ==="
# 2025年11月以降、YouTube の対応にJSランタイムが必須になる。
# yt-dlp が deno / node を自動検出する。Deno公式インストーラを root で実行し、
# /usr/local/bin に配置して PATH を汚さない。
curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y
command -v deno && deno --version

echo "[INFO] === rclone を /usr/local/bin へ ==="
curl https://rclone.org/install.sh | sudo bash
rclone version | head -n 1

echo "[INFO] === Python 依存 (google-api-python-client, requests, chat-downloader) ==="
# ジョブごとに作り直されるので venv は作らず、pip install --user で入れる。
# （chat-downloader は Twitch 用）
python3 -m pip install --user --upgrade \
  google-api-python-client \
  requests \
  chat-downloader

# PATH にユーザー/site-packages を通す（googleapiclient の import 用）
RC_LINES='export PATH="$HOME/.local/bin:$PATH"'
grep -q 'HOME/.local/bin' ~/.bashrc 2>/dev/null || echo "$RC_LINES" >> ~/.bashrc
export PATH="$HOME/.local/bin:$PATH"

echo "[INFO] === バージョン確認 ==="
python3 -c "import googleapiclient; print('googleapiclient OK')"
command -v chat_downloader >/dev/null 2>&1 && echo "chat_downloader OK" || echo "chat_downloader: 未検出（動画のみ記録）"
python3 -c "import requests; print('requests OK')"

echo "[INFO] === セットアップ完了 ==="
