"""单车危险区内超速时段复核测试。

POST /api/v1/trips/{trip_id}/speeding：
相邻点按线性轨迹计算恒定速度，点间隔超过上限的整段视为无覆盖；仅在所选区域
几何内（带洞边界沿用穿越规则）且生效时计入；重叠区域按最低限速；速度严格
大于限速才违规，零时长接触不计。返回按时序切分的违规正时长区间、每段速度、
适用限速与并列限速区 ID、实际使用的区域修订号和轨迹版本，以及违规总时长和
超速距离总量。区域与车次有效点从同一已提交读视图读取。
"""
import threading

from tests.conftest import DAY_WINDOWS, SQUARE, register_square


def _batch(points):
    return {"points": [{"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in points]}


def _upload(client, trip, points):
    r = client.post(f"/api/v1/trips/{trip}/points", json=_batch(points))
    assert r.status_code == 200, r.text
    return r


def _ask(client, trip, limits=None, gap=1000.0, **overrides):
    body = {
        "since": "2026-09-30T10:00:00Z",
        "until": "2026-09-30T10:10:00Z",
        "zone_speed_limits": limits if limits is not None else {"sp": 1.0},
        "max_sample_interval_sec": gap,
    }
    body.update(overrides)
    return client.post(f"/api/v1/trips/{trip}/speeding", json=body)


# 10:00:00 自 x=-50 出发，10:01:00 到 x=100：150m/60s=2.5m/s；
# x=0 在 10:00:20，x=100 在 10:01:00，方块 [0,100]x[0,100] 内共 40 秒
CROSS = [
    ("a", "2026-09-30T10:00:00Z", -50, 50),
    ("b", "2026-09-30T10:01:00Z", 100, 50),
]

DAY = DAY_WINDOWS


# ---------------------------------------------------------------------
# 基本超速区间与超速距离
# ---------------------------------------------------------------------

def test_basic_speeding_segment(client):
    register_square(client, zone_id="sp")
    _upload(client, "T1", CROSS)

    r = _ask(client, "T1", limits={"sp": 1.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trip_id"] == "T1"
    assert body["point_count"] == 2
    assert body["zone_speed_limits"] == {"sp": 1.0}
    assert body["max_sample_interval_sec"] == 1000.0
    assert "revision" not in body
    assert "track_version" not in body
    assert len(body["segments"]) == 1
    seg = body["segments"][0]
    assert seg["start"] == "2026-09-30T10:00:20Z"
    assert seg["end"] == "2026-09-30T10:01:00Z"
    assert seg["duration_sec"] == 40.0
    assert seg["speed_mps"] == 2.5
    assert seg["speed_limit_mps"] == 1.0
    assert seg["zone_ids"] == ["sp"]
    # (2.5 - 1) * 40 = 60 米
    assert seg["overspeed_distance_m"] == 60.0
    assert body["total_duration_sec"] == 40.0
    assert body["total_overspeed_distance_m"] == 60.0


def test_no_violation_empty_segments_zero_totals(client):
    """全程不超速：空区间与零总量。"""
    register_square(client, zone_id="sp")
    _upload(client, "T2", CROSS)
    r = _ask(client, "T2", limits={"sp": 5.0})
    body = r.json()
    assert body["segments"] == []
    assert body["total_duration_sec"] == 0
    assert body["total_overspeed_distance_m"] == 0


def test_speed_equal_limit_not_violation(client):
    """速度恰好等于限速不违规（严格大于才违规）。"""
    register_square(client, zone_id="sp")
    _upload(client, "T3", CROSS)
    r = _ask(client, "T3", limits={"sp": 2.5})
    body = r.json()
    assert body["segments"] == []
    assert body["total_duration_sec"] == 0
    assert body["total_overspeed_distance_m"] == 0


def test_stationary_inside_zone_not_speeding(client):
    """静止在区内：速度 0，不违规。"""
    register_square(client, zone_id="st")
    _upload(client, "TS", [
        ("a", "2026-09-30T10:00:00Z", 10, 10),
        ("b", "2026-09-30T10:01:00Z", 10, 10),
    ])
    r = _ask(client, "TS", limits={"st": 1.0})
    body = r.json()
    assert body["segments"] == []
    assert body["total_duration_sec"] == 0


def test_query_window_halfopen_clip(client):
    """查询时段按半开 [since, until) 裁剪违规区间。"""
    register_square(client, zone_id="sp")
    _upload(client, "T4", CROSS)
    r = _ask(client, "T4", limits={"sp": 1.0},
             since="2026-09-30T10:00:30Z", until="2026-09-30T10:00:50Z")
    body = r.json()
    assert len(body["segments"]) == 1
    seg = body["segments"][0]
    assert seg["start"] == "2026-09-30T10:00:30Z"
    assert seg["end"] == "2026-09-30T10:00:50Z"
    assert seg["duration_sec"] == 20.0
    assert body["total_duration_sec"] == 20.0
    assert body["total_overspeed_distance_m"] == 30.0


def test_segments_chronological_with_speed_changes(client):
    """不同相邻点段速度不同：按时序切分，每段给出各自速度。"""
    register_square(client, zone_id="sp")
    # 段 a->b：200m/40s=5m/s；段 b->c：50m/50s=1m/s（不超速）
    _upload(client, "T5", [
        ("a", "2026-09-30T10:00:00Z", -100, 50),
        ("b", "2026-09-30T10:00:40Z", 100, 50),
        ("c", "2026-09-30T10:01:30Z", 150, 50),
    ])
    r = _ask(client, "T5", limits={"sp": 2.0})
    body = r.json()
    # a->b 段内 x∈[0,100]：10:00:20..10:00:40，速度 5；b->c 段速度 1 不超速
    assert len(body["segments"]) == 1
    seg = body["segments"][0]
    assert (seg["start"], seg["end"], seg["speed_mps"]) == (
        "2026-09-30T10:00:20Z", "2026-09-30T10:00:40Z", 5.0,
    )
    assert seg["speed_limit_mps"] == 2.0
    assert seg["overspeed_distance_m"] == 60.0
    assert body["total_overspeed_distance_m"] == 60.0


def test_touching_segments_same_state_merge(client):
    """被采样时刻切开但速度/限速/并列区相同的相接违规段合并为一段。"""
    register_square(client, zone_id="sp")
    # 两段同为 2.5m/s，中间采样点 (50,50) 不应导致分段
    _upload(client, "T6", [
        ("a", "2026-09-30T10:00:00Z", -50, 50),
        ("m", "2026-09-30T10:00:40Z", 50, 50),
        ("b", "2026-09-30T10:01:20Z", 150, 50),
    ])
    r = _ask(client, "T6", limits={"sp": 1.0})
    body = r.json()
    assert len(body["segments"]) == 1
    seg = body["segments"][0]
    assert seg["start"] == "2026-09-30T10:00:20Z"
    assert seg["end"] == "2026-09-30T10:01:00Z"
    assert seg["duration_sec"] == 40.0
    assert seg["speed_mps"] == 2.5


def test_limit_change_splits_segments(client):
    """适用限速变化（进出限速更低的重叠区）即分段。"""
    register_square(client, zone_id="lo")
    assert client.post("/api/v1/zones", json={
        "zone_id": "hi",
        "exterior": [[50, 0], [100, 0], [100, 100], [50, 100]],
        "holes": [], "windows": DAY,
    }).status_code == 201
    # 2.5m/s：10:00:20 进 lo，10:00:40 进 hi，10:01:00 出
    _upload(client, "T7", [
        ("a", "2026-09-30T10:00:00Z", -50, 50),
        ("b", "2026-09-30T10:01:20Z", 150, 50),
    ])
    r = _ask(client, "T7", limits={"lo": 3.0, "hi": 2.0})
    body = r.json()
    # 前段限速 3：2.5 不超速；重叠段限速 2：超速 20s
    assert len(body["segments"]) == 1
    seg = body["segments"][0]
    assert (seg["start"], seg["end"]) == (
        "2026-09-30T10:00:40Z", "2026-09-30T10:01:00Z",
    )
    assert seg["speed_mps"] == 2.5
    assert seg["speed_limit_mps"] == 2.0
    assert seg["zone_ids"] == ["hi"]
    assert seg["overspeed_distance_m"] == 10.0
    assert body["total_duration_sec"] == 20.0
    assert body["total_overspeed_distance_m"] == 10.0


def test_overlap_min_limit_with_tied_zone_ids(client):
    """重叠区域按最低限速；多区并列最低限速时全部列出。"""
    register_square(client, zone_id="za")
    assert client.post("/api/v1/zones", json={
        "zone_id": "zb",
        "exterior": [[50, 0], [100, 0], [100, 100], [50, 100]],
        "holes": [], "windows": DAY,
    }).status_code == 201
    _upload(client, "T8", [
        ("a", "2026-09-30T10:00:00Z", -50, 50),
        ("b", "2026-09-30T10:01:20Z", 150, 50),
    ])
    r = _ask(client, "T8", limits={"za": 1.0, "zb": 1.0})
    segs = r.json()["segments"]
    assert [(s["start"], s["end"], s["zone_ids"]) for s in segs] == [
        ("2026-09-30T10:00:20Z", "2026-09-30T10:00:40Z", ["za"]),
        ("2026-09-30T10:00:40Z", "2026-09-30T10:01:00Z", ["za", "zb"]),
    ]
    # 并列区集合不同 => 不合并；超速距离两段各 30m
    assert [s["overspeed_distance_m"] for s in segs] == [30.0, 30.0]
    assert r.json()["total_overspeed_distance_m"] == 60.0


# ---------------------------------------------------------------------
# 最大采样间隔：超上限整段无覆盖
# ---------------------------------------------------------------------

def test_sample_interval_over_limit_whole_segment_uncovered(client):
    """相邻点间隔严格大于上限：整段无覆盖，不插值、不判超速。"""
    register_square(client, zone_id="sp")
    # a->b 间隔 90s，横穿方块；b->c 间隔 30s，仅在区外
    _upload(client, "TG", [
        ("a", "2026-09-30T10:00:00Z", 0, 50),
        ("b", "2026-09-30T10:01:30Z", 100, 50),  # 100m/90s
        ("c", "2026-09-30T10:02:00Z", 150, 50),
    ])
    r = _ask(client, "TG", limits={"sp": 0.5}, gap=60.0)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["segments"] == []
    assert body["total_duration_sec"] == 0
    assert body["total_overspeed_distance_m"] == 0


def test_sample_interval_equal_limit_is_covered(client):
    """相邻点间隔恰好等于上限：仍有覆盖（仅严格大于才视为无覆盖）。"""
    register_square(client, zone_id="sp")
    _upload(client, "TE", [
        ("a", "2026-09-30T10:00:00Z", 0, 50),
        ("b", "2026-09-30T10:01:40Z", 100, 50),  # 间隔恰好 100s
    ])
    r = _ask(client, "TE", limits={"sp": 0.5}, gap=100.0)
    body = r.json()
    assert len(body["segments"]) == 1
    assert (body["segments"][0]["start"], body["segments"][0]["end"]) == (
        "2026-09-30T10:00:00Z", "2026-09-30T10:01:40Z",
    )
    assert body["segments"][0]["speed_mps"] == 1.0


def test_gap_boundary_clips_violation_at_resume(client):
    """超上限段不覆盖；恢复采样后的相邻段照常核算。"""
    register_square(client, zone_id="sp")
    # a->b：间隔 600s（超上限，整段无覆盖）；b->c：100m/50s=2m/s
    _upload(client, "TR", [
        ("a", "2026-09-30T10:00:00Z", -100, 50),
        ("b", "2026-09-30T10:10:00Z", 0, 50),
        ("c", "2026-09-30T10:10:50Z", 100, 50),
    ])
    r = _ask(client, "TR", limits={"sp": 1.0}, gap=300.0,
             since="2026-09-30T09:00:00Z", until="2026-09-30T11:00:00Z")
    body = r.json()
    assert len(body["segments"]) == 1
    seg = body["segments"][0]
    assert seg["start"] == "2026-09-30T10:10:00Z"
    assert seg["end"] == "2026-09-30T10:10:50Z"
    assert seg["speed_mps"] == 2.0
    assert seg["overspeed_distance_m"] == 50.0


# ---------------------------------------------------------------------
# 沿用带洞边界、生效窗口与零时长接触规则
# ---------------------------------------------------------------------

def test_hole_splits_violation_segments(client):
    """穿洞（含洞边界）时间不计入：超速段被洞切成两段且不合并。"""
    register_square(client, zone_id="hh", holes=[[[30, 30], [70, 30], [70, 70], [30, 70]]])
    # 1m/s 沿 y=50：区内 [0,30) 与 (70,100] 共 60s
    _upload(client, "TH", [
        ("h1", "2026-09-30T10:00:00Z", 0, 50),
        ("h2", "2026-09-30T10:01:40Z", 100, 50),
    ])
    r = _ask(client, "TH", limits={"hh": 0.5}, gap=100.0)
    body = r.json()
    assert [(s["start"], s["end"]) for s in body["segments"]] == [
        ("2026-09-30T10:00:00Z", "2026-09-30T10:00:30Z"),
        ("2026-09-30T10:01:10Z", "2026-09-30T10:01:40Z"),
    ]
    assert body["total_duration_sec"] == 60.0
    assert body["total_overspeed_distance_m"] == 30.0


def test_zone_window_gap_not_counted(client):
    """区域生效窗口外的时间不计入。"""
    register_square(
        client, zone_id="ww",
        windows=[{"start": "2026-09-30T10:00:20Z", "end": "2026-09-30T10:00:40Z"}],
    )
    _upload(client, "TW", [
        ("a", "2026-09-30T10:00:00Z", -50, 50),
        ("b", "2026-09-30T10:01:00Z", 100, 50),
    ])
    r = _ask(client, "TW", limits={"ww": 1.0})
    body = r.json()
    assert [(s["start"], s["end"]) for s in body["segments"]] == [
        ("2026-09-30T10:00:20Z", "2026-09-30T10:00:40Z"),
    ]
    assert body["total_duration_sec"] == 20.0


def test_zero_duration_contact_not_counted(client):
    """仅瞬时擦过角点：无违规。"""
    register_square(client, zone_id="tt")
    _upload(client, "TT", [
        ("t1", "2026-09-30T10:00:00Z", -10, 10),
        ("t2", "2026-09-30T10:00:10Z", 10, -10),
    ])
    r = _ask(client, "TT", limits={"tt": 0.1})
    body = r.json()
    assert body["segments"] == []
    assert body["total_duration_sec"] == 0
    assert body["total_overspeed_distance_m"] == 0


def test_outside_zone_speed_irrelevant(client):
    """全程在区外：区内无暴露，再快也不计。"""
    register_square(client, zone_id="oo")
    _upload(client, "TO", [
        ("a", "2026-09-30T10:00:00Z", 200, 200),
        ("b", "2026-09-30T10:00:01Z", 400, 200),  # 200m/s
    ])
    r = _ask(client, "TO", limits={"oo": 1.0})
    body = r.json()
    assert body["segments"] == []
    assert body["total_overspeed_distance_m"] == 0


# ---------------------------------------------------------------------
# 修订号与轨迹版本复现
# ---------------------------------------------------------------------

def test_pinned_revision_reproduces(client):
    # rev1: 100x100；rev2: 放大为 200x200
    register_square(client, zone_id="rv")
    assert client.post("/api/v1/zones", json={
        "zone_id": "rv",
        "exterior": [[0, 0], [200, 0], [200, 200], [0, 200]],
        "holes": [], "windows": DAY,
    }).status_code == 201
    _upload(client, "TRV", [
        ("a", "2026-09-30T10:00:00Z", -50, 50),
        ("b", "2026-09-30T10:02:30Z", 250, 50),  # 300m/150s=2m/s
    ])
    r1 = _ask(client, "TRV", limits={"rv": 1.0}, revision=1)
    b1 = r1.json()
    assert b1["revision"] == 1
    # rev1 下 x∈[0,100]：25s..75s => 超速 50s，超速距离 50m
    assert b1["segments"][0]["start"] == "2026-09-30T10:00:25Z"
    assert b1["segments"][-1]["end"] == "2026-09-30T10:01:15Z"
    assert b1["total_duration_sec"] == 50.0
    assert b1["total_overspeed_distance_m"] == 50.0

    r2 = _ask(client, "TRV", limits={"rv": 1.0})
    b2 = r2.json()
    assert "revision" not in b2
    # 最新（200 宽）：x∈[0,200]：25s..125s => 100s
    assert b2["total_duration_sec"] == 100.0


def test_pinned_track_version_reproduces(client):
    register_square(client, zone_id="tv")
    _upload(client, "TTV", [
        ("a", "2026-09-30T10:00:00Z", 0, 50),
        ("b", "2026-09-30T10:01:40Z", 100, 50),  # 版本1：1m/s
    ])
    # 更正：末点改到 (300,50)，当前点集段速 3m/s
    assert client.post("/api/v1/trips/TTV/corrections", json={
        "base_version": 1,
        "reason": "现场复核",
        "items": [{"action": "update", "point_id": "b",
                   "ts": "2026-09-30T10:01:40Z", "x": 300, "y": 50}],
    }).status_code == 200

    r_latest = _ask(client, "TTV", limits={"tv": 2.0})
    assert "track_version" not in r_latest.json()
    # 当前段速 3m/s，区内前 100/3 秒：超速距离 (3-2)*100/3
    assert r_latest.json()["total_overspeed_distance_m"] == round(100.0 / 3.0, 6)

    r_v1 = _ask(client, "TTV", limits={"tv": 0.5}, track_version=1)
    body = r_v1.json()
    assert body["track_version"] == 1
    assert body["point_count"] == 2
    assert body["total_duration_sec"] == 100.0
    assert body["total_overspeed_distance_m"] == 50.0


def test_revision_and_track_version_together(client):
    register_square(client, zone_id="both")
    _upload(client, "TB", CROSS)
    r = _ask(client, "TB", limits={"both": 1.0}, revision=1, track_version=1)
    body = r.json()
    assert body["revision"] == 1
    assert body["track_version"] == 1
    assert body["total_duration_sec"] == 40.0
    assert body["total_overspeed_distance_m"] == 60.0


# ---------------------------------------------------------------------
# 明确拒绝：未知资源 / 非法输入
# ---------------------------------------------------------------------

def test_unknown_trip_rejected(client):
    register_square(client, zone_id="unk")
    r = _ask(client, "ghost", limits={"unk": 1.0})
    assert r.status_code == 404
    assert "车次不存在" in r.json()["detail"]


def test_unknown_zone_rejected(client):
    _upload(client, "TU", CROSS)
    r = _ask(client, "TU", limits={"ghost-zone": 1.0})
    assert r.status_code == 404
    assert "危险区不存在" in r.json()["detail"]


def test_some_unknown_zone_rejected(client):
    register_square(client, zone_id="known")
    _upload(client, "TU2", CROSS)
    r = _ask(client, "TU2", limits={"known": 1.0, "nope": 2.0})
    assert r.status_code == 404
    assert "nope" in r.json()["detail"]


def test_revision_out_of_range_rejected(client):
    register_square(client, zone_id="rr")
    _upload(client, "TRR", CROSS)
    r = _ask(client, "TRR", limits={"rr": 1.0}, revision=999)
    assert r.status_code == 404
    assert "修订号" in r.json()["detail"]


def test_zone_not_registered_at_revision_rejected(client):
    register_square(client, zone_id="early")
    assert client.post("/api/v1/zones", json={
        "zone_id": "late", "exterior": SQUARE, "holes": [], "windows": DAY,
    }).status_code == 201
    _upload(client, "TL", CROSS)
    r = _ask(client, "TL", limits={"late": 1.0}, revision=1)
    assert r.status_code == 404
    assert "尚未登记" in r.json()["detail"]


def test_track_version_out_of_range_rejected(client):
    register_square(client, zone_id="tvx")
    _upload(client, "TTX", CROSS)
    r = _ask(client, "TTX", limits={"tvx": 1.0}, track_version=9)
    assert r.status_code == 404
    r = _ask(client, "TTX", limits={"tvx": 1.0}, track_version=0)
    assert r.status_code == 404


def test_invalid_time_range_rejected(client):
    register_square(client, zone_id="bad-t")
    _upload(client, "TBT", CROSS)
    r = _ask(client, "TBT", limits={"bad-t": 1.0},
             since="2026-09-30T10:05:00Z", until="2026-09-30T10:00:00Z")
    assert r.status_code == 400
    assert "since" in r.json()["detail"]


def test_naive_time_rejected(client):
    register_square(client, zone_id="naive-sp")
    _upload(client, "TN", CROSS)
    r = client.post("/api/v1/trips/TN/speeding", json={
        "since": "2026-09-30T10:00:00",
        "until": "2026-09-30T10:10:00Z",
        "zone_speed_limits": {"naive-sp": 1.0},
        "max_sample_interval_sec": 1.0,
    })
    assert r.status_code in (400, 422)


def test_empty_zone_limits_rejected(client):
    _upload(client, "TE", CROSS)
    r = client.post("/api/v1/trips/TE/speeding", json={
        "since": "2026-09-30T10:00:00Z",
        "until": "2026-09-30T10:10:00Z",
        "zone_speed_limits": {},
        "max_sample_interval_sec": 1.0,
    })
    assert r.status_code in (400, 422)


def test_non_positive_speed_limits_rejected(client):
    _upload(client, "TF", CROSS)
    for bad in (0, -1, "NaN", "Infinity", "-Infinity", True):
        r = client.post("/api/v1/trips/TF/speeding", json={
            "since": "2026-09-30T10:00:00Z",
            "until": "2026-09-30T10:10:00Z",
            "zone_speed_limits": {"sp": bad},
            "max_sample_interval_sec": 1.0,
        })
        assert r.status_code in (400, 422), bad


def test_non_positive_sample_interval_rejected(client):
    _upload(client, "TI", CROSS)
    for bad in (0, -5, "NaN", "Infinity"):
        r = client.post("/api/v1/trips/TI/speeding", json={
            "since": "2026-09-30T10:00:00Z",
            "until": "2026-09-30T10:10:00Z",
            "zone_speed_limits": {"sp": 1.0},
            "max_sample_interval_sec": bad,
        })
        assert r.status_code in (400, 422), bad


def test_extra_field_rejected(client):
    _upload(client, "TX", CROSS)
    r = client.post("/api/v1/trips/TX/speeding", json={
        "since": "2026-09-30T10:00:00Z",
        "until": "2026-09-30T10:10:00Z",
        "zone_speed_limits": {"sp": 1.0},
        "max_sample_interval_sec": 1.0,
        "unexpected": 1,
    })
    assert r.status_code == 422


def test_missing_field_rejected(client):
    r = client.post("/api/v1/trips/TX/speeding", json={
        "since": "2026-09-30T10:00:00Z",
        "until": "2026-09-30T10:10:00Z",
        "zone_speed_limits": {"sp": 1.0},
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------
# 同一已提交读视图（并发下结果确定一致）
# ---------------------------------------------------------------------

def test_concurrent_updates_read_view_consistent(client):
    assert client.post("/api/v1/zones", json={
        "zone_id": "live", "exterior": SQUARE, "holes": [],
        "windows": [{"start": "2026-09-30T00:00:00Z", "end": "2026-10-02T00:00:00Z"}],
    }).status_code == 201
    client.post("/api/v1/trips/live-trip/points", json=_batch([
        ("a", "2026-09-30T10:00:00Z", -50, 50),
        ("b", "2026-09-30T10:01:20Z", 150, 50),  # 2.5m/s
    ]))

    stop = threading.Event()
    failures = []

    def writer():
        i = 0
        while not stop.is_set():
            size = 100 + (i % 4) * 40
            client.post("/api/v1/zones", json={
                "zone_id": "live",
                "exterior": [[0, 0], [size, 0], [size, size], [0, size]],
                "holes": [],
                "windows": [{"start": "2026-09-30T00:00:00Z",
                             "end": "2026-10-02T00:00:00Z"}],
            })
            i += 1

    def reader():
        while not stop.is_set():
            body = {
                "since": "2026-09-30T09:00:00Z",
                "until": "2026-09-30T11:00:00Z",
                "zone_speed_limits": {"live": 1.0},
                "max_sample_interval_sec": 1000.0,
            }
            r1 = client.post("/api/v1/trips/live-trip/speeding", json=body)
            r2 = client.post("/api/v1/trips/live-trip/speeding", json=body)
            if r1.status_code != 200 or r2.status_code != 200:
                failures.append(("status", r1.status_code, r2.status_code))
                return
            b1, b2 = r1.json(), r2.json()
            # 同快照语义：各段超速距离之和等于总量；总量为自洽的合法值
            for b in (b1, b2):
                assert abs(
                    sum(s["overspeed_distance_m"] for s in b["segments"])
                    - b["total_overspeed_distance_m"]
                ) < 1e-4
                assert abs(
                    sum(s["duration_sec"] for s in b["segments"])
                    - b["total_duration_sec"]
                ) < 1e-4
            if b1 == b2:
                continue
            # 允许恰好跨越一次提交：两次结果都必须自洽且为正
            if not (b1["total_duration_sec"] >= 0 and b2["total_duration_sec"] >= 0):
                failures.append(("neg", b1["total_duration_sec"], b2["total_duration_sec"]))
                return

    threads = [threading.Thread(target=writer),
               threading.Thread(target=reader), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    stop.wait(timeout=1.0)
    stop.set()
    for t in threads:
        t.join(timeout=10)
    assert not failures
