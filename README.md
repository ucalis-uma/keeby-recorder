# Keeby Live Recorder — GitHub Actions 版

旧 **Xserver 無料VPS**（サービス終了）で行っていた

> YouTube / Twitch のライブ監視 → 録画＋コメント保存 → Google Drive へ自動アップロード

を、**GitHub Actions** で実現する構成です。

- **無料**（public リポジトリの標準ランナーは「無料・無制限」。**クレジットカード登録は不要**）
- **手元のPCの電源が入っていなくても動く**（実行するのは GitHub 側のランナー）
- 常時プロセスを持たない代わりに、**15分ごとに起動する 1 回限りのジョブ**として実装

---

## 1. なぜGitHub Actionsでできるのか（一次情報）

| 項目 | 内容 |
|---|---|
| 料金 | public リポジトリ + 標準ランナーは **free & unlimited**。支払い方法の登録も不要 |
| ランナー性能（public） | Linux x64: **4 vCPU / 16GB RAM / 14GB SSD** |
| ジョブ実行時間 | **最大6時間**（`timeout-minutes` で設定。350分=約5.8時間を設定） |
| cron | 15分間隔（`5,20,35,50 * * * *`）。**`:00` は実行集中で遅延・drop しやすいため意図的にずらす** |
| 60日ルール | **public リポジトリは60日アクティビティが無ければ schedule が自動disable** → `keepalive.yml` で対策 |

旧VPSは 2GB メモリ + 4GB スワップで稼働していた。YouTube Data API キーを複数登録し、キーごとにフェイルオーバーさせて運用していた。

---

## 2. ファイル構成

```
github-actions/
├── recorder.py                        # YouTube録画（1回のjobで完結）
├── twitch_recorder.py                 # Twitch録画（1回のjobで完結）
├── setup_dependencies.sh              # 依存インストール（yt-dlp/deno/ffmpeg/rclone/pip）
├── setup_potoken.sh                   # PO Token provider（bgutil）の導入
├── setup_warp.sh                      # Cloudflare WARP（出口IPを変える）
├── setup_rclone.sh                    # Secrets から rclone.conf を生成
├── requirements.txt                   # Python 依存
├── check_all.py                       # 開発用チェック（YAML/構文/文字化け/ロジックテスト）
├── test_recorder_logic.py             # recorder.py のロジックテスト（通信なし）
├── .gitattributes                     # LF・UTF-8 固定
├── .gitignore                         # 録画データ・認証情報・venv を除外
└── .github/workflows/
    ├── youtube-recorder.yml           # YouTube用 cron ジョブ
    ├── twitch-recorder.yml            # Twitch用 cron ジョブ
    ├── access-check.yml               # 週1の疎通確認（WARP + PO Token）
    └── keepalive.yml                  # 60日ルール対策（毎日 空コミット push）
```

---

## 3. セットアップ手順

### 3-1. リポジトリ作成（public・無料）

1. GitHub（**`ucalis-uma` アカウント**）で新規リポジトリ `keeby-recorder` を作成し、**Public** にする。
   - private でも動作するが、Actions の分数上限（月2,000分）が適用され、超過時は課金はblocked（カード未登録でも課金は発生しないが制限される）。**public の方が制約なし**。
2. この `github-actions/` ディレクトリをリポジトリのルートへ push。

### 3-2. YouTube Data API キー

- Google Cloud Console で YouTube Data API v3 を有効化し、APIキーを発行。
- 旧VPSで使っていたキー（`再構築.md` の `API_KEYS` 相当）をそのまま使える。

### 3-3. Google Drive（rclone）の設定

`setup_rclone.sh` は **Secrets の `RCLONE_CONFIG_B64`** から `rclone.conf` を復元する方式。手順：

1. 手元（あるいは旧VPSが生きているうちに）で rclone の Google Drive リモートを用意済みなら、その設定ファイルをそのまま使う。
   ```bash
   # rclone が既に gdrive という名前で設定済みなら
   rclone config
   rclone listremotes        # → gdrive: が表示されるはず

   # rclone.conf を base64 化
   base64 -w0 ~/.config/rclone/rclone.conf
   ```
   Windows PowerShell なら:
   ```powershell
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("$env:USERPROFILE\AppData\Roaming\rclone\rclone.conf"))
   ```
