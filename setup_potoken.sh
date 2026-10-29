#!/usr/bin/env bash
# ============================================================
# bgutil-ytdlp-pot-provider のセットアップ
#
# YouTube はデータセンターIP（GitHub Actions のランナー）からの player API に
# 「Sign in to confirm you're not a bot」を返す。Cookie を付けても、
# ランナーのIPがその時点で怪しければ通らない（2026-10-29 実測：Cookieありでも全滅）。
#
# PO Token provider は YouTube の BotGuard JS を実行して正規の PO Token を発行する。
# これがあるとログインなしで player API を通せるため、Cookie の定期更新が不要になる。
#
# 失敗してもジョブは止めない（Cookie フォールバックが残るため）。
# ============================================================
set -uo pipefail

VERSION="${BGUTIL_VERSION:-2.0.2}"
PLUGIN_DIR="${HOME}/.config/yt-dlp/plugins"
PORT=4416

echo "[INFO] === PO Token provider (bgutil ${VERSION}) ==="

# --- 1. yt-dlp プラグイン ---
mkdir -p "$PLUGIN_DIR"
if curl -fsSL -o /tmp/bgutil.zip \
  "https://github.com/Brainicism/bgutil-ytdlp-pot-provider/releases/download/${VERSION}/bgutil-ytdlp-pot-provider.zip"; then
  unzip -oq /tmp/bgutil.zip -d "$PLUGIN_DIR"
  echo "[INFO] plugin installed: ${PLUGIN_DIR}/yt_dlp_plugins"
else
  echo "::warning::PO token plugin の取得に失敗しました（Cookie フォールバックで続行）"
  exit 0
fi

# --- 2. provider サーバ（Docker） ---
if ! command -v docker >/dev/null 2>&1; then
  echo "::warning::docker が見つかりません（provider を起動できません）"
  exit 0
fi

docker rm -f bgutil-provider >/dev/null 2>&1 || true
started=0
for i in 1 2 3; do
  if docker run -d --name bgutil-provider --init \
      -p "127.0.0.1:${PORT}:${PORT}" \
      "brainicism/bgutil-ytdlp-pot-provider:${VERSION}" >/dev/null 2>&1; then
    started=1
    echo "[INFO] provider container started"
    break
  fi
  echo "[WARN] docker run failed (attempt ${i}/3)"
  sleep 5
done

if [ "$started" -ne 1 ]; then
  echo "::warning::provider を起動できませんでした（Cookie フォールバックで続行）"
  exit 0
fi

# --- 3. 起動待ち（最大60秒） ---
ready=0
for _ in $(seq 1 30); do
  if docker inspect -f '{{.State.Running}}' bgutil-provider 2>/dev/null | grep -q true; then
    if curl -fsS --max-time 2 -o /dev/null "http://127.0.0.1:${PORT}/ping" 2>/dev/null; then
      ready=1
      break
    fi
  fi
  sleep 2
done

if [ "$ready" -eq 1 ]; then
  echo "[INFO] provider is ready on 127.0.0.1:${PORT}"
else
  echo "::warning::provider の応答がありません（起動はしているので続行します）"
fi
