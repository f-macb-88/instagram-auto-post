#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Instagram リール自動投稿スクリプト

スプレッドシートの「投稿予定時刻」が現在(JST)以前で、かつ「ステータス」が空欄の行を探し、
Googleドライブから動画・カバー画像を取得 → Cloudinaryへアップして公開URL化 →
Instagram Graph API でリールを投稿し、結果をスプレッドシートに書き戻す。

GitHub Actions の cron から定期実行される想定。
設定値はすべて環境変数(GitHub Secrets)から読み込む。
"""

import io
import json
import os
import sys
import time
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
IG_USER_ID = os.environ["IG_USER_ID"]
IG_ACCESS_TOKEN = os.environ["IG_ACCESS_TOKEN"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]
SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
# CLOUDINARY_URL 環境変数は cloudinary ライブラリが自動で読む

GRAPH = "https://graph.facebook.com/v21.0"
JST = ZoneInfo("Asia/Tokyo")

# スプレッドシートの列ヘッダー（1行目）と内部キーの対応
COL_SCHEDULED = "投稿予定時刻"
COL_VIDEO = "動画ファイル名"
COL_COVER = "カバー画像ファイル名"
COL_CAPTION = "キャプション"
COL_STATUS = "ステータス"
COL_URL = "投稿URL"
COL_ERROR = "エラー"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def log(msg):
    print(f"[{datetime.now(JST):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


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


def find_drive_file(drive, name):
    """ドライブの対象フォルダ内からファイル名で検索し file_id を返す。"""
    q = (
        f"'{DRIVE_FOLDER_ID}' in parents and name = '{name}' "
        f"and trashed = false"
    )
    res = drive.files().list(
        q=q,
        fields="files(id, name, mimeType)",
        pageSize=10,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = res.get("files", [])
    if not files:
        raise FileNotFoundError(f"ドライブにファイルが見つかりません: '{name}'")
    if len(files) > 1:
        log(f"  警告: '{name}' が複数見つかりました。先頭を使用します。")
    return files[0]["id"]


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


def create_reel_container(video_url, cover_url, caption):
    data = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "access_token": IG_ACCESS_TOKEN,
    }
    if cover_url:
        data["cover_url"] = cover_url
    r = requests.post(f"{GRAPH}/{IG_USER_ID}/media", data=data, timeout=120)
    body = r.json()
    if "id" not in body:
        raise RuntimeError(f"コンテナ作成に失敗: {body}")
    return body["id"]


def wait_until_finished(creation_id, max_wait_sec=600, interval=15):
    """動画処理の完了(FINISHED)を待つ。"""
    waited = 0
    while waited < max_wait_sec:
        r = requests.get(
            f"{GRAPH}/{creation_id}",
            params={"fields": "status_code,status", "access_token": IG_ACCESS_TOKEN},
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


def publish_container(creation_id):
    r = requests.post(
        f"{GRAPH}/{IG_USER_ID}/media_publish",
        data={"creation_id": creation_id, "access_token": IG_ACCESS_TOKEN},
        timeout=120,
    )
    body = r.json()
    if "id" not in body:
        raise RuntimeError(f"公開に失敗: {body}")
    return body["id"]


def get_permalink(media_id):
    r = requests.get(
        f"{GRAPH}/{media_id}",
        params={"fields": "permalink", "access_token": IG_ACCESS_TOKEN},
        timeout=60,
    )
    return r.json().get("permalink", "")


def main():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(SERVICE_ACCOUNT_JSON), scopes=SCOPES
    )
    gc = gspread.authorize(creds)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    ws = gc.open_by_key(SPREADSHEET_ID).sheet1
    rows = ws.get_all_records()  # 1行目をヘッダーとした辞書のリスト
    header = ws.row_values(1)
    col_idx = {name: i + 1 for i, name in enumerate(header)}  # 1始まりの列番号

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
            ws.update_cell(sheet_row, col_idx[COL_STATUS], "エラー")
            if COL_ERROR in col_idx:
                ws.update_cell(sheet_row, col_idx[COL_ERROR], str(e))
            continue

        if scheduled is None or scheduled > now:
            continue  # 時刻未到来 or 空欄

        video_name = str(row.get(COL_VIDEO, "")).strip()
        cover_name = str(row.get(COL_COVER, "")).strip()
        caption = str(row.get(COL_CAPTION, ""))

        log(f"行{sheet_row}: 投稿開始（予定 {scheduled:%Y-%m-%d %H:%M} / 動画 {video_name}）")
        uploaded = []  # Cloudinary後始末用
        try:
            if not video_name:
                raise ValueError("動画ファイル名が空です")

            # 1) ドライブから取得
            video_id = find_drive_file(drive, video_name)
            video_path = f"/tmp/{video_name}"
            download_drive_file(drive, video_id, video_path)
            log("  動画ダウンロード完了")

            cover_url = ""
            if cover_name:
                cover_id = find_drive_file(drive, cover_name)
                cover_path = f"/tmp/{cover_name}"
                download_drive_file(drive, cover_id, cover_path)
                cover_url, cpid, crt = upload_to_cloudinary(cover_path, "image")
                uploaded.append((cpid, crt))
                log("  カバー画像アップロード完了")

            # 2) Cloudinaryへ
            video_url, vpid, vrt = upload_to_cloudinary(video_path, "video")
            uploaded.append((vpid, vrt))
            log("  動画アップロード完了")

            # 3) Instagram投稿
            creation_id = create_reel_container(video_url, cover_url, caption)
            log(f"  コンテナ作成: {creation_id}")
            wait_until_finished(creation_id)
            media_id = publish_container(creation_id)
            permalink = get_permalink(media_id)
            log(f"  公開完了: {permalink}")

            # 4) シートに結果を記入
            ws.update_cell(sheet_row, col_idx[COL_STATUS], "投稿済")
            if COL_URL in col_idx:
                ws.update_cell(sheet_row, col_idx[COL_URL], permalink)
            if COL_ERROR in col_idx:
                ws.update_cell(sheet_row, col_idx[COL_ERROR], "")
            posted += 1

        except Exception as e:
            log(f"  失敗: {e}")
            ws.update_cell(sheet_row, col_idx[COL_STATUS], "エラー")
            if COL_ERROR in col_idx:
                ws.update_cell(sheet_row, col_idx[COL_ERROR], str(e)[:500])
        finally:
            for pid, rt in uploaded:
                delete_from_cloudinary(pid, rt)

    log(f"完了。今回の投稿数: {posted}")


if __name__ == "__main__":
    main()
