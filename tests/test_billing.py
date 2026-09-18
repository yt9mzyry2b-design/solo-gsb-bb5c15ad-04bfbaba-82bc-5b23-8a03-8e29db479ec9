"""核验逻辑单元测试。金额单位：分。运行：python -m pytest -q"""

from datetime import datetime

import pytest

from app.billing import IN, OUT, Swipe, bill, service_day_of, validate


def dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


def sw(seq, card, station, t, kind):
    return Swipe(seq, card, station, dt(t), kind)


ZONES = {"S1": 1, "S2": 2, "S3": 3}
# 1↔2 = 300, 2↔3 = 300, 1↔3 = 400
FARES = {(1, 2): 300, (2, 3): 300, (1, 3): 400}


def test_service_day_boundary():
    assert service_day_of(dt("2026-09-18 03:59")) == "2026-09-17"
    assert service_day_of(dt("2026-09-18 04:00")) == "2026-09-18"
    assert service_day_of(dt("2026-09-19 00:30")) == "2026-09-18"


def test_simple_trip_no_discount():
    swipes = [sw(1, "C", "S1", "2026-09-18 08:00", IN),
              sw(2, "C", "S2", "2026-09-18 08:30", OUT)]
    trips, errors = bill(swipes, ZONES, FARES, 100, 1000)
    assert errors == []
    assert len(trips) == 1
    t = trips[0]
    assert (t.base_fare, t.discount, t.charged, t.daily_total) == (300, 0, 300, 300)
    assert t.is_transfer is False and t.gap_minutes is None
    assert not t.capped


def test_transfer_discount_within_90_min():
    swipes = [
        sw(1, "C", "S1", "2026-09-18 08:00", IN),
        sw(2, "C", "S2", "2026-09-18 08:30", OUT),       # 300
        sw(3, "C", "S2", "2026-09-18 09:40", IN),        # 距出站 70 分钟
        sw(4, "C", "S3", "2026-09-18 10:10", OUT),       # 300-100=200
    ]
    trips, errors = bill(swipes, ZONES, FARES, 100, 1000)
    assert errors == []
    assert [t.charged for t in trips] == [300, 200]
    assert trips[1].discount == 100
    assert trips[1].gap_minutes == 70 and trips[1].is_transfer
    assert trips[1].daily_total == 500


def test_no_discount_after_90_min_boundary():
    swipes = [
        sw(1, "C", "S1", "2026-09-18 08:00", IN),
        sw(2, "C", "S2", "2026-09-18 08:30", OUT),
        sw(3, "C", "S2", "2026-09-18 10:01", IN),        # 91 分钟，不优惠
        sw(4, "C", "S3", "2026-09-18 10:30", OUT),
    ]
    trips, errors = bill(swipes, ZONES, FARES, 100, 1000)
    assert errors == []
    assert trips[1].discount == 0
    assert trips[1].charged == 300


def test_exactly_90_min_qualifies():
    swipes = [
        sw(1, "C", "S1", "2026-09-18 08:00", IN),
        sw(2, "C", "S2", "2026-09-18 08:30", OUT),
        sw(3, "C", "S2", "2026-09-18 10:00", IN),        # 恰好 90 分钟
        sw(4, "C", "S3", "2026-09-18 10:20", OUT),
    ]
    trips, errors = bill(swipes, ZONES, FARES, 100, 1000)
    assert trips[1].discount == 100


def test_discount_then_daily_cap():
    # 300 + (300-100) + (400-100) = 800；第 4 段 300-100=200 会超 900 封顶
    swipes = [
        sw(1, "C", "S1", "2026-09-18 08:00", IN),
        sw(2, "C", "S2", "2026-09-18 08:30", OUT),
        sw(3, "C", "S2", "2026-09-18 09:00", IN),
        sw(4, "C", "S3", "2026-09-18 09:30", OUT),
        sw(5, "C", "S3", "2026-09-18 10:00", IN),
        sw(6, "C", "S1", "2026-09-18 10:30", OUT),       # 1↔3 = 400
        sw(7, "C", "S1", "2026-09-18 11:00", IN),
        sw(8, "C", "S2", "2026-09-18 11:30", OUT),
    ]
    trips, errors = bill(swipes, ZONES, FARES, 100, 900)
    assert errors == []
    assert [t.charged for t in trips] == [300, 200, 300, 100]
    assert trips[3].capped is True
    assert trips[3].daily_total == 900
    # 封顶后再乘车当日实扣为 0
    swipes += [
        sw(9, "C", "S2", "2026-09-18 12:00", IN),
        sw(10, "C", "S1", "2026-09-18 12:30", OUT),
    ]
    trips, _ = bill(swipes, ZONES, FARES, 100, 900)
    assert trips[4].charged == 0
    assert trips[4].capped is True


