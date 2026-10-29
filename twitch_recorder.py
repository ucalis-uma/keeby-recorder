#!/usr/bin/env python3
"""
Keeby Twitch Live Recorder (GitHub Actions edition)

旧VPSの twitch_dl_vps.py のActions版。

【重要】GitHub Actions の schedule は 15分間隔では走らない。
公開リポジトリでは GitHub 側が schedule を間引くため、実測では
1回あたり 3.2〜6.8時間（中央値 5.7時間）空く。そのため
job 自体を監視ループにし、終了時に次 job を自己起動して監視を切らさない。

    監視ループ → 配信検知 → (動画DL + チャットDL 並行)
               → 終了待ち → Google Driveへアップロード → 次jobを起動
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

# --- 監視ループ（cronの間引き対策） ---
try:
    WATCH_MAX_SECONDS = int(os.environ.get("WATCH_MAX_SECONDS", "0") or "0")
except ValueError:
    WATCH_MAX_SECONDS = 0
try:
    POLL_INTERVAL = max(60, int(os.environ.get("POLL_INTERVAL", "180") or "180"))
except ValueError:
    POLL_INTERVAL = 180
SELF_DISPATCH = os.environ.get("SELF_DISPATCH", "") == "1"
SELF_WORKFLOW_FILE = os.environ.get("SELF_WORKFLOW_FILE", "")
try:
    MAX_CONSECUTIVE_API_ERRORS = int(os.environ.get("MAX_CONSECUTIVE_API_ERRORS", "5") or "5")
except ValueError:
    MAX_CONSECUTIVE_API_ERRORS = 5
try:
    UPLOAD_RETRIES = max(1, int(os.environ.get("UPLOAD_RETRIES", "3") or "3"))
except ValueError:
    UPLOAD_RETRIES = 3

EXIT_API_ERROR = 3
EXIT_UPLOAD_ERROR = 4
EXIT_DOWNLOAD_ERROR = 5

# --- テスト用（workflow_dispatch の inputs から注入。既定は本番動作） ---
# TEST_TARGET_URL : 指定時は配信検知をスキップし、その URL を直接録画する
# TEST_MAX_SECONDS : 指定時はその秒数で打ち切りドライラン終了する
# SKIP_UPLOAD : "1" のとき rclone 実行せずローカル保持する（Drive 不使用）
TEST_TARGET_URL = os.environ.get("TEST_TARGET_URL", "").strip()
try:
    TEST_MAX_SECONDS = int(os.environ.get("TEST_MAX_SECONDS", "0") or "0")
except ValueError:
    TEST_MAX_SECONDS = 0
SKIP_UPLOAD = os.environ.get("SKIP_UPLOAD", "") == "1"


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
    """配信中か判定（yt-dlp --simulate）。

    戻り値: True=配信中 / False=配信なし / None=判定失敗（エラー）
    例外を False と区別できるようにして、API障害で黙って見逃さない。
    """
    try:
        res = subprocess.run(
            [YT_DLP_BIN, "--simulate", "--quiet", "--no-warnings", TARGET_URL],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        logger.error(f"{YT_DLP_BIN} が見つかりません。")
        return None
    except Exception as e:
        logger.error(f"配信判定で例外: {type(e).__name__}: {e}")
        return None

    err = res.stderr.decode("utf-8", errors="replace").strip()
    if res.returncode == 0:
        return True
    # オフライン（チャンネルが配信していない）は yt-dlp が非0で返す。
    # ネットワーク/レート制限/その他の異常は判定失敗として扱う。
    offline_markers = (
        "not currently live",
        "is offline",
        "This channel is not currently live",
        "No video formats found",
        "Unable to find video",
        "HTTP Error 404",
    )
    if any(m in err for m in offline_markers):
        return False
    if err:
        logger.warning(f"配信判定の応答: {err[-500:]}")
    return None


def maybe_dispatch_next(reason):
    """監視の切れ目で次の job を workflow_dispatch する（cron間引き対策）。

    戻り値: True=起動した/不要, False=起動に失敗
    """
    if not SELF_DISPATCH:
        return True
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    ref = os.environ.get("GITHUB_REF_NAME", "main")
    if not (repo and token and SELF_WORKFLOW_FILE):
        logger.error(
            "自己ディスパッチの設定が不足しています "
            f"(repo={bool(repo)}, token={bool(token)}, file={SELF_WORKFLOW_FILE!r})"
        )
        return False
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{SELF_WORKFLOW_FILE}/dispatches"
    try:
        import requests

        res = requests.post(
            url,
            json={"ref": ref},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )
        if res.status_code in (201, 204):
            logger.info(f"次の監視jobを起動しました（{reason}）。")
            return True
        logger.error(f"自己ディスパッチ失敗: {res.status_code} {res.text[:300]}")
        return False
    except Exception as e:
        logger.error(f"自己ディスパッチ例外: {type(e).__name__}: {e}")
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
    """OUTPUT_DIR を Google Drive へアップロード。成功時のみローカル削除。

    失敗した場合は False を返し、呼び出し側で job を失敗させる（録画の消失を防ぐ）。
    """
    import shutil as _shutil

    if _shutil.which(RCLONE_BIN) is None:
        logger.error(f"rclone が見つかりません（{RCLONE_BIN}）。アップロードできません。")
        return False
    time.sleep(3)  # rclone がファイルを確定するのを待つ
    cmd = [RCLONE_BIN, "copy", OUTPUT_DIR, GDRIVE_TWITCH_DEST, "--drive-chunk-size=64M"]
    for attempt in range(1, UPLOAD_RETRIES + 1):
        logger.info(f"Google Driveへアップロード (試行 {attempt}/{UPLOAD_RETRIES}): {' '.join(cmd)}")
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except FileNotFoundError:
            logger.error(f"rclone が見つかりません（{RCLONE_BIN}）。アップロードできません。")
            return False
        if res.returncode == 0:
            logger.info("アップロード完了。ローカルファイルを削除します。")
            cleanup_dir(OUTPUT_DIR)
            return True
        out = res.stdout.decode("utf-8", errors="replace")[-2000:] if res.stdout else "(none)"
        logger.error(f"アップロード失敗(コード: {res.returncode})。出力: {out}")
        low = out.lower()
        if "storagequotaexceeded" in low or "quota" in low:
            logger.error(
                "Google Drive の空き容量が不足しています（storageQuotaExceeded）。"
                "ドライブの整理 or プラン変更が必要です。"
            )
        if attempt < UPLOAD_RETRIES:
            time.sleep(30)
    logger.error("アップロードは全試行で失敗しました。ファイルは保持します。")
    return False


# ============================================================
# 録画本体
# ============================================================
def record_and_upload(with_chat=True, target_url=None, max_seconds=0):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    target_url = target_url or TARGET_URL

    base_name = f"{OUTPUT_DIR}/{STREAMER_ID}_live"
    video_pattern = f"{base_name}.%(ext)s"
    chat_file = f"{base_name}_chat.json"

    logger.info(f"配信検知！録画開始... target={target_url}")
    if max_seconds > 0:
        logger.info(f"テストモード: {max_seconds}秒で打ち切ります。")

    # --- A. 動画DL ---
    # テスト時は通常の動画URLでも落とせるよう --live-from-start を外す（本番は付与）。
    # yt-dlp の stdout/stderr は LOG_DIR に保存し、失敗時は末尾を転記する
    # （YouTube 側の test で終了コード1の原因が残らず特定不能だった反省）。
    video_cmd = [YT_DLP_BIN]
    if not TEST_TARGET_URL:
        video_cmd.append("--live-from-start")
    video_cmd += ["-o", video_pattern, target_url]
    logger.info(f"CMD: {' '.join(video_cmd)}")
    video_stdout_path = os.path.join(LOG_DIR, "ytdlp-twitch-stdout.log")
    video_stderr_path = os.path.join(LOG_DIR, "ytdlp-twitch-stderr.log")
    f_out = open(video_stdout_path, "w", encoding="utf-8", errors="replace")
    f_err = open(video_stderr_path, "w", encoding="utf-8", errors="replace")
    try:
        p_video = subprocess.Popen(video_cmd, stdout=f_out, stderr=f_err)
    except Exception:
        f_out.close()
        f_err.close()
        raise

    # --- B. チャットDL（chat_downloader があれば） ---
    p_chat = None
    if with_chat:
        p_chat = subprocess.Popen(
            [CHAT_DOWNLOADER_BIN, target_url, "--output", chat_file],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    # --- 停滞監視しながら p_video の終了を待つ ---
    last_size = get_dir_size(OUTPUT_DIR)
    last_change = time.time()
    start_time = time.time()
    test_cutoff = False
    # テスト時は短時間打ち切りのため監視間隔を短くする（本番は既定60秒のまま）
    interval = min(10, CHECK_INTERVAL) if max_seconds > 0 else CHECK_INTERVAL

    while p_video.poll() is None:
        time.sleep(interval)
        if max_seconds > 0 and (time.time() - start_time) >= max_seconds:
            logger.info("テスト時間に到達。動画DLを停止します。")
            p_video.terminate()
            try:
                p_video.wait(timeout=60)
            except subprocess.TimeoutExpired:
                p_video.kill()
            test_cutoff = True
            break
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

    video_code = p_video.poll()
    try:
        f_out.close()
    except Exception:
        pass
    try:
        f_err.close()
    except Exception:
        pass
    if video_code not in (0, None):
        try:
            with open(video_stderr_path, encoding="utf-8", errors="replace") as f:
                tail = "\n".join(f.read().splitlines()[-30:])
        except OSError as e:
            tail = f"(stderr読取失敗: {e})"
        if tail:
            logger.error(f"yt-dlp stderr末尾 (code={video_code}):\n{tail}")
        else:
            logger.error(f"yt-dlp stderr が空でした (code={video_code})。")

    # --- チャット停止 ---
    if p_chat is not None:
        p_chat.terminate()
        try:
            p_chat.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p_chat.kill()

    if SKIP_UPLOAD:
        logger.info("SKIP_UPLOAD=1 のため Drive へ上げずローカル保持します（artifact で回収できます）。")
        status = "ok"
    else:
        status = "ok" if upload_to_drive() else "upload_failed"
        if status == "upload_failed":
            logger.error(
                "アップロードに失敗しました。録画ファイルは保持し、job を失敗させます"
                "（artifact として退避します）。"
            )

    if test_cutoff:
        logger.info("(テスト時間打ち切り — ドライラン正常終了)")

    size = get_dir_size(OUTPUT_DIR)
    if size == 0 and not test_cutoff:
        logger.error("録画ファイルが0バイトです。録画に失敗しています。")
        return "download_failed"
    logger.info(f"録画サイクル完了 (status={status}, size={size / (1024 ** 2):.1f}MB)")
    return status


# ============================================================
# エントリポイント
# ============================================================
def main():
    """監視ループ → 検知 → 録画 → アップロード。戻り値は終了コード。"""
    logger.info("===== Keeby Twitch Recorder (Actions版) 起動 =====")
    logger.info(f"対象: {TARGET_URL}")
    logger.info(
        f"監視モード: WATCH_MAX_SECONDS={WATCH_MAX_SECONDS} "
        f"POLL_INTERVAL={POLL_INTERVAL} SELF_DISPATCH={int(SELF_DISPATCH)}"
    )
    if TEST_TARGET_URL:
        logger.info(f"テストモード: TEST_TARGET_URL={TEST_TARGET_URL}")
    if TEST_MAX_SECONDS > 0:
        logger.info(f"テストモード: TEST_MAX_SECONDS={TEST_MAX_SECONDS}秒")
    if SKIP_UPLOAD:
        logger.info("テストモード: SKIP_UPLOAD=1（Drive へ上げずローカル保持）")

    target_url = TEST_TARGET_URL or TARGET_URL

    if TEST_TARGET_URL:
        # テスト時は配信検知をスキップし、指定 URL を直接録画する。
        have_chat = (
            os.path.isfile(CHAT_DOWNLOADER_BIN)
            and os.access(CHAT_DOWNLOADER_BIN, os.X_OK)
        )
        if not have_chat:
            logger.warning(f"{CHAT_DOWNLOADER_BIN} が見つかりません。チャット保存なしで続行。")
        status = record_and_upload(
            with_chat=have_chat, target_url=target_url, max_seconds=TEST_MAX_SECONDS
        )
        logger.info("テスト録画が完了しました。")
        if status == "ok":
            return 0
        return EXIT_UPLOAD_ERROR if status == "upload_failed" else EXIT_DOWNLOAD_ERROR

    have_chat = (
        os.path.isfile(CHAT_DOWNLOADER_BIN)
        and os.access(CHAT_DOWNLOADER_BIN, os.X_OK)
    )
    if not have_chat:
        logger.warning(f"{CHAT_DOWNLOADER_BIN} が見つかりません。チャット保存なしで続行。")

    # --- 監視ループ（cron は実測 中央値5.7時間間隔でしか来ない） ---
    deadline = time.time() + WATCH_MAX_SECONDS if WATCH_MAX_SECONDS > 0 else 0
    poll = 0
    consecutive_errors = 0

    while True:
        poll += 1
        logger.info(f"--- 監視ポーリング #{poll} ---")
        streaming = is_streaming()

        if streaming is None:
            consecutive_errors += 1
            logger.error(
                f"配信判定に失敗 ({consecutive_errors}/{MAX_CONSECUTIVE_API_ERRORS})"
            )
            if consecutive_errors >= MAX_CONSECUTIVE_API_ERRORS:
                logger.error("配信判定が連続失敗しました。ジョブを失敗させます。")
                return EXIT_API_ERROR
        else:
            consecutive_errors = 0

        if streaming:
            logger.info("配信中を検知しました。録画を開始します。")
            maybe_dispatch_next("配信検知時（job上限で切れた場合の保険）")
            status = record_and_upload(with_chat=have_chat)
            if status == "ok":
                logger.info("録画とアップロードが完了しました。")
                maybe_dispatch_next("録画完了後（監視を継続）")
                return 0
            if status == "upload_failed":
                maybe_dispatch_next("アップロード失敗後の監視継続")
                return EXIT_UPLOAD_ERROR
            maybe_dispatch_next("録画失敗後の監視継続")
            return EXIT_DOWNLOAD_ERROR

        logger.info("配信していません。")
        if deadline and time.time() >= deadline:
            logger.info(
                f"監視時間 {WATCH_MAX_SECONDS // 60}分が経過しました。"
                "次の監視jobを起動して終了します。"
            )
            if not maybe_dispatch_next("監視時間の終了"):
                logger.error("次jobを起動できませんでした。監視の連鎖が切れます。")
                return EXIT_API_ERROR
            return 0
        if not deadline:
            # 単発モード（手動実行の既定）。1回確認して終了する。
            logger.info("監視時間が0のため1回だけ確認して終了します。")
            return 0

        sleep_seconds = POLL_INTERVAL
        if deadline:
            sleep_seconds = min(POLL_INTERVAL, max(10, int(deadline - time.time())))
        logger.info(f"{sleep_seconds}秒後に再確認します。")
        time.sleep(max(10, sleep_seconds))


if __name__ == "__main__":
    sys.exit(main())
