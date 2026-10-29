#!/usr/bin/env python3
"""recorder.py のロジックテスト（Google API / yt-dlp を呼ばない）。

`python check_all.py` から自動実行される。実際の通信は一切しない。

確認する内容:
  - 低コスト検知（channels → playlistItems → videos、search.list は補助のみ）
  - APIキーの quota フェイルオーバー
  - 監視ループと終了コード 3/4/5
  - 1.6GB 事故の再発防止（HLS 一時終端からのセグメント継続）
  - 403 の再発防止（先頭セグメントは必ず --live-from-start）
"""
import logging
import os
import pathlib
import shutil
import sys
import tempfile
import types

REPO = pathlib.Path(__file__).resolve().parent

# --- googleapiclient のスタブ ---
pkg = types.ModuleType("googleapiclient")
pkg.__path__ = []
disc = types.ModuleType("googleapiclient.discovery")
errs = types.ModuleType("googleapiclient.errors")


class HttpError(Exception):
    def __init__(self, resp, content, uri=None):
        self.resp = resp
        self.content = content
        self.uri = uri
        super().__init__(content.decode() if isinstance(content, bytes) else str(content))


class _Resp:
    status = 403
    reason = "Forbidden"


def _build(*a, **k):
    return CURRENT_CLIENT[0]


disc.build = _build
errs.HttpError = HttpError
pkg.discovery = disc
pkg.errors = errs
sys.modules["googleapiclient"] = pkg
sys.modules["googleapiclient.discovery"] = disc
sys.modules["googleapiclient.errors"] = errs

TMP = pathlib.Path(tempfile.mkdtemp(prefix="recorder-logic-"))
os.environ["KEEBY_BASE_DIR"] = str(TMP)
os.environ["LOG_DIR"] = str(TMP / "logs")
os.environ["YOUTUBE_API_KEYS"] = "keyA,keyB"
os.environ["WATCH_MAX_SECONDS"] = "3600"
os.environ["POLL_INTERVAL"] = "60"
os.environ["SELF_DISPATCH"] = "0"
os.environ["SKIP_UPLOAD"] = "0"
os.environ["TEST_LIVE_URL"] = ""
os.environ["MAX_DOWNLOAD_GB"] = "0"
os.environ["PROBE_ONLY"] = ""
os.environ["USE_COOKIES"] = ""
os.environ["YTDLP_PROXY"] = ""

sys.path.insert(0, str(REPO))
import recorder  # noqa: E402

REAL_EXECUTE = recorder.execute_scripts
REAL_DISPATCH = recorder.maybe_dispatch_next

recorder.logger.handlers = [
    h for h in recorder.logger.handlers
    if not (isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler))
]
recorder.logger.propagate = False
# テスト中は recorder のログを黙らせる（PASS/FAIL だけを見たい）
recorder.logger.setLevel(logging.CRITICAL)
recorder.time.sleep = lambda *_: None
recorder.check_internet_connection = lambda: True

CURRENT_CLIENT = [None]
failures = []


def check(name, cond, detail=""):
    line = ("PASS " if cond else "FAIL ") + name
    if detail and not cond:
        line += " :: " + str(detail)
    print(line)
    if not cond:
        failures.append(name)


