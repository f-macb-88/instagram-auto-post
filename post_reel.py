#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Instagram リール自動投稿スクリプト（複数アカウント対応）

スプレッドシートの「投稿予定時刻」が現在(JST)以前で、かつ「ステータス」が空欄の行を探し、
Googleドライブから動画・カバー画像を取得 → Cloudinaryへアップして公開URL化 →
Instagram Graph API でリールを投稿し、結果をスプレッドシートに書き戻す。
「媒体」が tiktok の行は、Cloudinary の動画URLを Buffer の API（createPost）に渡して TikTok へ投稿する。

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
# Buffer の API は GraphQL の1本だけ（https://developers.buffer.com/guides/getting-started.md）
BUFFER_API = "https://api.buffer.com"
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
    # Buffer の API キーはアカウント（Buffer のログイン）単位で1つ。全アカウント共通
    buffer_key = env.get("BUFFER_API_KEY", "")
    return {
        "love64": {
            "ig_user_id": env.get("IG_USER_ID", ""),
            "access_token": env.get("IG_ACCESS_TOKEN", ""),
            "drive_folder_id": love64_folder,
            "buffer_api_key": buffer_key,
            "buffer_channel_tiktok": env.get("BUFFER_CHANNEL_TIKTOK_LOVE64", ""),
        },
        "jobtype64": {
            "ig_user_id": env.get("IG_USER_ID_JOBTYPE64", ""),
            "access_token": env.get("IG_ACCESS_TOKEN_JOBTYPE64", ""),
            # 専用フォルダが未設定なら love64 と同じフォルダから素材を探す
            "drive_folder_id": env.get("DRIVE_FOLDER_ID_JOBTYPE64", "") or love64_folder,
            "buffer_api_key": buffer_key,
            "buffer_channel_tiktok": env.get("BUFFER_CHANNEL_TIKTOK_JOBTYPE64", ""),
        },
    }


def missing_tiktok_settings(cfg):
    """TikTok 投稿に必要な設定のうち、未設定のものの名前を返す（値は返さない）。"""
    missing = []
    if not cfg.get("buffer_api_key"):
        missing.append("BUFFER_API_KEY")
    if not cfg.get("buffer_channel_tiktok"):
        missing.append("BUFFER_CHANNEL_TIKTOK_*")
    if not cfg.get("drive_folder_id"):
        missing.append("DRIVE_FOLDER_ID")
    return missing


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
COL_BUFFER_ID = "BufferID"  # 任意の列（TikTok）。送信中の行を次回確認するために Buffer の投稿IDを書く
COL_AI = "AI生成"  # 任意の列（TikTok）。はい/true/1 などなら TikTok に「AI生成」と表示する

STATUS_SENDING = "送信中"  # TikTok：Buffer へ渡したが、公開の完了をまだ確認できていない

# TikTok（Buffer 経由）の上限。出典: developers.buffer.com/guides/character-limits.md と
# Buffer ヘルプ「Using TikTok with Buffer」（動画付きは2,200字・ハッシュタグは5個まで）
TIKTOK_CAPTION_MAX = 2200
TIKTOK_HASHTAG_MAX = 5
# 投稿直後に公開完了を待つ時間。これを過ぎたら「送信中」にして次回の実行で確認する
BUFFER_WAIT_SEC = 600
BUFFER_POLL_SEC = 20
# 予定時刻からこれだけ過ぎても送信中のままなら、あきらめて「エラー」にする
BUFFER_GIVE_UP_SEC = 24 * 60 * 60

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


def utf16_len(text):
    """Buffer の文字数の数え方（UTF-16 の単位）で長さを返す。"""
    return len(str(text).encode("utf-16-le")) // 2


def count_hashtags(text):
    """キャプション中のハッシュタグ（# か ＃ で始まる語）の数。"""
    return len(re.findall(r"[#＃][^\s#＃]+", str(text)))