2. 出力をリポジトリの **Settings → Secrets and variables → Actions → New repository secret** に
   **名前 `RCLONE_CONFIG_B64`** で登録。

> **注意**: `rclone.conf` は Google のOAuthトークンを含むため、**絶対にコミットしない**こと。`.gitignore` にも `rclone.conf` を追加済み。

### 3-4. リポジトリVariables（公開してよい値）

**Settings → Secrets and variables → Actions → Variables** に登録：

| 名前 | 値（例） |
|---|---|
| `RCLONE_REMOTE` | `gdrive` |
| `GDRIVE_VIDEO_DEST` | `gdrive:/keeby/videos` |
| `GDRIVE_COMMENT_DEST` | `gdrive:/keeby/comments` |
| `GDRIVE_TWITCH_DEST` | `gdrive:/keeby/twitch` |
| `TWITCH_STREAMER_ID` | `keeby_boy` |

### 3-5. リポジトリSecrets（非公開が必要な値）

**Settings → Secrets and variables → Actions → Secrets** に登録：

| 名前 | 内容 |
|---|---|
| `YOUTUBE_API_KEYS` | カンマ区切りのYouTube Data APIキー（例: `KEY1,KEY2,KEY3`） |
| `RCLONE_CONFIG_B64` | 手順3-3で base64 化した `rclone.conf` |
| `COOKIES_TXT` | `cookies.txt` の全文（YouTubeの403対策・cookie監視用。空だとcookie監視は失敗する） |

> `COOKIES_TXT` はログイン情報を含むため **必ず Secret** にして、リポジトリには置かないこと。

---

## 4. 動作確認

1. リポジトリの **Actions** タブを開く。
2. `YouTube Live Recorder` を選び **Run workflow**（手動実行）でテスト。
3. ログに以下が出ればOK：
   - ライブ中なら `ライブ配信を検知: https://...` → 録画 → `Google Driveへアップロード中` → `アップロード完了`
   - ライブ無しなら `どのチャンネルもライブ配信なし。何もしないで終了します。`
4. 経路を変えたときは `Actions → YouTube Access Check → Run workflow` で疎通確認する（週1でも自動実行される）。

> **注意**: 実チャンネルがライブ中じゃないと「動画DL」には入らない。手動テスト時は一時的に `CHANNEL_IDS` を
> ライブ中のチャンネルIDに変更するか、`workflow_dispatch` でそのまま実行してログの挙動を確かめること。

---

## 5. cron について（Actionsの遅延対策）

GitHub公式ドキュメントに「**毎時0分は実行が集中し、遅延およびdropが起きうる**」と明記されている。
そのため本リポジトリでは：

- YouTube: `5,20,35,50 * * * *`（15分間隔、`:00` を回避）
- Twitch: `12,27,42,57 * * * *`（15分間隔、YouTube側と時刻をずらす）
- access-check: `23 4 * * 1`（毎週月曜4:23 UTC。週1の疎通確認）
- keepalive: `12 3 * * *`（毎日3:12）

> 注意: GitHub Actions の cron は5フィールド必須（分 時 日 月 曜日）。
> 4フィールド（例 `5,20,35,50 * * *`）では
> `invalid cron attribute` でワークフロー自体が起動不可になる。

---

## 6. 60日ルール対策

GitHub公式ルール「**public リポジトリは60日アクティビティ無しで scheduled workflow が自動disable**」に対する対策。
`keepalive.yml` が毎日1回、`git commit --allow-empty` で**空コミットをpush**してアクティビティを発生させる。
差分ファイルはゼロなので、リポジトリ内容には影響しない。

---

## 7. 移行前の注意（VPS時代との差異）

