"""YouTube cookie checker for GitHub Actions.

login  : fetch random YouTube pages with cookies and require LOGGED_IN:true.
canary : run yt-dlp --simulate against random videos from a stable pool.
"""

from __future__ import annotations

import argparse
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LOGIN_URLS = [
    "https://www.youtube.com/feed/subscriptions",
    "https://www.youtube.com/feed/history",
    "https://www.youtube.com/feed/library",
    "https://www.youtube.com/feed/you",
    "https://www.youtube.com/playlist?list=WL",
    "https://www.youtube.com/playlist?list=LL",
    "https://www.youtube.com/",
    "https://www.youtube.com/feed/trending",
    "https://www.youtube.com/account",
    "https://www.youtube.com/feed/courses",
    "https://m.youtube.com/feed/subscriptions",
    "https://m.youtube.com/feed/history",
]

VIDEO_IDS = [
    "jNQXAC9IVRw",
    "dQw4w9WgXcQ",
    "9bZkp7q19f0",
    "kJQP7kiw5Fk",
    "OPf0YbXqDm0",
    "fJ9rUzIMcZQ",
    "hTWKbfoikeg",
    "YQHsXMglC9A",
    "RgKAFK5djSk",
    "CevxZvSJLk8",
    "4NRXx6U8ABQ",
    "2Vv-BfVoq4g",
    "JGwWNGJdvx8",
    "60ItHLz5WEA",
    "pRpeEdMmmQ0",
    "7wtfhZwyrcc",
    "ktvTqknDobU",
    "1G4isv_Fylg",
    "RB-RcX5DS5A",
    "k4V3Mo61fJM",
    "lp-EO5I60KA",
    "hT_nvWreIhg",
    "rYEDA3JcQqw",
    "uelHwf8o7_U",
    "ZbZSe6N_BXs",
    "09R8_2nJtjg",
    "L_jWHffIx5E",
    "1w7OgIMMRc4",
    "CvBfHwUxHIk",
    "kXYiU_JCYtU",
    "eVTXPUF4Oz4",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
]

LOGIN_MARKER = re.compile(r'"LOGGED_IN"\s*:\s*true')
COOKIE_ERROR = re.compile(
    r"cookies are no longer valid|"
    r"Sign in to confirm you(?:'|’)re not a bot|"
    r"confirm you(?:'|’)re not a bot",
    re.IGNORECASE,
)
RATE_LIMIT = re.compile(r"HTTP Error 429|too many requests|rate limit", re.IGNORECASE)


def read_cookie_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        raise ValueError("cookie file is empty")
    return text


def print_tail(text: str, lines: int = 12) -> None:
    for line in text.strip().splitlines()[-lines:]:
        print(f"  | {line}", flush=True)


def fetch_page(url: str, cookies: Path, user_agent: str, timeout: int) -> tuple[int, str, str, str]:
    curl = shutil.which("curl")
    if not curl:
        return 127, "", "", "curl not found"
    with tempfile.NamedTemporaryFile(prefix="yt-cookie-", suffix=".html", delete=False) as handle:
        output = Path(handle.name)
    try:
        command = [
            curl,
            "-sS",
            "-L",
            "--compressed",
            "--max-time",
            str(timeout),
            "-A",
            user_agent,
            "-H",
            "Accept-Language: en-US,en;q=0.9",
            "-b",
            str(cookies),
            "-o",
            str(output),
            "-w",
            "%{http_code} %{url_effective}",
            url,
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout + 15,
        )
        body = output.read_text(encoding="utf-8", errors="replace") if output.exists() else ""
        return completed.returncode, completed.stdout.strip(), body, completed.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "", f"timeout after {timeout}s"
    finally:
        output.unlink(missing_ok=True)


