# Instagram リール自動投稿

スプレッドシートで指定した時刻に、Googleドライブの動画とカバー画像を使って
Instagramへリールを自動投稿する仕組み。GitHub Actions の cron で無人運用する。

## 仕組み

1. GitHub Actions が15分ごとに `post_reel.py` を実行
2. スプレッドシートで「ステータス」が空欄かつ「投稿予定時刻」が現在(JST)以前の行を探す
3. ドライブから動画・カバー画像を取得 → Cloudinary へアップして公開URL化
4. Instagram Graph API でリールを投稿
5. 結果（投稿済/URL/エラー）をスプレッドシートへ書き戻し

## スプレッドシートの列

| 投稿予定時刻 | 動画ファイル名 | カバー画像ファイル名 | キャプション | ステータス | 投稿URL | エラー |
|---|---|---|---|---|---|---|
| 2026-06-01 19:00 | reel_01.mp4 | cover_01.jpg | 本文＋ハッシュタグ | （空欄） | | |

- 「ステータス」が空欄の行だけが投稿対象。投稿後に自動で「投稿済」が入る。
- 投稿予定時刻は JST。書式は `2026-06-01 19:00` など。

## 必要な GitHub Secrets

| 名前 | 内容 |
|---|---|
| `IG_USER_ID` | InstagramビジネスアカウントID |
| `IG_ACCESS_TOKEN` | 無期限のページアクセストークン（`get_page_token.py`で取得） |
| `SPREADSHEET_ID` | 投稿管理スプレッドシートのID |
| `DRIVE_FOLDER_ID` | 素材を置くドライブフォルダのID |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | サービスアカウントJSONキーの中身（全文） |
| `CLOUDINARY_URL` | `cloudinary://APIKEY:APISECRET@CLOUDNAME` 形式 |

## ローカルでの無期限トークン取得

```
APP_ID=... APP_SECRET=... SYSTEM_USER_TOKEN=... python3 get_page_token.py
```

出力されたページトークン（有効期限が「無期限」のもの）を `IG_ACCESS_TOKEN` に登録する。