def is_truthy_cell(value):
    """「AI生成」列などの はい/いいえ を判定する。空欄は False。"""
    return _normalize_cell(value) in ("はい", "true", "yes", "1", "○", "◯", "y", "on", "ai")


def check_row(row, accounts):
    """投稿時刻を迎えた行について、投稿してよいかを判定する（ネットワークは使わない）。

    戻り値:
      ("instagram", アカウント名, 設定) … Instagram に投稿する
      ("tiktok", アカウント名, 設定)    … Buffer 経由で TikTok に投稿する
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
        return check_tiktok_row(row, account, accounts[account])

    cfg = accounts[account]
    if not (cfg["ig_user_id"] and cfg["access_token"] and cfg["drive_folder_id"]):
        return ("error", account, f"未設定のアカウント（{account}）")

    if account == "jobtype64" and contains_mbti(row.get(COL_CAPTION, "")):
        return ("error", account, "ジョブタイプ64のキャプションに「MBTI」が含まれています（使用禁止）")

    return ("instagram", account, cfg)


def check_tiktok_row(row, account, cfg):
    """TikTok（Buffer 経由）の行の事前チェック。投稿してよければ ("tiktok", アカウント名, 設定)。"""
    missing = missing_tiktok_settings(cfg)
    if missing:
        return ("error", account, f"未設定（TikTok {account}）：{'・'.join(missing)}")

    caption = str(row.get(COL_CAPTION, ""))
    if account == "jobtype64" and contains_mbti(caption):
        return ("error", account, "ジョブタイプ64のキャプションに「MBTI」が含まれています（使用禁止）")
    if utf16_len(caption) > TIKTOK_CAPTION_MAX:
        return ("error", account, f"TikTok のキャプションが長すぎます（{utf16_len(caption)}字・上限{TIKTOK_CAPTION_MAX}字）")
    tags = count_hashtags(caption)
    if tags > TIKTOK_HASHTAG_MAX:
        return ("error", account, f"TikTok のハッシュタグが多すぎます（{tags}個・上限{TIKTOK_HASHTAG_MAX}個）")

    return ("tiktok", account, cfg)


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


def upload_to_cloudinary(path, resource_type, folder="ig_reels"):
    """Cloudinary にアップロードして公開URLと public_id を返す。"""
    if resource_type == "video":
        result = cloudinary.uploader.upload_large(
            path, resource_type="video", folder=folder
        )
    else:
        result = cloudinary.uploader.upload(
            path, resource_type="image", folder=folder
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
    for k in ("IG_ACCESS_TOKEN", "IG_ACCESS_TOKEN_JOBTYPE64", "CLOUDINARY_URL", "BUFFER_API_KEY"):
        v = os.environ.get(k)
        if v:
            text = text.replace(v, "***")
    text = re.sub(r"(Bearer)\s+[^\s'\",]+", r"\1 ***", text)
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


# ---- TikTok（Buffer 経由）------------------------------------------------------
# 仕様の出典（2026-11-06 確認）
#   認証・エンドポイント: https://developers.buffer.com/guides/authentication.md
#   投稿の作成・状態:     https://developers.buffer.com/guides/posts-and-scheduling.md
#   動画の渡し方:         https://developers.buffer.com/guides/hosting-media.md
#   エラーの返り方:       https://developers.buffer.com/guides/error-handling.md

BUFFER_CREATE_POST = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess {
      post { id status externalLink }
    }
    ... on MutationError {
      message
    }
  }
}
"""

BUFFER_GET_POST = """
query GetPost($input: PostInput!) {
  post(input: $input) {
    id
    status
    externalLink
    error { message }
  }
}
"""


class BufferNotFound(RuntimeError):
    """Buffer に投稿が見つからない（Buffer の画面で消された等）。"""


