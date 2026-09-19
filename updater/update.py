#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Nikkei 33 行业 Daily Update

保证：
1. 抓取 gyoushu.php。
2. 自动发现同站脚本，并从中定位 country_jp_gyo_past.js。
3. 从 past URL 派生 current URL，完整保留 query string。
4. 所有请求共用同一个 requests.Session、Cookie 和 Referer。
5. Gyo[0..32] 仍使用数字索引解析。
6. G1/G2 按标量 JS 字符串解析。
7. G2 点数转换为真实涨跌百分比。
8. past JS 使用独立正则，允许 GY[q]、GY[q++] 等非数字索引表达式。
9. 同一日期保留较晚时间，按日期升序，至少 21 个交易日，最多保留 90 日。
10. 计算 5/20 日涨幅、RS、排名、排名变化、连续强弱和轮动状态。
11. 不伪造资金流，相关字段固定为 null。
12. 只有全部抓取、解析、计算及 HTML 渲染成功后才原子替换 index.html。
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import re
import statistics
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests


ENTRY_URL = os.environ.get(
    "NIKKEI_ENTRY_URL",
    "https://nikkei225jp.com/gyoushu.php",
)

ROOT_DIR = Path(__file__).resolve().parents[1]
INDEX_PATH = Path(
    os.environ.get("INDEX_HTML", str(ROOT_DIR / "index.html"))
).resolve()

TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "30"))
MAX_HISTORY_DAYS = 90
MIN_HISTORY_DAYS = 21
SECTOR_COUNT = 33

# auto:
#   - G2 相对 G1 很小：视为 G1=当前点数、G2=涨跌点数
#   - G1/G2 同量级：视为 G1=前值、G2=当前值
#
# 可显式设为：
#   current_change  : G1=当前点数，G2=涨跌点数
#   previous_change : G1=前值，G2=涨跌点数
#   previous_current: G1=前值，G2=当前点数
CURRENT_VALUE_MODE = os.environ.get(
    "CURRENT_VALUE_MODE",
    "auto",
).strip().lower()

USER_AGENT = os.environ.get(
    "HTTP_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0 Safari/537.36 GitHubActions-DailyUpdate/15",
)

# JS 字符串字面量：支持单引号和双引号。
JS_LITERAL = r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""

DATE_SEARCH_RE = re.compile(
    r"(?P<year>20\d{2})[/-](?P<month>\d{1,2})[/-](?P<day>\d{1,2})"
)
TIME_SEARCH_RE = re.compile(
    r"(?<!\d)(?P<hour>[0-2]?\d):(?P<minute>[0-5]\d)"
    r"(?::(?P<second>[0-5]\d))?(?!\d)"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger("daily-update")


class UpdateError(RuntimeError):
    """预期内的更新失败。"""


@dataclass(frozen=True)
class DailyRow:
    trading_date: date
    time_text: str
    time_seconds: int
    values: Tuple[float, ...]


class ScriptSrcParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: List[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: List[Tuple[str, Optional[str]]],
    ) -> None:
        if tag.lower() != "script":
            return

        attr_map = {
            key.lower(): value
            for key, value in attrs
            if value is not None
        }
        src = attr_map.get("src")
        if src:
            self.sources.append(src.strip())


def response_text(response: requests.Response) -> str:
    """
    requests 对无 charset 的日文文本有时会默认 ISO-8859-1。
    优先尊重服务端明确 charset，否则使用 apparent_encoding。
    """
    content_type = response.headers.get("Content-Type", "")
    has_explicit_charset = "charset=" in content_type.lower()

    if not has_explicit_charset:
        guessed = response.apparent_encoding
        if guessed:
            response.encoding = guessed

    return response.text


def fetch_text(
    session: requests.Session,
    url: str,
    *,
    referer: str,
) -> str:
    LOG.info("GET %s", url)

    response = session.get(
        url,
        headers={"Referer": referer},
        timeout=TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()

    text = response_text(response)
    if not text.strip():
        raise UpdateError(f"响应为空：{url}")

    return text


def discover_same_origin_scripts(
    html: str,
    page_url: str,
) -> List[str]:
    parser = ScriptSrcParser()
    parser.feed(html)

    page_parts = urlsplit(page_url)
    page_origin = (
        page_parts.scheme.lower(),
        page_parts.netloc.lower(),
    )

    result: List[str] = []
    seen = set()

    for source in parser.sources:
        absolute = urljoin(page_url, source)
        parts = urlsplit(absolute)
        origin = (parts.scheme.lower(), parts.netloc.lower())

        if origin != page_origin:
            continue
        if absolute in seen:
            continue

        seen.add(absolute)
        result.append(absolute)

    return result


def derive_current_url(past_url: str) -> str:
    """
    只修改 path，query 和 fragment 原样保留。

    例如：
      /country_jp_gyo_past.js?59661018
    变为：
      /country_jp_gyo.js?59661018
    """
    parts = urlsplit(past_url)
    new_path, replacements = re.subn(
        r"_past(?=\.js$)",
        "",
        parts.path,
        count=1,
        flags=re.IGNORECASE,
    )

    if replacements != 1:
        raise UpdateError(
            f"无法由 past URL 派生 current URL：{past_url}"
        )

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            new_path,
            parts.query,
            parts.fragment,
        )
    )


