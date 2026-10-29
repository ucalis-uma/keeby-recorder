#!/usr/bin/env python3
"""
Keeby YouTube Live Recorder (GitHub Actions edition)

旧VPS常時起動スクリプト (keeby-live-downloader.py) のActions版。

【重要】GitHub Actions の schedule は 15分間隔では走らない。
公開リポジトリでは GitHub 側が schedule を間引くため、実測では
1回あたり 3.5〜6.1時間（中央値 5.3時間）空く。そのため
「15分ごとに起動して1回だけ見る」設計では配信を取りこぼす。

そこで job 自体を「監視ループ」にし、最大 WATCH_MAX_SECONDS の間
POLL_INTERVAL ごとにライブを確認し続ける。監視時間を終えたら
workflow_dispatch で次の job を自分で起動する（SELF_DISPATCH=1）ため、
cron が間引かれても監視が途切れない。

    監視ループ → ライブ検知 → (動画DL + コメントDL 並行)
               → Google Driveへアップロード → 後始末 → 次jobを起動

環境変数（workflow の Secrets/Variables から注入）:
    YOUTUBE_API_KEYS      : カンマ区切りのYouTube Data APIキー
    COOKIES_FILE          : 書き出したcookies.txtのパス（403対策、任意）
    RCLONE_REMOTE         : rcloneのリモート名（例: gdrive）
    GDRIVE_VIDEO_DEST     : 動画のアップロード先
    GDRIVE_COMMENT_DEST   : コメントのアップロード先
    KEEBY_BASE_DIR        : 作業ルート（既定: cwd）
    YT_DLP_BIN            : yt-dlpのパス（既定: yt-dlp）
    WATCH_MAX_SECONDS     : 1 job の監視時間（既定 0=1回だけ）
    POLL_INTERVAL         : 監視ループの間隔秒（既定 300）
    SELF_DISPATCH         : "1" なら監視終了時に次 job を起動する
    SELF_WORKFLOW_FILE    : 自己ディスパッチする workflow ファイル名
"""

import os
import re
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

# --- 監視ループ（cronの間引き対策） ---
# GitHubのscheduleは15分間隔では走らない（実測 中央値5.3時間）。
# job側でポーリングし続け、終了時に次jobを自己起動して監視を切らさない。
try:
    WATCH_MAX_SECONDS = int(os.environ.get("WATCH_MAX_SECONDS", "0") or "0")
except ValueError:
    WATCH_MAX_SECONDS = 0
try:
    POLL_INTERVAL = max(60, int(os.environ.get("POLL_INTERVAL", "300") or "300"))
except ValueError:
    POLL_INTERVAL = 300
SELF_DISPATCH = os.environ.get("SELF_DISPATCH", "") == "1"
SELF_WORKFLOW_FILE = os.environ.get("SELF_WORKFLOW_FILE", "")
# 監視ループ内で API が全滅し続けたらジョブを失敗させる（緑のまま見逃さない）
try:
    MAX_CONSECUTIVE_API_ERRORS = int(os.environ.get("MAX_CONSECUTIVE_API_ERRORS", "5") or "5")
except ValueError:
    MAX_CONSECUTIVE_API_ERRORS = 5
# search.list は 100 units と高いので、低コスト検知の補助として N ポーリングに1回だけ使う
try:
    SEARCH_FALLBACK_EVERY = max(1, int(os.environ.get("SEARCH_FALLBACK_EVERY", "24") or "24"))
except ValueError:
    SEARCH_FALLBACK_EVERY = 24
# rclone のアップロード試行回数
try:
    UPLOAD_RETRIES = max(1, int(os.environ.get("UPLOAD_RETRIES", "3") or "3"))
except ValueError:
    UPLOAD_RETRIES = 3

# 終了コード（workflow 側で job を赤くするために使う）
EXIT_API_ERROR = 3       # API が連続失敗（quota/認証/ネットワーク）
EXIT_UPLOAD_ERROR = 4    # 録画はできたが Drive へ上げられなかった
EXIT_DOWNLOAD_ERROR = 5  # ライブは検知したが録画できなかった

# --- テスト用（workflow_dispatch の inputs から注入。既定は本番動作） ---
# TEST_LIVE_URL : 指定時は API 検知をスキップし、その URL を直接録画する
# TEST_MAX_SECONDS : 指定時はその秒数で動画DLを打ち切りドライラン終了する
# SKIP_UPLOAD : "1" のとき rclone 実行とローカル削除を両方スキップする
TEST_LIVE_URL = os.environ.get("TEST_LIVE_URL", "").strip()
try:
    TEST_MAX_SECONDS = int(os.environ.get("TEST_MAX_SECONDS", "0") or "0")
except ValueError:
    TEST_MAX_SECONDS = 0
SKIP_UPLOAD = os.environ.get("SKIP_UPLOAD", "") == "1"
# MAX_DOWNLOAD_GB: テスト時にダウンロード量を GB で打ち切る（0=無制限。本番は0）
try:
    MAX_DOWNLOAD_BYTES = int(
        float(os.environ.get("MAX_DOWNLOAD_GB", "0") or "0") * (1024 ** 3)
    )
except ValueError:
    MAX_DOWNLOAD_BYTES = 0