| 項目 | 旧VPS | 現在のActions |
|---|---|---|
| 常駐プロセス | systemd で常時起動 | job内の監視ループ（最大4時間）＋終了時に次jobを自己起動 |
| 検知間隔 | 15分ごと | 監視中は YouTube 5分 / Twitch 3分 |
| 出口IP | VPSの固定IP | GitHubの共有IP（bot判定されるため WARP 経由。13-8） |
| ファイル保存 | `/root/keeby/downloads` に一時保存 | ランナー内 `/home/runner/work/.../downloads`（ephemeral） |
| 保存先 | rclone で Google Drive | 同じ（変更なし） |
| メモリ／スワップ | 2GB + 4GB swap | 16GB（ランナー標準。swap不要） |

---

## 8. 開発

構文/YAML/cron/実行時名前・文字化けのチェックは以下で一括実行できます（Windows ローカルでも）:

```powershell
cd C:\Users\umaro\Videos\xserver\github-actions
$env:PYTHONIOENCODING='utf-8'
python check_all.py
```

---

## 9. 修正履歴（初回 Actions 失敗の調査結果）

初回 push（`b75f28c`）直後の Actions 実行は、ワークフロー自体が起動不可だった。
`actions/runs/37194592094` の注釈に `invalid cron attribute "5,20,35,50 * * *"` と出ていた。
以下を修正済み：

| # | 原因 | 修正 |
|---|---|---|
| 1 | cron が4フィールド（`5,20,35,50 * * *`）で `invalid cron attribute` になり起動不可 | 5フィールド化（`5,20,35,50 * * * *` / Twitch 側も同様）。`check_all.py` に5フィールド検査を追加 |
| 2 | `setup_dependencies.sh` の `pip install --user` が ubuntu-24.04（PEP 668）で `externally-managed-environment` エラー | リポジトリ直下に `.venv` を作り `pip install -r requirements.txt` に統一。workflow 側は `.venv/bin/python` で実行 |
| 3 | `setup_rclone.sh` が `RCLONE_CONFIG_B64` 未設定で `exit 1`（Secret 未登録だと録画前に即死） | 未設定時は警告のみで `exit 0`（アップロードなしで録画継続）。`recorder.py` の `upload_with_rclone()` は元から失敗時も例外を投げない設計 |
| 4 | `recorder.py` の `format_bytes` 未定義・`datetime` が `if __name__` 内遅延 import のみ | `format_bytes` を追加（旧VPS版と同一ロジック）、`from datetime import datetime` を冒頭に移動。`check_all.py` に必須名検査を追加 |
| 5 | Twitch 側が `setup_dependencies.sh` と workflow 内で二重に venv/`chat-downloader` を導入 | `requirements.txt` に `chat-downloader` を集約し `.venv` 一本化。workflow 側の `.venv-twitch` 作成手順を削除 |
| 6 | workflow が `cookies.txt` を `$HOME` に書くのに `recorder.py` へパスを渡していない（`COOKIES_FILE` 未配線） | workspace 直下に書き出し、`COOKIES_FILE` 環境変数で明示的に渡す。存在しない場合は警告して Cookie なしで継続 |
| 7 | `RCLONE_BIN` を `/usr/local/bin/rclone` に固定していたが rclone installer は `/usr/bin` に置く場合があり `FileNotFoundError` の恐れ | `RCLONE_BIN: rclone` にして PATH 解決に任せる（`setup_rclone.sh` の `rclone listremotes` と同じ解決）。Python 側も `FileNotFoundError` を捕捉して警告のみで継続 |
| 8 | Drive 満杯時にテスト録画ができず、かつ YouTube 側が rclone 失敗でもローカル削除していた（録画消失の恐れ） | `TEST_LIVE_URL` / `TEST_TARGET_URL` / `TEST_MAX_SECONDS` / `SKIP_UPLOAD` を追加。`workflow_dispatch` の inputs から指定し、Drive に触らず artifact で回収するドライラン方式に。YouTube 側もアップロード成功時のみ削除に変更 |

その他：yt-dlp の安定版→nightly 二重取得を nightly 1回に整理、Deno 導入は `sudo env DENO_INSTALL=...` 方式に変更（`sudo` の `env_reset` 対策）、rclone 導入は先にダウンロードしてから `sudo bash` する方式に変更。

---

## 10. ドライラン手順（Drive が満杯でもテスト可）