def decode_js_string(literal: str) -> str:
    """
    解码简单 JS 字符串字面量。

    除 JSON 转义外，也支持 JS 常见的：
      \\xNN
      \\uNNNN
      单引号字符串
      行续接
    """
    if len(literal) < 2 or literal[0] not in ("'", '"'):
        raise UpdateError(f"不是有效的 JS 字符串字面量：{literal[:80]!r}")

    quote = literal[0]
    if literal[-1] != quote:
        raise UpdateError("JS 字符串引号未闭合")

    source = literal[1:-1]
    output: List[str] = []
    index = 0

    simple_escapes = {
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "0": "\0",
        "\\": "\\",
        "/": "/",
        "'": "'",
        '"': '"',
    }

    while index < len(source):
        char = source[index]

        if char != "\\":
            output.append(char)
            index += 1
            continue

        index += 1
        if index >= len(source):
            raise UpdateError("JS 字符串末尾存在孤立反斜杠")

        escaped = source[index]

        # JS 行续接。
        if escaped == "\n":
            index += 1
            continue
        if escaped == "\r":
            index += 1
            if index < len(source) and source[index] == "\n":
                index += 1
            continue

        if escaped in simple_escapes:
            output.append(simple_escapes[escaped])
            index += 1
            continue

        if escaped == "x":
            digits = source[index + 1:index + 3]
            if len(digits) != 2 or not re.fullmatch(r"[0-9A-Fa-f]{2}", digits):
                raise UpdateError("无效的 JS \\x 转义")
            output.append(chr(int(digits, 16)))
            index += 3
            continue

        if escaped == "u":
            digits = source[index + 1:index + 5]
            if len(digits) != 4 or not re.fullmatch(r"[0-9A-Fa-f]{4}", digits):
                raise UpdateError("无效的 JS \\u 转义")
            output.append(chr(int(digits, 16)))
            index += 5
            continue

        # JS 对未知 identity escape 通常保留被转义字符。
        output.append(escaped)
        index += 1

    return "".join(output)


def indexed_assignments(text: str, variable: str) -> Dict[int, str]:
    """
    仅用于 Gyo[0]、Gyo[1] 等确实使用数字索引的数组。

    注意：past GY 不允许调用这个函数，因为真实数据是 GY[q]。
    """
    pattern = re.compile(
        rf"(?<![\w$]){re.escape(variable)}"
        rf"\s*\[\s*(\d+)\s*\]\s*=\s*"
        rf"(?P<literal>{JS_LITERAL})",
        flags=re.MULTILINE,
    )

    result: Dict[int, str] = {}
    for match in pattern.finditer(text):
        index = int(match.group(1))
        result[index] = decode_js_string(match.group("literal"))

    return result


def scalar_string_assignment(text: str, variable: str) -> str:
    pattern = re.compile(
        rf"(?<![\w$]){re.escape(variable)}"
        rf"\s*=\s*(?P<literal>{JS_LITERAL})",
        flags=re.MULTILINE,
    )

    matches = list(pattern.finditer(text))
    if not matches:
        raise UpdateError(f"current JS 中未找到标量字符串 {variable}")

    # 若文件里有初始化和最终赋值，使用最后一次赋值。
    return decode_js_string(matches[-1].group("literal"))


def parse_csv_fields(value: str) -> List[str]:
    try:
        rows = list(csv.reader([value], skipinitialspace=True))
    except csv.Error as exc:
        raise UpdateError(f"CSV 字符串解析失败：{exc}") from exc

    if len(rows) != 1:
        raise UpdateError("CSV 字符串产生了多行数据")

    return [field.strip() for field in rows[0]]


def parse_date_value(value: str) -> Optional[date]:
    match = DATE_SEARCH_RE.search(value.strip())
    if not match:
        return None

    try:
        return date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError:
        return None


def parse_time_value(value: str) -> Optional[Tuple[str, int]]:
    match = TIME_SEARCH_RE.search(value.strip())
    if not match:
        return None

    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    second = int(match.group("second") or "0")

    if hour > 23:
        return None

    seconds = hour * 3600 + minute * 60 + second
    if second:
        normalized = f"{hour:02d}:{minute:02d}:{second:02d}"
    else:
        normalized = f"{hour:02d}:{minute:02d}"

    return normalized, seconds


def parse_number(value: str) -> float:
    normalized = (
        value.strip()
        .replace("\u2212", "-")
        .replace("\uff0d", "-")
        .replace("\u3000", "")
        .replace("%", "")
    )

    if normalized in {"", "-", "--", "null", "undefined", "NaN"}:
        raise UpdateError(f"缺失或无效数值：{value!r}")

    try:
        number = float(normalized)
    except ValueError as exc:
        raise UpdateError(f"无法解析数值：{value!r}") from exc

    if not math.isfinite(number):
        raise UpdateError(f"数值不是有限数：{value!r}")

    return number


