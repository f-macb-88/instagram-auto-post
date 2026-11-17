"""metrics_jt64.py の単体テスト（ネットワークに出ない）。python -m unittest test_metrics_jt64"""

import unittest
from datetime import datetime
from unittest import mock

import metrics_jt64 as m

NOW = datetime(2026, 11, 7, 6, 10, tzinfo=m.JST)


class FakeResp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body


class TestHelpers(unittest.TestCase):
    def test_ids(self):
        self.assertEqual(m.ig_shortcode("https://www.instagram.com/reel/DfL3cM3zhAB/?igsh=x"), "DfL3cM3zhAB")
        self.assertEqual(m.ig_shortcode("https://www.instagram.com/jobtype64/reel/ABC_-1/"), "ABC_-1")
        self.assertEqual(m.tiktok_video_id("https://www.tiktok.com/@jobtype64/video/7434567890123456789"), "7434567890123456789")
        self.assertEqual(m.tiktok_video_id("https://vm.tiktok.com/ZSabc/"), "")

    def test_date_key(self):
        self.assertEqual(m.normalize_date_key("2026/11/7"), "2026-11-07")
        self.assertEqual(m.normalize_date_key("2026-11-07"), "2026-11-07")
        self.assertEqual(m.normalize_date_key("46333"), "2026-11-07")

    def test_parse_iso(self):
        self.assertEqual(m.fmt_dt(m.parse_iso("2026-11-06T10:30:00+0000")), "2026-11-06 19:30")
        self.assertEqual(m.fmt_dt(m.parse_iso("2026-11-06T10:30:00.000Z")), "2026-11-06 19:30")

    def test_read_source_rows(self):
        values = [
            ["動画ファイル名", "インスタ\n投稿URL", "TikTok＿投稿URL", "BufferID", "メモ"],
            ["01.mp4", "https://www.instagram.com/reel/AAA/", "", "", ""],
            ["", "", "", "", "空行"],
            ["02.mp4", "", "https://www.tiktok.com/@j/video/111", "'buf1", ""],
        ]
        rows = m.read_source_rows(values)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["ig_url"], "https://www.instagram.com/reel/AAA/")
        self.assertEqual(rows[1]["buffer_id"], "buf1")


class TestInstagram(unittest.TestCase):
    def test_parse_insights_both_shapes(self):
        body = {"data": [
            {"name": "views", "period": "lifetime", "values": [{"value": 1200}]},
            {"name": "reach", "total_value": {"value": 900}},
            {"name": "ig_reels_avg_watch_time", "values": [{"value": 4567}]},
        ]}
        self.assertEqual(m.parse_insights(body), {"views": 1200, "reach": 900, "ig_reels_avg_watch_time": 4567})

    def test_fetch_falls_back_per_metric(self):
        def fake_get(url, params=None, headers=None, timeout=None):
            self.assertNotIn("access_token", url)
            self.assertTrue(headers["Authorization"].startswith("Bearer "))
            metric = params["metric"]
            if "," in metric or metric == "reposts":
                return FakeResp({"error": {"code": 100, "message": "bad metric"}}, 400)
            return FakeResp({"data": [{"name": metric, "values": [{"value": 5}]}]})

        with mock.patch.object(m.requests, "get", side_effect=fake_get):
            vals, failed = m.fetch_ig_insights("123", "tok")
        self.assertEqual(failed, ["reposts"])
        self.assertEqual(vals["views"], 5)

    def test_build_ig_row(self):
        media = {"id": "1789", "permalink": "https://www.instagram.com/reel/AAA/", "timestamp": "2026-11-05T22:10:00+0000"}
        ins = {"views": 1000, "reach": 800, "likes": 30, "ig_reels_avg_watch_time": 4567, "ig_reels_video_view_total_time": 4567000}
        row = m.build_ig_row(media, ins, [], "01.mp4", NOW)
        self.assertEqual(row["投稿日時"], "2026-11-06 07:10")
        self.assertEqual(row["経過日数"], 1.0)
        self.assertEqual(row["再生数"], 1000)
        self.assertIsInstance(row["再生数"], int)
        self.assertEqual(row["平均視聴時間_秒"], 4.57)
        self.assertEqual(row["合計視聴時間_秒"], 4567.0)
        self.assertEqual(row["取得状況"], "OK")
        self.assertEqual(set(row) - set(m.IG_HEADER), set())

    def test_no_keys_means_pending(self):
        with mock.patch.object(m.requests, "get", side_effect=AssertionError("通信してはいけない")):
            self.assertIsNone(m.collect_instagram([], NOW, env={}))
            self.assertIsNone(m.collect_instagram([], NOW, env={"IG_USER_ID_JOBTYPE64": "1"}))

    def test_list_reels_paging_and_filter(self):
        pages = [
            FakeResp({"data": [{"id": "1", "media_product_type": "REELS"}, {"id": "2", "media_product_type": "FEED"}],
                      "paging": {"next": "https://graph.facebook.com/v25.0/next"}}),
            FakeResp({"data": [{"id": "3", "media_product_type": "REELS"}]}),
        ]
        with mock.patch.object(m.requests, "get", side_effect=pages):
            self.assertEqual([x["id"] for x in m.list_ig_reels("ig", "tok")], ["1", "3"])


