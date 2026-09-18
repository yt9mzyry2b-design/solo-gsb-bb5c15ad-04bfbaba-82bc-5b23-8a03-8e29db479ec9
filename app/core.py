"""公交刷卡票价核验核心逻辑。

纯标准库实现：文本解析、记录校验、进出站配对、
换乘抵扣与运营日封顶计算。不依赖 FastAPI / SQLite，便于单独测试。

金额在内部统一以「元」的浮点数表示并保留两位小数；
存储层写入 SQLite 时转换为整数分，避免二进制浮点误差。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

# 允许的文本分隔符：逗号、制表符、分号、空白(多空格)、全角逗号
_SPLIT_RE = re.compile(r"[,\t;，]|\s+")

# 日期与时刻之间可能用空格连接（如 "2026-09-18 08:00:00"），
# 而空格本身又是字段分隔符，切分前先把时间戳内部空白替换为 T。
# 形如：YYYY[-/]MM[-/]DD HH:MM[:SS] 或 MM[-/]DD HH:MM
_DATETIME_RE = re.compile(
    r"\d{2,4}[-/]\d{1,2}(?:[-/]\d{1,2})?[ \t]+\d{1,2}:\d{2}(?::\d{2})?"
)

# 支持的进站时刻格式
_TIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%dT%H:%M:%S",
    "%Y/%m/%dT%H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%m-%dT%H:%M",       # 缺省年份，补当前年
    "%m-%d %H:%M",
    "%m/%dT%H:%M",
    "%m/%d %H:%M",
)

ACTION_IN = "IN"
ACTION_OUT = "OUT"
_ACTION_ALIASES = {
    "IN": "IN", "进": "IN", "进站": "IN", "入": "IN", "上": "IN",
    "OUT": "OUT", "出": "OUT", "出站": "OUT", "下": "OUT",
}


def _err(code: str, message: str, line: Optional[int] = None,
         card: Optional[str] = None, time: Optional[str] = None,
         station: Optional[str] = None) -> dict:
    """构造定位错误对象。行号 / 卡号 / 时刻 / 站点尽量补齐，便于定位。"""
    loc: dict = {}
    if line is not None:
        loc["line"] = line
    if card:
        loc["card"] = card
    if time:
        loc["time"] = time
    if station:
        loc["station"] = station
    e: dict = {"code": code, "message": message}
    if loc:
        e["location"] = loc
    return e


def _split(line: str) -> list[str]:
    """按分隔符切分一行文本，去掉空白段。"""
    return [p for p in _SPLIT_RE.split(line.strip()) if p != ""]


def _round2(x: float) -> float:
    return round(float(x) + 1e-9, 2)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Station:
    name: str
    zone: int
    line: int = 1  # 保存时的原始行号，用于错误定位


@dataclass
class Fare:
    from_zone: int
    to_zone: int
    price: float
    line: int = 1


@dataclass
class Swipe:
    card: str
    t: datetime
    station: str
    action: str          # IN / OUT
    line: int = 1        # 文本行号（按卡号排序校验时用于定位）

    @property
    def time_text(self) -> str:
        return self.t.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Config:
    transfer_minutes: int = 90          # 换乘有效间隔（分钟）
    transfer_discount: float = 1.0      # 换乘抵扣额（元）
    daily_cap: float = 10.0             # 每卡每运营日封顶额（元）
    day_split_hour: int = 4             # 运营日分界时刻


@dataclass
class VerifyResult:
    ok: bool
    bills: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 文本解析（录入）
# ---------------------------------------------------------------------------

def parse_time(text: str, default_year: Optional[int] = None) -> datetime:
    """解析刷卡时刻，支持多种常见格式；失败抛 ValueError。"""
    text = text.strip()
    for fmt in _TIME_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            dt = dt.replace(year=default_year or datetime.now().year)
        return dt
    raise ValueError(f"无法识别的时刻格式：{text}（示例 2026-09-18 08:00:00）")


def _parse_int(text: str, label: str) -> int:
    try:
        v = int(str(text).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须是整数：{text!r}")
    return v


def _parse_amount(text: str, label: str = "金额") -> float:
    try:
        v = float(str(text).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须是数字：{text!r}")
    if math.isnan(v) or math.isinf(v) or v < 0:
        raise ValueError(f"{label}必须是不小于 0 的数字：{text!r}")
    return _round2(v)


def parse_stations_text(text: str) -> tuple[list[Station], list[dict]]:
    """站点分区文本，每行：站点名, 分区号。"""
    stations: list[Station] = []
    errors: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = _split(line)
        if len(parts) != 2:
            errors.append(_err("STATION_BAD_FIELDS",
                               f"站点行应为「站点名, 分区号」，实际 {len(parts)} 段：{line}",
                               line=i))
            continue
        name, zone_text = parts
        if name in seen:
            errors.append(_err("STATION_DUPLICATE",
                               f"站点 {name} 重复定义", line=i, station=name))
            continue
        try:
            zone = _parse_int(zone_text, "分区号")
        except ValueError as ex:
            errors.append(_err("STATION_BAD_ZONE", str(ex), line=i, station=name))
            continue
        stations.append(Station(name=name, zone=zone, line=i))
        seen.add(name)
    return stations, errors


def parse_fares_text(text: str) -> tuple[list[Fare], list[dict]]:
    """票价矩阵文本，每行：起始分区, 到达分区, 票价。"""
    fares: list[Fare] = []
    errors: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = _split(line)
        if len(parts) != 3:
            errors.append(_err("FARE_BAD_FIELDS",
                               f"票价行应为「起始分区, 到达分区, 票价」，实际 {len(parts)} 段：{line}",
                               line=i))
            continue
        fz_text, tz_text, price_text = parts
        try:
            fz = _parse_int(fz_text, "起始分区")
            tz = _parse_int(tz_text, "到达分区")
            price = _parse_amount(price_text, "票价")
        except ValueError as ex:
            errors.append(_err("FARE_BAD_VALUE", str(ex), line=i))
            continue
        key = (fz, tz)
        if key in seen:
            errors.append(_err("FARE_DUPLICATE",
                               f"分区 {fz}->{tz} 的票价重复定义", line=i))
            continue
        fares.append(Fare(from_zone=fz, to_zone=tz, price=price, line=i))
        seen.add(key)
    return fares, errors


def parse_records_text(text: str, default_year: Optional[int] = None
                       ) -> tuple[list[Swipe], list[dict]]:
    """刷卡记录文本，每行：卡号, 时刻, 站点, 进/出。

    保存阶段只做行格式解析；同卡配对、时间逆序、重复刷卡等
    跨记录的业务校验统一在 verify() 中完成，保证错误一次性报全。
    """
    records: list[Swipe] = []
    errors: list[dict] = []
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 粘合时间戳内部的空白，避免被当作字段分隔符
        line = _DATETIME_RE.sub(lambda m: re.sub(r"[ \t]+", "T", m.group(0)), line)
        parts = _split(line)
        if len(parts) != 4:
            errors.append(_err("RECORD_BAD_FIELDS",
                               f"记录行应为「卡号, 时刻, 站点, 进/出」，实际 {len(parts)} 段：{line}",
                               line=i))
            continue
        card, time_text, station, action_text = parts
        card = card.strip()
        if not card:
            errors.append(_err("RECORD_BAD_CARD", "卡号不能为空", line=i))
            continue
        try:
            t = parse_time(time_text, default_year=default_year)
        except ValueError as ex:
            errors.append(_err("RECORD_BAD_TIME", str(ex),
                               line=i, card=card, station=station))
            continue
        action = _ACTION_ALIASES.get(action_text.upper())
        if action is None:
            errors.append(_err("RECORD_BAD_ACTION",
                               f"进出方向必须是 IN/进 或 OUT/出，实际：{action_text!r}",
                               line=i, card=card, time=time_text, station=station))
            continue
        records.append(Swipe(card=card, t=t, station=station,
                             action=action, line=i))
    return records, errors


# ---------------------------------------------------------------------------
# 运营日
# ---------------------------------------------------------------------------

def operating_day(t: datetime, split_hour: int = 4) -> str:
    """以某日 split_hour 点为界归属运营日。

    04:00 之前（含 00:00-03:59:59）归属前一自然日；
    04:00 及之后归属当日。例如 2026-09-18 03:30 属于 2026-09-17 运营日。
    """
    if t.hour < split_hour:
        t = t - timedelta(days=1)
    return t.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 配置校验
# ---------------------------------------------------------------------------

def validate_config(cfg: Config) -> list[dict]:
    errors: list[dict] = []
    if cfg.transfer_minutes <= 0:
        errors.append(_err("CONFIG_BAD", "换乘有效间隔必须为正整数（分钟）"))
    if cfg.transfer_discount < 0:
        errors.append(_err("CONFIG_BAD", "换乘抵扣额不能为负"))
    if cfg.daily_cap < 0:
        errors.append(_err("CONFIG_BAD", "每日封顶额不能为负"))
    if not (0 <= cfg.day_split_hour <= 23):
        errors.append(_err("CONFIG_BAD", "运营日分界时刻必须在 0-23 之间"))
    return errors


# ---------------------------------------------------------------------------
# 核验 + 计价
# ---------------------------------------------------------------------------

def verify(stations: list[Station],
           fares: list[Fare],
           records: list[Swipe],
           cfg: Config) -> VerifyResult:
    """全量核验并计价。

    原子语义：任何记录级错误（缺出站、缺进站、时间逆序、重复刷卡、
    未知站点、矩阵缺票价）都会使整批核验失败，不生成任何账单。
    只有全部通过时才一次性产出全部行程账单。
    """
    errors: list[dict] = []
    errors.extend(validate_config(cfg))

    zone_of = {s.name: s.zone for s in stations}
    fare_of: dict[tuple[int, int], float] = {
        (f.from_zone, f.to_zone): f.price for f in fares
    }

    # 1) 站点引用检查（未知站点）
    for r in records:
        if r.station not in zone_of:
            errors.append(_err(
                "UNKNOWN_STATION",
                f"刷卡站点 {r.station} 未在站点分区中定义",
                line=r.line, card=r.card,
                time=r.time_text, station=r.station))

    # 2) 按卡号分组，时间升序，逐卡做重复刷卡 / 时间逆序 / 进出站结构检查
    by_card: dict[str, list[Swipe]] = {}
    for r in records:
        by_card.setdefault(r.card, []).append(r)

    trips: list[tuple[str, int, Swipe, Swipe]] = []
    # trips: (卡号, 卡内序号(从1), 进站记录, 出站记录)

    for card, rs in by_card.items():
        # 录入顺序（数据库按 id 返回，即文本录入先后）必须构成
        # 单调不减的时间流：下降=时间逆序；同卡同时刻=重复刷卡。
        ordered = True
        seen_times: dict[datetime, Swipe] = {}
        for r in rs:
            first = seen_times.get(r.t)
            if first is not None:
                errors.append(_err(
                    "DUPLICATE_SWIPE",
                    f"卡号 {card} 在 {r.time_text} 重复刷卡"
                    f"（第 {r.line} 行与第 {first.line} 行时刻完全相同）",
                    line=r.line, card=card, time=r.time_text,
                    station=r.station))
                ordered = False
            else:
                seen_times[r.t] = r
        for a, b in zip(rs, rs[1:]):
            if b.t < a.t:
                errors.append(_err(
                    "TIME_REVERSED",
                    f"卡号 {card} 刷卡时间逆序：第 {b.line} 行 {b.time_text} "
                    f"早于第 {a.line} 行 {a.time_text}",
                    line=b.line, card=card, time=b.time_text,
                    station=b.station))
                ordered = False

        # 存在逆序/重复时，进出站配对不可靠，跳过该卡结构检查
        # （结构异常很可能是乱序的假象，避免误报叠加）。
        if not ordered:
            continue

        # 时间流有序时录入顺序即时间顺序；逐对做进出站结构检查。
        rs_sorted = rs

        # 3) 进出站交替结构检查并配对。
        #    期望从进站开始，进出成对；起始为出站=缺进站，末尾落单=缺出站。
        i, seq = 0, 0
        while i < len(rs_sorted):
            cur = rs_sorted[i]
            if cur.action != ACTION_IN:
                errors.append(_err(
                    "MISSING_ENTRY",
                    f"卡号 {card} 的出站刷卡缺少配对进站"
                    f"（{cur.time_text} @ {cur.station}）",
                    line=cur.line, card=card,
                    time=cur.time_text, station=cur.station))
                i += 1
                continue
            if i + 1 >= len(rs_sorted):
                errors.append(_err(
                    "MISSING_EXIT",
                    f"卡号 {card} 进站后缺失出站"
                    f"（{cur.time_text} @ {cur.station}）",
                    line=cur.line, card=card,
                    time=cur.time_text, station=cur.station))
                i += 1
                continue
            nxt = rs_sorted[i + 1]
            if nxt.action != ACTION_OUT:
                errors.append(_err(
                    "MISSING_EXIT",
                    f"卡号 {card} 在 {cur.time_text} @ {cur.station} 进站后，"
                    f"下一条不是出站（第 {nxt.line} 行又是进站）",
                    line=cur.line, card=card,
                    time=cur.time_text, station=cur.station))
                i += 1
                continue
            seq += 1
            trips.append((card, seq, cur, nxt))
            i += 2

    # 4) 票价矩阵覆盖检查（仅对结构完整、站点已知的行程）
    for card, seq, sw_in, sw_out in trips:
        z1 = zone_of.get(sw_in.station)
        z2 = zone_of.get(sw_out.station)
        if z1 is None or z2 is None:
            continue
        if (z1, z2) not in fare_of:
            errors.append(_err(
                "MISSING_FARE",
                f"票价矩阵缺少分区 {z1}->{z2}（{sw_in.station} -> "
                f"{sw_out.station}）的票价",
                line=sw_in.line, card=card, time=sw_in.time_text,
                station=sw_in.station))

    if errors:
        # 原子失败：不生成任何账单（无部分账单）
        return VerifyResult(ok=False, bills=[], errors=errors)

    # 5) 计价：按 (卡号, 进站时刻) 顺序处理。
    #    - 换乘抵扣看「同卡前次出站 -> 本次进站」的真实间隔（跨运营日也算）；
    #    - 封顶按行程进站时刻所属运营日、分卡累计。
    per_card_prev_out: dict[str, Swipe] = {}
    # 每卡每运营日累计实扣：card -> {day: amount}
    cumulative: dict[str, dict[str, float]] = {}

    bills: list[dict] = []
    ordered_trips = sorted(trips, key=lambda x: (x[0], x[2].t))

    for card, _seq, sw_in, sw_out in ordered_trips:
        z1 = zone_of[sw_in.station]
        z2 = zone_of[sw_out.station]
        base = _round2(fare_of[(z1, z2)])
        day = operating_day(sw_in.t, cfg.day_split_hour)

        discount = 0.0
        source = "无"
        prev_out = per_card_prev_out.get(card)
        if prev_out is not None:
            gap_min = (sw_in.t - prev_out.t).total_seconds() / 60.0
            if 0 <= gap_min <= cfg.transfer_minutes:
                discount = _round2(min(cfg.transfer_discount, base))
                source = (f"换乘优惠（距上次出站 {int(round(gap_min))} 分钟，"
                          f"≤{cfg.transfer_minutes} 分钟，抵扣 {discount:.2f} 元）")

        after_discount = _round2(base - discount)

        card_days = cumulative.setdefault(card, {})
        already = _round2(card_days.get(day, 0.0))
        cap_room = _round2(cfg.daily_cap - already)
        charge = _round2(max(0.0, min(after_discount, cap_room)))
        if cap_room <= 0:
            source = (f"{source}；当日已封顶" if discount > 0
                      else f"当日封顶（已累计 {already:.2f} 元，达到 "
                           f"{cfg.daily_cap:.2f} 元）")
        elif charge < after_discount:
            source = (f"{source}；封顶保护" if discount > 0
                      else f"封顶保护（当日封顶 {cfg.daily_cap:.2f} 元）")

        card_days[day] = _round2(already + charge)
        per_card_prev_out[card] = sw_out

        bills.append({
            "card": card,
            "seq": None,  # 下方按全局展示顺序统一编号
            "operating_day": day,
            "entry_time": sw_in.time_text,
            "exit_time": sw_out.time_text,
            "entry_station": sw_in.station,
            "exit_station": sw_out.station,
            "from_zone": z1,
            "to_zone": z2,
            "base_fare": base,
            "discount": discount,
            "discount_source": source,
            "charged_fare": charge,
            "daily_cumulative": card_days[day],
            "daily_cap": _round2(cfg.daily_cap),
            "entry_line": sw_in.line,
            "exit_line": sw_out.line,
        })

    # 同一卡号内按进站顺序给出展示序号
    seq_by_card: dict[str, int] = {}
    for b in bills:
        seq_by_card[b["card"]] = seq_by_card.get(b["card"], 0) + 1
        b["seq"] = seq_by_card[b["card"]]

    return VerifyResult(ok=True, bills=bills, errors=[])