def parse_past_js(text: str) -> List[DailyRow]:
    """
    关键修复：

    真实文件使用：
        GY[q] = "2026/09/18,15:30,...";
        q++;

    所以这里必须使用独立正则，并允许方括号内出现 q、q++ 或其他
    任意非 ']' 索引表达式，不能调用只接受数字索引的
    indexed_assignments(text, "GY")。
    """
    pattern = re.compile(
        rf"(?<![\w$])GY"
        rf"\s*\[\s*[^\]]+\s*\]\s*=\s*"
        rf"(?P<literal>{JS_LITERAL})",
        flags=re.MULTILINE,
    )

    matches = list(pattern.finditer(text))
    if not matches:
        raise UpdateError(
            "past JS 中未找到 GY[...] 行式数据；"
            "解析器已允许 GY[q] 等变量索引"
        )

    # 日期 -> 同日较晚的一条记录。
    rows_by_date: Dict[date, DailyRow] = {}

    for line_number, match in enumerate(matches, start=1):
        decoded = decode_js_string(match.group("literal"))
        fields = parse_csv_fields(decoded)

        expected = 2 + SECTOR_COUNT
        if len(fields) != expected:
            raise UpdateError(
                f"past 第 {line_number} 条 GY 数据字段数错误："
                f"期望 {expected}，实际 {len(fields)}；"
                f"内容={decoded[:160]!r}"
            )

        trading_date = parse_date_value(fields[0])
        parsed_time = parse_time_value(fields[1])

        if trading_date is None:
            raise UpdateError(
                f"past 第 {line_number} 条日期无效：{fields[0]!r}"
            )
        if parsed_time is None:
            raise UpdateError(
                f"past 第 {line_number} 条时间无效：{fields[1]!r}"
            )

        time_text, time_seconds = parsed_time
        values = tuple(parse_number(field) for field in fields[2:])

        if len(values) != SECTOR_COUNT:
            raise UpdateError(
                f"past 第 {line_number} 条行业值不是 {SECTOR_COUNT} 个"
            )

        row = DailyRow(
            trading_date=trading_date,
            time_text=time_text,
            time_seconds=time_seconds,
            values=values,
        )

        previous = rows_by_date.get(trading_date)
        if previous is None or row.time_seconds >= previous.time_seconds:
            rows_by_date[trading_date] = row

    rows = sorted(
        rows_by_date.values(),
        key=lambda item: item.trading_date,
    )

    if len(rows) < MIN_HISTORY_DAYS:
        raise UpdateError(
            f"past 有效交易日不足：需要至少 {MIN_HISTORY_DAYS} 日，"
            f"实际只有 {len(rows)} 日"
        )

    LOG.info(
        "past 解析成功：原始 GY 记录 %d 条，去重后 %d 个交易日，范围 %s 至 %s",
        len(matches),
        len(rows),
        rows[0].trading_date.isoformat(),
        rows[-1].trading_date.isoformat(),
    )

    return rows


def extract_vector_and_datetime(
    scalar_value: str,
) -> Tuple[List[float], Optional[date], Optional[Tuple[str, int]]]:
    """
    G1/G2 是标量 JS 字符串，字符串内部可能是：
        日期,时间,33个值
    也可能只有：
        33个值

    日期和时间字段不计入数值向量。
    """
    fields = parse_csv_fields(scalar_value)

    found_date: Optional[date] = None
    found_time: Optional[Tuple[str, int]] = None
    numbers: List[float] = []

    for field in fields:
        possible_date = parse_date_value(field)
        possible_time = parse_time_value(field)

        if possible_date is not None:
            if found_date is not None and found_date != possible_date:
                raise UpdateError(
                    f"同一 G1/G2 字符串中出现不同日期："
                    f"{found_date} 与 {possible_date}"
                )
            found_date = possible_date

            # 日期字段可能同时含时间，例如 2026/09/18 15:30。
            if possible_time is not None:
                found_time = possible_time
            continue

        if possible_time is not None:
            found_time = possible_time
            continue

        numbers.append(parse_number(field))

    if len(numbers) != SECTOR_COUNT:
        raise UpdateError(
            f"G1/G2 数值数量错误：期望 {SECTOR_COUNT}，"
            f"实际 {len(numbers)}；原始字段数 {len(fields)}"
        )

    return numbers, found_date, found_time


def find_datetime_in_text(
    text: str,
) -> Tuple[Optional[date], Optional[Tuple[str, int]]]:
    """
    当 G1/G2 字符串本身不携带日期或时间时，从 current JS 的
    其他元数据中寻找。这里只提取真实存在的日期/时间，不推测日期。
    """
    found_date: Optional[date] = None
    found_time: Optional[Tuple[str, int]] = None

    date_match = DATE_SEARCH_RE.search(text)
    if date_match:
        found_date = parse_date_value(date_match.group(0))

        nearby = text[
            date_match.end():
            min(len(text), date_match.end() + 80)
        ]
        time_match = TIME_SEARCH_RE.search(nearby)
        if time_match:
            found_time = parse_time_value(time_match.group(0))

    if found_time is None:
        time_match = TIME_SEARCH_RE.search(text)
        if time_match:
            found_time = parse_time_value(time_match.group(0))

    return found_date, found_time


