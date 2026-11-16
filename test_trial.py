"""トライアルリールの指定とフォルダの探し方の試験（投稿はしない）。python3 test_trial.py"""
import json
import post_reel as pr


def test_trial_strategy():
    for v in ("", None, "FALSE", "いいえ"):
        assert pr.trial_strategy(v) is None, v
    for v in ("TRUE", True, "自動", "はい", "ＴＲＵＥ"):
        assert pr.trial_strategy(v) == "SS_PERFORMANCE", v
    for v in ("手動", "MANUAL"):
        assert pr.trial_strategy(v) == "MANUAL", v
    try:
        pr.trial_strategy("たぶん")
        raise AssertionError("読めない値は止める")
    except ValueError:
        pass


def test_container_request():
    url, data = pr.reel_container_request("U1", "T", "https://v", "", "cap")
    assert url == f"{pr.GRAPH}/U1/media" and "trial_params" not in data
    assert data["media_type"] == "REELS"
    url, data = pr.reel_container_request("U1", "T", "https://v", "https://c", "cap", "SS_PERFORMANCE")
    assert url == f"{pr.GRAPH_TRIAL}/U1/media"
    assert json.loads(data["trial_params"]) == {"graduation_strategy": "SS_PERFORMANCE"}
    assert data["media_type"] == "REELS" and data["cover_url"] == "https://c"


def test_find_in_folders():
    calls = []
    files = {"EXTRA": {"a.mp4": "ID_A"}}

    def fake(drive, folder_id, name):
        calls.append(folder_id)
        if name in files.get(folder_id, {}):
            return files[folder_id][name]
        raise FileNotFoundError(name)

    orig = pr.find_drive_file
    pr.find_drive_file = fake
    try:
        cfg = {"drive_folder_id": "MAIN", "drive_extra_folder_ids": ["EXTRA"]}
        assert pr.find_in_folders(None, cfg, "a.mp4") == "ID_A" and calls == ["MAIN", "EXTRA"]
        try:
            pr.find_in_folders(None, cfg, "b.mp4")
            raise AssertionError
        except FileNotFoundError:
            pass
        assert pr.find_in_folders.__name__  # 追加なしでも動く
        calls.clear()
        try:
            pr.find_in_folders(None, {"drive_folder_id": "MAIN"}, "a.mp4")
            raise AssertionError
        except FileNotFoundError:
            assert calls == ["MAIN"]
    finally:
        pr.find_drive_file = orig


def test_accounts_env():
    acc = pr.load_accounts({"DRIVE_FOLDER_ID_JOBTYPE64": "M", "DRIVE_EXTRA_FOLDER_IDS_JOBTYPE64": " X1, X2 "})
    assert acc["jobtype64"]["drive_extra_folder_ids"] == ["X1", "X2"]
    assert pr.load_accounts({})["jobtype64"]["drive_extra_folder_ids"] == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("OK", name)