def buffer_graphql(api_key, query, variables=None):
    """Buffer の GraphQL を1回呼んで data を返す。

    API キーは Authorization ヘッダーだけで送り、URL には入れない。
    GraphQL は失敗でも HTTP 200 を返すので、本文の errors を必ず見る。
    """
    r = requests.post(
        BUFFER_API,
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=60,
    )
    if r.status_code == 429:
        raise RuntimeError(
            f"Buffer API の回数制限に達しました（{r.headers.get('Retry-After', '?')}秒後に再試行可）"
        )
    try:
        body = r.json()
    except ValueError:
        raise RuntimeError(f"Buffer API の応答を読めません（HTTP {r.status_code}）")
    errors = body.get("errors") or []
    if errors:
        err = errors[0] or {}
        code = (err.get("extensions") or {}).get("code", "")
        msg = f"Buffer API エラー（{code or 'HTTP ' + str(r.status_code)}）: {err.get('message', '')}"
        if code == "NOT_FOUND":
            raise BufferNotFound(msg)
        raise RuntimeError(msg)
    if r.status_code >= 400:
        raise RuntimeError(f"Buffer API エラー（HTTP {r.status_code}）")
    return body.get("data") or {}


def buffer_create_tiktok_post(cfg, video_url, caption, ai_generated):
    """Buffer に「今すぐ投稿」で TikTok の動画投稿を作り、post（id・status・externalLink）を返す。"""
    post_input = {
        "text": caption,
        "channelId": cfg["buffer_channel_tiktok"],
        "schedulingType": "automatic",  # Buffer が自動で公開する（通知して手で出す方式ではない）
        "mode": "shareNow",
        # サムネイル指定（thumbnailOffset）は無料プランに無い「カスタム動画カバー」なので付けない
        "assets": [{"video": {"url": video_url}}],
    }
    if ai_generated:
        post_input["metadata"] = {"tiktok": {"isAiGenerated": True}}
    data = buffer_graphql(cfg["buffer_api_key"], BUFFER_CREATE_POST, {"input": post_input})
    res = data.get("createPost") or {}
    post = res.get("post")
    if not post or not post.get("id"):
        raise RuntimeError(f"Buffer の投稿作成に失敗: {res.get('message') or res}")
    return post


def buffer_get_post(api_key, post_id):
    """Buffer の投稿の状態（status・externalLink・error）を返す。"""
    data = buffer_graphql(api_key, BUFFER_GET_POST, {"input": {"id": post_id}})
    post = data.get("post")
    if not post:
        raise BufferNotFound(f"Buffer に投稿が見つかりません（{post_id}）")
    return post


def buffer_error_message(post):
    msg = ((post.get("error") or {}).get("message")) or "理由は Buffer の画面で確認してください"
    return f"Buffer での公開に失敗: {msg}"


def cloudinary_tag(post_id):
    """送信中の動画を、次回の実行で消せるように Buffer の投稿IDで目印を付ける。"""
    return f"buffer_{post_id}"


def tag_cloudinary_video(public_id, post_id):
    try:
        cloudinary.uploader.add_tag(cloudinary_tag(post_id), [public_id], resource_type="video")
    except Exception as e:
        log(f"  Cloudinaryの目印付けに失敗（動画が残る可能性あり）: {redact(e)}")


def delete_cloudinary_by_tag(post_id):
    """送信中だった動画を、目印（Buffer の投稿ID）で Cloudinary から消す。"""
    try:
        import cloudinary.api  # Admin API。使うときだけ読み込む

        cloudinary.api.delete_resources_by_tag(cloudinary_tag(post_id), resource_type="video")
    except Exception as e:
        log(f"  Cloudinary削除に失敗（無視）: {redact(e)}")


def wait_buffer_post(api_key, post_id, max_wait_sec=BUFFER_WAIT_SEC, interval=BUFFER_POLL_SEC):
    """公開完了（sent）か失敗（error）まで待つ。時間切れ・確認できないときは None。"""
    waited = 0
    while waited < max_wait_sec:
        time.sleep(interval)
        waited += interval
        try:
            post = buffer_get_post(api_key, post_id)
        except Exception as e:
            log(f"  Buffer の状態確認に失敗（続けて待つ）: {redact(e)}")
            continue
        status = post.get("status")
        if status in ("sent", "error"):
            return post
        log(f"  Buffer 送信中... ({status}) {waited}s")
    return None