def reconcile_metadata(
    g1_date: Optional[date],
    g1_time: Optional[Tuple[str, int]],
    g2_date: Optional[date],
    g2_time: Optional[Tuple[str, int]],
    text_date: Optional[date],
    text_time: Optional[Tuple[str, int]],
) -> Tuple[date, Tuple[str, int]]:
    dates = [
        value
        for value in (g1_date, g2_date, text_date)
        if value is not None
    ]

    if not dates:
        raise UpdateError(
            "current JS 中未找到交易日期；为避免伪造日期，终止更新"
        )

    if len(set(dates)) != 1:
        raise UpdateError(
            "current JS 中日期不一致："
            + ", ".join(item.isoformat() for item in dates)
        )

    times = [
        value
        for value in (g1_time, g2_time, text_time)
        if value is not None
    ]

    if not times:
        raise UpdateError(
            "current JS 中未找到更新时间；为避免伪造时间，终止更新"
        )

    # 若多个位置给出不同时间，使用较晚时间。
    chosen_time = max(times, key=lambda item: item[1])
    return dates[0], chosen_time


def convert_points_to_percentages(
    g1: Sequence[float],
    g2: Sequence[float],
) -> Tuple[List[float], str]:
    """
    将 current JS 的 G2 点数转换为真实百分比。

    常见源格式一：
        G1 = 当前指数点数
        G2 = 相对前收盘的涨跌点数
        percent = G2 / (G1 - G2) * 100

    常见源格式二：
        G1 = 前收盘指数点数
        G2 = 当前指数点数
        percent = (G2 / G1 - 1) * 100

    auto 通过 G2/G1 的量级判断：
      - G2 明显较小：按“当前点数 + 涨跌点数”
      - 两者同量级：按“前值 + 当前值”
    """
    if len(g1) != SECTOR_COUNT or len(g2) != SECTOR_COUNT:
        raise UpdateError("G1/G2 长度必须都是 33")

    requested_mode = CURRENT_VALUE_MODE

    if requested_mode == "auto":
        ratios = [
            abs(second / first)
            for first, second in zip(g1, g2)
            if abs(first) > 1e-12
        ]
        if not ratios:
            raise UpdateError("G1 全部为零，无法转换 G2 点数")

        median_ratio = statistics.median(ratios)
        if median_ratio < 0.35:
            mode = "current_change"
        else:
            mode = "previous_current"

        LOG.info(
            "G2 转百分比自动判断：median(abs(G2/G1))=%.6f，模式=%s",
            median_ratio,
            mode,
        )
    else:
        mode = requested_mode

    percentages: List[float] = []

    for index, (first, second) in enumerate(zip(g1, g2)):
        if mode == "current_change":
            # G1=current，G2=change。
            previous = first - second
            if abs(previous) < 1e-12:
                raise UpdateError(
                    f"行业 {index} 前值为零，无法用涨跌点数换算百分比"
                )
            percentage = second / previous * 100.0

        elif mode == "previous_change":
            # G1=previous，G2=change。
            if abs(first) < 1e-12:
                raise UpdateError(
                    f"行业 {index} 前值为零，无法用涨跌点数换算百分比"
                )
            percentage = second / first * 100.0

        elif mode == "previous_current":
            # G1=previous，G2=current。
            if abs(first) < 1e-12:
                raise UpdateError(
                    f"行业 {index} 前值为零，无法计算百分比"
                )
            percentage = (second / first - 1.0) * 100.0

        else:
            raise UpdateError(
                "CURRENT_VALUE_MODE 无效："
                f"{requested_mode!r}；允许 auto、current_change、"
                "previous_change、previous_current"
            )

        if not math.isfinite(percentage):
            raise UpdateError(f"行业 {index} 百分比不是有限数")

        # 防止错误字段被静默当成百分比。
        if abs(percentage) > 200:
            raise UpdateError(
                f"行业 {index} 换算结果异常：{percentage:.6f}%"
            )

        percentages.append(percentage)

    return percentages, mode


def parse_current_js(text: str) -> DailyRow:
    g1_scalar = scalar_string_assignment(text, "G1")
    g2_scalar = scalar_string_assignment(text, "G2")

    g1_values, g1_date, g1_time = extract_vector_and_datetime(g1_scalar)
    g2_values, g2_date, g2_time = extract_vector_and_datetime(g2_scalar)
    text_date, text_time = find_datetime_in_text(text)

    trading_date, parsed_time = reconcile_metadata(
        g1_date,
        g1_time,
        g2_date,
        g2_time,
        text_date,
        text_time,
    )

    percentages, conversion_mode = convert_points_to_percentages(
        g1_values,
        g2_values,
    )

    time_text, time_seconds = parsed_time

    LOG.info(
        "current 解析成功：%s %s，G2 转换模式=%s",
        trading_date.isoformat(),
        time_text,
        conversion_mode,
    )

    return DailyRow(
        trading_date=trading_date,
        time_text=time_text,
        time_seconds=time_seconds,
        values=tuple(percentages),
    )