# PROBE_ONLY=1: ダウンロードせず、どの player_client なら通るかだけを検証する
PROBE_ONLY = os.environ.get("PROBE_ONLY", "") == "1"
# USE_COOKIES=1: Cookie を最優先で使う（既定は匿名→Cookieの順）。
# 匿名で通れば Cookie の定期更新が不要になるため、既定は匿名優先。
USE_COOKIES_FIRST = os.environ.get("USE_COOKIES", "") == "1"
# 1回の録画で結合するセグメント数の上限（HLSの一時終端で切れた時に継ぎ足す）
try:
    MAX_SEGMENTS = max(1, int(os.environ.get("MAX_SEGMENTS", "30") or "30"))
except ValueError:
    MAX_SEGMENTS = 30

BASE_DIR = os.environ.get("KEEBY_BASE_DIR", os.getcwd())
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
COMMENTS_DIR = os.path.join(BASE_DIR, "comments")

YT_DLP_BIN = os.environ.get("YT_DLP_BIN", "yt-dlp")
# yt-dlp のスタンドアロンバイナリは既定で ~/.config/yt-dlp/plugins を
# 拾わないことがある（実測: Plugin directories: none）。明示的に渡す。
YT_DLP_PLUGIN_DIR = os.environ.get(
    "YT_DLP_PLUGIN_DIR",
    os.path.join(os.path.expanduser("~"), ".config", "yt-dlp", "plugins"),
)
# 出口IPを変えたいとき（WARP 等）に HTTP プロキシを指定する
YTDLP_PROXY = os.environ.get("YTDLP_PROXY", "").strip()
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
    """複数のAPIキーをフェイルオーバーで回す。

    旧実装は「1回呼んで失敗したら次のキーに切り替えるだけ」で、
    同じ呼び出しを再試行しなかったため、quota 切れのキーに当たると
    その回の検知は失敗したまま終わっていた。ここではキー数ぶん再試行する。
    """

    QUOTA_MARKERS = (
        "quotaExceeded",
        "dailyLimitExceeded",
        "rateLimitExceeded",
        "userRateLimitExceeded",
    )

    def __init__(self, api_keys):
        self.api_keys = api_keys
        self.current_key_index = 0
        self.youtube_clients = [
            build("youtube", "v3", developerKey=key) for key in api_keys if key
        ]
        if not self.youtube_clients:
            raise RuntimeError("YOUTUBE_API_KEYS が設定されていません。")
        self.uploads_playlists = {}

    def get_current_client(self):
        return self.youtube_clients[self.current_key_index]

    def switch_to_next_key(self):
        before = self.current_key_index
        self.current_key_index = (self.current_key_index + 1) % len(self.youtube_clients)
        if len(self.youtube_clients) > 1:
            logger.warning(f"APIキーを切り替えます: #{before} → #{self.current_key_index}")

    @classmethod
    def _is_quota_error(cls, err):
        text = str(err)
        return any(m in text for m in cls.QUOTA_MARKERS) or "quota" in text.lower()

    def call(self, fn, what):
        """API呼び出しをキー数ぶんリトライする。全滅したら例外を投げる。"""
        last_error = None
        for _ in range(len(self.youtube_clients)):
            try:
                return fn(self.get_current_client())
            except HttpError as e:
                last_error = e
                if self._is_quota_error(e):
                    logger.error(f"{what}: quota/rate limit に到達。次のキーで再試行します。")
                    self.switch_to_next_key()
                    continue
                # 権限エラー等はキーを変えても無駄なので即座に送出
                raise
            except Exception as e:  # ネットワーク断など
                last_error = e
                logger.error(f"{what}: {type(e).__name__}: {e}")
                self.switch_to_next_key()
                continue
        raise RuntimeError(f"{what}: すべてのAPIキーで失敗しました: {last_error}")


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
    """バイト数を人間が読みやすい表記に変換（負の差分にも対応）"""
    size = float(size)
    sign = "-" if size < 0 else ""
    size = abs(size)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{sign}{size:.1f}{unit}"
        size /= 1024
    return f"{sign}{size:.1f}TB"


def ytdlp_base_cmd():
    """yt-dlp の共通引数。PO Token plugin とプロキシを明示的に渡す。"""
    cmd = [YT_DLP_BIN]
    if os.path.isdir(YT_DLP_PLUGIN_DIR):
        cmd += ["--plugin-dirs", YT_DLP_PLUGIN_DIR]
    if YTDLP_PROXY:
        cmd += ["--proxy", YTDLP_PROXY]
    return cmd


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
def get_uploads_playlist_id(api_manager, channel_id):
    """チャンネルの uploads プレイリストIDを取得（1 unit・job内でキャッシュ）。"""
    cached = api_manager.uploads_playlists.get(channel_id)
    if cached:
        return cached

    def _call(yt):
        return yt.channels().list(
            part="contentDetails", id=channel_id, maxResults=1
        ).execute()

    resp = api_manager.call(_call, f"channels.list({channel_id})")
    items = resp.get("items") or []
    if not items:
        raise RuntimeError(f"チャンネルが見つかりません: {channel_id}")
    playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    api_manager.uploads_playlists[channel_id] = playlist_id
    return playlist_id


