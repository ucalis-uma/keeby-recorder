#!/usr/bin/env bash
# ============================================================
# Cloudflare WARP を「プロキシモード」で起動する
#
# GitHub Actions の共有IPは YouTube から bot 扱いされ、
# Cookie も PO Token も効かない（2026-10-29 実測）。
# WARP は TUN を作らず HTTP プロキシとして使えるため、
# yt-dlp --proxy http://127.0.0.1:40000 で出口IPだけ変えられる。
#
# 失敗してもジョブは止めない（直接接続にフォールバック）。
# ============================================================
set -uo pipefail

PORT="${WARP_PROXY_PORT:-40000}"
KEYRING=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
LIST=/etc/apt/sources.list.d/cloudflare-client.list

echo "[INFO] === Cloudflare WARP (proxy mode, port ${PORT}) ==="

install_warp() {
  local codename="$1"
  echo "deb [signed-by=${KEYRING}] https://pkg.cloudflareclient.com/ ${codename} main" \
    | sudo tee "$LIST" >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y cloudflare-warp >/dev/null
}

curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
  | sudo gpg --yes --dearmor -o "$KEYRING" 2>/dev/null || true

if ! install_warp "$(lsb_release -cs)"; then
  echo "[WARN] $(lsb_release -cs) 用パッケージが無いため jammy を試します"
  if ! install_warp jammy; then
    echo "::warning::WARP を導入できませんでした（直接接続で続行）"
    exit 0
  fi
fi

# 既存登録があっても失敗しない
warp-cli --accept-tos registration new >/dev/null 2>&1 || true
warp-cli --accept-tos mode proxy >/dev/null 2>&1 || true
warp-cli --accept-tos proxy port "$PORT" >/dev/null 2>&1 || true
warp-cli --accept-tos connect >/dev/null 2>&1 || true

connected=0
for _ in $(seq 1 20); do
  status="$(warp-cli --accept-tos status 2>&1 || true)"
  if echo "$status" | grep -qi 'Connected'; then
    connected=1
    break
  fi
  sleep 2
done

if [ "$connected" -ne 1 ]; then
  echo "::warning::WARP が Connected になりません（直接接続で続行）"
  warp-cli --accept-tos status || true
  exit 0
fi

echo "[INFO] WARP connected"
if command -v curl >/dev/null 2>&1; then
  curl -s --max-time 15 --proxy "http://127.0.0.1:${PORT}" \
    https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null | head -n 4 || true
fi
