#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Buffer に接続しているチャンネル（SNSアカウント）の一覧を出す補助スクリプト。

GitHub Secrets の BUFFER_CHANNEL_TIKTOK_JOBTYPE64 / BUFFER_CHANNEL_TIKTOK_LOVE64 に
登録するチャンネルIDを調べるために使う。読み取りだけで、投稿は作らない。

使い方（API キーはコマンドに直接書かず、環境変数で渡す）:
    read -s BUFFER_API_KEY && export BUFFER_API_KEY
    python3 list_buffer_channels.py

API キーの値は表示しない。表示するのは組織名・チャンネル名・サービス・ID だけ。
仕様の出典: https://developers.buffer.com/guides/agent-setup.md
"""

import os
import sys

import requests

BUFFER_API = "https://api.buffer.com"

GET_ORGANIZATIONS = """
query GetOrganizations {
  account {
    organizations { id name }
  }
}
"""

GET_CHANNELS = """
query GetChannels($organizationId: OrganizationId!) {
  channels(input: { organizationId: $organizationId }) {
    id
    name
    displayName
    service
    isDisconnected
    isLocked
  }
}
"""


def call(api_key, query, variables=None):
    r = requests.post(
        BUFFER_API,
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=60,
    )
    try:
        body = r.json()
    except ValueError:
        sys.exit(f"Buffer API の応答を読めません（HTTP {r.status_code}）")
    if body.get("errors"):
        err = body["errors"][0] or {}
        code = (err.get("extensions") or {}).get("code", "")
        # エラー文に鍵が混ざっていても出さない
        msg = str(err.get("message", "")).replace(api_key, "***")
        sys.exit(f"Buffer API エラー（{code or 'HTTP ' + str(r.status_code)}）: {msg}")
    return body.get("data") or {}


def main():
    api_key = os.environ.get("BUFFER_API_KEY", "")
    if not api_key:
        sys.exit("環境変数 BUFFER_API_KEY が未設定です（値は表示しません）。")

    orgs = (call(api_key, GET_ORGANIZATIONS).get("account") or {}).get("organizations") or []
    if not orgs:
        print("組織が見つかりませんでした。")
        return

    for org in orgs:
        print(f"■ 組織: {org.get('name')}（ID {org.get('id')}）")
        channels = call(api_key, GET_CHANNELS, {"organizationId": org["id"]}).get("channels") or []
        if not channels:
            print("  （チャンネルなし）")
        for ch in channels:
            notes = []
            if ch.get("isDisconnected"):
                notes.append("接続切れ")
            if ch.get("isLocked"):
                notes.append("ロック中")
            note = f"　※{'・'.join(notes)}" if notes else ""
            name = ch.get("displayName") or ch.get("name")
            print(f"  - {ch.get('service'):<10} {name}（@{ch.get('name')}）  ID: {ch.get('id')}{note}")
    print()
    print("TikTok の行の ID を、GitHub Secrets の BUFFER_CHANNEL_TIKTOK_JOBTYPE64 / BUFFER_CHANNEL_TIKTOK_LOVE64 に登録します。")


if __name__ == "__main__":
    main()