def test_cap_resets_per_service_day():
    swipes = [
        sw(1, "C", "S1", "2026-09-18 23:50", IN),
        sw(2, "C", "S2", "2026-09-19 00:20", OUT),       # 运营日 09-18
        sw(3, "C", "S2", "2026-09-19 04:30", IN),        # 运营日 09-19
        sw(4, "C", "S1", "2026-09-19 05:00", OUT),
    ]
    trips, errors = bill(swipes, ZONES, FARES, 100, 350)
    assert [t.service_day for t in trips] == ["2026-09-18", "2026-09-19"]
    assert all(not t.capped for t in trips)
    assert trips[1].charged == 300  # 新运营日不继承累计


def test_missing_check_out():
    swipes = [sw(1, "C", "S1", "2026-09-18 08:00", IN)]
    errors = validate(swipes)
    assert any(e.code == "missing_check_out" and e.swipe_ids == [1] for e in errors)
    trips, errs = bill(swipes, ZONES, FARES, 100, 1000)
    assert trips == [] and errs  # 有错误时绝不出账单


def test_missing_check_in():
    swipes = [sw(1, "C", "S1", "2026-09-18 08:00", OUT)]
    errors = validate(swipes)
    assert any(e.code == "missing_check_in" for e in errors)
    trips, errs = bill(swipes, ZONES, FARES, 100, 1000)
    assert trips == [] and errs


def test_consecutive_in_and_out():
    swipes = [
        sw(1, "C", "S1", "2026-09-18 08:00", IN),
        sw(2, "C", "S2", "2026-09-18 08:30", IN),   # 连续进站
        sw(3, "C", "S2", "2026-09-18 09:00", OUT),
        sw(4, "C", "S1", "2026-09-18 09:30", OUT),  # 连续出站
    ]
    codes = {e.code for e in validate(swipes)}
    assert "missing_check_out" in codes and "missing_check_in" in codes
    trips, errs = bill(swipes, ZONES, FARES, 100, 1000)
    assert trips == [] and errs  # 绝不产生部分账单


def test_time_reversed():
    swipes = [
        sw(1, "C", "S1", "2026-09-18 09:00", IN),
        sw(2, "C", "S2", "2026-09-18 08:30", OUT),
    ]
    errors = validate(swipes)
    assert any(e.code == "time_reversed" and e.swipe_ids == [1, 2] for e in errors)


def test_duplicate_tap():
    swipes = [
        sw(1, "C", "S1", "2026-09-18 08:00", IN),
        sw(2, "C", "S1", "2026-09-18 08:00", IN),
    ]
    errors = validate(swipes)
    assert any(e.code == "duplicate_tap" and e.swipe_ids == [2] for e in errors)


def test_same_station_same_time_different_kind_ok():
    # 同站同时刻但类型不同（进出站）不算重复
    swipes = [
        sw(1, "C", "S1", "2026-09-18 08:00", IN),
        sw(2, "C", "S1", "2026-09-18 08:00", OUT),
    ]
    assert validate(swipes) == []


def test_unknown_station_and_missing_fare():
    swipes = [
        sw(1, "C", "SX", "2026-09-18 08:00", IN),
        sw(2, "C", "S2", "2026-09-18 08:30", OUT),
    ]
    trips, errors = bill(swipes, ZONES, FARES, 100, 1000)
    assert trips == [] and any(e.code == "unknown_station" for e in errors)

    swipes2 = [
        sw(1, "C", "S1", "2026-09-18 08:00", IN),
        sw(2, "C", "S2", "2026-09-18 08:30", OUT),
    ]
    trips, errors = bill(swipes2, ZONES, {(1, 3): 400}, 100, 1000)
    assert trips == [] and any(e.code == "missing_fare" for e in errors)


def test_discount_never_exceeds_fare():
    swipes = [
        sw(1, "C", "S1", "2026-09-18 08:00", IN),
        sw(2, "C", "S2", "2026-09-18 08:30", OUT),
        sw(3, "C", "S2", "2026-09-18 09:00", IN),
        sw(4, "C", "S1", "2026-09-18 09:20", OUT),   # 票价 300，抵扣 500
    ]
    trips, errors = bill(swipes, ZONES, FARES, 500, 10000)
    assert errors == []
    assert trips[1].discount == 300 and trips[1].charged == 0


def test_pairing_independent_between_cards():
    swipes = [
        sw(1, "A", "S1", "2026-09-18 08:00", IN),
        sw(2, "B", "S2", "2026-09-18 08:05", IN),
        sw(3, "A", "S2", "2026-09-18 08:40", OUT),
        sw(4, "B", "S3", "2026-09-18 08:45", OUT),
    ]
    trips, errors = bill(swipes, ZONES, FARES, 100, 1000)
    assert errors == []
    assert sorted(t.card for t in trips) == ["A", "B"]