Drive に一切書き込まず、録画パイプライン（yt-dlp + deno + ffmpeg 結合）を通す手順。
本番の cron には影響しない（inputs 未指定時＝空文字・`0`・false は従来の本番動作）。

YouTube 側：`Actions → YouTube Live Recorder → Run workflow` で以下を指定

| input | 例 | 意味 |
|---|---|---|
| `test_live_url` | `https://www.youtube.com/watch?v=...`（適当なライブ/動画URL） | API 検知をスキップし直接録画 |
| `test_max_seconds` | `90` | 90秒で打ち切り終了 |
| `skip_upload` | `true` | Drive へ上げずローカル保持→artifact 保存 |
| `max_download_gb` | `2.5` | 約2.5GBでダウンロードを打ち切る（0=無制限） |

Twitch 側：`Actions → Twitch Live Recorder → Run workflow` で以下を指定

| input | 例 | 意味 |
|---|---|---|
| `test_target_url` | `https://www.twitch.tv/...`（適当な配信/アーカイブURL） | 配信検知をスキップし直接録画 |
| `test_max_seconds` | `90` | 90秒で打ち切り終了 |
| `skip_upload` | `true` | Drive へ上げずローカル保持→artifact 保存 |

確認：run 終了後に `Artifacts` の `youtube-test-recording` / `twitch-test-recording`
（3日保持）をダウンロードし、mp4 が再生できれば成功。Drive 容量は消費しない。
SMALL（短い動画・テスト用URL）を選ぶと artifact も小さく済む。

> テスト時は通常の動画URLでも落とせるよう `--live-from-start` を外す（本番は付与）。
> そのため短い通常動画をテストURLに使える。なお YouTube 側のコメント取得
> （`--write-comments`）はライブ以外では空になる場合があるが、
> 動画DLの成否確認には影響しない。

---

## 11. ドライラン失敗時の調査（yt-dlp 終了コード1）

テスト実行で `[動画DL] 終了 (コード: 1)` → `ダウンロードファイルが0バイト` になった場合、
以前は yt-dlp の stderr を破棄していたため原因が特定できなかった。現在は以下を保存している：

- `recorder.log` に `CMD:`（実行コマンド全文）と `yt-dlp stderr末尾`（最大30行）を転記
- artifact `recorder-logs` に `ytdlp-video-stdout.log` / `ytdlp-video-stderr.log`
  （再試行時は `ytdlp-video-retry-*.log`、コメント取得は `ytdlp-comment-*.log`）全文
- Twitch 側も同様（`twitch-recorder-logs` に `ytdlp-twitch-*.log`）

よくある原因と対処：

| stderr の内容 | 意味 | 対処 |
|---|---|---|
| `Requested format is not available` | `-f` 指定の mp4/m4a ペアが無い（Shorts 等） | 自動で `bv*+ba/b` に緩めて再試行する（手動対応不要） |
| `Sign in to confirm you're not a bot` / `403` | YouTube の bot 検出 | `COOKIES_TXT` Secret を登録して再実行 |
| `Private video` / `Video unavailable` | 非公開・削除済み・地域制限 | 別の公開動画URLで再テスト |
| `Age-restricted` | 年齢制限（ログイン必須） | `COOKIES_TXT` 登録が必要。それでも不可なら別URLで再テスト |

---

## 12. アクセス監視（access-check）

### 12-1. Cookie 巡回は廃止した

以前は `cookie-check.yml`（毎時）と `cookie-canary.yml`（日次）で、ログイン済み
Cookie を使って `/feed/subscriptions` や `/feed/history` を巡回していた。廃止理由は2つ。

- ログイン済みアカウントで feed を機械的に巡回するのは scraper そのもので、
  Google のリスクスコアを上げる。同じセッションを消耗するため、
  Cookie の失効を早めている疑いがあった。
- 2026-10-29 の実測で、**Cookie を付けても GitHub Actions の IP では
  `Sign in to confirm you're not a bot` で全滅**した。Cookie の鮮度は
  ボトルネックではなかった（詳細は 13-8）。