def post_tiktok_video(drive, cfg, video_name, caption, ai_generated):
    """1行分の動画を Buffer 経由で TikTok に投稿する。

    戻り値: {"state": "sent" | "sending", "post_id": ..., "url": ...}
    - 公開が終わった（sent）ら Cloudinary の動画を消す
    - 待ち時間内に終わらなければ "sending"。Buffer が公開時に動画を取りに来るので、動画は消さずに残す
    - Buffer 側で失敗（error）・作成前の失敗は例外（Cloudinary の動画は消す）
    """
    folder_id = cfg["drive_folder_id"]
    video_id = find_drive_file(drive, folder_id, video_name)
    video_path = safe_tmp_path(video_name)
    download_drive_file(drive, video_id, video_path)
    log("  動画ダウンロード完了")

    video_url, public_id, _ = upload_to_cloudinary(video_path, "video", folder="tiktok")
    log("  動画アップロード完了")

    try:
        post = buffer_create_tiktok_post(cfg, video_url, caption, ai_generated)
    except Exception:
        delete_from_cloudinary(public_id, "video")  # Buffer に渡っていないので消してよい
        raise
    post_id = post["id"]
    log(f"  Buffer 投稿作成: {post_id}（{post.get('status')}）")

    if post.get("status") not in ("sent", "error"):
        post = wait_buffer_post(cfg["buffer_api_key"], post_id) or post

    status = post.get("status")
    if status == "sent":
        delete_from_cloudinary(public_id, "video")
        return {"state": "sent", "post_id": post_id, "url": post.get("externalLink") or ""}
    if status == "error":
        delete_from_cloudinary(public_id, "video")
        raise RuntimeError(buffer_error_message(post))

    # まだ公開されていない。次回の実行で消せるように目印を付けて残す
    tag_cloudinary_video(public_id, post_id)
    return {"state": "sending", "post_id": post_id, "url": ""}


def write_tiktok_result(ws, col_idx, sheet_row, account, video_name, result):
    """TikTok の結果（投稿済／送信中）をシートに書く。投稿済なら Slack にも知らせる。"""
    if result["state"] == "sent":
        ws.update_cell(sheet_row, col_idx[COL_STATUS], "投稿済")
        if COL_URL in col_idx:
            ws.update_cell(sheet_row, col_idx[COL_URL], result["url"])
        if COL_ERROR in col_idx:
            ws.update_cell(sheet_row, col_idx[COL_ERROR], "")
        notify_slack(
            f"✅ TikTok（{account}）に動画を投稿しました\n"
            f"動画: {video_name}\n"
            f"{result['url'] or '（URLは Buffer の画面で確認）'}"
        )
        return

    ws.update_cell(sheet_row, col_idx[COL_STATUS], STATUS_SENDING)
    if COL_BUFFER_ID in col_idx:
        # 先頭の ' は、数字だけのIDをシートが数値に変えないようにするため（シート上は表示されない）
        ws.update_cell(sheet_row, col_idx[COL_BUFFER_ID], "'" + result["post_id"])
        note = ""
    else:
        note = "「BufferID」列が無いため、次回の自動確認ができません。Buffer の画面で公開を確認してください"
    if COL_ERROR in col_idx:
        ws.update_cell(sheet_row, col_idx[COL_ERROR], note)