class TestTikTok(unittest.TestCase):
    def test_merge_targets(self):
        src = [
            {"video": "01.mp4", "ig_url": "", "tt_url": "https://www.tiktok.com/@j/video/111", "buffer_id": ""},
            {"video": "02.mp4", "ig_url": "", "tt_url": "", "buffer_id": "b2"},
        ]
        posts = [
            {"id": "b1", "externalLink": "https://www.tiktok.com/@j/video/111"},
            {"id": "b2", "externalLink": "https://www.tiktok.com/@j/video/222"},
            {"id": "b3", "externalLink": None},
        ]
        t = {x["url"]: x for x in m.merge_tiktok_targets(src, posts)}
        self.assertEqual(len(t), 2)
        self.assertEqual(t["https://www.tiktok.com/@j/video/111"]["video"], "01.mp4")
        self.assertEqual(t["https://www.tiktok.com/@j/video/111"]["buffer_post"]["id"], "b1")
        self.assertEqual(t["https://www.tiktok.com/@j/video/222"]["video"], "02.mp4")

    def test_build_row_public_and_buffer(self):
        info = {"id": "111", "timestamp": 1762428600, "view_count": 1500, "like_count": 40,
                "comment_count": 3, "repost_count": 2, "save_count": 5}
        post = {"sentAt": "2026-11-06T10:30:00Z", "metricsUpdatedAt": "2026-11-06T20:00:00Z",
                "metrics": [{"type": "averageTimeWatched", "value": 6.2, "unit": "count"},
                            {"type": "totalTimeWatched", "value": 150.0, "unit": "count"},
                            {"type": "views", "value": 1400.0, "unit": "count"}]}
        row = m.build_tiktok_row("https://www.tiktok.com/@j/video/111", "01.mp4", info, post, "", NOW)
        self.assertEqual(row["再生数"], 1500)  # 公開ページの値を優先
        self.assertEqual(row["平均視聴時間_秒"], 6.2)
        self.assertEqual(row["合計視聴時間_分"], 150.0)
        self.assertEqual(row["取得元"], "公開ページ+Buffer")
        self.assertEqual(row["Buffer指標の更新日時"], "2026-11-07 05:00")
        self.assertEqual(row["取得状況"], "OK")
        self.assertEqual(set(row) - set(m.TT_HEADER), set())

    def test_build_row_public_failed_uses_buffer(self):
        post = {"sentAt": "2026-11-06T10:30:00Z", "metrics": [{"type": "views", "value": 1400.0}]}
        row = m.build_tiktok_row("https://www.tiktok.com/@j/video/111", "", None, post, "", NOW)
        self.assertEqual(row["再生数"], 1400.0)
        self.assertEqual(row["投稿日時"], "2026-11-06 19:30")
        self.assertIn("公開ページ取得失敗", row["取得状況"])

    def test_buffer_without_metrics_yet(self):
        info = {"id": "111", "view_count": 10}
        row = m.build_tiktok_row("u/video/111", "", info, {"metrics": []}, "", NOW)
        self.assertIn("最大24時間遅れ", row["取得状況"])