### 12-2. 週1回の疎通確認（access-check.yml）

- 毎週月曜 04:23 UTC に1回だけ実行する。
- 本番とまったく同じ経路（WARP → yt-dlp → PO Token provider）で
  `recorder.py` の PROBE モードを回し、各 player_client の可否をログに出す。
- 失敗したときだけ Issue `YouTube access check failed` を作成する
  （assignee と cc 付きなのでメールが飛ぶ）。復旧時に自動クローズする。
- ログは artifact `access-check-logs` に14日保存する。

### 12-3. 録画時の失敗検知

- `recorder.py` は API 連続失敗=3 / アップロード失敗=4 / 録画失敗=5 で終了する。
- workflow は `failure()` で Issue を作成し、録画を artifact に退避する。
- ログの `cookies are no longer valid` は警告に留める（Cookie なし再試行で
  成功する場合があるため）。

### 12-4. Cookie はもう必須ではない

- 既定は「匿名 → android_vr → mweb → tv_simply → … → 最後に Cookie」の順。
- `COOKIES_TXT` は保険として残してあるが、更新しなくても録画は動く。
- 更新する場合: Secrets の `COOKIES_TXT` を差し替え、
  `Actions → YouTube Access Check → Run workflow` で疎通確認する。

> `COOKIES_TXT` はログイン情報を含む。値やcookieファイルの中身をログ・Issue・READMEに書かない。

---

## 13. 2026-10-28 の配信を取りこぼした原因と対策

### 13-1. 原因（実測）

「15分間隔のcronで起動して1回だけライブを見る」設計が成立していなかった。
GitHub Actions の `schedule` は、この公開リポジトリでは **15分間隔では実行されない**。
Actions API の実行履歴（直近40件）を集計した実測値：

| workflow | 実行間隔の最小 | 中央値 | 最大 | 30分以内の実行 |
|---|---|---|---|---|
| YouTube Live Recorder | 207分 | **318分（5.3時間）** | 366分 | 0 / 10 |
| Twitch Live Recorder | 190分 | **342分（5.7時間）** | 408分 | 0 / 11 |

つまり 96回/日 の予定が **約4〜5回/日** しか走っていない。実際、2026-10-28 の YouTube 側の実行は
`00:03 / 05:25 / 11:31 / 17:17 / 21:42` の5回だけだった。配信がこの隙間に始まれば、
cron が一度も来ないまま終わる。これが「昨日配信があったのに Drive に何も無い」の直接原因。

補足：この日の実行はすべて success で、ログも `どのチャンネルもライブ配信なし` で終わっている。
検知やアップロードのバグではなく、**見に行くタイミングが来ていなかった**。

### 13-2. 対策

1. **job 自体を監視ループにする**（`recorder.py` / `twitch_recorder.py`）
   - `WATCH_MAX_SECONDS`（本番 14400秒=4時間）の間、`POLL_INTERVAL`（YouTube 300秒 / Twitch 180秒）ごとにライブを確認し続ける。
   - ライブを検知したらその場で録画〜アップロードし、終了する。
   - ライブが無いまま監視時間を終えたら 0 で終了する。
   - `WATCH_MAX_SECONDS=0` は単発モード（手動実行の既定）。

2. **監視の切れ目で次の job を自己起動する**（`SELF_DISPATCH=1`）
   - 監視時間の終了時とライブ検知時に、REST API で `workflow_dispatch` を叩いて次 job を起動する。
   - cron が何時間来なくても監視が途切れない。cron は「連鎖が切れたときの保険」になる。
   - 必要な権限は `permissions: actions: write`。`GITHUB_TOKEN` を env で渡す。
   - **`inputs.chain=1` を必ず渡す**。`workflow_dispatch` は `schedule` と同じ扱いにならないため、
     これが無いと次 job が「監視0秒・WARPなし・自己ディスパッチなし」で起動し、連鎖が1回で切れる（実際に切れていた）。
   - **待機中の run があれば起動しない**。検知時と終了時の2回呼ぶため、dedupe しないと
     待機列が延々と伸びる（`status=queued` を確認してから dispatch する）。

