#!/usr/bin/env python3
"""
Keeby YouTube Live Recorder (GitHub Actions edition)

旧VPS常時起動スクリプト (keeby-live-downloader.py) のActions版。
while True ループを除き、**1回のjobで完結**する:

    ライブ検知 → (動画DL + コメントDL 並行) → Google Driveへアップロード → 後始末 → 終了

15分ごとにcronで起動されるため、常時プロセスを持たず、PCの電源が入っていなくても録画できる。

環境変数（すべて workflow の Secrets/Variables から与传统経由で注入）:
    YOUTUBE_API_KEYS    : カンマ区切りのYouTube Data APIキー
    COOKIES_FILE        : 書き出したcookies.txtのパス（403対策、任意）
    RCLONE_REMOTE       : rcloneのリモート名（例: gdrive）
    GDRIVE_VIDEO_DEST   : 動画のアップロード先
    GDRIVE_COMMENT_DEST : コメントのアップロード先
    KEEBY_BASE_DIR      : 作業ルート（既定: cwd）
    YT_DLP_BIN          : yt-dlpのパス（既定: yt-dlp）
"""

import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ============================================================
# ログ設定
# ============================================================
LOG_DIR = os.environ.get("LOG_DIR", "./logs")
os.makedirs(LOG_DIR, exist_ok=True)

import logging  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "recorder.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# ============================================================
# 設定
# ============================================================
API_KEYS = [k.strip() for k in os.environ.get("YOUTUBE_API_KEYS", "").split(",") if k.strip()]

CHANNEL_IDS = [
    "UCUDxTV-LMKn29uZOMVRLDFQ",  # keeby_boy チャンネル
    "UCfn5XCDhHzNyJny-wSuKR4w",  # きんたま チャンネル
]

STALL_TIMEOUT = 1800          # 30分間サイズ変化なしなら停滞(=配信終了)とみなす
WATCHDOG_CHECK_INTERVAL = 60   # 1分ごとにサイズチェック

BASE_DIR = os.environ.get("KEEBY_BASE_DIR", os.getcwd())
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
COMMENTS_DIR = os.path.join(BASE_DIR, "comments")

YT_DLP_BIN = os.environ.get("YT_DLP_BIN", "yt-dlp")
RCLONE_BIN = os.environ.get("RCLONE_BIN", "rclone")
RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "gdrive")
GDRIVE_VIDEO_DEST = os.environ.get("GDRIVE_VIDEO_DEST", f"{RCLONE_REMOTE}:/keeby/videos")
GDRIVE_COMMENT_DEST = os.environ.get(
    "GDRIVE_COMMENT_DEST", f"{RCLONE_REMOTE}:/keeby/comments"
)


# ============================================================
# ユーティリティ
# ============================================================
class YouTubeAPIManager:
    """複数のAPIキーをフェイルオーバーで回す（VPS版と同一）"""

    def __init__(self, api_keys):
        self.api_keys = api_keys
        self.current_key_index = 0
        self.youtube_clients = [
            build("youtube", "v3", developerKey=key) for key in api_keys if key
        ]
        if not self.youtube_clients:
            raise RuntimeError("YOUTUBE_API_KEYS が設定されていません。")

    def get_current_client(self):
        return self.youtube_clients[self.current_key_index]

    def switch_to_next_key(self):
        self.current_key_index = (self.current_key_index + 1) % len(self.youtube_clients)


def check_internet_connection():
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=3)
        return True
    except OSError:
        return False


def get_dir_size(path):
    """ディレクトリ内の全ファイルの合計サイズ(bytes)"""
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


def format_bytes(size):
    """バイト数を人間が読みやすい表記に変換（旧VPS版と同一）"""
    size = float(size)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def rescue_part_files(download_dir):
    """
    yt-dlpがハング等で結合に失敗した .part ファイル群を手動で ffmpeg 結合する。
    （VPS版 rescue_part_files と同一ロジック）
    """
    import re

    logger.info("保険機能: 未結合の .part ファイルから .mp4 の手動復旧を試みます。")
    if not os.path.exists(download_dir):
        return

    files = os.listdir(download_dir)
    groups = {}
    pattern = re.compile(r"^(.*?)(?:\.f[a-zA-Z0-9]+)?(\.[a-zA-Z0-9]+)\.part$")

    for f in files:
        if not f.endswith(".part"):
            continue
        m = pattern.match(f)
        if m:
            groups.setdefault(m.group(1), []).append(f)

    for base_name, parts in groups.items():
        parts = sorted(parts)
        out_path = os.path.join(download_dir, f"{base_name}.mp4")

        cmd = ["ffmpeg", "-y"]
        for i in range(min(2, len(parts))):  # 映像と音声で最大2つ
            cmd.extend(["-i", os.path.join(download_dir, parts[i])])
        cmd.extend(["-c", "copy", out_path])

        logger.info(f"結合コマンド実行: {' '.join(cmd)}")
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if res.returncode == 0:
            logger.info(f"復旧成功: {base_name}.mp4")
            for p in parts:
                p_path = os.path.join(download_dir, p)
                if os.path.exists(p_path):
                    os.remove(p_path)
                ytdl_path = p_path.replace(".part", ".ytdl")
                if os.path.exists(ytdl_path):
                    os.remove(ytdl_path)
        else:
            logger.error(f"復旧に失敗しました: {base_name}")


