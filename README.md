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
├── .gitattributes                     # LF・UTF-8 固定
├── .gitignore                         # 録画データ・認証情報・venv を除外
└── .github/workflows/
    ├── youtube-recorder.yml           # YouTube用 cron ジョブ
    ├── twitch-recorder.yml            # Twitch用 cron ジョブ
    └── keepalive.yml                  # 60日ルール対策（毎日 空コミット push）
```

---

## 3. セットアップ手順

### 3-1. リポジトリ作成（public・無料）

1. GitHub（**`ucalis-uma` アカウント**）で新規リポジトリ `keeby-recorder` を作成し、**Public** にする。
   - private でも動作するが、Actions の分数上限（月2,000分）が適用され、超過時は課金はblocked（カード未登録でも課金は発生しないが制限される）。**public の方が制約无表情**。
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
| `COOKIES_TXT` | `cookies.txt` の全文（任意。YouTubeの403対策。空でも動く） |

> `COOKIES_TXT` はログイン情報を含むため **必ず Secret** にして、リポジトリには置かないこと。

---

## 4. 動作確認

1. リポジトリの **Actions** タブを開く。
2. `YouTube Live Recorder` を選び **Run workflow**（手動実行）でテスト。
3. ログに以下が出ればOK：
   - ライブ中なら `ライブ配信を検知: https://...` → 録画 → `Google Driveへアップロード中` → `アップロード完了`
   - ライブ無しなら `どのチャンネルもライブ配信なし。何もしないで終了します。`

> **注意**: 実チャンネルがライブ中じゃないと「動画DL」には入らない。手動テスト時は一時的に `CHANNEL_IDS` を
> ライブ中のチャンネルIDに変更するか、`workflow_dispatch` でそのまま実行してログの挙動を確かめること。

---

## 5. cron について（Actionsの遅延対策）

GitHub公式ドキュメントに「**毎時0分は実行が集中し、遅延およびdropが起きうる**」と明記されている。
そのため本リポジトリでは：

- YouTube: `5,20,35,50 * * * *`（15分間隔、`:00` を回避）
- Twitch: `12,27,42,57 * * * *`（15分間隔、YouTube側と時刻をずらす）
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

## 7. 移行前の注意（VPS时代的差异）

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

その他：yt-dlp の安定版→nightly 二重取得を nightly 1回に整理、Deno 導入は `sudo env DENO_INSTALL=...` 方式に変更（`sudo` の `env_reset` 対策）、rclone 導入は先にダウンロードしてから `sudo bash` する方式に変更。
