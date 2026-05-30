#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
無期限のページアクセストークンを取得するためのワンタイム・ローカル実行スクリプト。

Metaで生成した「60日のシステムユーザートークン」を入力すると、
そこからFacebookページのアクセストークンとIGビジネスアカウントIDを取り出し、
debug_token でそのページトークンの有効期限を確認して表示する。

通常、長期トークン由来のページアクセストークンは「無期限」になるため、
出力されたページトークンを GitHub Secret の IG_ACCESS_TOKEN に登録すれば、
以後リフレッシュ不要で運用できる。

使い方:
    APP_ID=... APP_SECRET=... SYSTEM_USER_TOKEN=... python3 get_page_token.py
"""

import os
import sys
import requests

GRAPH = "https://graph.facebook.com/v21.0"

APP_ID = os.environ.get("APP_ID")
APP_SECRET = os.environ.get("APP_SECRET")
TOKEN = os.environ.get("SYSTEM_USER_TOKEN")

if not (APP_ID and APP_SECRET and TOKEN):
    print("環境変数 APP_ID / APP_SECRET / SYSTEM_USER_TOKEN を指定してください。")
    print('例: APP_ID=xxx APP_SECRET=yyy SYSTEM_USER_TOKEN=zzz python3 get_page_token.py')
    sys.exit(1)

# 1) 管理しているページとそのアクセストークン、IGビジネスアカウントを取得
r = requests.get(
    f"{GRAPH}/me/accounts",
    params={
        "fields": "name,access_token,instagram_business_account{id,username}",
        "access_token": TOKEN,
    },
    timeout=60,
)
data = r.json()
pages = data.get("data", [])
if not pages:
    print("ページが取得できませんでした。レスポンス:")
    print(data)
    sys.exit(1)

print("=== 取得したページ ===")
for p in pages:
    iga = p.get("instagram_business_account") or {}
    print(f"- ページ名: {p.get('name')}")
    print(f"  IGアカウント: @{iga.get('username')} (ID: {iga.get('id')})")
    page_token = p.get("access_token", "")

    # 2) このページトークンの有効期限を debug_token で確認
    dbg = requests.get(
        f"{GRAPH}/debug_token",
        params={
            "input_token": page_token,
            "access_token": f"{APP_ID}|{APP_SECRET}",
        },
        timeout=60,
    ).json().get("data", {})
    expires_at = dbg.get("expires_at")
    if expires_at == 0:
        expiry = "無期限（never）✅"
    elif expires_at:
        from datetime import datetime, timezone
        expiry = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()
    else:
        expiry = "不明"
    print(f"  有効期限: {expiry}")
    print(f"  ▼ このページのアクセストークン（IG_ACCESS_TOKENに登録）:")
    print(f"  {page_token}")
    print()