def find_live_video_id(api_manager, channel_id):
    """uploads プレイリストの直近動画からライブ中のものを探す（合計2〜3 units）。

    search.list(eventType=live) は 100 units/回で、5分ポーリングでは
    1日でquotaを食い潰す。channels/playlistItems/videos は各1 unitで済む。
    """
    playlist_id = get_uploads_playlist_id(api_manager, channel_id)

    def _items(yt):
        return yt.playlistItems().list(
            part="contentDetails", playlistId=playlist_id, maxResults=10
        ).execute()

    items = api_manager.call(_items, f"playlistItems.list({channel_id})").get("items") or []
    video_ids = []
    for item in items:
        vid = (item.get("contentDetails") or {}).get("videoId")
        if vid and vid not in video_ids:
            video_ids.append(vid)
    if not video_ids:
        return None

    def _videos(yt):
        return yt.videos().list(
            part="snippet,liveStreamingDetails",
            id=",".join(video_ids),
            maxResults=len(video_ids),
        ).execute()

    videos = api_manager.call(_videos, f"videos.list({channel_id})").get("items") or []
    for video in videos:
        details = video.get("liveStreamingDetails") or {}
        # actualStartTime があって actualEndTime が無い = ライブ中
        if details.get("actualStartTime") and not details.get("actualEndTime"):
            return video["id"]
    return None


def search_live_video_id(api_manager, channel_id):
    """補助用: search.list(eventType=live)。100 units なので低頻度でのみ使う。

    uploads の直近10件に現れない長期配信（24時間配信など）を拾うための保険。
    """
    def _call(yt):
        return yt.search().list(
            part="snippet",
            channelId=channel_id,
            eventType="live",
            type="video",
            order="date",
            maxResults=1,
        ).execute()

    resp = api_manager.call(_call, f"search.list({channel_id})")
    items = resp.get("items") or []
    if items:
        return items[0]["id"]["videoId"]
    return None


def check_live_status(api_manager, target_channel_id, use_search_fallback=False):
    """対象チャンネルがライブ中なら videoId、ライブでなければ None。"""
    if not check_internet_connection():
        raise RuntimeError("ネット未接続")
    if use_search_fallback:
        logger.info(f"  search.list で確認（補助・100units）: {target_channel_id}")
        return search_live_video_id(api_manager, target_channel_id)
    return find_live_video_id(api_manager, target_channel_id)


# ============================================================
# セグメント継続（HLSの一時終端対策）
# ============================================================
# 2026-10-29 の実配信テストで、配信が続いているのに HLS が ENDLIST を返し、
# yt-dlp が exit 0 で終了して録画が 1.6GB で途切れた。
# 「exit 0 = 配信終了」と決めつけず、Data API で still live を確認して継ぎ足す。
VIDEO_ID_URL_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})")
VIDEO_ID_BRACKET_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]")
MEDIA_EXTS = (".mp4", ".mkv", ".webm", ".ts", ".flv", ".m4a")


def extract_video_id(url):
    """URL から動画IDを取り出す（取れなければ None）。"""
    m = VIDEO_ID_URL_RE.search(url or "")
    return m.group(1) if m else None


def get_video_state(api_manager, video_id):
    """(title, is_live) を返す。判定できなければ (None, None)。

    videos.list は 1 unit。player API を叩かないので bot 判定の材料にならない。
    """
    if api_manager is None or not video_id:
        return None, None
    try:
        def _call(yt):
            return yt.videos().list(
                part="snippet,liveStreamingDetails", id=video_id, maxResults=1
            ).execute()

        resp = api_manager.call(_call, f"videos.list({video_id})")
    except Exception as e:
        logger.warning(f"ライブ状態の取得に失敗: {type(e).__name__}: {e}")
        return None, None

    items = resp.get("items") or []
    if not items:
        return None, None
    item = items[0]
    details = item.get("liveStreamingDetails") or {}
    is_live = bool(details.get("actualStartTime")) and not details.get("actualEndTime")
    return (item.get("snippet") or {}).get("title"), is_live


def _media_files(directory):
    out = []
    for name in os.listdir(directory):
        if name.endswith(".part") or name.endswith(".ytdl"):
            continue
        if os.path.splitext(name)[1].lower() in MEDIA_EXTS:
            out.append(name)
    return out


def derive_stem(directory):
    """最初のセグメントのファイル名から拡張子を除いた stem を返す。"""
    files = _media_files(directory)
    if not files:
        return None
    files.sort(key=lambda n: os.path.getsize(os.path.join(directory, n)), reverse=True)
    return os.path.splitext(files[0])[0]


