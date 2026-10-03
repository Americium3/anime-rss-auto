import anime_rss as c


def test_season_packs_are_detected():
    for name in (
        "[LoliHouse] Yani Neko [01-12][WebRip 1080p HEVC-10bit AAC]",
        "[JYFanSub][Youjo_Senki][01-12+SP][GB][1080p][BDrip]",
        "[合集][碧蓝之海][01-12话][720P][GB][MP4]",
        "[ANi] Foo [Batch][1080P]",
    ):
        assert c._is_batch_pack(name), name


def test_single_episodes_are_not_packs():
    for name in (
        "[LoliHouse] Yani Neko - 12 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv",
        "[ANi] Foo - 05 [1080P][Baha][WEB-DL][AAC AVC][CHT]",
        "[X] Show [2026-10][1080p]",
        "[X] Show - 10 [1080p-2][CHS]",
    ):
        assert not c._is_batch_pack(name), name
