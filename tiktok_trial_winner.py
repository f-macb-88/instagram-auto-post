"""インスタのトライアルで一番再生が多かった版を、TikTok に予約する（ジョブタイプ64）。

2026-11-27 渕山さん指示：「TikTokはインスタで当たったやつを投稿する形に変えたい、トライアルで2日回して
一番再生が多いやつをTikTokに投稿する形で設計して」

流れ（GitHub Actions で1時間ごと・.github/workflows/tiktok_winner.yml）
  ① 「投稿予定」タブのトライアルの行（名前に「_トライアル_」）を、同じ本（「_トライアル_」の前が同じ）ごとにまとめる
  ② その本のトライアルが全部出終わり（予定の残りなし・エラーは数えない）、最後の1本から2日（WAIT_HOURS）たったら判定する
  ③ 各版の再生数（インスタのインサイト views）を取り、一番多い版の行に TikTok の予約を書く
     ・TikTok_確認済み＝TRUE／TikTok_投稿予定時刻＝次の空き枠（TT_SLOTS）／TikTok_キャプション（空ならインスタのものを写す）
     ・メモに「TikTok＝トライアル勝ち（再生 N・2位 M）」を足す
     投稿そのものは今までどおり post_reel.py（5分ごと）が行う
  ④ 渕山さんの DM に、勝った版と再生数を知らせる

飛ばす本
  ・同じ番号の行で、TikTok がもう出ている（投稿済・手動投稿・送信中）か、TikTok の予約が生きている（確認済み＋時刻あり＋ステータス空）
    → 二重に出さない。「保留」の行は飛ばす理由にしない
  ・勝った版の動画が Drive の探す先（本番＋追加のフォルダ）に無いときは予約せず DM で知らせる

--dry-run：判定と勝ち負けを表示するだけ（シートに書かない・DMも送らない）
"""
import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone

import metrics_jt64 as M

JST = timezone(timedelta(hours=9))
WAIT_HOURS = float(os.environ.get("TT_WINNER_WAIT_HOURS", "48"))
TT_SLOTS = os.environ.get("TT_WINNER_SLOTS", "07:30,15:00,18:30,21:00")
LEAD_MIN = 20  # 書いた時刻がすぐ来すぎないよう、今から20分より後の枠にする

TAB = "投稿予定"
TAB_DONE = "投稿済み"
TRIAL_MARK = "_トライアル_"
DONE = ("投稿済", "手動投稿", "送信中")
TRIAL_DONE = ("トライアル投稿済", "投稿済", "手動投稿")


def nfc(s):
    return unicodedata.normalize("NFC", str(s or "")).strip()


def norm(h):
    return re.sub(r"[\s_＿]+", "_", nfc(h))


def truthy(v):
    return nfc(v).lower() in ("true", "1", "はい", "yes", "✓", "☑")


def parse_t(s):
    s = nfc(s)
    for f in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(s, f).replace(tzinfo=JST)
        except ValueError:
            pass
    return None


def book_key(name):
    name = nfc(name)
    return name.split(TRIAL_MARK)[0] if TRIAL_MARK in name else None


def num_of(name):
    m = re.match(r"(\d+)_", nfc(name))
    return m.group(1) if m else None


class Sheet:
    """見出し名で列を引く。見出しの改行・空白・＿は「_」とみなす（post_reel.py と同じ）"""

    def __init__(self, values):
        self.header = values[0] if values else []
        self.col = {norm(h): i for i, h in enumerate(self.header)}
        self.rows = values[1:]

    def get(self, row, name):
        i = self.col.get(norm(name))
        return row[i] if i is not None and i < len(row) else ""


def tiktok_taken(sheets, num):
    """同じ番号の本で、TikTok がもう出ている／予約が生きているか"""
    for sh in sheets:
        for r in sh.rows:
            if num_of(sh.get(r, "動画ファイル名")) != num:
                continue
            st = nfc(sh.get(r, "TikTok_ステータス"))
            if st in DONE:
                return f"TikTok {st}（{nfc(sh.get(r, '動画ファイル名'))}）"
            if not st and truthy(sh.get(r, "TikTok_確認済み")) and parse_t(sh.get(r, "TikTok_投稿予定時刻")):
                return f"TikTok 予約あり（{nfc(sh.get(r, '動画ファイル名'))}）"
    return None