class TestIgFallback(unittest.TestCase):
    def test_likes_comments_without_insights(self):
        media = {"id": "1", "permalink": "https://www.instagram.com/reel/AAA/", "timestamp": "2026-11-10T00:00:00+0000",
                 "like_count": 12, "comments_count": 3}
        row = m.build_ig_row(media, {}, ["全指標（Graph API エラー（HTTP 400 code 10）: (#10) ...）"], "v.mp4", NOW)
        self.assertEqual((row["いいね"], row["コメント"]), (12, 3))
        self.assertNotIn("再生数", row)
        self.assertIn("いいね・コメントのみ", row["取得状況"])

    def test_insights_win(self):
        media = {"id": "1", "permalink": "u", "like_count": 1}
        row = m.build_ig_row(media, {"likes": 9, "views": 100}, [], "", NOW)
        self.assertEqual((row["いいね"], row["再生数"], row["取得状況"]), (9, 100, "OK"))


class TestUpsert(unittest.TestCase):
    """1投稿＝1行。毎日同じ行を上書きし、同じ投稿の古い行は消す（2026-11-16 渕山さん指示）"""

    def row(self, date, url, vid="", views=""):
        r = [""] * len(m.TT_HEADER)
        r[m.TT_HEADER.index("投稿日時")] = date
        r[m.TT_HEADER.index("投稿URL")] = url
        r[m.TT_HEADER.index("動画ID")] = vid
        r[m.TT_HEADER.index("再生数")] = views
        return r

    def test_new_tab(self):
        rows = [{"取得日": "2026-11-07", "投稿URL": "u1", "再生数": 10}]
        header, upd, app, rewrite, dels = m.plan_upsert([], m.TT_HEADER, rows)
        self.assertTrue(rewrite)
        self.assertEqual(header, m.TT_HEADER)
        self.assertEqual(upd, {})
        self.assertEqual(dels, [])
        self.assertEqual(app[0][header.index("再生数")], 10)

    def test_other_day_overwrites_same_row(self):
        existing = [m.TT_HEADER, self.row("2026-11-06", "https://tiktok.com/@a/video/111", "111", 5)]
        rows = [{"取得日": "2026-11-07", "投稿URL": "https://tiktok.com/@a/video/111", "動画ID": "111", "再生数": 20}]
        header, upd, app, rewrite, dels = m.plan_upsert(existing, m.TT_HEADER, rows)
        self.assertEqual(list(upd), [2])
        self.assertEqual(upd[2][header.index("再生数")], 20)
        self.assertEqual(app, [])
        self.assertEqual(dels, [])

    def test_duplicates_keep_newest_and_delete_older(self):
        u1, u2 = "https://tiktok.com/@a/video/111", "https://tiktok.com/@a/video/222"
        existing = [m.TT_HEADER,
                    self.row("2026-11-06", u1, "111", 5),     # 2 古い → 消す
                    self.row("2026-11-06", u2, "222", 1),     # 3 古い → 消す
                    self.row("2026-11-07", u1, "111", 9),     # 4 古い → 消す
                    self.row("2026-11-07", u2, "222.0", 3),   # 5 u2 の最新（今回は取れなかった）→ 残す
                    self.row("2026-11-08", u1, "111", 12)]    # 6 u1 の最新 → 上書き
        rows = [{"取得日": "2026-11-09", "投稿URL": u1, "動画ID": "111", "再生数": 30},
                {"取得日": "2026-11-09", "投稿URL": "https://tiktok.com/@a/video/333", "動画ID": "333", "再生数": 1}]
        header, upd, app, rewrite, dels = m.plan_upsert(existing, m.TT_HEADER, rows)
        self.assertEqual(dels, [2, 3, 4])
        self.assertEqual(list(upd), [6])
        self.assertEqual(upd[6][header.index("再生数")], 30)
        self.assertEqual(len(app), 1)

    def test_ig_key_by_media_id(self):
        r = [""] * len(m.IG_HEADER)
        r[m.IG_HEADER.index("投稿URL")] = "https://www.instagram.com/reel/AAA/"
        r[m.IG_HEADER.index("メディアID")] = "1789"
        existing = [m.IG_HEADER, list(r), list(r)]
        rows = [{"投稿URL": "https://www.instagram.com/reel/AAA/", "メディアID": "1789", "再生数": 7}]
        _, upd, app, _, dels = m.plan_upsert(existing, m.IG_HEADER, rows)
        self.assertEqual((list(upd), app, dels), ([3], [], [2]))

    def test_blank_key_rows_are_never_deleted(self):
        existing = [m.TT_HEADER, self.row("2026-11-06", "", "", 1), self.row("2026-11-07", "", "", 2)]
        _, upd, app, _, dels = m.plan_upsert(existing, m.TT_HEADER, [])
        self.assertEqual((upd, app, dels), ({}, [], []))

    def test_group_runs(self):
        self.assertEqual(m.group_runs([9, 3, 4, 5]), [(3, 5), (9, 9)])

    def test_missing_header_appended_at_end(self):
        existing = [["投稿日時", "投稿URL", "再生数"]]
        header, _, _, rewrite, _ = m.plan_upsert(existing, m.TT_HEADER, [])
        self.assertTrue(rewrite)
        self.assertEqual(header[:3], ["投稿日時", "投稿URL", "再生数"])
        self.assertEqual(len(header), len(m.TT_HEADER))

    def test_first_row_offset(self):
        # 新しい形：1行目＝取得日時・2行目＝見出し → 1投稿目はシートの3行目
        u1 = "https://tiktok.com/@a/video/111"
        existing = [m.TT_HEADER, self.row("", u1, "111", 1), self.row("", u1, "111", 2)]
        rows = [{"投稿URL": u1, "動画ID": "111", "再生数": 3}]
        _, upd, app, _, dels = m.plan_upsert(existing, m.TT_HEADER, rows, first_row=3)
        self.assertEqual((list(upd), app, dels), ([4], [], [3]))

    def test_layout_and_migrate(self):
        self.assertTrue(m.is_new_layout([["取得日時", "2026-11-17 12:49"], m.IG_HEADER]))
        self.assertFalse(m.is_new_layout([["取得日", "取得時刻", "動画ファイル名"]]))
        self.assertFalse(m.is_new_layout([]))
        old_h = ["取得日", "取得時刻", "動画ファイル名", "投稿URL", "メディアID", "投稿日時", "経過日数", "再生数", "メモ"]
        old = [old_h, ["2026-11-17", "2026-11-17 12:49", "a.mp4", "https://www.instagram.com/reel/AAA/", "1789", "2026-11-16 07:32", "1.2", "173", "x"],
               [""] * 9]
        t = m.migrate_values(old, m.IG_HEADER)
        self.assertEqual(t[0][:len(m.IG_HEADER)], m.IG_HEADER)
        self.assertEqual(t[0][0], "投稿日時")
        self.assertNotIn("取得日", t[0])
        self.assertNotIn("取得時刻", t[0])
        self.assertEqual(t[0][-1], "メモ")  # 知らない列は右端に残す
        self.assertEqual(len(t), 2)        # 空の行は落とす
        r = dict(zip(t[0], t[1]))
        self.assertEqual((r["投稿日時"], r["再生数"], r["メディアID"], r["メモ"]), ("2026-11-16 07:32", "173", "1789", "x"))
        self.assertEqual(m.migrate_values([], m.IG_HEADER), [m.IG_HEADER])
        # A1 を手で消しても、2行目が見出しなら新しい形のまま
        self.assertTrue(m.is_new_layout([["", ""], m.IG_HEADER]))
        # 投稿URL の無い知らない表は組み替えない
        with self.assertRaises(ValueError):
            m.migrate_values([["a", "b"], ["1", "2"]], m.IG_HEADER)

    def test_safe_cell(self):
        self.assertEqual(m.safe_cell("=HYPERLINK(1)"), "'=HYPERLINK(1)")
        self.assertEqual(m.safe_cell("+81"), "'+81")
        self.assertEqual(m.safe_cell("2026-11-16 07:32"), "2026-11-16 07:32")
        self.assertEqual(m.safe_cell(173), 173)
        self.assertEqual(m.safe_cell(" =1+1"), "' =1+1")
        self.assertEqual(m.safe_cell("-SUM(1)"), "'-SUM(1)")
        self.assertEqual(m.safe_cell("-5"), "-5")
        self.assertEqual(m.migrate_values([["投稿URL", "=x"]], m.IG_HEADER)[0][-1], "'=x")
        # 1行目が見出し（前の形）なら、2行目に投稿URL があっても新しい形とはみなさない
        self.assertFalse(m.is_new_layout([["取得日", "取得時刻", "動画ファイル名"], ["投稿URL", "投稿日時"]]))

    def test_col_letter(self):
        self.assertEqual(m.col_letter(1), "A")
        self.assertEqual(m.col_letter(26), "Z")
        self.assertEqual(m.col_letter(27), "AA")


if __name__ == "__main__":
    unittest.main()