# ============================================================
# 偽 API クライアント
# ============================================================
class FakeChannelList:
    def __init__(self, client):
        self.c = client

    def list(self, **kw):
        self.c.calls.append("channels")
        return self

    def execute(self):
        self.c.maybe_raise("channels")
        return {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU" + self.c.ch}}}]}


class FakePlaylistItems:
    def __init__(self, client):
        self.c = client

    def list(self, **kw):
        self.c.calls.append("playlistItems")
        return self

    def execute(self):
        self.c.maybe_raise("playlistItems")
        return {"items": [{"contentDetails": {"videoId": self.c.live_id}}]}


class FakeVideos:
    def __init__(self, client):
        self.c = client

    def list(self, **kw):
        self.c.calls.append("videos")
        return self

    def execute(self):
        self.c.maybe_raise("videos")
        item = {"id": self.c.live_id, "liveStreamingDetails": {}}
        if self.c.is_live:
            item["liveStreamingDetails"]["actualStartTime"] = "2026-10-29T00:00:00Z"
        return {"items": [item]}


class FakeSearch:
    def __init__(self, client):
        self.c = client

    def list(self, **kw):
        self.c.calls.append("search")
        return self

    def execute(self):
        self.c.maybe_raise("search")
        return {"items": [{"id": {"videoId": self.c.live_id}}] if self.c.is_live else []}


class FakeClient:
    def __init__(self, ch, live_id, is_live, fail_on=None):
        self.ch = ch
        self.live_id = live_id
        self.is_live = is_live
        self.fail_on = fail_on or {}
        self.calls = []

    def maybe_raise(self, op):
        n = self.fail_on.get(op, 0)
        if n > 0:
            self.fail_on[op] = n - 1
            raise HttpError(_Resp(), b'{"error":{"errors":[{"reason":"quotaExceeded"}]}}')

    def channels(self):
        return FakeChannelList(self)

    def playlistItems(self):
        return FakePlaylistItems(self)

    def videos(self):
        return FakeVideos(self)

    def search(self):
        return FakeSearch(self)


# ============================================================
# 1. 低コスト検知
# ============================================================
c1 = FakeClient("UCA", "vid1", is_live=True)
mgr = recorder.YouTubeAPIManager(["keyA"])
mgr.youtube_clients = [c1]
vid = recorder.find_live_video_id(mgr, "UCA")
check("低コスト検知でライブIDを返す", vid == "vid1", repr(vid))
check("search.list を使っていない（100units節約）", "search" not in c1.calls, str(c1.calls))
check("呼び出しは3回（channels/playlistItems/videos）", len(c1.calls) == 3, str(c1.calls))

c1.calls.clear()
recorder.find_live_video_id(mgr, "UCA")
check("uploads playlist をキャッシュする", "channels" not in c1.calls, str(c1.calls))

c2 = FakeClient("UCB", "vid2", is_live=False)
mgr2 = recorder.YouTubeAPIManager(["keyA"])
mgr2.youtube_clients = [c2]
check("非ライブなら None", recorder.find_live_video_id(mgr2, "UCB") is None)

bad = FakeClient("UCC", "vid3", is_live=True, fail_on={"channels": 1})
good = FakeClient("UCC", "vid3", is_live=True)
mgr3 = recorder.YouTubeAPIManager(["keyA", "keyB"])
mgr3.youtube_clients = [bad, good]
check("quotaエラーで別キーに再試行して成功", recorder.find_live_video_id(mgr3, "UCC") == "vid3")

# ============================================================
# 2. 監視ループと終了コード
# ============================================================
state = {"n": 0}


def fake_check(api_manager, channel_id, use_search_fallback=False):
    state["n"] += 1
    return "vidLIVE" if state["n"] >= 2 else None


recorder.check_live_status = fake_check
recorded = {}


def fake_exec(url, cookies_file=None, api_manager=None):
    recorded["url"] = url
    return "ok"


recorder.execute_scripts = fake_exec
check(
    "監視ループが2回目でライブを拾う",
    recorder.main() == 0 and recorded.get("url", "").endswith("vidLIVE"),
)

state["n"] = 0
recorder.check_live_status = lambda *a, **k: None
recorder.maybe_dispatch_next = lambda reason: True
recorder.WATCH_MAX_SECONDS = 0
check("ライブなしの単発モードは exit 0", recorder.main() == 0)


def always_fail(*a, **k):
    raise RuntimeError("quota")


recorder.check_live_status = always_fail
recorder.WATCH_MAX_SECONDS = 36000
check("API連続失敗は exit 3", recorder.main() == recorder.EXIT_API_ERROR)

recorder.check_live_status = lambda *a, **k: "vidX"
recorder.execute_scripts = lambda url, cookies_file=None, api_manager=None: "upload_failed"
recorder.WATCH_MAX_SECONDS = 3600
check("アップロード失敗は exit 4", recorder.main() == recorder.EXIT_UPLOAD_ERROR)
recorder.execute_scripts = lambda url, cookies_file=None, api_manager=None: "download_failed"
check("録画失敗は exit 5", recorder.main() == recorder.EXIT_DOWNLOAD_ERROR)

# ============================================================
# 3. ユーティリティ
# ============================================================
check("format_bytes 負値", recorder.format_bytes(-1024) == "-1.0KB", recorder.format_bytes(-1024))
check(
    "extract_video_id (watch)",
    recorder.extract_video_id("https://www.youtube.com/watch?v=A745Pi5_G-g") == "A745Pi5_G-g",
)
check(
    "extract_video_id (channel live) は None",
    recorder.extract_video_id("https://www.youtube.com/channel/UC5jpxDZx4yoBo324pMQ91Ww/live")
    is None,
)

# ============================================================
# 3-2. 自己ディスパッチ（連鎖が 1 回で止まらないこと）
# ============================================================
# workflow_dispatch は schedule と同じ扱いにならない。chain=1 を渡さないと
# 次 job が「監視0秒・WARPなし・自己ディスパッチなし」で起動して連鎖が切れる。
class _FakeResp:
    status_code = 204
    text = ""


captured = {}


def _fake_post(url, json=None, headers=None, timeout=None):
    captured["url"] = url
    captured["json"] = json
    return _FakeResp()


_prev_requests = sys.modules.get("requests")
sys.modules["requests"] = types.SimpleNamespace(post=_fake_post)
os.environ["GITHUB_REPOSITORY"] = "owner/repo"
os.environ["GITHUB_TOKEN"] = "dummy"
_prev_self, _prev_file = recorder.SELF_DISPATCH, recorder.SELF_WORKFLOW_FILE
recorder.SELF_DISPATCH = True
recorder.SELF_WORKFLOW_FILE = "youtube-recorder.yml"
_ok = REAL_DISPATCH("test")
recorder.SELF_DISPATCH, recorder.SELF_WORKFLOW_FILE = _prev_self, _prev_file
if _prev_requests is None:
    sys.modules.pop("requests", None)
else:
    sys.modules["requests"] = _prev_requests

check("自己ディスパッチが成功する", _ok is True, _ok)
check(
    "自己ディスパッチが chain=1 を渡す",
    ((captured.get("json") or {}).get("inputs") or {}).get("chain") == "1",
    captured,
)
check(
    "自己ディスパッチ先が workflow dispatches API",
    str(captured.get("url", "")).endswith("/dispatches"),
    captured.get("url"),
)


# ============================================================
# 4. 録画の継続（1.6GB 事故 / 403 事故の再発防止）
# ============================================================
class FakeProc:
    pid = 999999

    def wait(self, timeout=None):
        return 0


def run_execute(live_sequence, max_bytes=0):
    """execute_scripts を依存スタブ付きで1回実行する。"""
    dl = pathlib.Path(recorder.DOWNLOAD_DIR)
    cm = pathlib.Path(recorder.COMMENTS_DIR)
    shutil.rmtree(dl, ignore_errors=True)
    shutil.rmtree(cm, ignore_errors=True)
    dl.mkdir(parents=True, exist_ok=True)
    cm.mkdir(parents=True, exist_ok=True)

    calls = {"dl": 0, "cmds": []}
    seq = list(live_sequence)

    def fake_watchdog(cmd, watch_dir, label="process", max_seconds=0, max_bytes=0):
        calls["dl"] += 1
        calls["cmds"].append(list(cmd))
        n = calls["dl"]
        name = f"test [A745Pi5_G-g]" + ("" if n == 1 else f" part{n:02d}") + ".mp4"
        with open(os.path.join(watch_dir, name), "wb") as f:
            f.write(b"x" * 1024)

    def fake_state(api_manager, video_id):
        return ("title", seq.pop(0) if seq else False)

    orig = (
        recorder.run_with_watchdog,
        recorder.get_video_state,
        recorder.upload_with_rclone,
        recorder.concat_segments,
        recorder.cleanup_dir,
        recorder.subprocess.Popen,
        recorder.MAX_DOWNLOAD_BYTES,
        recorder.TEST_LIVE_URL,
    )
    rec = {"concat": 0, "upload": 0}

    def fake_concat(directory, stem):
        rec["concat"] += 1
        return None

    def fake_upload(cmd, retries=None):
        rec["upload"] += 1
        return True

    recorder.run_with_watchdog = fake_watchdog
    recorder.get_video_state = fake_state
    recorder.concat_segments = fake_concat
    recorder.upload_with_rclone = fake_upload
    recorder.cleanup_dir = lambda p: None
    recorder.subprocess.Popen = lambda *a, **k: FakeProc()
    recorder.MAX_DOWNLOAD_BYTES = max_bytes
    recorder.TEST_LIVE_URL = ""

    status = REAL_EXECUTE(
        "https://www.youtube.com/watch?v=A745Pi5_G-g", api_manager=object()
    )

    (
        recorder.run_with_watchdog,
        recorder.get_video_state,
        recorder.upload_with_rclone,
        recorder.concat_segments,
        recorder.cleanup_dir,
        recorder.subprocess.Popen,
        recorder.MAX_DOWNLOAD_BYTES,
        recorder.TEST_LIVE_URL,
    ) = orig
    return status, calls, rec


status, calls, rec = run_execute([True, False])
check("配信継続中なら2セグメント目を録る", calls["dl"] == 2, f"dl={calls['dl']}")
check("セグメントを結合する", rec["concat"] == 1, f"concat={rec['concat']}")
check("継続録画でも status=ok", status == "ok", status)
check(
    "先頭セグメントは --live-from-start 付き（ライブ端403の回避）",
    "--live-from-start" in calls["cmds"][0],
    calls["cmds"][0],
)
check(
    "2セグメント目は --live-from-start を付けない（重複回避）",
    "--live-from-start" not in calls["cmds"][1],
    calls["cmds"][1],
)

status, calls, rec = run_execute([False])
check("配信終了なら1セグメントで止める", calls["dl"] == 1, f"dl={calls['dl']}")

status, calls, rec = run_execute([True, True, True], max_bytes=1024)
check("サイズ上限に達したら継続しない", calls["dl"] == 1, f"dl={calls['dl']}")

shutil.rmtree(TMP, ignore_errors=True)

print()
if failures:
    print("FAILURES:", failures)
    sys.exit(1)
print("ALL PASS")