3. **ライブ検知を低コスト化**（`find_live_video_id`）
   - `search.list(eventType=live)` は 100 units/回。5分ポーリングでは1日でquotaを食い潰す。
   - `channels.list`（1 unit・job内キャッシュ）+ `playlistItems.list`（1 unit）+ `videos.list`（1 unit）で判定する。
   - 直近10件のアップロードに現れない長期配信のために、24ポーリングに1回だけ `search.list` を補助使用（約2,400 units/日）。

4. **APIキーのフェイルオーバーを実際に再試行する**（`YouTubeAPIManager.call`）
   - 旧実装は1回失敗したらキーを替えるだけで、同じ呼び出しを再試行していなかった。
   - 現在は quota/rate limit を検知したらキーを替えて同じ呼び出しを最大キー数ぶん再試行する。

5. **失敗を緑のままにしない**
   - API が全チャンネルで連続5回失敗 → exit 3
   - 録画はできたが Drive へ上げられない → exit 4（ローカルは削除しない）
   - ライブを検知したが録画が0バイト → exit 5
   - workflow 側で `failure()` のとき録画を artifact（`youtube-fallback-recording` / `twitch-fallback-recording`、3日保持）へ退避する。
   - 同様に `failure()` で Issue を作成し、次回 success で自動クローズする。

6. **Cookie起因の録画失敗は Cookie なしで1回再試行**
   - `cookies are no longer valid` / `Sign in to confirm you` 等を検出したら `--cookies` を外して再試行する。

### 13-3. 動作確認

- `python check_all.py` が `workflow_env` を含めて全PASS。
- `test_recorder_logic.py` の25件（監視ループ、quotaフェイルオーバー、終了コード3/4/5、
  単発モード、セグメント継続、`--live-from-start` の有無、自己ディスパッチの `chain=1` と dedupe）が全PASS。

### 13-4. 残る制約

- 監視jobが4時間で終わるため、job上限（350分）に近い長さの配信では、録画がタイムアウトで切れる可能性がある。その場合、次jobがライブを再検知して `--live-from-start` で頭から取り直す（重複はrcloneが同名上書き）。
- 監視の連鎖が何らかの理由で切れた場合に備え、cron はそのまま残している（`5,20,35,50` / `12,27,42,57`）。
- 60日ルール対策の `keepalive.yml` も従来どおり必要。

### 13-5. Drive 保存の容量テスト（実配信で 2〜3GB）

実際のライブ配信から 2〜3GB だけ取得し、Google Drive まで保存できるかを確認する手順。

`Actions → YouTube Live Recorder → Run workflow` で以下を指定する。

| input | 値 | 意味 |
|---|---|---|
| `test_live_url` | `https://www.youtube.com/channel/UC.../live` または `https://www.youtube.com/watch?v=...` | 配信中のURL。`/channel/<ID>/live` はライブへ自動転送される |
| `test_max_seconds` | `0` | 時間ではなくサイズで打ち切る |
| `skip_upload` | **false**（チェックしない） | 実際に Drive へアップロードする |
| `max_download_gb` | `2.5` | 約2.5GBに達したら SIGINT で停止し、結合後にアップロード |
| `use_warp` | **true** | Cloudflare WARP 経由にする（本番の schedule は常時有効） |
| `probe_only` | false | true にすると DL せず経路の可否だけ確認する |

- `test_max_seconds` と `max_download_gb` は併用でき、先に達した方で打ち切る。
- サイズ上限で止めた場合は終了コード -3 として扱い、**失敗ではなく正常な打ち切り**（そのままアップロードへ進む）。
- アップロード先は Variables の `GDRIVE_VIDEO_DEST`（例 `gdrive:/keeby/videos`）。
- 成功するとログに `アップロード完了 & ローカル削除済み。` と `status=ok` が出る。
- Drive が満杯なら `storageQuotaExceeded` を検出して exit 4 になり、
  録画は `youtube-fallback-recording` として artifact に3日間退避される。

> 2.5GB の取得には回線・配信ビットレートにより数十分かかる。
> 途中経過は `recorder.log` の `[動画DL] DL中... サイズ:` で確認できる。

