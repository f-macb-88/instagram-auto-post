#!/usr/bin/env python3
"""ジョブタイプ64のオーガニック投稿（TikTok・インスタ @jobtype64）の実績を毎日スプレッドシートに記録する。

1行＝1投稿×1日のスナップショット（時系列）。同じ日に2回動いたら、その日の行を最新の値で上書きする（重複しない）。

  TikTok  → タブ「TikTok実績」
    ・再生数／いいね／コメント／シェア／保存 … 公開ページ（yt-dlp・ログイン不要）
    ・平均視聴時間／合計視聴時間／リーチ   … Buffer API の投稿指標（Buffer 経由で出した投稿だけ・取得は1日1回・最大24時間遅れ）
  インスタ → タブ「インスタ実績」
    ・Instagram Graph API のメディアインサイト。@jobtype64 のリールを全部（手動で出した分も）記録する
    ・鍵（IG_USER_ID_JOBTYPE64・IG_ACCESS_TOKEN_JOBTYPE64）が無いときは「未設定のため保留」で何もしない

仕様の出典（2026-11-06 確認）
  Instagram メディアインサイト: https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/insights
  Instagram 変更履歴（plays・video_views・impressions の廃止、views の追加）: https://developers.facebook.com/docs/instagram-platform/changelog
  Graph API の版と期限: https://developers.facebook.com/docs/graph-api/changelog/versions
  Buffer 投稿指標: https://developers.buffer.com/guides/post-metrics.md
  Buffer 指標の種類と単位（PostMetricType）: https://developers.buffer.com/reference.md

「投稿管理」タブは読むだけ（動画ファイル名と URL の照合に使う）。書き込まない。

使い方
  python metrics_jt64.py                    # 取得してシートに書く
  python metrics_jt64.py --dry-run          # 取得して表示するだけ（シートに書かない）
  python metrics_jt64.py --only tiktok      # TikTok だけ
  python metrics_jt64.py --tiktok-test-url URL   # その公開動画1本だけ取得して表示（シートは開かない）

環境変数
  SPREADSHEET_ID_JOBTYPE64            ジョブタイプ64のシートID（必須）
  GOOGLE_SERVICE_ACCOUNT_JSON         サービスアカウント JSON。無ければ gspread.oauth()（Mac で動かすとき）
  BUFFER_API_KEY / BUFFER_CHANNEL_TIKTOK_JOBTYPE64   無ければ Buffer の指標は空欄
  IG_USER_ID_JOBTYPE64 / IG_ACCESS_TOKEN_JOBTYPE64   無ければインスタは保留
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

JST = timezone(timedelta(hours=9))

GRAPH = "https://graph.facebook.com/v25.0"  # v25.0 は 2028-07-29 まで使える
BUFFER_API = "https://api.buffer.com"

SOURCE_TAB = "投稿管理"  # 読むだけ
TAB_TIKTOK = "TikTok実績"
TAB_IG = "インスタ実績"

# 「投稿管理」タブの見出し（norm_header 後の名前）
SRC_VIDEO = "動画ファイル名"
SRC_IG_URL = "インスタ_投稿URL"
SRC_TT_URL = "TikTok_投稿URL"
SRC_BUFFER_ID = "BufferID"

# ---- 記録タブの見出し（この並びで作る。既にあるタブは見出し名で列を探す）
TT_HEADER = [
    "取得日", "取得時刻", "動画ファイル名", "投稿URL", "動画ID", "投稿日時", "経過日数",
    "再生数", "いいね", "コメント", "シェア", "保存",
    "平均視聴時間_秒", "合計視聴時間_分", "リーチ", "Buffer指標の更新日時",
    "取得元", "取得状況",
]
IG_HEADER = [
    "取得日", "取得時刻", "動画ファイル名", "投稿URL", "メディアID", "投稿日時", "経過日数",
    "再生数", "リーチ", "いいね", "コメント", "保存", "シェア", "リポスト", "合計インタラクション",
    "平均視聴時間_秒", "合計視聴時間_秒", "取得状況",
]
KEY_COLS = ("取得日", "投稿URL")  # 同じ日・同じ投稿の行は上書きする
DATE_COLS = {"取得日": "yyyy-mm-dd", "取得時刻": "yyyy-mm-dd hh:mm", "投稿日時": "yyyy-mm-dd hh:mm",
             "Buffer指標の更新日時": "yyyy-mm-dd hh:mm"}

# Instagram のリールで取る指標（2026-11 時点の現行名。plays・video_views・impressions は廃止済み）
IG_METRICS = [
    "views", "reach", "likes", "comments", "saved", "shares", "reposts",
    "total_interactions", "ig_reels_avg_watch_time", "ig_reels_video_view_total_time",
]
IG_METRIC_TO_COL = {
    "views": "再生数", "reach": "リーチ", "likes": "いいね", "comments": "コメント",
    "saved": "保存", "shares": "シェア", "reposts": "リポスト",
    "total_interactions": "合計インタラクション",
}
# ★視聴時間の単位は公式ドキュメントに書かれていない。実データはミリ秒で返る（開発者の実測で広く知られている）ため秒に直す。
#   初回の値をインスタのアプリの「平均視聴時間」と見比べて、ずれていたらここを直す
IG_WATCH_TIME_DIVISOR = 1000.0

# Buffer の指標（PostMetricType）→ TikTok実績の列。単位は Buffer の型定義どおり
#   averageTimeWatched: 秒 / totalTimeWatched: 分
BUFFER_METRIC_TO_COL = {
    "averageTimeWatched": "平均視聴時間_秒",
    "totalTimeWatched": "合計視聴時間_分",
    "reach": "リーチ",
}
# 公開ページで取れなかったときだけ Buffer の値で埋める
BUFFER_FALLBACK_TO_COL = {
    "views": "再生数", "likes": "いいね", "reactions": "いいね", "comments": "コメント",
    "shares": "シェア", "saves": "保存",
}

SECRET_ENV = ("IG_ACCESS_TOKEN_JOBTYPE64", "BUFFER_API_KEY", "GOOGLE_SERVICE_ACCOUNT_JSON")


def log(msg):
    print(f"[{datetime.now(JST):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def redact(text):
    """エラー文に鍵の値が混ざっても出さない（公開リポジトリの Actions ログは誰でも見られる）"""
    text = str(text)
    for k in SECRET_ENV:
        v = os.environ.get(k, "")
        if v and len(v) > 8:
            text = text.replace(v, "***")
    return re.sub(r"(access_token=)[^&\s]+", r"\1***", text)


def norm_header(name):
    """見出しの表記ゆれをそろえる：前後の空白を除き、改行・空白・全角＿の並びを「_」1つにする（post_reel.py と同じ規則）"""
    return re.sub(r"[\s_＿]+", "_", str(name or "").strip())


# ---- URL から ID を取り出す ----------------------------------------------------

def ig_shortcode(url):
    m = re.search(r"instagram\.com/(?:[^/]+/)?(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", str(url or ""))
    return m.group(1) if m else ""


def tiktok_video_id(url):
    m = re.search(r"tiktok\.com/.*?/(?:video|photo)/(\d+)", str(url or ""))
    return m.group(1) if m else ""


def canonical_ig_url(url):
    code = ig_shortcode(url)
    return f"https://www.instagram.com/reel/{code}/" if code else str(url or "").strip()


# ---- 日付 ---------------------------------------------------------------------

def fmt_dt(dt):
    return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M") if dt else ""


def parse_iso(s):
    """'2026-11-06T10:30:00+0000' / '...Z' / '...+00:00' を datetime に"""
    if not s:
        return None
    s = str(s).strip().replace("Z", "+00:00")
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def elapsed_days(posted, now):
    if not posted:
        return ""
    return round((now - posted).total_seconds() / 86400, 1)


def normalize_date_key(value):
    """シートから読み戻した取得日（'2026-11-07' / '2026/11/7' / シリアル値）を 'YYYY-MM-DD' にそろえる"""
    s = str(value or "").strip()
    if not s:
        return ""
    if re.fullmatch(r"\d+(\.\d+)?", s):  # シリアル値
        return (datetime(1899, 12, 30) + timedelta(days=float(s))).strftime("%Y-%m-%d")
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return s


def row_key(date_value, url):
    return (normalize_date_key(date_value), str(url or "").strip())


# ---- 投稿管理タブ（読むだけ） ---------------------------------------------------

def read_source_rows(values):
    """投稿管理タブの全セル（get_all_values の結果）から、照合に使う列だけを取り出す"""
    if not values:
        return []
    header = [norm_header(h) for h in values[0]]
    idx = {h: i for i, h in enumerate(header)}

    def get(r, col):
        i = idx.get(col)
        return str(r[i]).strip() if i is not None and i < len(r) else ""

    out = []
    for r in values[1:]:
        video = get(r, SRC_VIDEO)
        if not video:
            continue
        out.append({
            "video": video,
            "ig_url": get(r, SRC_IG_URL),
            "tt_url": get(r, SRC_TT_URL),
            "buffer_id": get(r, SRC_BUFFER_ID).lstrip("'"),
        })
    return out


# ---- Instagram ----------------------------------------------------------------

def graph_get(path, token, params=None):
    """Graph API を GET。トークンは Authorization ヘッダーで送り、URL に入れない"""
    url = path if path.startswith("http") else f"{GRAPH}/{path}"
    r = requests.get(url, params=params or {}, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    try:
        body = r.json()
    except ValueError:
        raise RuntimeError(f"Graph API の応答を読めません（HTTP {r.status_code}）")
    if r.status_code >= 400 or "error" in body:
        err = body.get("error") or {}
        raise RuntimeError(redact(f"Graph API エラー（HTTP {r.status_code} code {err.get('code')}）: {err.get('message', '')}"))
    return body


def list_ig_reels(ig_user_id, token, max_pages=10):
    """@jobtype64 のメディア一覧からリールだけ返す（手動投稿分も含む）"""
    fields = "id,permalink,shortcode,timestamp,media_type,media_product_type"
    body = graph_get(f"{ig_user_id}/media", token, {"fields": fields, "limit": 50})
    out = []
    for _ in range(max_pages):
        for m in body.get("data") or []:
            if m.get("media_product_type") == "REELS":
                out.append(m)
        nxt = (body.get("paging") or {}).get("next")
        if not nxt:
            break
        body = graph_get(nxt, token)
    return out


def parse_insights(body):
    """インサイトの応答 {"data":[{"name":..,"values":[{"value":..}]} or {"total_value":{"value":..}}]} を {名前: 値} に"""
    out = {}
    for item in body.get("data") or []:
        name = item.get("name")
        val = None
        if isinstance(item.get("total_value"), dict):
            val = item["total_value"].get("value")
        if val is None and item.get("values"):
            val = (item["values"][-1] or {}).get("value")
        if name and isinstance(val, (int, float)):
            out[name] = val
    return out


def fetch_ig_insights(media_id, token, metrics=IG_METRICS):
    """まとめて取り、どれか1つが使えずエラーになったら1つずつ取り直す（使えない指標は飛ばす）。
    戻り値: (値の dict, 取れなかった指標のリスト)"""
    try:
        return parse_insights(graph_get(f"{media_id}/insights", token, {"metric": ",".join(metrics)})), []
    except RuntimeError:
        pass
    vals, failed = {}, []
    for m in metrics:
        try:
            vals.update(parse_insights(graph_get(f"{media_id}/insights", token, {"metric": m})))
        except RuntimeError:
            failed.append(m)
    return vals, failed


def build_ig_row(media, insights, failed, video_name, now):
    posted = parse_iso(media.get("timestamp"))
    row = {
        "取得日": now.strftime("%Y-%m-%d"),
        "取得時刻": fmt_dt(now),
        "動画ファイル名": video_name,
        "投稿URL": canonical_ig_url(media.get("permalink")),
        "メディアID": str(media.get("id", "")),
        "投稿日時": fmt_dt(posted),
        "経過日数": elapsed_days(posted, now),
    }
    for metric, col in IG_METRIC_TO_COL.items():
        if metric in insights:
            row[col] = insights[metric]
    if "ig_reels_avg_watch_time" in insights:
        row["平均視聴時間_秒"] = round(insights["ig_reels_avg_watch_time"] / IG_WATCH_TIME_DIVISOR, 2)
    if "ig_reels_video_view_total_time" in insights:
        row["合計視聴時間_秒"] = round(insights["ig_reels_video_view_total_time"] / IG_WATCH_TIME_DIVISOR, 1)
    row["取得状況"] = "OK" if not failed else "一部取得できず: " + ",".join(failed)
    return row


def collect_instagram(source_rows, now, env=os.environ):
    ig_user_id = env.get("IG_USER_ID_JOBTYPE64", "")
    token = env.get("IG_ACCESS_TOKEN_JOBTYPE64", "")
    if not ig_user_id or not token:
        log("インスタ: IG_USER_ID_JOBTYPE64・IG_ACCESS_TOKEN_JOBTYPE64 が未設定のため保留（何もしません）")
        return None
    by_code = {ig_shortcode(r["ig_url"]): r["video"] for r in source_rows if ig_shortcode(r["ig_url"])}
    reels = list_ig_reels(ig_user_id, token)
    log(f"インスタ: リール {len(reels)} 本")
    rows = []
    for m in reels:
        code = m.get("shortcode") or ig_shortcode(m.get("permalink"))
        try:
            ins, failed = fetch_ig_insights(m["id"], token)
        except Exception as e:  # 1本の失敗で全体を止めない
            ins, failed = {}, [redact(e)[:80]]
        rows.append(build_ig_row(m, ins, failed, by_code.get(code, ""), now))
    return rows


# ---- Buffer（TikTok の視聴時間など） ---------------------------------------------

BUFFER_CHANNEL_Q = "query($input: ChannelInput!){ channel(input:$input){ id organizationId service } }"
BUFFER_POSTS_Q = """
query($first:Int,$after:String,$input:PostsInput!){
  posts(first:$first, after:$after, input:$input){
    edges{ node{ id status sentAt externalLink %s } }
    pageInfo{ hasNextPage endCursor }
  }
}
"""
BUFFER_METRICS_FIELDS = "metrics{ type value unit } metricsUpdatedAt"


def buffer_graphql(api_key, query, variables=None):
    r = requests.post(
        BUFFER_API,
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=60,
    )
    try:
        body = r.json()
    except ValueError:
        raise RuntimeError(f"Buffer API の応答を読めません（HTTP {r.status_code}）")
    if body.get("errors"):
        err = body["errors"][0] or {}
        code = (err.get("extensions") or {}).get("code", "")
        raise RuntimeError(redact(f"Buffer API エラー（{code or 'HTTP ' + str(r.status_code)}）: {err.get('message', '')}"))
    if r.status_code >= 400:
        raise RuntimeError(f"Buffer API エラー（HTTP {r.status_code}）")
    return body.get("data") or {}


def fetch_buffer_posts(api_key, channel_id, max_pages=10):
    """チャンネルの送信済み投稿を返す。指標を読む権限が無いときは指標なしで取り直す。
    戻り値: (投稿のリスト, 指標を読めたか, メモ)"""
    ch = buffer_graphql(api_key, BUFFER_CHANNEL_Q, {"input": {"id": channel_id}}).get("channel") or {}
    org = ch.get("organizationId")
    if not org:
        raise RuntimeError("Buffer のチャンネルが見つかりません")

    def run(with_metrics):
        q = BUFFER_POSTS_Q % (BUFFER_METRICS_FIELDS if with_metrics else "")
        posts, after = [], None
        for _ in range(max_pages):
            data = buffer_graphql(api_key, q, {
                "first": 50, "after": after,
                "input": {"organizationId": org, "filter": {"channelIds": [channel_id], "status": ["sent"]}},
            })
            res = data.get("posts") or {}
            posts += [e["node"] for e in res.get("edges") or [] if e.get("node")]
            page = res.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            after = page.get("endCursor")
        return posts

    try:
        return run(True), True, ""
    except RuntimeError as e:
        note = f"Buffer の指標を読めません（{e}）"
        return run(False), False, note


def buffer_metrics_dict(post):
    return {m.get("type"): m.get("value") for m in post.get("metrics") or [] if isinstance(m.get("value"), (int, float))}


# ---- TikTok（公開ページ） --------------------------------------------------------

def fetch_tiktok_public(url, retries=2):
    """yt-dlp で公開動画のメタデータを取る（ログイン不要・動画は落とさない）"""
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    last = None
    for i in range(retries + 1):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"公開ページを読めません: {str(last)[:150]}")


def build_tiktok_row(url, video_name, info, buffer_post, buffer_note, now):
    """公開ページの値（info）と Buffer の投稿（buffer_post）から1行を作る。どちらかが None でもよい"""
    info = info or {}
    bm = buffer_metrics_dict(buffer_post) if buffer_post else {}
    posted = None
    if info.get("timestamp"):
        posted = datetime.fromtimestamp(info["timestamp"], tz=JST)
    elif buffer_post:
        posted = parse_iso(buffer_post.get("sentAt"))
    vid = str(info.get("id") or tiktok_video_id(url))
    row = {
        "取得日": now.strftime("%Y-%m-%d"),
        "取得時刻": fmt_dt(now),
        "動画ファイル名": video_name,
        "投稿URL": url,
        "動画ID": vid,
        "投稿日時": fmt_dt(posted),
        "経過日数": elapsed_days(posted, now),
    }
    public_map = {"view_count": "再生数", "like_count": "いいね", "comment_count": "コメント",
                  "repost_count": "シェア", "save_count": "保存"}
    for k, col in public_map.items():
        if isinstance(info.get(k), (int, float)):
            row[col] = info[k]
    sources = ["公開ページ"] if info else []
    used_buffer = False
    for t, col in BUFFER_METRIC_TO_COL.items():
        if t in bm:
            row[col] = bm[t]
            used_buffer = True
    if not info:
        for t, col in BUFFER_FALLBACK_TO_COL.items():
            if t in bm and col not in row:
                row[col] = bm[t]
                used_buffer = True
    if used_buffer:
        sources.append("Buffer")
        row["Buffer指標の更新日時"] = fmt_dt(parse_iso(buffer_post.get("metricsUpdatedAt")))
    row["取得元"] = "+".join(sources)
    notes = []
    if not info:
        notes.append("公開ページ取得失敗")
    if buffer_note:
        notes.append(buffer_note)
    elif buffer_post and not bm:
        notes.append("Buffer指標まだ無し（最大24時間遅れ）")
    row["取得状況"] = "OK" if not notes else " / ".join(notes)[:300]
    return row


def merge_tiktok_targets(source_rows, buffer_posts):
    """シートの TikTok_投稿URL と Buffer の送信済み投稿を、動画IDでまとめる。
    戻り値: [{url, video, buffer_post}]"""
    by_buffer_id = {r["buffer_id"]: r["video"] for r in source_rows if r["buffer_id"]}
    targets = {}
    for r in source_rows:
        if r["tt_url"].startswith("http"):
            key = tiktok_video_id(r["tt_url"]) or r["tt_url"]  # 短縮URLなど動画IDが読めないものは URL で
            targets[key] = {"url": r["tt_url"], "video": r["video"], "buffer_post": None}
    for p in buffer_posts or []:
        url = p.get("externalLink") or ""
        vid = tiktok_video_id(url)
        if not vid:
            continue
        t = targets.setdefault(vid, {"url": url, "video": "", "buffer_post": None})
        t["buffer_post"] = p
        if not t["video"]:
            t["video"] = by_buffer_id.get(p.get("id"), "")
    return list(targets.values())


def collect_tiktok(source_rows, now, env=os.environ):
    api_key = env.get("BUFFER_API_KEY", "")
    channel = env.get("BUFFER_CHANNEL_TIKTOK_JOBTYPE64", "")
    buffer_posts, buffer_note = [], ""
    if api_key and channel:
        try:
            buffer_posts, ok, buffer_note = fetch_buffer_posts(api_key, channel)
            log(f"TikTok: Buffer の送信済み投稿 {len(buffer_posts)} 本（指標を読めた: {ok}）")
        except Exception as e:
            buffer_note = f"Buffer に接続できません（{redact(e)[:120]}）"
            log(f"TikTok: {buffer_note}")
    else:
        buffer_note = "Buffer 未設定"
        log("TikTok: BUFFER_API_KEY か BUFFER_CHANNEL_TIKTOK_JOBTYPE64 が未設定（視聴時間は空欄）")

    targets = merge_tiktok_targets(source_rows, buffer_posts)
    log(f"TikTok: 対象 {len(targets)} 本")
    rows = []
    for t in targets:
        info = None
        try:
            info = fetch_tiktok_public(t["url"])
        except Exception as e:
            log(f"TikTok: {t['url']} {e}")
        rows.append(build_tiktok_row(t["url"], t["video"], info, t["buffer_post"], buffer_note, now))
    return rows


# ---- シートへの書き込み ------------------------------------------------------------

def plan_upsert(existing_values, header, rows):
    """既存のセル（get_all_values）と新しい行から、上書きする行と追記する行を決める。
    戻り値: (使う見出し, {シートの行番号: 値のリスト}, [追記する値のリスト], 見出しを書き直すか)"""
    cur_header = [norm_header(h) for h in (existing_values[0] if existing_values else [])]
    rewrite_header = not any(cur_header)
    use_header = list(header) if rewrite_header else list(cur_header)
    for h in header:  # 足りない見出しは右端に足す（既存の並びは変えない）
        if h not in use_header:
            use_header.append(h)
            rewrite_header = True
    idx = {h: i for i, h in enumerate(use_header)}
    k0, k1 = idx[KEY_COLS[0]], idx[KEY_COLS[1]]
    existing = {}
    for n, r in enumerate(existing_values[1:] if existing_values else [], start=2):
        r = list(r) + [""] * (len(use_header) - len(r))
        existing[row_key(r[k0], r[k1])] = n

    def to_list(row):
        return [row.get(h, "") for h in use_header]

    updates, appends = {}, []
    for row in rows:
        n = existing.get(row_key(row.get(KEY_COLS[0]), row.get(KEY_COLS[1])))
        if n:
            updates[n] = to_list(row)
        else:
            appends.append(to_list(row))
    return use_header, updates, appends, rewrite_header


def col_letter(n):
    s = ""
    while n:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


def ensure_tab(sh, title, header):
    import gspread

    try:
        return sh.worksheet(title), False
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1000, cols=max(len(header), 20))
        log(f"タブ「{title}」を作りました")
        return ws, True


def write_rows(sh, title, header, rows):
    ws, created = ensure_tab(sh, title, header)
    values = ws.get_all_values()
    use_header, updates, appends, rewrite_header = plan_upsert(values, header, rows)
    last = col_letter(len(use_header))
    if ws.col_count < len(use_header):
        ws.add_cols(len(use_header) - ws.col_count)
    if rewrite_header:
        ws.update(range_name=f"A1:{last}1", values=[use_header], value_input_option="RAW")
    if created or rewrite_header:
        ws.freeze(rows=1)
        ws.format(f"A1:{last}1", {"textFormat": {"bold": True}})
        for col, pattern in DATE_COLS.items():
            if col in use_header:
                c = col_letter(use_header.index(col) + 1)
                ws.format(f"{c}2:{c}", {"numberFormat": {"type": "DATE_TIME", "pattern": pattern}})
    if updates:
        ws.batch_update(
            [{"range": f"A{n}:{last}{n}", "values": [v]} for n, v in sorted(updates.items())],
            value_input_option="USER_ENTERED",
        )
    if appends:
        ws.append_rows(appends, value_input_option="USER_ENTERED", table_range="A1")
    log(f"「{title}」: 上書き {len(updates)} 行・追記 {len(appends)} 行")


def open_sheet(env=os.environ):
    import gspread

    sheet_id = env.get("SPREADSHEET_ID_JOBTYPE64", "")
    if not sheet_id:
        raise SystemExit("SPREADSHEET_ID_JOBTYPE64 が未設定です")
    sa = env.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if sa:
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_info(
            json.loads(sa), scopes=["https://www.googleapis.com/auth/spreadsheets"])
        gc = gspread.authorize(creds)
    else:
        gc = gspread.oauth()  # Mac で動かすとき（~/.config/gspread の認証）
    return gc.open_by_key(sheet_id)


def print_rows(title, rows):
    log(f"--- {title}（{len(rows)} 行・シートには書きません）")
    for r in rows:
        print(json.dumps(r, ensure_ascii=False), flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="ジョブタイプ64のオーガニック実績を記録する")
    ap.add_argument("--dry-run", action="store_true", help="取得して表示するだけ（シートに書かない）")
    ap.add_argument("--only", choices=["tiktok", "instagram"], help="片方だけ")
    ap.add_argument("--tiktok-test-url", help="この公開動画1本だけ取得して表示（シートは開かない）")
    args = ap.parse_args(argv)
    now = datetime.now(JST)

    if args.tiktok_test_url:
        info = fetch_tiktok_public(args.tiktok_test_url)
        print_rows("TikTok 取得テスト", [build_tiktok_row(args.tiktok_test_url, "", info, None, "", now)])
        return 0

    sh = open_sheet()
    source_rows = read_source_rows(sh.worksheet(SOURCE_TAB).get_all_values())  # 読むだけ
    log(f"投稿管理: 動画 {len(source_rows)} 行")

    failed = False
    if args.only in (None, "tiktok"):
        try:
            rows = collect_tiktok(source_rows, now)
            if args.dry_run:
                print_rows(TAB_TIKTOK, rows)
            elif rows:
                write_rows(sh, TAB_TIKTOK, TT_HEADER, rows)
            else:
                log("TikTok: 記録する投稿がまだありません")
        except Exception as e:
            failed = True
            log(f"TikTok の記録に失敗: {redact(e)}")
    if args.only in (None, "instagram"):
        try:
            rows = collect_instagram(source_rows, now)
            if rows is None:
                pass  # 鍵が未設定（保留）
            elif args.dry_run:
                print_rows(TAB_IG, rows)
            elif rows:
                write_rows(sh, TAB_IG, IG_HEADER, rows)
        except Exception as e:
            failed = True
            log(f"インスタの記録に失敗: {redact(e)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