def concat_segments(directory, stem):
    """stem + stem part02... を ffmpeg で1本に結合する。失敗時は None（元は保持）。"""
    files = _media_files(directory)
    first = [f for f in files if os.path.splitext(f)[0] == stem]
    parts = sorted(
        f for f in files if os.path.splitext(f)[0].startswith(stem + " part")
    )
    if not first or not parts:
        return None

    ordered = first[:1] + parts
    ext = os.path.splitext(ordered[0])[1]
    merged = stem + ".merged" + ext
    list_path = os.path.join(directory, "_concat.txt")

    with open(list_path, "w", encoding="utf-8") as f:
        for name in ordered:
            f.write("file '" + name.replace("'", "'\\''") + "'\n")

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-f", "concat", "-safe", "0", "-i", "_concat.txt",
        "-c", "copy", merged,
    ]
    logger.info(f"セグメントを結合します: {len(ordered)}本 → {merged}")
    try:
        res = subprocess.run(
            cmd, cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
    except FileNotFoundError:
        logger.error("ffmpeg が見つからないため結合できません（セグメントは保持）。")
        return None

    if res.returncode != 0:
        out = (res.stdout or b"").decode("utf-8", "replace")[-1000:]
        logger.error(f"結合に失敗しました（セグメントは保持）: {out}")
        return None

    for name in ordered:
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            pass
    try:
        os.remove(list_path)
    except OSError:
        pass
    logger.info(f"結合完了: {merged}")
    return merged


# ============================================================
# 自己ディスパッチ（cronの間引き対策）
# ============================================================
def maybe_dispatch_next(reason):
    """監視の切れ目で次の job を workflow_dispatch する。

    GitHub の schedule は公開リポジトリで大幅に間引かれる（実測 中央値5.3時間）。
    監視ループの終了時に次 job を自分で起動することで、cron に依存せず監視を継続する。

    戻り値: True=起動した/不要, False=起動に失敗（呼び出し側で job を失敗させる）
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


# ============================================================
# ウォッチドッグ付きダウンロード実行
# ============================================================
def _log_stderr_tail(stderr_path, label):
    """yt-dlp の stderr 末尾をログに出す（失敗原因の特定用）。"""
    try:
        with open(stderr_path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as e:
        logger.error(f"[{label}] stderr読取失敗 ({stderr_path}): {e}")
        return
    tail = "\n".join(lines[-30:])
    if tail:
        logger.error(f"[{label}] yt-dlp stderr末尾:\n{tail}")
    else:
        logger.error(f"[{label}] stderr が空でした ({stderr_path})")


def run_with_watchdog(command, watch_dir, label="process", max_seconds=0, max_bytes=0):
    """
    子プロセスを起動し、watch_dir のサイズを定期監視。
    STALL_TIMEOUT 秒間サイズが変化しなければプロセスを強制終了（配信終了とみなす）。
    旧VPS版と同一ロジック。max_seconds > 0 のときはテスト用にその秒数で打ち切る。
    max_bytes > 0 のときはダウンロード量がその値に達したら打ち切る（容量テスト用）。
    戻り値: 終了コード（停滞打ち切り=-1、時間打ち切り=-2、サイズ上限=-3）。
    yt-dlp の stdout/stderr は LOG_DIR/ytdlp-<label>-*.log に保存し、
    失敗時は stderr 末尾をこのログにも転記する（原因特定用）。
    """
    import re

    safe_label = {"動画DL": "video", "動画DL再試行": "video-retry"}.get(
        label, re.sub(r"[^A-Za-z0-9_-]+", "_", label)
    )
    stdout_path = os.path.join(LOG_DIR, f"ytdlp-{safe_label}-stdout.log")
    stderr_path = os.path.join(LOG_DIR, f"ytdlp-{safe_label}-stderr.log")
    logger.info(f"[{label}] CMD: {' '.join(command)}")
    f_out = open(stdout_path, "w", encoding="utf-8", errors="replace")
    f_err = open(stderr_path, "w", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(command, stdout=f_out, stderr=f_err, start_new_session=True)
    except Exception:
        f_out.close()
        f_err.close()
        raise
    logger.info(f"[{label}] PID {proc.pid} (PGID {os.getpgid(proc.pid)}) で開始")
    if max_seconds > 0:
        logger.info(f"[{label}] テストモード: {max_seconds}秒で打ち切ります。")

    def _close_logs():
        try:
            f_out.close()
        except Exception:
            pass
        try:
            f_err.close()
        except Exception:
            pass

    last_size = get_dir_size(watch_dir)
    last_change_time = time.time()
    start_time = time.time()
    # テスト時は短時間打ち切りのため監視間隔を短くする（本番は60秒のまま）
    interval = (
        min(10, WATCHDOG_CHECK_INTERVAL)
        if (max_seconds > 0 or max_bytes > 0)
        else WATCHDOG_CHECK_INTERVAL
    )

    while proc.poll() is None:
        time.sleep(interval)
        if max_seconds > 0 and (time.time() - start_time) >= max_seconds:
            logger.info(f"[{label}] テスト時間に到達。SIGINTで停止します。")
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                logger.warning(f"[{label}] 停止待ちタイムアウト。SIGKILLします。")
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    proc.wait()
                except ProcessLookupError:
                    pass
            _close_logs()
            return -2
        current_size = get_dir_size(watch_dir)

        if max_bytes > 0 and current_size >= max_bytes:
            logger.info(
                f"[{label}] サイズ上限 {format_bytes(max_bytes)} に到達"
                f"（現在 {format_bytes(current_size)}）。SIGINTで停止します。"
            )
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=300)  # ffmpeg結合の猶予
            except subprocess.TimeoutExpired:
                logger.warning(f"[{label}] 停止待ちタイムアウト。SIGKILLします。")
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    proc.wait()
                except ProcessLookupError:
                    pass
            _close_logs()
            return -3

        if current_size != last_size:
            elapsed = int(time.time() - last_change_time)
            delta = current_size - last_size
            delta_text = (
                f"+{format_bytes(delta)}" if delta >= 0 else format_bytes(delta)
            )
            logger.info(
                f"[{label}] DL中... サイズ: {format_bytes(current_size)} "
                f"({delta_text} / {elapsed}秒)"
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
                _close_logs()
                return -1
            elif stall_seconds >= 300:
                logger.info(
                    f"[{label}] サイズ変化なし {stall_seconds // 60}分経過... "
                    f"(残り{(STALL_TIMEOUT - stall_seconds) // 60}分)"
                )

    logger.info(f"[{label}] 終了 (コード: {proc.returncode})")
    _close_logs()
    if proc.returncode not in (0, None):
        _log_stderr_tail(stderr_path, label)
    return proc.returncode


# ============================================================
# アップロード・後始末
# ============================================================
def upload_with_rclone(cmd, retries=None):
    """rclone アップロード。成功時 True。失敗・未検出時は False（例外は投げない）。

    Drive 満杯などの一時的でない失敗もあるため、リトライしつつ
    最終的に失敗したら False を返して呼び出し側で job を失敗させる。
    """
    retries = UPLOAD_RETRIES if retries is None else retries
    last_out = ""
    for attempt in range(1, retries + 1):
        logger.info(f"rclone (試行 {attempt}/{retries}): {' '.join(cmd)}")
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except FileNotFoundError:
            logger.error(f"rclone が見つかりません（{cmd[0]}）。アップロードできません。")
            return False
        out = res.stdout.decode("utf-8", errors="replace") if res.stdout else ""
        last_out = out
        if res.returncode == 0:
            logger.info("rclone 完了。")
            return True
        logger.error(f"rclone が失敗 (コード: {res.returncode})。出力: {out[-2000:]}")
        low = out.lower()
        if "storagequotaexceeded" in low or "quota" in low or "insufficient" in low:
            logger.error(
                "Google Drive の空き容量が不足しています（storageQuotaExceeded）。"
                "ドライブの整理 or プラン変更が必要です。"
            )
        if attempt < retries:
            time.sleep(30)
    logger.error(f"rclone は {retries} 回失敗しました。最終出力: {last_out[-1000:]}")
    return False


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


def _read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _is_cookie_error(text):
    markers = (
        "cookies are no longer valid",
        "Sign in to confirm you",
        "Use --cookies-from-browser",
        "cookies for the authentication",
        "HTTP Error 403",
    )
    return any(m in text for m in markers)


# ============================================================
# メイン録画処理（VPS版 execute_scripts を1回完結版に）
# ============================================================
def execute_scripts(live_url, cookies_file=None, api_manager=None):
    """録画〜アップロードを1回実行する。

    戻り値: "ok" / "upload_failed" / "download_failed" / "no_data"
    """
    logger.info(f"========== ライブ開始検出: {live_url} ==========")

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(COMMENTS_DIR, exist_ok=True)

    # --- コメント取得をバックグラウンドで開始 ---
    # 出力は LOG_DIR に保存し、失敗時は末尾を転記する（動画DLと同様）。
    comment_cmd = ytdlp_base_cmd() + ["--write-comments", "--skip-download", "--add-metadata",
                   "--no-playlist"]
    if cookies_file:
        comment_cmd += ["--cookies", cookies_file]
    comment_cmd += ["-o", os.path.join(COMMENTS_DIR, "%(title)s [%(id)s].%(ext)s"), live_url]

    logger.info("コメント取得を開始...")
    comment_stdout_path = os.path.join(LOG_DIR, "ytdlp-comment-stdout.log")
    comment_stderr_path = os.path.join(LOG_DIR, "ytdlp-comment-stderr.log")
    comment_f_out = open(comment_stdout_path, "w", encoding="utf-8", errors="replace")
    comment_f_err = open(comment_stderr_path, "w", encoding="utf-8", errors="replace")
    comment_proc = subprocess.Popen(
        comment_cmd, stdout=comment_f_out, stderr=comment_f_err, start_new_session=True
    )

    # --- 動画ダウンロード（ウォッチドッグ付き・セグメント継続） ---
    # 1回の yt-dlp が「配信終了」と判断しても、Data API 上まだライブなら
    # 続きを別セグメントとして録り、最後に ffmpeg で1本に結合する。
    # （2026-10-29: HLS が一時的に ENDLIST を返し、配信継続中なのに 1.6GB で
    #   exit 0 した。exit 0 = 配信終了と決めつけると録画が途切れる。）

    def make_video_cmd(out_template, use_cookies=False, extra=None, format_expr=None):
        cmd = ytdlp_base_cmd()
        # テスト時は通常の動画URLでも落とせるよう --live-from-start を外す（本番は付与）。
        if not TEST_LIVE_URL:
            cmd.append("--live-from-start")
        cmd += [
            "--retries", "infinite",
            "--fragment-retries", "infinite",
            "--socket-timeout", "30",
            "--no-playlist",
            "-f", format_expr or "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "-o", out_template,
        ]
        if extra:
            cmd += extra
        if use_cookies and cookies_file:
            cmd += ["--cookies", cookies_file]
        cmd.append(live_url)
        return cmd

    def build_strategies(out_template):
        """匿名 → クライアント変更 → 緩い形式 → 最後に Cookie、の順で試す。

        Cookie は「あれば安定」ではなく、2026年は失効すると即エラーになる。
        匿名で通るなら Cookie の定期更新が不要なので、既定は匿名優先。
        """
        strategies = []
        if USE_COOKIES_FIRST and cookies_file:
            strategies.append(
                ("Cookie優先", make_video_cmd(out_template, use_cookies=True))
            )
        strategies += [
            (
                "android_vr",
                make_video_cmd(
                    out_template,
                    extra=["--extractor-args", "youtube:player_client=android_vr"],
                ),
            ),
            (
                "mweb",
                make_video_cmd(
                    out_template,
                    extra=["--extractor-args", "youtube:player_client=mweb"],
                ),
            ),
            (
                "tv_simply",
                make_video_cmd(
                    out_template,
                    extra=["--extractor-args", "youtube:player_client=tv_simply"],
                ),
            ),
            ("匿名", make_video_cmd(out_template)),
            (
                "web_safari",
                make_video_cmd(
                    out_template,
                    extra=["--extractor-args", "youtube:player_client=web_safari"],
                ),
            ),
            (
                "web_embedded",
                make_video_cmd(
                    out_template,
                    extra=["--extractor-args", "youtube:player_client=web_embedded"],
                ),
            ),
            (
                "tv",
                make_video_cmd(
                    out_template,
                    extra=["--extractor-args", "youtube:player_client=tv"],
                ),
            ),
            ("緩いフォーマット", make_video_cmd(out_template, format_expr="bv*+ba/b")),
        ]
        if cookies_file and not USE_COOKIES_FIRST:
            strategies.append(
                ("Cookieフォールバック", make_video_cmd(out_template, use_cookies=True))
            )
        return strategies

    video_id = extract_video_id(live_url)
    started_at = time.time()
    stem = None
    segment = 1
    video_exit = None

    while True:
        if segment == 1:
            out_template = os.path.join(DOWNLOAD_DIR, "%(title)s [%(id)s].%(ext)s")
        else:
            # stem はタイトル由来なので、yt-dlp のテンプレート記号 % をエスケープする
            out_template = os.path.join(
                DOWNLOAD_DIR, f"{stem.replace('%', '%%')} part{segment:02d}.%(ext)s"
            )

        segment_exit = None
        size_before = get_dir_size(DOWNLOAD_DIR)
        for index, (name, cmd) in enumerate(build_strategies(out_template)):
            if index > 0:
                # 前のセグメントのファイルがあるため、増えたかどうかで判定する
                if segment_exit in (0, -1, -2, -3) or get_dir_size(DOWNLOAD_DIR) > size_before:
                    break
                logger.warning(
                    f"録画に失敗（コード {segment_exit}）。別の方式で再試行します: {name}"
                )
            logger.info(f"[segment {segment}] 試行: {name}")
            segment_exit = run_with_watchdog(
                cmd,
                DOWNLOAD_DIR,
                label=f"video-s{segment}-{index}",
                max_seconds=TEST_MAX_SECONDS,
                max_bytes=MAX_DOWNLOAD_BYTES,
            )
            if index == 0 and segment_exit not in (0, -1, -2, -3):
                stderr_text = _read_text(
                    os.path.join(LOG_DIR, f"ytdlp-video-s{segment}-{index}-stderr.log")
                )
                if _is_cookie_error(stderr_text):
                    logger.warning(
                        "Cookie が失効/拒否されています。COOKIES_TXT の更新が必要です"
                        "（別方式で再試行を続けます）。"
                    )

        video_exit = segment_exit

        if stem is None:
            stem = derive_stem(DOWNLOAD_DIR)
            if stem is None:
                logger.error("動画ファイルが作成されませんでした。")
                break

        # --- 打ち切り系は継続しない ---
        if video_exit in (-2, -3):
            break
        if segment >= MAX_SEGMENTS:
            logger.info(f"セグメント上限 {MAX_SEGMENTS} に達しました。")
            break
        if TEST_MAX_SECONDS > 0 and time.time() - started_at >= TEST_MAX_SECONDS:
            logger.info("テスト時間の上限に達しました。")
            break
        if MAX_DOWNLOAD_BYTES > 0 and get_dir_size(DOWNLOAD_DIR) >= MAX_DOWNLOAD_BYTES:
            break

        # --- 配信が続いているなら継ぎ足す（exit 0 を鵜呑みにしない） ---
        vid = video_id
        if not vid:
            m = VIDEO_ID_BRACKET_RE.search(stem)
            vid = m.group(1) if m else None
        _, is_live = get_video_state(api_manager, vid)
        if is_live is None:
            logger.info("ライブ状態を確認できないため、このセグメントで終了します。")
            break
        if not is_live:
            logger.info("配信は終了しています。録画を完了します。")
            break

        segment += 1
        logger.info(
            f"配信は継続中です（{format_bytes(get_dir_size(DOWNLOAD_DIR))}）。"
            f"セグメント {segment} を開始します。"
        )
        time.sleep(3)

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
    finally:
        try:
            comment_f_out.close()
        except Exception:
            pass
        try:
            comment_f_err.close()
        except Exception:
            pass

    # --- アップロード前の結合保険 ---
    status = "no_data"
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

        # セグメントを1本に結合（失敗してもセグメントのままアップロードされる）
        if stem:
            concat_segments(DOWNLOAD_DIR, stem)
            dl_size = get_dir_size(DOWNLOAD_DIR)

        if SKIP_UPLOAD:
            logger.info(
                f"SKIP_UPLOAD=1 のため Drive へ上げずローカル保持します "
                f"(動画: {format_bytes(dl_size)}。artifact で回収できます)。"
            )
            status = "ok"
        else:
            logger.info(f"Google Driveへアップロード中... (動画: {format_bytes(dl_size)})")
            ok_video = upload_with_rclone(
                [RCLONE_BIN, "copy", DOWNLOAD_DIR, GDRIVE_VIDEO_DEST, "--drive-chunk-size=64M"]
            )
            ok_comment = upload_with_rclone(
                [RCLONE_BIN, "copy", COMMENTS_DIR, GDRIVE_COMMENT_DEST, "--drive-chunk-size=64M"]
            )
            if ok_video and ok_comment:
                cleanup_dir(DOWNLOAD_DIR)
                cleanup_dir(COMMENTS_DIR)
                logger.info("アップロード完了 & ローカル削除済み。")
                status = "ok"
            else:
                # ここで黙って緑にすると「録画はしたのにDriveに無い」が起きる。
                # ローカルは残し、job を失敗させて artifact で回収できるようにする。
                logger.error(
                    "アップロードに失敗しました。録画ファイルは破棄せず保持します"
                    "（job を失敗させ、artifact として退避します）。"
                )
                status = "upload_failed"
    else:
        logger.error("ダウンロードファイルが0バイトです。録画に失敗しています。")
        status = "download_failed"

    if video_exit == -1:
        logger.info("(ウォッチドッグにより停止 — 配信は正常に終了した可能性が高い)")
    elif video_exit == -2:
        logger.info("(テスト時間打ち切り — ドライラン正常終了)")
    elif video_exit == -3:
        logger.info(
            f"(サイズ上限 {format_bytes(MAX_DOWNLOAD_BYTES)} で打ち切り — 容量テスト正常終了)"
        )

    logger.info(f"========== 録画サイクル完了 (status={status}) ==========")
    return status


# ============================================================
# PROBE（経路の実測）
# ============================================================
def probe_clients(live_url, cookies_file=None):
    """ダウンロードせず、どの player_client が通るかだけを検証する。

    Cookie を捨てられるか / PO Token が必要かを、推測ではなく実測で決めるための
    一時モード（PROBE_ONLY=1）。player API を数回叩くだけで済む。
    """
    logger.info("===== PROBE MODE =====")
    logger.info(f"対象: {live_url}")

    try:
        res = subprocess.run([YT_DLP_BIN, "--version"], capture_output=True, timeout=60)
        logger.info(f"yt-dlp: {res.stdout.decode('utf-8', 'replace').strip()}")
    except Exception as e:
        logger.error(f"yt-dlp の実行に失敗: {e}")
        return EXIT_DOWNLOAD_ERROR

    # PO Token provider のプラグインが読み込まれているか（導入後の確認用）
    try:
        res = subprocess.run(
            ytdlp_base_cmd() + ["-v", "--simulate", "--no-warnings", live_url],
            capture_output=True,
            timeout=180,
        )
        text = (res.stdout + res.stderr).decode("utf-8", "replace")
        for line in text.splitlines():
            if "Plugin directories" in line or "PO Token Providers" in line:
                logger.info(f"  {line.strip()}")
    except Exception as e:
        logger.warning(f"プラグイン確認に失敗: {type(e).__name__}: {e}")

    def cmd_for(use_cookies=False, extra=None):
        cmd = ytdlp_base_cmd() + [
            "--simulate", "--no-warnings", "--no-playlist",
            "--print", "%(id)s|%(live_status)s|%(format_id)s",
        ]
        if not TEST_LIVE_URL:
            cmd.append("--live-from-start")
        cmd += ["-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"]
        if extra:
            cmd += extra
        if use_cookies and cookies_file:
            cmd += ["--cookies", cookies_file]
        cmd.append(live_url)
        return cmd

    candidates = [
        ("android_vr", {"extra": ["--extractor-args", "youtube:player_client=android_vr"]}),
        ("web", {"extra": ["--extractor-args", "youtube:player_client=web"]}),
        ("mweb", {"extra": ["--extractor-args", "youtube:player_client=mweb"]}),
        ("tv_simply", {"extra": ["--extractor-args", "youtube:player_client=tv_simply"]}),
        ("web_safari", {"extra": ["--extractor-args", "youtube:player_client=web_safari"]}),
        ("web_embedded", {"extra": ["--extractor-args", "youtube:player_client=web_embedded"]}),
        ("tv", {"extra": ["--extractor-args", "youtube:player_client=tv"]}),
        ("匿名", {}),
    ]
    if cookies_file:
        candidates.append(("Cookie", {"use_cookies": True}))

    results = []
    for name, kwargs in candidates:
        try:
            res = subprocess.run(cmd_for(**kwargs), capture_output=True, timeout=180)
        except subprocess.TimeoutExpired:
            results.append((name, False, "timeout"))
            continue
        ok = res.returncode == 0
        out = res.stdout.decode("utf-8", "replace").strip().splitlines()
        err = res.stderr.decode("utf-8", "replace").strip().splitlines()
        detail = (out[-1] if (ok and out) else (err[-1] if err else ""))[:160]
        results.append((name, ok, detail))

    logger.info("===== PROBE RESULT =====")
    for name, ok, detail in results:
        logger.info(f"  [{'OK' if ok else 'NG'}] {name:<14} {detail}")

    # ライブが全滅しても、配信終了後の VOD なら保存できるか（後追いフォールバックの判断）
    vod_url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    try:
        res = subprocess.run(
            ytdlp_base_cmd()
            + ["--simulate", "--no-warnings", "--print", "%(id)s|%(duration)s", vod_url],
            capture_output=True,
            timeout=180,
        )
        vod_ok = res.returncode == 0
        vod_detail = (
            res.stdout.decode("utf-8", "replace").strip()
            if vod_ok
            else res.stderr.decode("utf-8", "replace").strip().splitlines()[-1][:160]
        )
    except subprocess.TimeoutExpired:
        vod_ok, vod_detail = False, "timeout"
    logger.info(f"  [{'OK' if vod_ok else 'NG'}] {'VOD':<14} {vod_detail}")
    logger.info("=======================")

    if not any(ok for _, ok, _ in results):
        logger.error("全経路が NG。PO Token provider か Cookie が必要です。")
        return EXIT_DOWNLOAD_ERROR
    return 0


# ============================================================
# エントリポイント
# ============================================================
def main():
    """監視ループ → 検知 → 録画 → アップロード。戻り値は終了コード。"""
    logger.info("===== Keeby YouTube Recorder (Actions版) 起動 =====")
    logger.info(f"監視チャンネル数: {len(CHANNEL_IDS)}")
    logger.info(f"ウォッチドッグタイムアウト: {STALL_TIMEOUT // 60}分")
    logger.info(f"開始時刻: {datetime.now().isoformat(timespec='seconds')}")
    logger.info(
        f"監視モード: WATCH_MAX_SECONDS={WATCH_MAX_SECONDS} "
        f"POLL_INTERVAL={POLL_INTERVAL} SELF_DISPATCH={int(SELF_DISPATCH)}"
    )
    if TEST_LIVE_URL:
        logger.info(f"テストモード: TEST_LIVE_URL={TEST_LIVE_URL}")
    if TEST_MAX_SECONDS > 0:
        logger.info(f"テストモード: TEST_MAX_SECONDS={TEST_MAX_SECONDS}秒")
    if SKIP_UPLOAD:
        logger.info("テストモード: SKIP_UPLOAD=1（Drive へ上げずローカル保持）")
    if MAX_DOWNLOAD_BYTES > 0:
        logger.info(
            f"テストモード: ダウンロード上限 {format_bytes(MAX_DOWNLOAD_BYTES)}"
        )

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(COMMENTS_DIR, exist_ok=True)

    cookies_file = os.environ.get("COOKIES_FILE") or None
    if cookies_file and not os.path.exists(cookies_file):
        logger.warning(f"COOKIES_FILE が存在しません: {cookies_file}（Cookieなしで続行）")
        cookies_file = None
    if cookies_file:
        logger.info(f"Cookie を利用可能: {cookies_file}（既定は匿名優先）")
    else:
        logger.info("Cookie なしで動作します。")

    api_manager = None
    if API_KEYS:
        try:
            api_manager = YouTubeAPIManager(API_KEYS)
        except Exception as e:
            logger.warning(f"API マネージャの初期化に失敗: {type(e).__name__}: {e}")

    # --- PROBE モード（経路の実測だけして終了） ---
    if PROBE_ONLY:
        url = TEST_LIVE_URL
        if not url and api_manager:
            for channel_id in CHANNEL_IDS:
                try:
                    vid = find_live_video_id(api_manager, channel_id)
                except Exception as e:
                    logger.warning(f"ライブ検知に失敗 ({channel_id}): {e}")
                    continue
                if vid:
                    url = f"https://www.youtube.com/watch?v={vid}"
                    break
        if not url:
            url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
            logger.warning("ライブが見つからないため、固定動画で経路だけ検証します。")
        return probe_clients(url, cookies_file)

    if TEST_LIVE_URL:
        # テスト時は API 検知をスキップし、指定 URL を直接録画する。
        status = execute_scripts(
            TEST_LIVE_URL, cookies_file=cookies_file, api_manager=api_manager
        )
        logger.info("テスト録画が完了しました。")
        return 0 if status == "ok" else EXIT_DOWNLOAD_ERROR

    if not API_KEYS:
        logger.error("YOUTUBE_API_KEYS が未設定です。")
        return 2

    api_manager = api_manager or YouTubeAPIManager(API_KEYS)

    # --- 監視ループ ---
    # cron は 15分間隔では来ない（実測 中央値5.3時間）。job 内でポーリングし、
    # 監視時間を終えたら次 job を自己起動して切れ目を無くす。
    deadline = time.time() + WATCH_MAX_SECONDS if WATCH_MAX_SECONDS > 0 else 0
    poll = 0
    consecutive_api_errors = 0

    while True:
        poll += 1
        use_search = poll % SEARCH_FALLBACK_EVERY == 1
        logger.info(f"--- 監視ポーリング #{poll} （search補助={use_search}） ---")

        live_id = None
        channel_errors = 0
        for channel_id in CHANNEL_IDS:
            try:
                logger.info(f"Checking: {channel_id}...")
                live_id = check_live_status(
                    api_manager, channel_id, use_search_fallback=use_search
                )
            except Exception as e:
                channel_errors += 1
                logger.error(f"ライブ検知に失敗 ({channel_id}): {type(e).__name__}: {e}")
                continue
            if live_id:
                break
            logger.info("  → Liveなし")
            time.sleep(1)

        # --- 検知したら録画（次jobを保険として先に起動しておく） ---
        if live_id:
            url = f"https://www.youtube.com/watch?v={live_id}"
            logger.info(f"ライブ配信を検知: {url}")
            maybe_dispatch_next("ライブ検知時（job上限で切れた場合の保険）")
            status = execute_scripts(
                url, cookies_file=cookies_file, api_manager=api_manager
            )
            if status == "ok":
                logger.info("録画とアップロードが完了しました。")
                maybe_dispatch_next("録画完了後（監視を継続）")
                return 0
            if status == "upload_failed":
                # Drive 満杯などで失敗しても監視自体は続ける（cron頼みにしない）。
                maybe_dispatch_next("アップロード失敗後の監視継続")
                return EXIT_UPLOAD_ERROR
            maybe_dispatch_next("録画失敗後の監視継続")
            return EXIT_DOWNLOAD_ERROR

        # --- API が全チャンネルで失敗し続けたら赤くする（黙って見逃さない） ---
        if channel_errors == len(CHANNEL_IDS):
            consecutive_api_errors += 1
            logger.error(
                f"全チャンネルでライブ検知に失敗 ({consecutive_api_errors}/"
                f"{MAX_CONSECUTIVE_API_ERRORS})"
            )
            if consecutive_api_errors >= MAX_CONSECUTIVE_API_ERRORS:
                logger.error("API が連続失敗しました。ジョブを失敗させます。")
                return EXIT_API_ERROR
        else:
            consecutive_api_errors = 0

        # --- 監視時間の終了判定 ---
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
