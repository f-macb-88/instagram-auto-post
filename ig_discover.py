"""他のインスタのアカウント（ビジネス／クリエイター）の公開投稿を、ジョブタイプ64のインスタの鍵で取る（business_discovery）。2026-11-26
競合アカウントの表紙の分析用。投稿・シートには触らない。結果は discover_out/<ユーザー名>.json に書き、ワークフローの成果物として取り出す。
使い方: python ig_discover.py <ユーザー名> [<ユーザー名> ...] [--limit 100]
- 鍵は環境変数 IG_USER_ID_JOBTYPE64・IG_ACCESS_TOKEN_JOBTYPE64（表示しない）
- 再生数の項目（view_count）を付けて頼み、受け付けられなければ外して取り直す（取れたかどうかはログに出す）
"""
import json
import os
import sys
import time
from pathlib import Path

import requests

GRAPH = "https://graph.facebook.com/v25.0"
BASE = "caption,like_count,comments_count,media_type,media_product_type,timestamp,permalink,thumbnail_url,media_url"


def fetch(ig_id, token, user, limit, fields):
    out, after, meta = [], None, None
    while len(out) < limit:
        page = f".after({after})" if after else ""
        q = (f"business_discovery.username({user}){{username,name,followers_count,media_count,"
             f"media{page}.limit(50){{{fields}}}}}")
        r = requests.get(f"{GRAPH}/{ig_id}", params={"fields": q, "access_token": token}, timeout=60)
        j = r.json()
        if "error" in j:
            return meta, out, j["error"].get("message", "")[:300]
        bd = j["business_discovery"]
        meta = {k: bd.get(k) for k in ("username", "name", "followers_count", "media_count")}
        media = bd.get("media", {})
        out += media.get("data", [])
        after = media.get("paging", {}).get("cursors", {}).get("after")
        if not after or not media.get("paging", {}).get("next"):
            break
        time.sleep(1)
    return meta, out[:limit], None


def main():
    a = sys.argv[1:]
    limit = int(a[a.index("--limit") + 1]) if "--limit" in a else 100
    users = [x.lstrip("@") for x in a if not x.startswith("--") and not x.isdigit()]
    ig_id, token = os.environ.get("IG_USER_ID_JOBTYPE64", ""), os.environ.get("IG_ACCESS_TOKEN_JOBTYPE64", "")
    if not ig_id or not token:
        sys.exit("IG_USER_ID_JOBTYPE64・IG_ACCESS_TOKEN_JOBTYPE64 が未設定")
    d = Path("discover_out")
    d.mkdir(exist_ok=True)
    for u in users:
        meta, media, err = fetch(ig_id, token, u, limit, BASE + ",view_count")
        views = True
        if err:
            print(f"{u}: 再生数つきは不可（{err}）→ 再生数なしで取り直す")
            meta, media, err = fetch(ig_id, token, u, limit, BASE)
            views = False
        (d / f"{u}.json").write_text(json.dumps({"meta": meta, "views_field": views, "media": media, "error": err},
                                                ensure_ascii=False, indent=1))
        reels = sum(1 for m in media if m.get("media_product_type") == "REELS")
        print(f"{u}: フォロワー {meta and meta.get('followers_count')}・取得 {len(media)} 件（リール {reels}）"
              f"・再生数 {'あり' if views else 'なし'}" + (f"・エラー {err}" if err else ""))


if __name__ == "__main__":
    main()
