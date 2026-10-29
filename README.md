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
├── setup_rclone.sh                    # Secrets から rclone.conf を生成
├── requirements.txt                   # Python 依存
├── check_all.py                       # 開発用チェック（YAML/構文/文字化け）
├── cookie_check.py                    # Cookie検査（ログイン確認 / canary）
├── .gitattributes                     # LF・UTF-8 固定
├── .gitignore                         # 録画データ・認証情報・venv を除外
└── .github/workflows/
    ├── youtube-recorder.yml           # YouTube用 cron ジョブ
    ├── twitch-recorder.yml            # Twitch用 cron ジョブ
    ├── cookie-check.yml               # 毎時のcookieログイン確認
    ├── cookie-canary.yml              # 日次のcookie canary
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
4. cookieを更新したら、`YouTube Cookie Check` と `YouTube Cookie Canary` も手動実行し、両方successになることを確認する。

> **注意**: 実チャンネルがライブ中じゃないと「動画DL」には入らない。手動テスト時は一時的に `CHANNEL_IDS` を
> ライブ中のチャンネルIDに変更するか、`workflow_dispatch` でそのまま実行してログの挙動を確かめること。

---

## 5. cron について（Actionsの遅延対策）

GitHub公式ドキュメントに「**毎時0分は実行が集中し、遅延およびdropが起きうる**」と明記されている。
そのため本リポジトリでは：

- YouTube: `5,20,35,50 * * * *`（15分間隔、`:00` を回避）
- Twitch: `12,27,42,57 * * * *`（15分間隔、YouTube側と時刻をずらす）
- cookie-check: `17 * * * *`（毎時17分。実行後に0〜5分のランダム待機）
- cookie-canary: `43 4 * * *`（毎日4:43 UTC＝13:43 JST。実行後に0〜10分のランダム待機）
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
| 常駐プロセス | systemd で常時起動 | 15分ごとの単発ジョブ |
| 無限ループ | `while True` で15分待機しながら継続 | 1回検知して、あれば録画、無ければ即終了 |
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

## 12. Cookie監視（cookie-check / cookie-canary）

YouTubeのcookieは数週間〜数か月で失効する。失効中は録画が `Sign in to confirm you're not a bot` などで失敗するが、従来は録画開始時まで気づけず、Jobもsuccess扱いになることがあった。現在は次の3段階で検知する。

### 12-1. 毎時のログイン確認（cookie-check.yml）

- 毎時17分に起動し、0〜5分のランダム待機後に1回だけ実行する。
- `cookie_check.py login` が12個のURL（登録チャンネル、履歴、ライブラリ、後で見る、高評価、ホーム、急上昇、アカウント、コース、m.youtube版）から最大2個をランダムに選ぶ。
- cookie付きHTTP GETのHTMLに `"LOGGED_IN":true` があるかだけを見る。動画のplayer APIは叩かない。
- 失敗時はIssue `YouTube cookie session check failed` を作成し、Jobを失敗させる。次回成功時に自動クローズする。
- Issue通知はリポジトリのIssues機能を使う。無効だとIssue作成ステップで失敗する（Job自体も失敗する）。
- URL・User-Agent・待機時間を毎回変え、固定パターンのアクセスを避ける。

### 12-2. 日次canary（cookie-canary.yml）

- 毎日4:43 UTC（13:43 JST）に起動し、0〜10分のランダム待機後に実行する。
- nightly yt-dlpとdenoを導入し、`cookie_check.py canary` が31本の公開動画リストから最大5本をランダムに選ぶ。
- 各動画に `yt-dlp --simulate --cookies cookies.txt` を実行し、cookieで実際にメタデータ・フォーマットを取得できるか確認する。非公開・地域制限などで失敗した動画は読み飛ばす。
- cookie失効・bot判定を検知したらIssue `YouTube cookie canary failed` を作成する。
- 軽量チェックが通っても、player API側でだけ失敗する場合をここで拾う。

### 12-3. 録画時の失敗検知（youtube-recorder.yml）

- 録画後に `logs/recorder.log` を検査し、`cookies are no longer valid` または `Sign in to confirm you're not a bot` があればJobを失敗させる。
- これにより「録画に失敗したのにJobはsuccess」という状態をなくす。ログは従来どおりartifact `recorder-logs` に残る。

### 12-4. cookieを更新するとき

1. ブラウザでYouTubeにログインし、Netscape形式のcookieを再取得する。
2. GitHubの `Settings → Secrets and variables → Actions` で `COOKIES_TXT` を更新する。
3. `Actions → YouTube Cookie Check → Run workflow` と `Actions → YouTube Cookie Canary → Run workflow` を手動実行する。
4. 両方successになり、open中のcookie Issueが自動クローズされれば完了。

> cookieの失効日時を事前に予測するものではない。失効後、最大約1時間（＋ランダム待機・GitHub側の遅延）で検知する。監視は「失効に早く気づく」ためのもので、失効から更新までの間に始まった配信は録画できない。完全に防ぐにはcookieの自動更新か、別の認証方式が必要。

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
   - 同時実行は `concurrency` で1本に制限されるため、pending は最新1件に集約される。

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
- 偽API/偽yt-dlpでのロジックテスト20件（監視ループ、quotaフェイルオーバー、終了コード3/4/5、単発モード）が全PASS。

### 13-4. 残る制約

- 監視jobが4時間で終わるため、job上限（350分）に近い長さの配信では、録画がタイムアウトで切れる可能性がある。その場合、次jobがライブを再検知して `--live-from-start` で頭から取り直す（重複はrcloneが同名上書き）。
- 監視の連鎖が何らかの理由で切れた場合に備え、cron はそのまま残している（`5,20,35,50` / `12,27,42,57`）。
- 60日ルール対策の `keepalive.yml` も従来どおり必要。