def check_sending_row(ws, col_idx, sheet_row, row, accounts, now):
    """前回「送信中」で終えた TikTok の行について、Buffer での公開状態を確かめて書き戻す。"""
    if resolve_platform(row) != "tiktok":
        return  # 手で「送信中」と書いた Instagram の行などは触らない
    account = resolve_account(row)
    if COL_BUFFER_ID not in col_idx:
        log(f"行{sheet_row}: [{account}] 送信中（BufferID 列が無いため確認できません）")
        return
    post_id = str(ws.cell(sheet_row, col_idx[COL_BUFFER_ID]).value or "").strip().lstrip("'")
    if not post_id:
        log(f"行{sheet_row}: [{account}] 送信中（BufferID が空のため確認できません）")
        return
    cfg = accounts.get(account) or {}
    if not cfg.get("buffer_api_key"):
        log(f"行{sheet_row}: [{account}] 送信中のまま（BUFFER_API_KEY 未設定）")
        return

    try:
        post = buffer_get_post(cfg["buffer_api_key"], post_id)
    except BufferNotFound as e:
        log(f"行{sheet_row}: [{account}] {redact(e)}")
        mark_error(ws, col_idx, sheet_row, f"{e}（Buffer の画面で消された可能性があります）")
        delete_cloudinary_by_tag(post_id)
        return
    except Exception as e:
        log(f"行{sheet_row}: [{account}] Buffer の状態確認に失敗（次回また確認）: {redact(e)}")
        return

    status = post.get("status")
    video_name = str(row.get(COL_VIDEO, "")).strip()
    if status == "sent":
        log(f"行{sheet_row}: [{account}] TikTok 公開完了: {post.get('externalLink') or ''}")
        write_tiktok_result(
            ws, col_idx, sheet_row, account, video_name,
            {"state": "sent", "post_id": post_id, "url": post.get("externalLink") or ""},
        )
        delete_cloudinary_by_tag(post_id)
        return
    if status == "error":
        log(f"行{sheet_row}: [{account}] {buffer_error_message(post)}")
        mark_error(ws, col_idx, sheet_row, buffer_error_message(post))
        delete_cloudinary_by_tag(post_id)
        return
    if status in ("draft", "needs_approval"):
        mark_error(
            ws, col_idx, sheet_row,
            f"Buffer 側で{'下書き' if status == 'draft' else '承認待ち'}になっています。"
            "Buffer の画面で確認してください（動画は Cloudinary に残しています）",
        )
        return

    # sending / scheduled：まだ公開待ち。予定時刻から長く過ぎていたらあきらめる
    try:
        scheduled = parse_scheduled_time(row.get(COL_SCHEDULED, ""))
    except ValueError:
        scheduled = None
    if scheduled and (now - scheduled).total_seconds() > BUFFER_GIVE_UP_SEC:
        mark_error(
            ws, col_idx, sheet_row,
            f"予定時刻から24時間たっても Buffer で送信が終わりません（{status}）。Buffer の画面で確認してください",
        )
        return
    log(f"行{sheet_row}: [{account}] まだ送信中（{status}）")


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
        missing = missing_tiktok_settings(cfg)
        log(f"  TikTok（Buffer）{name}: {'設定あり' if not missing else '未設定（' + '・'.join(missing) + '）'}")

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

        status = str(row.get(COL_STATUS, "")).strip()
        if status == STATUS_SENDING:
            check_sending_row(ws, col_idx, sheet_row, row, accounts, now)
            continue
        if status:
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

        if action == "tiktok":
            log(
                f"行{sheet_row}: [{account}] TikTok 投稿開始"
                f"（予定 {scheduled:%Y-%m-%d %H:%M} / 動画 {video_name}）"
            )
            try:
                if not video_name:
                    raise ValueError("動画ファイル名が空です")
                result = post_tiktok_video(
                    drive, cfg, video_name, caption, is_truthy_cell(row.get(COL_AI, ""))
                )
                log(f"  {'公開完了: ' + result['url'] if result['state'] == 'sent' else '送信中（次回の実行で確認）'}")
                write_tiktok_result(ws, col_idx, sheet_row, account, video_name, result)
                if result["state"] == "sent":
                    posted += 1
            except Exception as e:
                log(f"  失敗: {redact(e)}")
                mark_error(ws, col_idx, sheet_row, e)
            continue

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