def check_login(cookies: Path, attempts: int, timeout: int) -> int:
    read_cookie_text(cookies)
    urls = random.sample(LOGIN_URLS, k=min(attempts, len(LOGIN_URLS)))
    saw_response = False
    for index, url in enumerate(urls, 1):
        print(f"login check {index}/{len(urls)}: {url}", flush=True)
        code, meta, body, error = fetch_page(url, cookies, random.choice(USER_AGENTS), timeout)
        status = 0
        if meta:
            try:
                status = int(meta.split()[0])
            except ValueError:
                status = 0
        found = bool(LOGIN_MARKER.search(body))
        print(f"  curl_exit={code} status={status} {meta} bytes={len(body)} logged_in={found}", flush=True)
        if status == 429:
            print("  rate limited; stopping", flush=True)
            print("RESULT: unknown", flush=True)
            return 2
        if code != 0:
            print(f"  curl error: {error[:300]}", flush=True)
        if status == 200:
            saw_response = True
            if found:
                print("RESULT: ok", flush=True)
                return 0
        time.sleep(random.uniform(1.0, 3.0))
    if saw_response:
        print("RESULT: cookie_invalid", flush=True)
        return 1
    print("RESULT: unknown", flush=True)
    return 2


def check_canary(cookies: Path, attempts: int, timeout: int, yt_dlp: str) -> int:
    read_cookie_text(cookies)
    binary = shutil.which(yt_dlp) or (yt_dlp if Path(yt_dlp).is_file() else None)
    if not binary:
        print(f"yt-dlp not found: {yt_dlp}", flush=True)
        print("RESULT: unknown", flush=True)
        return 2
    ids = random.sample(VIDEO_IDS, k=min(attempts, len(VIDEO_IDS)))
    cookie_error = False
    for index, video_id in enumerate(ids, 1):
        url = f"https://www.youtube.com/watch?v={video_id}"
        print(f"canary {index}/{len(ids)}: {url}", flush=True)
        command = [
            binary,
            "--simulate",
            "--no-playlist",
            "--no-progress",
            "--cookies",
            str(cookies),
            "--print",
            "%(id)s",
            url,
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            print(f"  timeout after {timeout}s", flush=True)
            continue
        detail = f"{completed.stdout}\n{completed.stderr}".strip()
        if completed.returncode == 0 and completed.stdout.strip():
            print(f"  ok: {completed.stdout.strip().splitlines()[-1]}", flush=True)
            print("RESULT: ok", flush=True)
            return 0
        print(f"  yt-dlp exit={completed.returncode}", flush=True)
        if COOKIE_ERROR.search(detail):
            print("  cookie error detected", flush=True)
            print_tail(detail)
            cookie_error = True
            break
        if RATE_LIMIT.search(detail):
            print("  rate limited; stopping", flush=True)
            print_tail(detail)
            print("RESULT: unknown", flush=True)
            return 2
        print_tail(detail)
        time.sleep(random.uniform(1.0, 3.0))
    if cookie_error:
        print("RESULT: cookie_invalid", flush=True)
        return 1
    print("RESULT: unknown", flush=True)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    login = subparsers.add_parser("login")
    login.add_argument("--cookies", required=True, type=Path)
    login.add_argument("--attempts", type=int, default=2)
    login.add_argument("--timeout", type=int, default=30)
    canary = subparsers.add_parser("canary")
    canary.add_argument("--cookies", required=True, type=Path)
    canary.add_argument("--attempts", type=int, default=5)
    canary.add_argument("--timeout", type=int, default=120)
    canary.add_argument("--yt-dlp", default="yt-dlp")
    args = parser.parse_args()
    if not args.cookies.is_file():
        print(f"cookie file not found: {args.cookies}", flush=True)
        print("RESULT: unknown", flush=True)
        return 2
    try:
        if args.command == "login":
            return check_login(args.cookies, max(1, args.attempts), max(5, args.timeout))
        return check_canary(args.cookies, max(1, args.attempts), max(10, args.timeout), args.yt_dlp)
    except (OSError, ValueError) as error:
        print(f"cookie file error: {error}", flush=True)
        print("RESULT: unknown", flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