def parse_sector_names(texts: Iterable[str]) -> List[str]:
    assignments: Dict[int, str] = {}

    for text in texts:
        assignments.update(indexed_assignments(text, "Gyo"))

    missing = [
        index
        for index in range(SECTOR_COUNT)
        if index not in assignments
    ]
    if missing:
        raise UpdateError(
            "未完整解析 Gyo[0..32]，缺少索引："
            + ", ".join(str(index) for index in missing)
        )

    names = [
        assignments[index].strip()
        for index in range(SECTOR_COUNT)
    ]

    if any(not name for name in names):
        raise UpdateError("Gyo[0..32] 中存在空行业名")

    if len(set(names)) != SECTOR_COUNT:
        duplicates = sorted(
            {
                name
                for name in names
                if names.count(name) > 1
            }
        )
        raise UpdateError(
            "Gyo[0..32] 中存在重复行业名："
            + ", ".join(duplicates)
        )

    LOG.info("行业名解析成功：Gyo[0..32] 共 %d 个", len(names))
    return names


def merge_current_row(
    past_rows: Sequence[DailyRow],
    current_row: DailyRow,
) -> List[DailyRow]:
    rows_by_date = {
        row.trading_date: row
        for row in past_rows
    }

    latest_past_date = max(rows_by_date)
    if current_row.trading_date < latest_past_date:
        raise UpdateError(
            "current 日期早于 past 最新日期："
            f"current={current_row.trading_date.isoformat()}，"
            f"past={latest_past_date.isoformat()}"
        )

    previous = rows_by_date.get(current_row.trading_date)
    if (
        previous is None
        or current_row.time_seconds >= previous.time_seconds
    ):
        rows_by_date[current_row.trading_date] = current_row
    else:
        LOG.warning(
            "current 的 %s 时间 %s 早于 past 中同日时间 %s，保留较晚的 past 数据",
            current_row.trading_date.isoformat(),
            current_row.time_text,
            previous.time_text,
        )

    rows = sorted(
        rows_by_date.values(),
        key=lambda item: item.trading_date,
    )

    if len(rows) < MIN_HISTORY_DAYS:
        raise UpdateError(
            f"合并后交易日不足 {MIN_HISTORY_DAYS} 日"
        )

    return rows[-MAX_HISTORY_DAYS:]


def compounded_return(
    rows: Sequence[DailyRow],
    sector_index: int,
    end_index: int,
    window: int,
) -> float:
    start_index = end_index - window + 1
    if start_index < 0:
        raise UpdateError(
            f"计算 {window} 日收益时历史数据不足"
        )

    factor = 1.0
    for row_index in range(start_index, end_index + 1):
        daily_percent = rows[row_index].values[sector_index]
        factor *= 1.0 + daily_percent / 100.0

    return (factor - 1.0) * 100.0


def rank_descending(values: Sequence[float]) -> List[int]:
    """
    返回每个原始位置对应的排名。数值越高排名越靠前。
    相同值使用稳定的行业索引顺序打破并列。
    """
    order = sorted(
        range(len(values)),
        key=lambda index: (-values[index], index),
    )

    ranks = [0] * len(values)
    for rank, index in enumerate(order, start=1):
        ranks[index] = rank

    return ranks


def calculate_ranking_at(
    rows: Sequence[DailyRow],
    end_index: int,
) -> Tuple[List[int], List[float], List[float]]:
    returns_5 = [
        compounded_return(rows, sector, end_index, 5)
        for sector in range(SECTOR_COUNT)
    ]
    returns_20 = [
        compounded_return(rows, sector, end_index, 20)
        for sector in range(SECTOR_COUNT)
    ]

    market_5 = statistics.fmean(returns_5)
    market_20 = statistics.fmean(returns_20)

    rs_5 = [
        value - market_5
        for value in returns_5
    ]
    rs_20 = [
        value - market_20
        for value in returns_20
    ]

    # 排名以 20 日相对强弱为准。
    ranks = rank_descending(rs_20)
    return ranks, rs_5, rs_20


def consecutive_strength(
    rows: Sequence[DailyRow],
    sector_index: int,
) -> int:
    """
    以每日行业涨跌幅相对 33 行业等权平均判断强弱。

    正数：连续强于行业平均的交易日数。
    负数：连续弱于行业平均的交易日数。
    0：最新日与行业平均近似相同。
    """
    latest_sign = 0
    count = 0

    for row in reversed(rows):
        market_daily = statistics.fmean(row.values)
        relative = row.values[sector_index] - market_daily

        if relative > 1e-12:
            sign = 1
        elif relative < -1e-12:
            sign = -1
        else:
            sign = 0

        if latest_sign == 0:
            latest_sign = sign
            if sign == 0:
                return 0
            count = 1
            continue

        if sign != latest_sign:
            break

        count += 1

    return count * latest_sign