### 13-6. 失敗通知（メール）が届かなかった原因と対策

2026-10-29 の録画失敗時、Issue は自動作成されたがメールは届かなかった。原因は2つ。

1. **Issue に担当者もメンションも無かった**
   - GitHub のメール通知は「担当・メンション・購読」が条件。`GITHUB_TOKEN` が作っただけの Issue は誰にも紐づかず、メールは送られない。
   - 対策：`gh issue create` に `--assignee "$GITHUB_REPOSITORY_OWNER"` を付け、本文にも `cc @<owner>` を入れた。
2. **Cookie 失効そのものが検知されていなかった**
   - `YouTube Cookie Check` は全51回 success。失効 Issue は一度も作られていない。
   - 旧 `cookie_check.py login` は HTML の `"LOGGED_IN":true` しか見ておらず、
     `Sign in to confirm you're not a bot`（player API の bot 判定）はログイン有効のまま起きるため検知できない。
   - その後、原因は Cookie の鮮度ではなく **ランナーのIP** だと判明した（13-8）。
     毎時のログイン巡回は廃止し、週1回の `access-check.yml` が本番経路の疎通だけを見る。

メール通知を確実に受け取るには、GitHub 側の設定も必要：

- `Settings → Notifications → Email` で、少なくとも **Assigned** と **Actions** を有効にする
- メールアドレスが verified であること
- リポジトリを Watch していること（`Watch → All Activity` または `Custom → Issues`）

これらが揃っていれば、次回の失敗からは Issue 作成時にメールが届く。

### 13-7. 2026-10-29 の容量テストが 1.6GB で止まった原因（HLSの一時終端）

2.5GB 指定で実行した run #149 (`40893407210`) は 1.6GB で終わった。サイズ上限でも
ウォッチドッグでもなく、**yt-dlp が exit 0 で正常終了**していた。

- 15:33:31 に `[video-s1-0] 終了 (コード: 0)`、直前でサイズが 1.7GB→1.6GB に減少
- `ytdlp-video-0-stderr.log` の末尾は `size= 1781090kB time=00:53:49.98`（ffmpegの正常終了）
- 同じ動画ID `A745Pi5_G-g` はその後も `isLive=true`（タイトルは次の試合に変化）

つまり **配信は続いているのに、HLS マニフェストが一時的に終端（ENDLIST）を返した**。
試合間の切り替えやエンコーダの再接続で起きる。従来は「exit 0 = 配信終了」と
決めつけていたため、そこで録画を打ち切って成功扱いにしていた。

**対策：セグメント継続**

- yt-dlp が終了したら Data API (`videos.list`, 1 unit) で `actualEndTime` の有無を確認する
- まだライブなら `... part02.mp4` として続きを録り、最後に ffmpeg で1本に結合する
- 配信が本当に終わっていた場合だけ完了する（API が引けない場合は安全側で終了）
- 打ち切り（時間上限 `-2` / サイズ上限 `-3`）は継続しない
- セグメント上限は `MAX_SEGMENTS`（既定30）

この確認は player API ではなく Data API なので、bot 判定の材料にならない。

### 13-8. GitHub Actions の IP が YouTube にブロックされる問題（2026-10-29 実測）

#### 何が起きたか

同じ URL・同じ yt-dlp で、実行場所だけを変えた結果：

| 実行場所 | 結果 |
|---|---|
| 手元（住宅IP） | 匿名で即 OK（`is_live / 301`） |
| GitHub Actions のランナー | Cookie ありでも `Sign in to confirm you're not a bot` |
| 同上（PO Token provider 導入後） | まだ NG |
| 同上（Cloudflare WARP 経由） | **全経路 OK** |

原因は Cookie でも yt-dlp でもなく、**ランナーの共有IPの評判**。GitHub Actions の IP は
大量のスクレイパーと共有されており、YouTube から見ると常に bot に近い。
Cookie の鮮度を上げても解決しない。

ランナーは毎回別のIPが割り当てられるため、**たまたま通る回がある**（14:39 の録画成功、
16:24 の `android_vr` 成功）。これが「動くときと動かないときがある」の正体。

