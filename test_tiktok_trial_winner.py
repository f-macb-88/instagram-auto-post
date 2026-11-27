"""tiktok_trial_winner.py の判定部分のテスト（シート・API には触らない）"""
import unittest
from datetime import datetime

import tiktok_trial_winner as W

H = ["動画ファイル名", "インスタ\n確認済み", "TikTok\n確認済み", "インスタ_投稿予定時刻", "TikTok_投稿予定時刻",
     "インスタ_キャプション", "TikTok_キャプション", "インスタ_ステータス", "インスタ_投稿URL", "TikTok_ステータス", "メモ"]


def row(name, ig_t="", ig_st="", url="", tt_ok="", tt_t="", tt_st=""):
    return [name, "", tt_ok, ig_t, tt_t, "cap", "", ig_st, url, tt_st, ""]


NOW = datetime(2026, 11, 29, 12, 30, tzinfo=W.JST)


class T(unittest.TestCase):
    def test_ready_after_48h(self):
        sh = W.Sheet([H,
                      row("171_深い言葉_x_トライアル_A.mp4", "2026-11-27 12:00", "トライアル投稿済", "u"),
                      row("171_深い言葉_x_トライアル_B.mp4", "2026-11-27 12:10", "トライアル投稿済", "u"),
                      row("172_深い言葉_y_トライアル_A.mp4", "2026-11-27 12:40", "トライアル投稿済", "u"),
                      row("172_深い言葉_y_トライアル_B.mp4", "2026-11-27 12:50", "トライアル投稿済", "u")])
        ready, waiting = W.groups_ready(sh, NOW)
        self.assertIn("171_深い言葉_x", ready)       # 最後 12:10 → 11/29 12:10 から判定
        self.assertIn("172_深い言葉_y", waiting)     # 最後 12:50 → 11/29 12:50 から
        ready2, _ = W.groups_ready(sh, datetime(2026, 11, 29, 12, 9, tzinfo=W.JST))
        self.assertNotIn("171_深い言葉_x", ready2)

    def test_pending_and_error(self):
        sh = W.Sheet([H,
                      row("148_深い言葉_z_トライアル_C.mp4", "2026-11-26 21:00", "トライアル投稿済", "u"),
                      row("148_深い言葉_z_トライアル_D.mp4", "2026-11-30 18:00", ""),
                      row("150_深い言葉_w_トライアル_A.mp4", "2026-11-27 12:00", "トライアル投稿済", "u"),
                      row("150_深い言葉_w_トライアル_B.mp4", "2026-11-27 12:10", "エラー")])
        ready, waiting = W.groups_ready(sh, NOW)
        self.assertIn("まだ出ていない", waiting["148_深い言葉_z"])
        self.assertIn("1本だけ", waiting["150_深い言葉_w"])  # エラーは数えない

    def test_taken(self):
        a = W.Sheet([H, row("143_深い言葉_q_動画入り.mp4", tt_st="投稿済")])
        b = W.Sheet([H, row("151_深い言葉_r_動画入り.mp4", tt_ok="TRUE", tt_t="2026-11-27 21:00", tt_st="保留")])
        c = W.Sheet([H, row("166_深い言葉_s_TikTok.mp4", tt_ok="TRUE", tt_t="2026-11-28 21:00")])
        self.assertTrue(W.tiktok_taken([a], "143"))
        self.assertIsNone(W.tiktok_taken([b], "151"))  # 保留は飛ばす理由にしない
        self.assertTrue(W.tiktok_taken([c], "166"))

    def test_pick_and_slot(self):
        order = W.pick_winner([(2, "a", 100), (3, "b", 300), (4, "c", 300)])
        self.assertEqual([x[0] for x in order], [3, 4, 2])
        sh = W.Sheet([H, row("x.mp4", tt_t="2026-11-29 15:00"), row("y.mp4", tt_t="2026-11-29 18:30", tt_st="保留")])
        self.assertEqual(W.next_tt_slot([sh], NOW).strftime("%m/%d %H:%M"), "11/29 18:30")
        self.assertEqual(W.next_tt_slot([sh], datetime(2026, 11, 29, 18, 20, tzinfo=W.JST)).strftime("%m/%d %H:%M"), "11/29 21:00")


if __name__ == "__main__":
    unittest.main()