def groups_ready(sh, now, wait_hours=WAIT_HOURS):
    """判定できる本 {本: [(行番号, 行), …]} と、待ちの本 {本: 理由} を返す"""
    books = {}
    for i, r in enumerate(sh.rows, 2):
        k = book_key(sh.get(r, "動画ファイル名"))
        if k and parse_t(sh.get(r, "インスタ_投稿予定時刻")):
            books.setdefault(k, []).append((i, r))
    ready, waiting = {}, {}
    for k, rs in books.items():
        posted, pending = [], 0
        for i, r in rs:
            st = nfc(sh.get(r, "インスタ_ステータス"))
            if st in TRIAL_DONE and nfc(sh.get(r, "インスタ_投稿URL")):
                posted.append((i, r))
            elif not st:
                pending += 1
        if pending:
            waiting[k] = f"まだ出ていない版が{pending}本"
            continue
        if len(posted) < 2:
            waiting[k] = f"出た版が{len(posted)}本だけ"
            continue
        last = max(parse_t(sh.get(r, "インスタ_投稿予定時刻")) for _, r in posted)
        due = last + timedelta(hours=wait_hours)
        if now < due:
            waiting[k] = f"判定は {due:%m/%d %H:%M} から"
            continue
        ready[k] = posted
    return ready, waiting


def pick_winner(views):
    """views: [(行番号, 行, 再生数), …] → 再生数の多い順。同じなら上の行（先に足した版）"""
    return sorted(views, key=lambda x: (-x[2], x[0]))


def next_tt_slot(sheets, now, slots_text=TT_SLOTS):
    """今から20分より後で、TikTok の予約が入っていない一番近い枠（保留・投稿しないの行の時刻は空きとみなす）"""
    taken = set()
    for sh in sheets:
        for r in sh.rows:
            t = parse_t(sh.get(r, "TikTok_投稿予定時刻"))
            if t and nfc(sh.get(r, "TikTok_ステータス")) not in ("保留", "投稿しない"):
                taken.add(t)
    slots = []
    for part in slots_text.split(","):
        h, m = part.strip().split(":")
        slots.append((int(h), int(m)))
    start = now + timedelta(minutes=LEAD_MIN)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for d in range(0, 60):
        for h, m in sorted(slots):
            t = day + timedelta(days=d, hours=h, minutes=m)
            if t >= start and t not in taken:
                return t
    raise RuntimeError("TikTok の空き枠が見つかりません")


def ig_media_index(env):
    token = env.get("IG_ACCESS_TOKEN_JOBTYPE64", "")
    ig_user = env.get("IG_USER_ID_JOBTYPE64", "")
    if not (token and ig_user):
        raise SystemExit("IG_USER_ID_JOBTYPE64・IG_ACCESS_TOKEN_JOBTYPE64 が未設定です")
    reels = M.list_ig_reels(ig_user, token)
    by_code = {(m.get("shortcode") or M.ig_shortcode(m.get("permalink", ""))): m["id"] for m in reels}
    return by_code, token


def write_winner(ws, sh, i, r, t, win_v, second):
    import gspread

    def a1(col):
        return gspread.utils.rowcol_to_a1(i, sh.col[norm(col)] + 1)

    fresh = Sheet([sh.header, ws.row_values(i)])
    if nfc(fresh.get(fresh.rows[0], "動画ファイル名")) != nfc(sh.get(r, "動画ファイル名")):
        raise RuntimeError(f"行{i}の中身が変わったので書きません")
    if nfc(fresh.get(fresh.rows[0], "TikTok_ステータス")):
        M.log(f"  行{i}: TikTok_ステータスが空でないので書きません")
        return False
    cap = nfc(sh.get(r, "TikTok_キャプション")) or sh.get(r, "インスタ_キャプション")
    memo = nfc(sh.get(r, "メモ"))
    add = f"TikTok＝トライアル勝ち（再生 {win_v:,}・2位 {second:,}）"
    ws.batch_update([
        {"range": a1("TikTok_確認済み"), "values": [[True]]},
        {"range": a1("TikTok_投稿予定時刻"), "values": [[f"{t:%Y-%m-%d %H:%M}"]]},
        {"range": a1("TikTok_キャプション"), "values": [[cap]]},
        {"range": a1("メモ"), "values": [[(memo + " / " if memo else "") + add]]},
    ], value_input_option="RAW")
    return True


