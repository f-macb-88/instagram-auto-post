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


class TestUpsert(unittest.TestCase):
    def test_new_tab(self):
        rows = [{"取得日": "2026-11-07", "投稿URL": "u1", "再生数": 10}]
        header, upd, app, rewrite = m.plan_upsert([], m.TT_HEADER, rows)
        self.assertTrue(rewrite)
        self.assertEqual(header, m.TT_HEADER)
        self.assertEqual(upd, {})
        self.assertEqual(app[0][header.index("再生数")], 10)

    def test_same_day_overwrites(self):
        existing = [m.TT_HEADER, ["2026/11/7"] + [""] * 2 + ["u1"] + [""] * (len(m.TT_HEADER) - 4),
                    ["2026-11-06", "", "", "u1"]]
        rows = [{"取得日": "2026-11-07", "投稿URL": "u1", "再生数": 20},
                {"取得日": "2026-11-07", "投稿URL": "u2", "再生数": 5}]
        header, upd, app, rewrite = m.plan_upsert(existing, m.TT_HEADER, rows)
        self.assertFalse(rewrite)
        self.assertEqual(list(upd), [2])
        self.assertEqual(len(app), 1)

    def test_missing_header_appended_at_end(self):
        existing = [["取得日", "投稿URL", "再生数"]]
        header, _, _, rewrite = m.plan_upsert(existing, m.TT_HEADER, [])
        self.assertTrue(rewrite)
        self.assertEqual(header[:3], ["取得日", "投稿URL", "再生数"])
        self.assertEqual(len(header), len(m.TT_HEADER))

    def test_col_letter(self):
        self.assertEqual(m.col_letter(1), "A")
        self.assertEqual(m.col_letter(26), "Z")
        self.assertEqual(m.col_letter(27), "AA")


if __name__ == "__main__":
    unittest.main()
