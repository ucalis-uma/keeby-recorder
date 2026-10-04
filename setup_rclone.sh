#!/usr/bin/env bash
# ============================================================
# Secrets YOU_TUBE から rclone の設定ファイルを生成する
#
# 旧VPSでは `rclone config` を対話形式で手動実行し、トークンを貼り付けていた。
# GitHub Actions ではジョブが毎回作り直されるため、
# 事先（ローカル or 旧VPSで）rclone config した内容をそのまま Secrets に登録しておき、
# ここではそれを ~/.config/rclone/rclone.conf に書き戻すだけにする。
#
# 必要な環境変数:
#   RCLONE_CONFIG_B64 : `base64` した rclone.conf の中身の文字列
#                       （例: ローカルで `base64 -w0 ~/.config/rclone/rclone.conf`）
# ============================================================
set -euo pipefail

CONFIG_PATH="${HOME}/.config/rclone/rclone.conf"
mkdir -p "$(dirname "$CONFIG_PATH")"

if [ -z "${RCLONE_CONFIG_B64:-}" ]; then
  echo "[ERROR] RCLONE_CONFIG_B64 が設定されていません。"
  exit 1
fi

echo "$RCLONE_CONFIG_B64" | base64 -d > "$CONFIG_PATH"
chmod 600 "$CONFIG_PATH"

echo "[INFO] rclone config を生成しました。"
rclone listremotes