# ============================================================
# ライブ監視
# ============================================================
def check_live_status(api_manager, target_channel_id):
    """対象チャンネルがライブ中ならvideoId、ライブでなければNone。"""
    try:
        if not check_internet_connection():
            logger.warning("ネット未接続。")
            return None

        youtube = api_manager.get_current_client()
        request = youtube.search().list(
            part="snippet",
            channelId=target_channel_id,
            eventType="live",
            type="video",
            order="date",
            maxResults=1,
        )
        response = request.execute()
        api_manager.switch_to_next_key()

        if response["items"]:
            return response["items"][0]["id"]["videoId"]
        return None
    except HttpError as e:
        logger.error(f"API Error (Channel: {target_channel_id}): {e}")
        api_manager.switch_to_next_key()
        return None
    except Exception as e:
        logger.error(f"Unknown error: {e}")
        api_manager.switch_to_next_key()
        return None


# ============================================================
# ウォッチドッグ付きダウンロード実行
# ============================================================
def run_with_watchdog(command, watch_dir, label="process"):
    """
    子プロセスを起動し、watch_dir のサイズを定期監視。
    STALL_TIMEOUT 秒間サイズが変化しなければプロセスを強制終了（配信終了とみなす）。
    旧VPS版と同一ロジック。
    """
    proc = subprocess.Popen(command, start_new_session=True)
    logger.info(f"[{label}] PID {proc.pid} (PGID {os.getpgid(proc.pid)}) で開始")

    last_size = get_dir_size(watch_dir)
    last_change_time = time.time()

    while proc.poll() is None:
        time.sleep(WATCHDOG_CHECK_INTERVAL)
        current_size = get_dir_size(watch_dir)

        if current_size != last_size:
            elapsed = int(time.time() - last_change_time)
            logger.info(
                f"[{label}] DL中... サイズ: {format_bytes(current_size)} "
                f"(+{format_bytes(current_size - last_size)} / {elapsed}秒)"
            )
            last_size = current_size
            last_change_time = time.time()
        else:
            stall_seconds = int(time.time() - last_change_time)
            if stall_seconds >= STALL_TIMEOUT:
                logger.warning(
                    f"[{label}] {STALL_TIMEOUT // 60}分間サイズ変化なし "
                    f"(サイズ: {format_bytes(current_size)})。SIGINTでクリーンに終了。"
                )
                pgid = os.getpgid(proc.pid)
                try:
                    os.killpg(pgid, signal.SIGINT)
                except ProcessLookupError:
                    pass

                try:
                    proc.wait(timeout=300)  # ffmpeg結合に猶予5分
                except subprocess.TimeoutExpired:
                    logger.warning(f"[{label}] SIGINT後5分経過。SIGTERM送信。")
                    try:
                        os.killpg(pgid, signal.SIGTERM)
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        logger.warning(f"[{label}] SIGTERM応答なし。SIGKILL。")
                        try:
                            os.killpg(pgid, signal.SIGKILL)
                            proc.wait()
                        except ProcessLookupError:
                            pass
                    except ProcessLookupError:
                        pass
                return -1
            elif stall_seconds >= 300:
                logger.info(
                    f"[{label}] サイズ変化なし {stall_seconds // 60}分経過... "
                    f"(残り{(STALL_TIMEOUT - stall_seconds) // 60}分)"
                )

    logger.info(f"[{label}] 終了 (コード: {proc.returncode})")
    return proc.returncode


# ============================================================
# アップロード・後始末
# ============================================================
def upload_with_rclone(cmd):
    """rclone アップロード。失敗しても例外を投げずログのみ（jobは失敗させない）。"""
    logger.info(f"rclone: {' '.join(cmd)}")
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if res.returncode != 0:
        out = res.stdout.decode("utf-8", errors="replace")[-2000:] if res.stdout else "(none)"
        logger.warning(f"rclone が失敗 (コード: {res.returncode})。出力: {out}")
    else:
        logger.info("rclone 完了。")


def cleanup_dir(path):
    """ディレクトリ内の全ファイルを削除（旧VPS版の rm -rf 相当）"""
    try:
        for entry in os.scandir(path):
            try:
                if entry.is_file():
                    os.remove(entry.path)
            except OSError as e:
                logger.warning(f"削除エラー {entry.path}: {e}")
    except OSError as e:
        logger.warning(f"クリーンアップエラー {path}: {e}")


