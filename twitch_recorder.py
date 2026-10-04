#!/usr/bin/env python3
"""
Keeby Twitch Live Recorder (GitHub Actions edition)

旧VPSの twitch_dl_vps.py のActions版。
無限ループ（while True）を外し、**1回のjobで完結**する:

    配信検知 → (動画DL + チャットDL 並行) → 終了待ち → Google Driveへアップロード → 後始末 → 終了

15分ごとにcronで起動される前提。jobは6時間が上限だが、
「配信中なら録画継続、終わったら終わる」ため6時間以内の配信なら収まる。
"""

import os
import subprocess
import sys
import time
import logging

# ============================================================
# ログ設定
# ============================================================
LOG_DIR = os.environ.get("LOG_DIR", "./logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "twitch_recorder.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# ============================================================
# 設定（すべて環境変数で差し替え可）
# ============================================================
STREAMER_ID = os.environ.get("TWITCH_STREAMER_ID", "keeby_boy")
TARGET_URL = f"https://www.twitch.tv/{STREAMER_ID}"

BASE_DIR = os.environ.get("KEEBY_BASE_DIR", os.getcwd())
OUTPUT_DIR = os.path.join(BASE_DIR, "twitch_temp")

YT_DLP_BIN = os.environ.get("YT_DLP_BIN", "yt-dlp")
CHAT_DOWNLOADER_BIN = os.environ.get("CHAT_DOWNLOADER_BIN", "chat_downloader")
RCLONE_BIN = os.environ.get("RCLONE_BIN", "rclone")
RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "gdrive")
GDRIVE_TWITCH_DEST = os.environ.get("GDRIVE_TWITCH_DEST", f"{RCLONE_REMOTE}:/keeby/twitch")

STALL_TIMEOUT = int(os.environ.get("TWITCH_STALL_TIMEOUT", "1800"))  # 30分
CHECK_INTERVAL = int(os.environ.get("TWITCH_CHECK_INTERVAL", "60"))    # 1分


# ============================================================
# ユーティリティ
# ============================================================
def get_dir_size(path):
    total = 0
    if not os.path.exists(path):
        return 0
    for entry in os.scandir(path):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                pass
    return total


def is_streaming():
    """配信中か判定（yt-dlp --simulate）"""
    try:
        res = subprocess.run(
            [YT_DLP_BIN, "--simulate", "--quiet", TARGET_URL],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return res.returncode == 0
    except FileNotFoundError:
        logger.error(f"{YT_DLP_BIN} が見つかりません。")
        return False


def cleanup_dir(path):
    import shutil

    try:
        for entry in os.scandir(path):
            try:
                if entry.is_file() or entry.is_symlink():
                    os.remove(entry.path)
                elif entry.is_dir():
                    shutil.rmtree(entry.path)
            except OSError as e:
                logger.warning(f"削除エラー {entry.path}: {e}")
    except OSError as e:
        logger.warning(f"クリーンアップエラー {path}: {e}")


def upload_to_drive():
    """OUTPUT_DIR を Google Drive へアップロード。成功時のみローカル削除。"""
    import shutil as _shutil

    if _shutil.which(RCLONE_BIN) is None:
        logger.warning(f"rclone が見つかりません（{RCLONE_BIN}）。アップロードをスキップします。")
        return False
    time.sleep(3)  # rclone がファイルを確定するのを待つ
    cmd = [RCLONE_BIN, "copy", OUTPUT_DIR, GDRIVE_TWITCH_DEST, "--drive-chunk-size=64M"]
    logger.info(f"Google Driveへアップロード: {' '.join(cmd)}")
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        logger.warning(f"rclone が見つかりません（{RCLONE_BIN}）。アップロードをスキップします。")
        return False
    if res.returncode == 0:
        logger.info("アップロード完了。ローカルファイルを削除します。")
        cleanup_dir(OUTPUT_DIR)
        return True
    out = res.stdout.decode("utf-8", errors="replace")[-2000:] if res.stdout else "(none)"
    logger.warning(f"アップロード失敗(コード: {res.returncode})。ファイルは保持。出力: {out}")
    return False


# ============================================================
# 録画本体
# ============================================================
def record_and_upload(with_chat=True):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    base_name = f"{OUTPUT_DIR}/{STREAMER_ID}_live"
    video_pattern = f"{base_name}.%(ext)s"
    chat_file = f"{base_name}_chat.json"

    logger.info(f"配信検知！録画開始... target={TARGET_URL}")

    # --- A. 動画DL ---
    p_video = subprocess.Popen(
        [YT_DLP_BIN, "--live-from-start", "-o", video_pattern, TARGET_URL]
    )

    # --- B. チャットDL（chat_downloader があれば） ---
    p_chat = None
    if with_chat:
        p_chat = subprocess.Popen(
            [CHAT_DOWNLOADER_BIN, TARGET_URL, "--output", chat_file],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    # --- 停滞監視しながら p_video の終了を待つ ---
    last_size = get_dir_size(OUTPUT_DIR)
    last_change = time.time()

    while p_video.poll() is None:
        time.sleep(CHECK_INTERVAL)
        cur = get_dir_size(OUTPUT_DIR)
        if cur != last_size:
            logger.info(f"DL中... size={cur / (1024 ** 2):.1f}MB")
            last_size = cur
            last_change = time.time()
        else:
            stalled = int(time.time() - last_change)
            if stalled >= STALL_TIMEOUT:
                logger.warning(f"{STALL_TIMEOUT}秒サイズ変化なし。配信終了とみなし停止。")
                p_video.terminate()
                try:
                    p_video.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    p_video.kill()
                break

    # --- チャット停止 ---
    if p_chat is not None:
        p_chat.terminate()
        try:
            p_chat.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p_chat.kill()

    upload_to_drive()


# ============================================================
# エントリポイント（1回完結。旧VPS版の無限ループは持たない）
# ============================================================
def main():
    logger.info("===== Keeby Twitch Recorder (Actions版) 起動 =====")
    logger.info(f"対象: {TARGET_URL}")

    if not is_streaming():
        logger.info("配信していません。何もしないで終了します。")
        return

    have_chat = (
        os.path.isfile(CHAT_DOWNLOADER_BIN)
        and os.access(CHAT_DOWNLOADER_BIN, os.X_OK)
    )
    if not have_chat:
        logger.warning(f"{CHAT_DOWNLOADER_BIN} が見つかりません。チャット保存なしで続行。")

    record_and_upload(with_chat=have_chat)
    logger.info("終了処理完了。次回の起動はcronに任せる。")


if __name__ == "__main__":
    main()
