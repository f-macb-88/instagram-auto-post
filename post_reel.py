#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Instagram リール自動投稿スクリプト（複数アカウント対応）

スプレッドシートの「投稿予定時刻」が現在(JST)以前で、かつ「ステータス」が空欄の行を探し、
Googleドライブから動画・カバー画像を取得 → Cloudinaryへアップして公開URL化 →
Instagram Graph API でリールを投稿し、結果をスプレッドシートに書き戻す。

投稿先は行ごとに「アカウント」列（love64／jobtype64。空欄は love64）と
「媒体」列（instagram／tiktok。空欄は instagram）で切り替える。
どちらの列も無いシートでは、従来どおり全行を love64 の Instagram に投稿する。

GitHub Actions から定期実行される想定。
設定値はすべて環境変数(GitHub Secrets)から読み込む。
"""

import io
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

import cloudinary
import cloudinary.uploader
import gspread
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ---- 設定（環境変数から）---------------------------------------------------
# シートとサービスアカウントは全アカウント共通。main() の中で必須チェックする。
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
# CLOUDINARY_URL 環境変数は cloudinary ライブラリが自動で読む
# Slack通知用（任意）。未設定なら通知をスキップする。
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

GRAPH = "https://graph.facebook.com/v21.0"
JST = ZoneInfo("Asia/Tokyo")

DEFAULT_ACCOUNT = "love64"
DEFAULT_PLATFORM = "instagram"
SUPPORTED_PLATFORMS = ("instagram", "tiktok")


def load_accounts(env=None):
    """アカウント名 → 投稿先の設定 の表を作る。

    値が無いアカウントも表には載せ、使う時点で「未設定」としてその行だけエラーにする
    （jobtype64 の値が無くても love64 の投稿は止めない）。
    """
    env = os.environ if env is None else env
    love64_folder = env.get("DRIVE_FOLDER_ID", "")
    return {
        "love64": {
            "ig_user_id": env.get("IG_USER_ID", ""),
            "access_token": env.get("IG_ACCESS_TOKEN", ""),
            "drive_folder_id": love64_folder,
        },
        "jobtype64": {
            "ig_user_id": env.get("IG_USER_ID_JOBTYPE64", ""),
            "access_token": env.get("IG_ACCESS_TOKEN_JOBTYPE64", ""),
            # 専用フォルダが未設定なら love64 と同じフォルダから素材を探す
            "drive_folder_id": env.get("DRIVE_FOLDER_ID_JOBTYPE64", "") or love64_folder,
        },
    }


# スプレッドシートの列ヘッダー（1行目）。列は位置ではなくこの項目名で特定する。
COL_SCHEDULED = "投稿予定時刻"
COL_VIDEO = "動画ファイル名"
COL_COVER = "カバー画像ファイル名"
COL_CAPTION = "キャプション"
COL_STATUS = "ステータス"
COL_URL = "投稿URL"
COL_ERROR = "エラー"
COL_ACCOUNT = "アカウント"  # 任意の列。無い・空欄なら love64
COL_PLATFORM = "媒体"  # 任意の列。無い・空欄なら instagram

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def log(msg):
    print(f"[{datetime.now(JST):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def notify_slack(text):
    """Slack の Incoming Webhook にメッセージを送る。未設定・失敗時は黙って無視。"""
    if not SLACK_WEBHOOK_URL:
        return
    try:
        requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=30)
    except Exception as e:
        log(f"  Slack通知に失敗（無視）: {e}")


def parse_scheduled_time(raw):
    """投稿予定時刻の文字列を JST の datetime に変換。複数の書式を許容。"""
    raw = str(raw).strip()
    if not raw:
        return None
    fmts = [
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=JST)
        except ValueError:
            continue
    raise ValueError(f"投稿予定時刻の書式を認識できません: '{raw}'（例: 2026-06-01 19:00）")


def _normalize_cell(value):
    """セルの値を比較用に整える（全角→半角・前後の空白除去・小文字化）。"""
    return unicodedata.normalize("NFKC", str(value or "")).strip().lower()


def resolve_account(row):
    """行の「アカウント」列からアカウント名を返す。列が無い・空欄なら love64。"""
    return _normalize_cell(row.get(COL_ACCOUNT, "")) or DEFAULT_ACCOUNT


def resolve_platform(row):
    """行の「媒体」列から媒体名を返す。列が無い・空欄なら instagram。"""
    return _normalize_cell(row.get(COL_PLATFORM, "")) or DEFAULT_PLATFORM


def contains_mbti(caption):
    """キャプションに「MBTI」が含まれるか（大文字小文字・全角半角を問わない）。"""
    return "mbti" in _normalize_cell(caption)


def check_row(row, accounts):
    """投稿時刻を迎えた行について、投稿してよいかを判定する（ネットワークは使わない）。

    戻り値:
      ("instagram", アカウント名, 設定) … Instagram に投稿する
      ("error", アカウント名, エラー文)  … 投稿せず、その行をエラーにする
    """
    account = resolve_account(row)
    platform = resolve_platform(row)

    if platform not in SUPPORTED_PLATFORMS:
        return ("error", account, f"不明な媒体（{platform}）。instagram か tiktok を指定してください")

    if account not in accounts:
        names = "／".join(accounts)
        return ("error", account, f"不明なアカウント（{account}）。{names} のどれかを指定してください")

    if platform == "tiktok":
        # TikTok は投稿方式が未決定。Buffer API 等で対応するときはここに分岐を足す。
        return ("error", account, "TikTok は未対応")

    cfg = accounts[account]
    if not (cfg["ig_user_id"] and cfg["access_token"] and cfg["drive_folder_id"]):
        return ("error", account, f"未設定のアカウント（{account}）")

    if account == "jobtype64" and contains_mbti(row.get(COL_CAPTION, "")):
        return ("error", account, "ジョブタイプ64のキャプションに「MBTI」が含まれています（使用禁止）")

    return ("instagram", account, cfg)


def find_drive_file(drive, folder_id, name):
    """ドライブの指定フォルダ内からファイル名で検索し file_id を返す。

    Mac由来のファイル名は濁点などがNFD（分解形）で保存されることがあり、
    スプレッドシート側のNFC（合成形）と単純比較すると一致しない。
    そのためフォルダ内の全ファイルを列挙し、NFCに正規化して比較する。
    """
    target = unicodedata.normalize("NFC", name)
    q = f"'{folder_id}' in parents and trashed = false"
    page_token = None
    while True:
        res = drive.files().list(
            q=q,
            fields="nextPageToken, files(id, name)",
            pageSize=1000,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageToken=page_token,
        ).execute()
        for f in res.get("files", []):
            if unicodedata.normalize("NFC", f["name"]) == target:
                return f["id"]
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    raise FileNotFoundError(f"ドライブにファイルが見つかりません: '{name}'")


def download_drive_file(drive, file_id, dest_path):
    """ドライブのファイルをローカルにダウンロード。"""
    request = drive.files().get_media(fileId=file_id)
    with io.FileIO(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return dest_path


def upload_to_cloudinary(path, resource_type):
    """Cloudinary にアップロードして公開URLと public_id を返す。"""
    if resource_type == "video":
        result = cloudinary.uploader.upload_large(
            path, resource_type="video", folder="ig_reels"
        )
    else:
        result = cloudinary.uploader.upload(
            path, resource_type="image", folder="ig_reels"
        )
    return result["secure_url"], result["public_id"], resource_type


def delete_from_cloudinary(public_id, resource_type):
    try:
        cloudinary.uploader.destroy(public_id, resource_type=resource_type)
    except Exception as e:
        log(f"  Cloudinary削除に失敗（無視）: {e}")


def create_reel_container(ig_user_id, access_token, video_url, cover_url, caption):
    data = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "access_token": access_token,
    }
    if cover_url:
        data["cover_url"] = cover_url
    r = requests.post(f"{GRAPH}/{ig_user_id}/media", data=data, timeout=120)
    body = r.json()
    if "id" not in body:
        raise RuntimeError(f"コンテナ作成に失敗: {body}")
    return body["id"]


def wait_until_finished(access_token, creation_id, max_wait_sec=600, interval=15):
    """動画処理の完了(FINISHED)を待つ。"""
    waited = 0
    while waited < max_wait_sec:
        r = requests.get(
            f"{GRAPH}/{creation_id}",
            params={"fields": "status_code,status", "access_token": access_token},
            timeout=60,
        )
        body = r.json()
        code = body.get("status_code")
        if code == "FINISHED":
            return
        if code == "ERROR":
            raise RuntimeError(f"動画処理がエラー: {body}")
        log(f"  動画処理中... ({code}) {waited}s")
        time.sleep(interval)
        waited += interval
    raise TimeoutError("動画処理がタイムアウトしました")


def publish_container(ig_user_id, access_token, creation_id):
    r = requests.post(
        f"{GRAPH}/{ig_user_id}/media_publish",
        data={"creation_id": creation_id, "access_token": access_token},
        timeout=120,
    )
    body = r.json()
    if "id" not in body:
        raise RuntimeError(f"公開に失敗: {body}")
    return body["id"]


def get_permalink(access_token, media_id):
    r = requests.get(
        f"{GRAPH}/{media_id}",
        params={"fields": "permalink", "access_token": access_token},
        timeout=60,
    )
    return r.json().get("permalink", "")


def safe_tmp_path(name):
    """シートに書かれたファイル名から、/tmp の中だけを指す保存先を作る（../ や / を含む名前で外へ書かせない）"""
    base = os.path.basename(str(name).replace("\\", "/")).strip()
    if not base or base in (".", ".."):
        raise ValueError(f"ファイル名が不正です: {name!r}")
    return os.path.join("/tmp", base)


def redact(text):
    """エラー文から鍵・トークンを伏せる（シートとログに書く前に必ず通す）"""
    text = str(text)
    for k in ("IG_ACCESS_TOKEN", "IG_ACCESS_TOKEN_JOBTYPE64", "CLOUDINARY_URL"):
        v = os.environ.get(k)
        if v:
            text = text.replace(v, "***")
    return re.sub(r"(access_token|api_key|api_secret|token)=([^&\s'\"]+)", r"\1=***", text)


def mark_error(ws, col_idx, sheet_row, message):
    """行のステータスを「エラー」にし、エラー列があれば理由を書く。"""
    ws.update_cell(sheet_row, col_idx[COL_STATUS], "エラー")
    if COL_ERROR in col_idx:
        ws.update_cell(sheet_row, col_idx[COL_ERROR], redact(message)[:500])


def post_instagram_reel(drive, cfg, video_name, cover_name, caption):
    """1行分のリールを投稿して permalink を返す。Cloudinary の一時ファイルは必ず消す。"""
    folder_id = cfg["drive_folder_id"]
    ig_user_id = cfg["ig_user_id"]
    token = cfg["access_token"]
    uploaded = []  # Cloudinary後始末用
    try:
        # 1) ドライブから取得
        video_id = find_drive_file(drive, folder_id, video_name)
        video_path = safe_tmp_path(video_name)
        download_drive_file(drive, video_id, video_path)
        log("  動画ダウンロード完了")

        cover_url = ""
        if cover_name:
            cover_id = find_drive_file(drive, folder_id, cover_name)
            cover_path = safe_tmp_path(cover_name)
            download_drive_file(drive, cover_id, cover_path)
            cover_url, cpid, crt = upload_to_cloudinary(cover_path, "image")
            uploaded.append((cpid, crt))
            log("  カバー画像アップロード完了")

        # 2) Cloudinaryへ
        video_url, vpid, vrt = upload_to_cloudinary(video_path, "video")
        uploaded.append((vpid, vrt))
        log("  動画アップロード完了")

        # 3) Instagram投稿
        creation_id = create_reel_container(ig_user_id, token, video_url, cover_url, caption)
        log(f"  コンテナ作成: {creation_id}")
        wait_until_finished(token, creation_id)
        media_id = publish_container(ig_user_id, token, creation_id)
        return get_permalink(token, media_id)
    finally:
        for pid, rt in uploaded:
            delete_from_cloudinary(pid, rt)


def main():
    for name, value in (
        ("SPREADSHEET_ID", SPREADSHEET_ID),
        ("GOOGLE_SERVICE_ACCOUNT_JSON", SERVICE_ACCOUNT_JSON),
    ):
        if not value:
            log(f"エラー: 環境変数 {name} が未設定です。")
            sys.exit(1)

    accounts = load_accounts()
    for name, cfg in accounts.items():
        ready = bool(cfg["ig_user_id"] and cfg["access_token"] and cfg["drive_folder_id"])
        log(f"アカウント {name}: {'設定あり' if ready else '未設定（この行はエラーにする）'}")

    creds = service_account.Credentials.from_service_account_info(
        json.loads(SERVICE_ACCOUNT_JSON), scopes=SCOPES
    )
    gc = gspread.authorize(creds)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    ws = gc.open_by_key(SPREADSHEET_ID).sheet1
    rows = ws.get_all_records()  # 1行目をヘッダーとした辞書のリスト
    header = ws.row_values(1)
    # 項目名 → 1始まりの列番号。列は必ずこの表で引く（列文字の直書きはしない）
    col_idx = {name: i + 1 for i, name in enumerate(header)}

    for required in (COL_SCHEDULED, COL_VIDEO, COL_CAPTION, COL_STATUS):
        if required not in col_idx:
            log(f"エラー: 必須列 '{required}' がスプレッドシートにありません。")
            sys.exit(1)

    now = datetime.now(JST)
    log(f"現在時刻(JST): {now:%Y-%m-%d %H:%M}　全{len(rows)}行をチェック")

    posted = 0
    for i, row in enumerate(rows):
        sheet_row = i + 2  # ヘッダー分+1、0始まり+1

        if str(row.get(COL_STATUS, "")).strip():
            continue  # ステータスが入っている行はスキップ（投稿済/エラー/手動記入）

        try:
            scheduled = parse_scheduled_time(row.get(COL_SCHEDULED, ""))
        except ValueError as e:
            log(f"行{sheet_row}: {e}")
            mark_error(ws, col_idx, sheet_row, e)
            continue

        if scheduled is None or scheduled > now:
            continue  # 時刻未到来 or 空欄

        action, account, detail = check_row(row, accounts)
        if action == "error":
            log(f"行{sheet_row}: [{account}] 投稿しません: {detail}")
            mark_error(ws, col_idx, sheet_row, detail)
            continue

        cfg = detail
        video_name = str(row.get(COL_VIDEO, "")).strip()
        cover_name = str(row.get(COL_COVER, "")).strip()
        caption = str(row.get(COL_CAPTION, ""))

        log(
            f"行{sheet_row}: [{account}] 投稿開始"
            f"（予定 {scheduled:%Y-%m-%d %H:%M} / 動画 {video_name}）"
        )
        try:
            if not video_name:
                raise ValueError("動画ファイル名が空です")

            permalink = post_instagram_reel(drive, cfg, video_name, cover_name, caption)
            log(f"  公開完了: {permalink}")

            # シートに結果を記入
            ws.update_cell(sheet_row, col_idx[COL_STATUS], "投稿済")
            if COL_URL in col_idx:
                ws.update_cell(sheet_row, col_idx[COL_URL], permalink)
            if COL_ERROR in col_idx:
                ws.update_cell(sheet_row, col_idx[COL_ERROR], "")
            posted += 1

            notify_slack(
                f"✅ Instagram（{account}）にリールを投稿しました\n"
                f"動画: {video_name}\n"
                f"{permalink}"
            )

        except Exception as e:
            log(f"  失敗: {redact(e)}")
            mark_error(ws, col_idx, sheet_row, e)

    log(f"完了。今回の投稿数: {posted}")


if __name__ == "__main__":
    main()
