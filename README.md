# Instagram リール自動投稿

スプレッドシートで指定した時刻に、Googleドライブの動画とカバー画像を使って
Instagramへリールを自動投稿する仕組み。GitHub Actions で無人運用する。

投稿先は行ごとに選べる。

| アカウント | Instagram | Facebookページ |
|---|---|---|
| `love64`（既定） | @love64_official | らぶ64 |
| `jobtype64` | @jobtype64（IG ユーザーID 17841436543196844） | JOB TYPE 64（1316322531565787） |

## 仕組み

1. Apps Script の5分ごとのトリガー（`apps_script_trigger.gs`）と GitHub の cron から `post_reel.py` を実行
2. スプレッドシートで「ステータス」が空欄かつ「投稿予定時刻」が現在(JST)以前の行を探す
3. 行の「アカウント」「媒体」から投稿先を決める
4. ドライブから動画・カバー画像を取得 → Cloudinary へアップして公開URL化
5. Instagram Graph API でリールを投稿
6. 結果（投稿済/URL/エラー）をスプレッドシートへ書き戻し、Slack に通知（アカウント名入り）

## スプレッドシートの列

| 投稿予定時刻 | 動画ファイル名 | カバー画像ファイル名 | キャプション | ステータス | 投稿URL | エラー | アカウント | 媒体 |
|---|---|---|---|---|---|---|---|---|
| 2026-06-01 19:00 | reel_01.mp4 | cover_01.jpg | 本文＋ハッシュタグ | （空欄） | | | （空欄＝love64） | （空欄＝instagram） |
| 2026-06-01 20:00 | jt_01.mp4 | | 本文 | （空欄） | | | jobtype64 | instagram |

- 列は**1行目の項目名で特定する**。並び順は自由で、途中に列を足しても動く（項目名は上の表と完全に同じにする）。
- 「ステータス」が空欄の行だけが投稿対象。投稿後に自動で「投稿済」が入る。
- 投稿予定時刻は JST。書式は `2026-06-01 19:00` など。
- 「アカウント」列：`love64` か `jobtype64`。空欄・列が無いときは `love64`（大文字小文字・全角半角は問わない）。
- 「媒体」列：`instagram` か `tiktok`。空欄・列が無いときは `instagram`。
- 「アカウント」「媒体」列が無いシートでも、今までどおり全行を love64 の Instagram に投稿する。

### 投稿せずに「エラー」にする行

時刻を迎えた時点で次に当てはまる行は、投稿せず「ステータス」を「エラー」、「エラー」列に理由を書く。
他の行（love64 など）の投稿は止まらない。

| 理由（エラー列） | 条件 |
|---|---|
| `未設定のアカウント（jobtype64）` | そのアカウントの Secrets（IG ユーザーID・トークン）が未登録 |
| `未設定（TikTok jobtype64）：…` | 媒体が `tiktok` で、Buffer の API キーかそのアカウントのチャンネルIDが未登録（足りない Secret の名前が続く） |
| `ジョブタイプ64のキャプションに「MBTI」が含まれています（使用禁止）` | アカウントが `jobtype64` で、キャプションに MBTI（大文字小文字問わず）を含む |
| `不明なアカウント（…）` / `不明な媒体（…）` | 上の表にない値が入っている |

直したあとに再投稿させるときは、その行の「ステータス」と「エラー」を空欄に戻す。

## 必要な GitHub Secrets

| 名前 | 内容 |
|---|---|
| `IG_USER_ID` | love64 の InstagramビジネスアカウントID |
| `IG_ACCESS_TOKEN` | love64 の無期限ページアクセストークン（`get_page_token.py`で取得） |
| `SPREADSHEET_ID` | 投稿管理スプレッドシートのID（全アカウント共通） |
| `DRIVE_FOLDER_ID` | love64 の素材を置くドライブフォルダのID |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | サービスアカウントJSONキーの中身（全文） |
| `CLOUDINARY_URL` | `cloudinary://APIKEY:APISECRET@CLOUDNAME` 形式 |
| `SLACK_WEBHOOK_URL` | 投稿完了の通知先（任意） |
| `IG_USER_ID_JOBTYPE64` | jobtype64 の InstagramビジネスアカウントID（17841436543196844） |
| `IG_ACCESS_TOKEN_JOBTYPE64` | 「JOB TYPE 64」ページの無期限ページアクセストークン |
| `DRIVE_FOLDER_ID_JOBTYPE64` | jobtype64 の素材フォルダのID（任意。未登録なら `DRIVE_FOLDER_ID` のフォルダから探す） |
| `BUFFER_API_KEY` | Buffer の API キー（TikTok 用・全アカウント共通。Buffer の Settings → API で作る） |
| `BUFFER_CHANNEL_TIKTOK_JOBTYPE64` | jobtype64 の TikTok を Buffer につないだチャンネルのID（`list_buffer_channels.py` で調べる） |
| `BUFFER_CHANNEL_TIKTOK_LOVE64` | love64 の TikTok のチャンネルID（使うときだけ） |

## TikTok（Buffer 経由）

「媒体」が `tiktok` の行は、TikTok を直接ではなく **Buffer の公式 API** を通して投稿する（Buffer 無料プラン）。