def open_drive():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from post_reel import SCOPES, load_accounts

    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]), scopes=SCOPES)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    return drive, load_accounts()["jobtype64"]


def main(argv=None):
    ap = argparse.ArgumentParser(description="トライアルで一番再生が多い版を TikTok に予約する")
    ap.add_argument("--dry-run", action="store_true", help="判定を表示するだけ（シートに書かない・DMも送らない）")
    a = ap.parse_args(argv)

    now = datetime.now(JST)
    book = M.open_sheet()
    ws = book.worksheet(TAB)
    sh = Sheet(ws.get_all_values())
    try:
        done = Sheet(book.worksheet(TAB_DONE).get_all_values())
    except Exception:
        done = Sheet([])
    sheets = [sh, done]

    ready, waiting = groups_ready(sh, now)
    for k, why in sorted(waiting.items()):
        M.log(f"待ち：{k}（{why}）")
    todo = {}
    for k, rs in sorted(ready.items()):
        taken = tiktok_taken(sheets, num_of(k))
        if taken:
            M.log(f"飛ばす：{k}（{taken}）")
        else:
            todo[k] = rs
    if not todo:
        M.log("今回 TikTok に予約する本はありません")
        return 0

    by_code, token = ig_media_index(os.environ)
    drive = cfg = None
    if not a.dry_run:
        drive, cfg = open_drive()
        from post_reel import find_in_folders, notify_slack
    for k, rs in sorted(todo.items()):
        vs = []
        for i, r in rs:
            code = M.ig_shortcode(sh.get(r, "インスタ_投稿URL"))
            mid = by_code.get(code)
            if not mid:
                M.log(f"  {nfc(sh.get(r, '動画ファイル名'))}: インスタの一覧に見つからない（{code}）")
                continue
            ins, _ = M.fetch_ig_insights(mid, token, metrics=["views"])
            vs.append((i, r, int(ins.get("views", 0))))
        if len(vs) < 2:
            M.log(f"飛ばす：{k}（再生数が取れた版が{len(vs)}本）")
            continue
        order = pick_winner(vs)
        win_i, win_r, win_v = order[0]
        second = order[1][2]
        name = nfc(sh.get(win_r, "動画ファイル名"))
        table = "、".join(
            f"{nfc(sh.get(r, '動画ファイル名')).split(TRIAL_MARK)[-1].rsplit('.', 1)[0]} {v:,}" for _, r, v in order)
        t = next_tt_slot(sheets, now)
        M.log(f"勝ち：{name}（再生 {win_v:,}・2位 {second:,}）→ TikTok {t:%Y-%m-%d %H:%M}｜{table}")
        if a.dry_run:
            continue
        try:
            find_in_folders(drive, cfg, name)
        except Exception as e:
            M.log(f"  動画が見つからないので予約しません: {M.redact(e)}")
            notify_slack(f"⚠️ TikTok（トライアル勝ち）を予約できません：{name} が Drive の本番・追加フォルダに見つかりません。")
            continue
        if write_winner(ws, sh, win_i, win_r, t, win_v, second):
            sh.rows[win_i - 2] = ws.row_values(win_i)  # 次の本の枠決めに、今書いた時刻を反映
            notify_slack(f"🎯 TikTok に予約しました（インスタのトライアル2日で一番再生）\n{name}\n{t:%m/%d %H:%M}｜{table}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
