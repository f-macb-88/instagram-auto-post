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
| `TikTok は未対応` | 媒体が `tiktok`（投稿方式が未決定のため。後で Buffer API 等を足す） |
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