# ============================================================
# メイン録画処理（VPS版 execute_scripts を1回完結版に）
# ============================================================
def execute_scripts(live_url, cookies_file=None):
    logger.info(f"========== ライブ開始検出: {live_url} ==========")

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(COMMENTS_DIR, exist_ok=True)

    # --- コメント取得をバックグラウンドで開始 ---
    comment_cmd = [YT_DLP_BIN, "--write-comments", "--skip-download", "--add-metadata"]
    if cookies_file:
        comment_cmd += ["--cookies", cookies_file]
    comment_cmd += ["-o", os.path.join(COMMENTS_DIR, "%(title)s [%(id)s].%(ext)s"), live_url]

    logger.info("コメント取得を開始...")
    comment_proc = subprocess.Popen(comment_cmd, start_new_session=True)

    # --- 動画ダウンロード（ウォッチドッグ付き） ---
    video_cmd = [
        YT_DLP_BIN,
        "--live-from-start",
        "--retries", "infinite",
        "--fragment-retries", "infinite",
        "--socket-timeout", "30",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "-o", os.path.join(DOWNLOAD_DIR, "%(title)s [%(id)s].%(ext)s"),
    ]
    if cookies_file:
        video_cmd += ["--cookies", cookies_file]
    video_cmd.append(live_url)

    video_exit = run_with_watchdog(video_cmd, DOWNLOAD_DIR, label="動画DL")

    # --- 動画DL終了後にコメントDLも終了させる ---
    logger.info("動画DLが終了。コメント取得プロセスを終了...")
    try:
        comment_pgid = os.getpgid(comment_proc.pid)
        os.killpg(comment_pgid, signal.SIGINT)
        comment_proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        logger.warning("コメント取得プロセスを強制終了(SIGKILL)")
        try:
            os.killpg(os.getpgid(comment_proc.pid), signal.SIGKILL)
            comment_proc.wait()
        except Exception as e:
            logger.warning(f"強制終了中にエラー: {e}")
    except (ProcessLookupError, OSError):
        pass
    except Exception as e:
        logger.warning(f"コメント終了処理でエラー: {e}")

    # --- アップロード前の結合保険 ---
    dl_size = get_dir_size(DOWNLOAD_DIR)
    if dl_size > 0:
        logger.info("[+] ダウンロード結果のチェック中...")
        part_files = [
            f for f in os.listdir(DOWNLOAD_DIR)
            if f.endswith(".part") or f.endswith(".ytdl")
        ]
        if part_files:
            logger.warning("未結合の .part/.ytdl が残っています。手動再結合を試みます。")
            rescue_part_files(DOWNLOAD_DIR)

        logger.info(f"Google Driveへアップロード中... (動画: {format_bytes(dl_size)})")
        upload_with_rclone(
            [RCLONE_BIN, "copy", DOWNLOAD_DIR, GDRIVE_VIDEO_DEST, "--drive-chunk-size=64M"]
        )
        upload_with_rclone(
            [RCLONE_BIN, "copy", COMMENTS_DIR, GDRIVE_COMMENT_DEST, "--drive-chunk-size=64M"]
        )
        cleanup_dir(DOWNLOAD_DIR)
        cleanup_dir(COMMENTS_DIR)
        logger.info("アップロード完了 & ローカル削除済み。")
    else:
        logger.warning("ダウンロードファイルが0バイトです。アップロードをスキップします。")

    if video_exit == -1:
        logger.info("(ウォッチドッグにより停止 — 配信は正常に終了した可能性が高い)")

    logger.info("========== 録画サイクル完了 ==========")


# ============================================================
# エントリポイント（1回完結。旧VPS版の無限ループは持たない）
# ============================================================
def main():
    logger.info("===== Keeby YouTube Recorder (Actions版) 起動 =====")
    logger.info(f"監視チャンネル数: {len(CHANNEL_IDS)}")
    logger.info(f"ウォッチドッグタイムアウト: {STALL_TIMEOUT // 60}分")
    logger.info(f"開始時刻: {datetime.now().isoformat(timespec='seconds')}")

    if not API_KEYS:
        logger.error("YOUTUBE_API_KEYS が未設定です。")
        sys.exit(2)

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(COMMENTS_DIR, exist_ok=True)

    api_manager = YouTubeAPIManager(API_KEYS)

    cookies_file = os.environ.get("COOKIES_FILE")
    if cookies_file:
        if os.path.exists(cookies_file):
            logger.info(f"Cookie を使用します: {cookies_file}")
        else:
            logger.warning(f"COOKIES_FILE が存在しません: {cookies_file}（Cookieなしで続行）")
            cookies_file = None
    else:
        logger.info("COOKIES_FILE 未設定（Cookieなしで続行）")

    # --- ライブチェック（各チャンネルを1回だけ。1つでも見つかれば録画） ---
    found_live = False
    for channel_id in CHANNEL_IDS:
        logger.info(f"Checking: {channel_id}...")
        live_id = check_live_status(api_manager, channel_id)

        if live_id:
            url = f"https://www.youtube.com/watch?v={live_id}"
            logger.info(f"ライブ配信を検知: {url}")
            execute_scripts(url, cookies_file=cookies_file)
            found_live = True
            break
        logger.info("  → Liveなし")
        time.sleep(1)

    if not found_live:
        logger.info("どのチャンネルもライブ配信なし。何もしないで終了します。")
    else:
        logger.info("録画とコメント保存が完了しました。")
    logger.info("終了処理完了。次回の起動はcronに任せる。")


if __name__ == "__main__":
    main()