#### 対策

1. **WARP で出口IPを変える（既定で有効）**
   - `setup_warp.sh` が Cloudflare WARP をプロキシモード（`127.0.0.1:40000`）で起動する。
   - TUN を作らないので、ランナーでも権限問題が起きにくい。
   - schedule 実行では常に有効。手動実行は `use_warp` を true にする。
   - 実測：WARP 経由にした瞬間、bot 壁が消えて `android_vr` / `mweb` / `tv_simply` /
     `web_embedded` / VOD がすべて通った。
   - 失敗してもジョブは止めず、直接接続にフォールバックする。

2. **PO Token provider（保険）**
   - `setup_potoken.sh` が `bgutil-ytdlp-pot-provider` の yt-dlp プラグインと
     Docker の provider（`127.0.0.1:4416`）を入れる。
   - プラグインは `<plugins>/<name>/yt_dlp_plugins` に置く。直下に置くと
     `Plugin directories: none` になり読み込まれない（実測）。
   - yt-dlp のスタンドアロンバイナリは既定の config ディレクトリを拾わないことが
     あるため、`--plugin-dirs` を明示している。

3. **Cookie は最後のフォールバック**
   - 戦略の最後にだけ置く。失効していても匿名経路で録画できる。

#### それでもダメな場合

WARP の IP も YouTube にブロックされることはあり得る。その場合は
**専用IPを持つホストに移す**のが確実。

| 案 | コスト | 特徴 |
|---|---|---|
| Oracle Cloud Always Free（ARM VM） | 0円 | 24/7・専用IP。無料枠の範囲で動く |
| 格安VPS（Xserver VPS 等） | 月500円〜 | 旧VPSで実績あり。本リポジトリのスクリプトを流用できる |
| 自宅の常時起動PC / Raspberry Pi | 1〜2万円（初期） | 住宅IPなので bot 判定がほぼ無い。最も確実 |

#### 経路の実測方法

`Actions → YouTube Live Recorder → Run workflow` で `probe_only` を true にして実行すると、
ダウンロードせずに各 player_client の可否だけを `PROBE RESULT` として出す。
`use_warp` と組み合わせれば、WARP あり/なしの比較も1回でできる。

### 13-9. WARP 導入後の 2.5GB テスト成功と、そこで見つかった「ライブ端 403」

WARP を入れた直後の run #158 (`40922075004`, 2026-10-29 17:31〜18:06 UTC) は
**2.5GB で正しく停止し、結合して Drive へアップロード完了**（`status=ok`）。

ただし途中で別の不具合が出た。`--live-from-start` を付けない既定クライアントは DVR の先頭から
ではなくライブ端から取り始め、`speed=1.16x` で先を読みに行く。まだ配信されていない
セグメントを要求して **HTTP 403** になり、45〜60秒で終了していた。そのたびに
セグメント継続が走り、1分の断片を30本（773MB）まで作っていた。

**対策**

- 先頭セグメントは常に `--live-from-start`（DVR の先頭から）。端に触れないので 403 が出ない
- 2セグメント目以降は重複するため付けない
- それでも何も録れない場合だけ、最後の逃げ道として `--live-from-start` なし（ライブ端）を1回試す
- 配信開始から `MAX_DVR_LAG_SECONDS`（既定1800秒）以上経っている場合は、DVR先頭ではなく
  ライブ端から録る。検知が数時間遅れたときに、数時間ぶんの遅延ダウンロードで
  job の6時間上限に達して録画を失うのを防ぐ
- `HTTP Error 403` を Cookie 失効の判定材料から外した（未配信セグメントでも出るため）
- 試行順を実測に合わせて `匿名 → android_vr → mweb → tv_simply → web_safari → web_embedded → tv → 緩い形式 → Cookie` に変更
  - 既定クライアントが WARP 下で 25分連続して安定していたのが理由
  - `android_vr` は formats が1本（301）しか返さず、長時間の連続取得に向かない

この2点（セグメント継続と `--live-from-start`）は `test_recorder_logic.py` で固定してあり、
`python check_all.py` が毎回検証する。