def rotation_state(
    *,
    rank: int,
    rank_change: int,
    rs20: float,
    streak: int,
) -> str:
    if rank_change >= 3:
        return "轮入"
    if rank_change <= -3:
        return "轮出"
    if rank <= 11 and rs20 >= 0 and streak >= 0:
        return "领涨"
    if rs20 >= 0:
        return "强势"
    return "弱势"


def clean_float(value: float, digits: int = 6) -> float:
    rounded = round(float(value), digits)
    if rounded == 0:
        return 0.0
    return rounded


def build_payload(
    names: Sequence[str],
    rows: Sequence[DailyRow],
    *,
    entry_url: str,
    past_url: str,
    current_url: str,
) -> dict:
    if len(names) != SECTOR_COUNT:
        raise UpdateError("行业名数量不是 33")
    if len(rows) < MIN_HISTORY_DAYS:
        raise UpdateError("构建指标时历史交易日不足 21 日")
    if len(rows) > MAX_HISTORY_DAYS:
        raise UpdateError("内部错误：历史交易日超过 90 日")

    latest_index = len(rows) - 1
    previous_index = latest_index - 1

    current_ranks, rs5_values, rs20_values = calculate_ranking_at(
        rows,
        latest_index,
    )
    previous_ranks, _, _ = calculate_ranking_at(
        rows,
        previous_index,
    )

    sectors = []

    for sector_index, name in enumerate(names):
        return_5 = compounded_return(
            rows,
            sector_index,
            latest_index,
            5,
        )
        return_20 = compounded_return(
            rows,
            sector_index,
            latest_index,
            20,
        )

        rank = current_ranks[sector_index]
        previous_rank = previous_ranks[sector_index]

        # 正数表示排名提升，例如 10 -> 6，变化为 +4。
        rank_change = previous_rank - rank
        streak = consecutive_strength(rows, sector_index)

        sectors.append(
            {
                "index": sector_index,
                "name": name,
                "dailyPercent": clean_float(
                    rows[-1].values[sector_index]
                ),
                "return5d": clean_float(return_5),
                "return20d": clean_float(return_20),
                "rs5": clean_float(rs5_values[sector_index]),
                "rs20": clean_float(rs20_values[sector_index]),
                "rank": rank,
                "previousRank": previous_rank,
                "rankChange": rank_change,
                "streak": streak,
                "strength": (
                    "强"
                    if streak > 0
                    else "弱"
                    if streak < 0
                    else "中性"
                ),
                "rotationState": rotation_state(
                    rank=rank,
                    rank_change=rank_change,
                    rs20=rs20_values[sector_index],
                    streak=streak,
                ),

                # 网站源数据没有可靠资金流字段，不以涨跌幅伪造。
                "fundFlow": None,
                "fundFlowRank": None,
            }
        )

    history = []
    for row in rows:
        history.append(
            {
                "date": row.trading_date.isoformat(),
                "time": row.time_text,
                "values": [
                    clean_float(value)
                    for value in row.values
                ],
            }
        )

    payload = {
        "schemaVersion": 2,
        "generatedAt": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "latestDate": rows[-1].trading_date.isoformat(),
        "latestTime": rows[-1].time_text,
        "tradingDayCount": len(rows),
        "industryCount": SECTOR_COUNT,
        "industryNames": list(names),
        "sectors": sectors,
        "history": history,
        "sources": {
            "entry": entry_url,
            "past": past_url,
            "current": current_url,
        },

        # 顶层也明确声明没有资金流源数据。
        "fundFlow": None,
    }

    validate_payload(payload)
    return payload


def validate_payload(payload: dict) -> None:
    if payload.get("industryCount") != SECTOR_COUNT:
        raise UpdateError("payload industryCount 错误")

    names = payload.get("industryNames")
    sectors = payload.get("sectors")
    history = payload.get("history")

    if not isinstance(names, list) or len(names) != SECTOR_COUNT:
        raise UpdateError("payload 行业名数量错误")

    if not isinstance(sectors, list) or len(sectors) != SECTOR_COUNT:
        raise UpdateError("payload 行业指标数量错误")

    if not isinstance(history, list):
        raise UpdateError("payload history 不是数组")

    if not (MIN_HISTORY_DAYS <= len(history) <= MAX_HISTORY_DAYS):
        raise UpdateError(
            f"payload history 日数错误：{len(history)}"
        )

    dates = [item.get("date") for item in history]
    if dates != sorted(dates):
        raise UpdateError("payload history 未按日期升序排列")

    if len(set(dates)) != len(dates):
        raise UpdateError("payload history 中存在重复日期")

    for item in history:
        values = item.get("values")
        if not isinstance(values, list) or len(values) != SECTOR_COUNT:
            raise UpdateError(
                f"payload {item.get('date')} 的行业值数量错误"
            )

    for sector in sectors:
        if sector.get("fundFlow") is not None:
            raise UpdateError("检测到伪造的行业资金流字段")
        if sector.get("fundFlowRank") is not None:
            raise UpdateError("检测到伪造的行业资金流排名")

    if payload.get("fundFlow") is not None:
        raise UpdateError("检测到伪造的顶层资金流字段")