1. ドライブから動画を取得 → Cloudinary の `tiktok/` フォルダへアップして公開URL化
2. Buffer の `createPost`（今すぐ投稿・自動公開）に動画URLとキャプションを渡す
3. 最大10分、Buffer の状態を見て待つ
   - 公開が終わった → 「投稿済」・投稿URL（Buffer が返したとき）を書き、Cloudinary の動画を消す
   - 10分で終わらない → 「送信中」にして「BufferID」列に Buffer の投稿IDを書く。**動画は消さない**（Buffer が公開するときに取りに来るため）
4. 次回以降の実行（5分ごと）で「送信中」の行を確かめる
   - 公開済み → 「投稿済」・投稿URL・Cloudinary の動画を削除・Slack 通知
   - Buffer 側で失敗 → 「エラー」と理由・Cloudinary の動画を削除
   - 予定時刻から24時間たっても送信中 → 「エラー」（Buffer の画面で確認する）

### 追加する列（任意）

| 列 | 内容 |
|---|---|
| `BufferID` | 送信中の行に自動で書く。**この列が無いと「送信中」の行を自動で確認できない**ので、TikTok を使うなら足しておく |
| `AI生成` | `はい`（true・1・○も可）なら TikTok に「AI生成コンテンツ」と表示する。空欄なら表示しない |

「カバー画像ファイル名」は TikTok では使わない（Buffer 無料プランは動画カバーの指定ができないため）。

### 投稿せずに「エラー」にする TikTok の行

| 理由 | 条件 |
|---|---|
| `未設定（TikTok jobtype64）：…` | `BUFFER_API_KEY` かチャンネルIDが未登録 |
| `ジョブタイプ64のキャプションに「MBTI」が含まれています（使用禁止）` | Instagram と同じ検査 |
| `TikTok のキャプションが長すぎます` | 2,200字超（動画付き TikTok の上限） |
| `TikTok のハッシュタグが多すぎます` | 6個以上（Buffer 経由の TikTok は1投稿5個まで） |

### Buffer 側の条件（2026-11-06 時点の公式ドキュメント）

- TikTok はビジネスアカウントにしてから Buffer につなぐ。Buffer の「Enable Notifications by default」はオフにする（オンだと自動公開ではなく通知になる）
- 動画：3秒〜10分・1GB以下・MP4/MOV/WEBM・縦横とも360px以上
- 無料プラン：チャンネル3つまで・予約はチャンネルごと10件まで・API は24時間600回／15分150回
- 音楽の追加はできない（API の制約）

### チャンネルIDの調べ方

```
read -s BUFFER_API_KEY && export BUFFER_API_KEY
python3 list_buffer_channels.py
gh secret set BUFFER_API_KEY
gh secret set BUFFER_CHANNEL_TIKTOK_JOBTYPE64
```

`service` が `tiktok` の行の ID を登録する。API キーの値は表示されない。

## jobtype64 を追加する手順

love64 と同じビジネスポートフォリオ・同じ Meta アプリ・同じシステムユーザーを使う。

1. Meta ビジネスマネージャ → ビジネス設定 → ユーザー → システムユーザー → 自動投稿で使っているシステムユーザーを選ぶ
2. 「アセットを割り当てる」で次の2つを割り当てる
   - ページ「JOB TYPE 64」：コンテンツの作成・管理（フルコントロールでも可）
   - Instagram アカウント「@jobtype64」：コンテンツの作成・管理
3. 同じ画面の「トークンを生成」で、いつものアプリを選び、権限 `instagram_basic` `instagram_content_publish` `pages_show_list` `pages_read_engagement` `business_management` を付けて生成する
4. ローカルで `get_page_token.py` を実行し、「JOB TYPE 64」のページトークン（有効期限が「無期限」のもの）と IG アカウントID（17841436543196844 であること）を確かめる
5. Secrets に登録する（値はコマンドに直接書かず、入力を求められたら貼り付ける）
   ```
   gh secret set IG_USER_ID_JOBTYPE64
   gh secret set IG_ACCESS_TOKEN_JOBTYPE64
   gh secret set DRIVE_FOLDER_ID_JOBTYPE64   # 専用フォルダを使う場合のみ
   ```
   専用フォルダを使う場合は、そのフォルダをサービスアカウントのメールアドレスに「閲覧者」で共有する
6. スプレッドシートの1行目に「アカウント」「媒体」の列を足し、jobtype64 の行に `jobtype64` と書く

## ローカルでの無期限トークン取得

```
APP_ID=... APP_SECRET=... SYSTEM_USER_TOKEN=... python3 get_page_token.py
```

システムユーザーが管理する全ページが一覧で出る。
ページ名を確かめ、有効期限が「無期限」のページトークンを該当アカウントの Secret
（love64 は `IG_ACCESS_TOKEN`、jobtype64 は `IG_ACCESS_TOKEN_JOBTYPE64`）に登録する。

## シートを2枚に分ける（2026-11-06）

- らぶ64のシート（Secret `SPREADSHEET_ID`）：「アカウント」列が無い・空欄の行は love64 として投稿
- ジョブタイプ64のシート（Secret `SPREADSHEET_ID_JOBTYPE64`）：「アカウント」列が無い・空欄の行は jobtype64 として投稿
- 1回の実行で2枚を順に処理する。片方のシートが開けなくても、もう片方は止まらない
- 5分ごとの起動（Apps Script のトリガー）はらぶ64のシートにある1本だけでよい。ジョブタイプ64のシートにトリガーは作らない
- ジョブタイプ64のシートも、サービスアカウントに「編集者」で共有する
