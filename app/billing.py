"""公交刷卡票价核验核心逻辑（与框架无关，便于测试）。

金额一律以整数「分」表示，避免浮点误差。
运营日：以进站时刻为准，当日 04:00 之前的行程归属前一日运营日。
换乘：同卡「前次出站」与「本次进站」间隔 <= TRANSFER_WINDOW_MIN 分钟时，
      在基础票价上先抵扣换乘优惠，再套用当日封顶。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

IN = "IN"   # 进站
OUT = "OUT"  # 出站

TRANSFER_WINDOW_MIN = 90
DAY_BOUNDARY_HOUR = 4


@dataclass(frozen=True)
class Swipe:
    """一条刷卡记录。seq 为录入顺序（对应数据库行 id）。"""
    seq: int
    card: str
    station: str
    t: datetime
    kind: str  # IN / OUT


@dataclass
class BillingError:
    code: str
    message: str
    swipe_ids: list[int] = field(default_factory=list)


@dataclass
class Trip:
    """一段完整行程（一次进站配对一次出站）。"""
    index: int
    card: str
    in_id: int
    out_id: int
    in_station: str
    out_station: str
    in_zone: int
    out_zone: int
    in_time: datetime
    out_time: datetime
    service_day: str
    base_fare: int          # 基础票价（分）
    discount: int           # 换乘抵扣额（分）
    is_transfer: bool       # 是否享受换乘优惠
    gap_minutes: int | None  # 距前次出站间隔（分钟），首段为 None
    charged: int            # 实扣额（分，封顶后）
    capped: bool            # 是否触发当日封顶
    daily_total: int        # 该卡该运营日累计实扣（分，含本段）


def service_day_of(t: datetime) -> str:
    """按 04:00 划分运营日，返回 YYYY-MM-DD。"""
    d = t.date()
    if t.hour < DAY_BOUNDARY_HOUR:
        d = d - timedelta(days=1)
    return d.isoformat()


def validate(swipes: Iterable[Swipe]) -> list[BillingError]:
    """全量校验刷卡记录。任何错误都将阻止账单生成（不产生部分账单）。

    检查项：
      1. duplicate_tap     完全重复刷卡（同卡、同站、同时、同类型）
      2. time_reversed     同卡刷卡时刻逆序（后录入的记录时刻更早）
      3. kind_conflict     同卡相邻刷卡类型未按 IN、OUT 严格交替
      4. missing_check_in  缺少进站（首刷为出站，或连续两次出站）
      5. missing_check_out 缺少出站（末刷为进站，或连续两次进站）
    """
    swipes = list(swipes)
    errors: list[BillingError] = []

    # 1. 完全重复
    seen: set[tuple] = set()
    for s in swipes:
        key = (s.card, s.station, s.t, s.kind)
        if key in seen:
            errors.append(BillingError(
                "duplicate_tap",
                f"卡号 {s.card} 在 {s.t:%Y-%m-%d %H:%M} 于 {s.station} "
                f"重复{('进站' if s.kind == IN else '出站')}刷卡",
                [s.seq],
            ))
        else:
            seen.add(key)

    by_card: dict[str, list[Swipe]] = {}
    for s in swipes:
        by_card.setdefault(s.card, []).append(s)

    for card, rows in by_card.items():
        rows.sort(key=lambda x: x.seq)  # 录入顺序即配对顺序
        prev: Swipe | None = None
        for s in rows:
            # 2. 时间逆序
            if prev is not None and s.t < prev.t:
                errors.append(BillingError(
                    "time_reversed",
                    f"卡号 {card} 的刷卡时间逆序：记录 #{s.seq}"
                    f"（{s.t:%Y-%m-%d %H:%M}）早于前一条记录 #{prev.seq}"
                    f"（{prev.t:%Y-%m-%d %H:%M}）",
                    [prev.seq, s.seq],
                ))
            # 3/4/5. 进出站配对
            if prev is None or prev.kind == OUT:
                if s.kind == OUT:
                    errors.append(BillingError(
                        "missing_check_in",
                        f"卡号 {card} 在 {s.station} 出站（记录 #{s.seq}）"
                        f"但缺少对应的进站刷卡",
                        [s.seq],
                    ))
            elif prev.kind == IN:
                if s.kind == IN:
                    errors.append(BillingError(
                        "missing_check_out",
                        f"卡号 {card} 连续两次进站：记录 #{prev.seq}"
                        f"（{prev.station}）之后未出站，又于记录 #{s.seq}"
                        f"（{s.station}）进站",
                        [prev.seq, s.seq],
                    ))
            prev = s

        if rows and rows[-1].kind == IN:
            last = rows[-1]
            errors.append(BillingError(
                "missing_check_out",
                f"卡号 {card} 最后一条记录 #{last.seq}（{last.station}，"
                f"{last.t:%Y-%m-%d %H:%M}）为进站，缺少出站刷卡",
                [last.seq],
            ))

    # 合并同一条记录上的重复报错，保持定位稳定
    errors.sort(key=lambda e: (e.swipe_ids[0] if e.swipe_ids else 0, e.code))
    return errors


def _fare(fares: dict[tuple[int, int], int], z1: int, z2: int) -> int | None:
    """票价矩阵查询，方向无关（A->B 与 B->A 同价）。"""
    return fares.get((z1, z2)) if (z1, z2) in fares else fares.get((z2, z1))


def bill(
    swipes: Iterable[Swipe],
    station_zone: dict[str, int],
    fares: dict[tuple[int, int], int],
    transfer_discount: int,
    daily_cap: int,
) -> tuple[list[Trip], list[BillingError]]:
    """核验并生成账单。

    返回 (行程列表, 错误列表)；错误非空时行程列表必为空（不出部分账单）。
    行程先按（运营日、进站时刻）排序；同一运营日内按时间顺序累计封顶。
    """
    swipes = list(swipes)
    errors = validate(swipes)
    if errors:
        return [], errors

    trips: list[Trip] = []
    by_card: dict[str, list[Swipe]] = {}
    for s in swipes:
        by_card.setdefault(s.card, []).append(s)

    for card, rows in by_card.items():
        rows.sort(key=lambda x: x.seq)
        prev_out: Swipe | None = None  # 同卡前次出站
        for i in range(0, len(rows), 2):
            sin, sout = rows[i], rows[i + 1]  # 校验通过后必为 IN/OUT 配对
            z_in = station_zone.get(sin.station)
            z_out = station_zone.get(sout.station)
            if z_in is None or z_out is None:
                missing = sin.station if z_in is None else sout.station
                errors.append(BillingError(
                    "unknown_station",
                    f"站点「{missing}」未设置分区，无法计价"
                    f"（记录 #{sin.seq if z_in is None else sout.seq}）",
                    [sin.seq if z_in is None else sout.seq],
                ))
                continue
            base = _fare(fares, z_in, z_out)
            if base is None:
                errors.append(BillingError(
                    "missing_fare",
                    f"票价矩阵缺少分区 {z_in} ↔ {z_out} 的票价"
                    f"（{sin.station}→{sout.station}，记录 #{sin.seq}）",
                    [sin.seq, sout.seq],
                ))
                continue

            gap_min = None
            if prev_out is not None:
                gap_min = (sin.t - prev_out.t).total_seconds() / 60.0
            is_transfer = gap_min is not None and 0 <= gap_min <= TRANSFER_WINDOW_MIN
            discount = min(transfer_discount, base) if is_transfer else 0

            trips.append(Trip(
                index=0,
                card=card,
                in_id=sin.seq,
                out_id=sout.seq,
                in_station=sin.station,
                out_station=sout.station,
                in_zone=z_in,
                out_zone=z_out,
                in_time=sin.t,
                out_time=sout.t,
                service_day=service_day_of(sin.t),
                base_fare=base,
                discount=discount,
                is_transfer=is_transfer,
                gap_minutes=int(gap_min) if gap_min is not None else None,
                charged=base - discount,  # 先占位，封顶后回写
                capped=False,
                daily_total=0,
            ))
            prev_out = sout

    if errors:
        return [], errors

    # 运营日内按进站时刻排序累计，应用当日封顶
    trips.sort(key=lambda tr: (tr.card, tr.service_day, tr.in_time))
    totals: dict[tuple[str, str], int] = {}
    for idx, tr in enumerate(trips, start=1):
        tr.index = idx
        key = (tr.card, tr.service_day)
        already = totals.get(key, 0)
        tentative = already + tr.charged
        if daily_cap >= 0 and tentative > daily_cap:
            tr.charged = max(0, daily_cap - already)
            tr.capped = True
        totals[key] = already + tr.charged
        tr.daily_total = totals[key]

    return trips, []