def make_data_block(payload: dict) -> str:
    json_text = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )

    # 防止行业名等内容意外形成 </script>。
    safe_json = json_text.replace("</", "<\\/")

    return (
        '<script id="daily-update-data" type="application/json">\n'
        f"{safe_json}\n"
        "</script>\n"
        "<script>\n"
        "(function () {\n"
        '  var node = document.getElementById("daily-update-data");\n'
        "  if (!node) return;\n"
        "  var data = JSON.parse(node.textContent);\n"
        "  window.DAILY_UPDATE_DATA = data;\n"
        "  window.INDUSTRY_DATA = data;\n"
        "  window.__DAILY_UPDATE_DATA__ = data;\n"
        "})();\n"
        "</script>"
    )


def replace_between_markers(
    html: str,
    start_marker: str,
    end_marker: str,
    body: str,
) -> Optional[str]:
    start_index = html.find(start_marker)
    end_index = html.find(end_marker)

    if start_index < 0 and end_index < 0:
        return None

    if start_index < 0 or end_index < 0:
        raise UpdateError(
            f"index.html 数据标记不完整："
            f"{start_marker!r} / {end_marker!r}"
        )

    content_start = start_index + len(start_marker)
    if end_index < content_start:
        raise UpdateError(
            f"index.html 数据标记顺序错误：{start_marker!r}"
        )

    return (
        html[:content_start]
        + "\n"
        + body
        + "\n"
        + html[end_index:]
    )


def render_index_html(original_html: str, payload: dict) -> str:
    block = make_data_block(payload)

    marker_pairs = [
        (
            "<!-- DAILY_UPDATE_DATA:START -->",
            "<!-- DAILY_UPDATE_DATA:END -->",
        ),
        (
            "<!-- DAILY_UPDATE_DATA_START -->",
            "<!-- DAILY_UPDATE_DATA_END -->",
        ),
        (
            "<!-- DAILY_UPDATE_START -->",
            "<!-- DAILY_UPDATE_END -->",
        ),
        (
            "<!-- AUTO_UPDATE_START -->",
            "<!-- AUTO_UPDATE_END -->",
        ),
        (
            "<!-- INDUSTRY_DATA_START -->",
            "<!-- INDUSTRY_DATA_END -->",
        ),
    ]

    for start_marker, end_marker in marker_pairs:
        replaced = replace_between_markers(
            original_html,
            start_marker,
            end_marker,
            block,
        )
        if replaced is not None:
            return replaced

    # 兼容已有的 application/json 数据节点。
    existing_script_pattern = re.compile(
        r"(<script\b[^>]*\bid\s*=\s*"
        r"(?P<quote>['\"])"
        r"(?:daily-update-data|industry-data)"
        r"(?P=quote)[^>]*>)"
        r".*?"
        r"(</script\s*>)",
        flags=re.IGNORECASE | re.DOTALL,
    )

    json_text = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).replace("</", "<\\/")

    if existing_script_pattern.search(original_html):
        return existing_script_pattern.sub(
            lambda match: (
                match.group(1)
                + "\n"
                + json_text
                + "\n"
                + match.group(3)
            ),
            original_html,
            count=1,
        )

    # 第一次运行、页面还没有标记时，安全插入标准标记块。
    standard_block = (
        "\n<!-- DAILY_UPDATE_DATA:START -->\n"
        + block
        + "\n<!-- DAILY_UPDATE_DATA:END -->\n"
    )

    body_close = re.search(
        r"</body\s*>",
        original_html,
        flags=re.IGNORECASE,
    )

    if body_close:
        insert_at = body_close.start()
        return (
            original_html[:insert_at]
            + standard_block
            + original_html[insert_at:]
        )

    html_close = re.search(
        r"</html\s*>",
        original_html,
        flags=re.IGNORECASE,
    )
    if html_close:
        insert_at = html_close.start()
        return (
            original_html[:insert_at]
            + standard_block
            + original_html[insert_at:]
        )

    raise UpdateError(
        "index.html 中没有数据标记、</body> 或 </html>，拒绝覆盖"
    )


def atomic_write_text(path: Path, content: str) -> None:
    """
    在目标文件同目录创建临时文件，完整写入并 fsync 后使用 os.replace。
    任何前置步骤失败都不会触碰原 index.html。
    """
    if not path.exists():
        raise UpdateError(f"index.html 不存在：{path}")
    if not path.is_file():
        raise UpdateError(f"index.html 不是普通文件：{path}")

    original_mode = path.stat().st_mode
    temp_name: Optional[str] = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_name = temp_file.name
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())

        os.chmod(temp_name, original_mode)
        os.replace(temp_name, path)
        temp_name = None

        # 尽量同步目录项；不支持时不影响更新结果。
        try:
            directory_fd = os.open(
                str(path.parent),
                os.O_RDONLY,
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass

    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def locate_past_url(
    script_urls: Sequence[str],
    script_texts: Dict[str, str],
) -> str:
    preferred = []

    for url in script_urls:
        path = urlsplit(url).path.lower()
        if path.endswith("/country_jp_gyo_past.js"):
            preferred.append(url)
        elif path.endswith("_gyo_past.js"):
            preferred.append(url)

    if preferred:
        return preferred[0]

    # 文件名发生轻微变化时，使用内容特征兜底。
    gy_pattern = re.compile(
        rf"(?<![\w$])GY\s*\[\s*[^\]]+\s*\]\s*=\s*{JS_LITERAL}",
        flags=re.MULTILINE,
    )

    for url in script_urls:
        text = script_texts.get(url)
        if text and gy_pattern.search(text):
            return url

    raise UpdateError(
        "自动发现的同站脚本中未找到 past GY 数据文件"
    )


def run() -> None:
    if not INDEX_PATH.exists():
        raise UpdateError(f"找不到 index.html：{INDEX_PATH}")

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,application/javascript,"
                "text/javascript,*/*;q=0.8"
            ),
            "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
    )

    # 第一次请求建立 Cookie 会话。
    entry_html = fetch_text(
        session,
        ENTRY_URL,
        referer=ENTRY_URL,
    )
    LOG.info("入口页抓取成功：%s", ENTRY_URL)

    script_urls = discover_same_origin_scripts(
        entry_html,
        ENTRY_URL,
    )
    LOG.info(
        "自动发现 %d 个同站脚本%s",
        len(script_urls),
        "（当前页面预期为 9 个）" if len(script_urls) != 9 else "",
    )

    if not script_urls:
        raise UpdateError("入口页未发现任何同站脚本")

    # 继续使用同一 Session、Cookie 和 Referer。
    script_texts: Dict[str, str] = {}
    script_errors: Dict[str, str] = {}

    for script_url in script_urls:
        try:
            script_texts[script_url] = fetch_text(
                session,
                script_url,
                referer=ENTRY_URL,
            )
        except requests.RequestException as exc:
            # 非关键脚本失败不立即终止；past/current 仍会严格检查。
            script_errors[script_url] = str(exc)
            LOG.warning(
                "非关键脚本暂时抓取失败：%s：%s",
                script_url,
                exc,
            )

    past_url = locate_past_url(script_urls, script_texts)
    LOG.info("自动发现 past URL：%s", past_url)

    if past_url in script_texts:
        past_js = script_texts[past_url]
    else:
        try:
            past_js = fetch_text(
                session,
                past_url,
                referer=ENTRY_URL,
            )
        except requests.RequestException as exc:
            detail = script_errors.get(past_url, str(exc))
            raise UpdateError(
                f"past JS 抓取失败：{past_url}：{detail}"
            ) from exc

    current_url = derive_current_url(past_url)
    LOG.info("由 past URL 派生 current URL：%s", current_url)

    try:
        current_js = fetch_text(
            session,
            current_url,
            referer=ENTRY_URL,
        )
    except requests.RequestException as exc:
        raise UpdateError(
            f"current JS 抓取失败：{current_url}：{exc}"
        ) from exc

    # Gyo 可能位于入口页、current JS 或其他同站脚本中。
    name_sources: List[str] = [
        entry_html,
        current_js,
        past_js,
    ]
    name_sources.extend(script_texts.values())

    names = parse_sector_names(name_sources)
    past_rows = parse_past_js(past_js)
    current_row = parse_current_js(current_js)
    merged_rows = merge_current_row(past_rows, current_row)

    LOG.info(
        "最终保留 %d 个交易日：%s 至 %s",
        len(merged_rows),
        merged_rows[0].trading_date.isoformat(),
        merged_rows[-1].trading_date.isoformat(),
    )

    payload = build_payload(
        names,
        merged_rows,
        entry_url=ENTRY_URL,
        past_url=past_url,
        current_url=current_url,
    )

    # 到此为止仍未写 index.html。
    original_html = INDEX_PATH.read_text(encoding="utf-8")
    updated_html = render_index_html(original_html, payload)

    if updated_html == original_html:
        raise UpdateError("渲染结果与原 index.html 完全相同，拒绝写入")

    latest_date = payload["latestDate"]
    if latest_date not in updated_html:
        raise UpdateError("渲染校验失败：HTML 中没有最新交易日期")

    if 'id="daily-update-data"' not in updated_html and \
       "id='daily-update-data'" not in updated_html and \
       'id="industry-data"' not in updated_html and \
       "id='industry-data'" not in updated_html:
        raise UpdateError("渲染校验失败：HTML 中没有数据节点")

    # 所有步骤成功后才原子替换。
    atomic_write_text(INDEX_PATH, updated_html)

    LOG.info(
        "更新成功：已原子写入 %s；最新交易日=%s，时间=%s",
        INDEX_PATH,
        payload["latestDate"],
        payload["latestTime"],
    )


def main() -> int:
    try:
        run()
        return 0

    except UpdateError as exc:
        # 失败发生在 atomic_write_text 之前时，原 index.html 完全不变。
        LOG.error("Daily Update 失败：%s", exc)
        return 1

    except requests.RequestException as exc:
        LOG.exception("网络请求失败：%s", exc)
        return 1

    except Exception as exc:
        LOG.exception("未预期错误：%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
