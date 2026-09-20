#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import html as html_lib
import json
import math
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import (
    urljoin,
    urlsplit,
    urlunsplit,
    urldefrag,
)
from urllib.request import (
    HTTPCookieProcessor,
    Request,
    build_opener,
)


ENTRY_URL = "https://nikkei225jp.com/chart/gyoushu.php"
INDUSTRY_COUNT = 33
MAX_HISTORY_DAYS = 90
MIN_HISTORY_DAYS = 21
MAX_HTTP_BYTES = 20 * 1024 * 1024
MAX_SUPPORT_SCRIPTS = 64

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

ROOT_DIR = Path(__file__).resolve().parent.parent
INDEX_PATH = ROOT_DIR / "index.html"


class UpdateError(RuntimeError):
    pass


class _ScriptSrcParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "script":
            return

        for key, value in attrs:
            if key.lower() == "src" and value:
                self.sources.append(value.strip())
                break


def normalize_site_host(hostname):
    host = (hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def is_same_site(url, reference_url=ENTRY_URL):
    candidate_host = normalize_site_host(urlsplit(url).hostname)
    reference_host = normalize_site_host(urlsplit(reference_url).hostname)
    return bool(candidate_host and candidate_host == reference_host)


def normalize_url(reference, base_url):
    if not reference:
        return None

    reference = html_lib.unescape(reference.strip())
    reference = reference.replace("\\/", "/")
    reference = reference.strip("\"' \t\r\n")

    if not reference or reference.lower().startswith(("javascript:", "data:")):
        return None

    absolute = urljoin(base_url, reference)
    absolute, _fragment = urldefrag(absolute)

    parts = urlsplit(absolute)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None

    return absolute


def decode_http_body(body, headers):
    encodings = []

    declared = None
    try:
        declared = headers.get_content_charset()
    except Exception:
        declared = None

    if declared:
        encodings.append(declared)

    head = body[:4096]
    meta_match = re.search(
        br"""charset\s*=\s*["']?\s*([A-Za-z0-9._-]+)""",
        head,
        flags=re.IGNORECASE,
    )
    if meta_match:
        try:
            encodings.append(meta_match.group(1).decode("ascii"))
        except UnicodeDecodeError:
            pass

    encodings.extend(
        [
            "utf-8-sig",
            "utf-8",
            "cp932",
            "shift_jis",
            "euc_jp",
        ]
    )

    tried = set()
    errors = []

    for encoding in encodings:
        normalized = encoding.lower()
        if normalized in tried:
            continue
        tried.add(normalized)

        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError) as exc:
            errors.append("{}: {}".format(encoding, exc))

    raise UpdateError(
        "无法解码响应内容；尝试过的编码均失败：{}".format("; ".join(errors))
    )


def fetch_text(opener, url):
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/javascript,text/javascript,"
                "application/json;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
            "Referer": ENTRY_URL,
            "Connection": "close",
        },
        method="GET",
    )

    try:
        with opener.open(request, timeout=30) as response:
            status = response.getcode()
            if status is not None and not 200 <= status < 300:
                raise UpdateError("HTTP 状态异常：{} -> {}".format(url, status))

            body = response.read(MAX_HTTP_BYTES + 1)
            if len(body) > MAX_HTTP_BYTES:
                raise UpdateError("响应内容过大：{}".format(url))

            final_url = response.geturl()
            text = decode_http_body(body, response.headers)
            return final_url, text
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError("网络请求失败：{}；{}".format(url, exc)) from exc


def discover_script_urls(text, base_url):
    parser = _ScriptSrcParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:
        raise UpdateError("入口 HTML 的 script 标签解析失败：{}".format(exc)) from exc

    discovered = []
    seen = set()

    for source in parser.sources:
        url = normalize_url(source, base_url)
        if url and url not in seen:
            seen.add(url)
            discovered.append(url)

    return discovered


def find_javascript_references(text, base_url):
    pattern = re.compile(
        r"""(["'])([^"'\\\r\n]+?\.js(?:\?[^"'\\\s<>]*)?)\1""",
        flags=re.IGNORECASE,
    )

    discovered = []
    seen = set()

    for match in pattern.finditer(text):
        url = normalize_url(match.group(2), base_url)
        if url and url not in seen:
            seen.add(url)
            discovered.append(url)

    return discovered


def find_past_references(text, base_url):
    pattern = re.compile(
        r"""
        (?P<ref>
            (?:
                (?:https?:)?//[^\s"'<>]*
                |
                [A-Za-z0-9_./~%-]*
            )
            country_jp_gyo_past\.js
            (?:\?[^'"<>\s\\]*)?
        )
        """,
        flags=re.IGNORECASE | re.VERBOSE,
    )

    discovered = []
    seen = set()

    for match in pattern.finditer(text):
        reference = match.group("ref").rstrip("),;]")
        url = normalize_url(reference, base_url)
        if not url:
            continue

        if not is_target_script(url, "country_jp_gyo_past.js"):
            continue

        if url not in seen:
            seen.add(url)
            discovered.append(url)

    return discovered


def is_target_script(url, filename):
    path = urlsplit(url).path
    basename = path.rsplit("/", 1)[-1]
    return basename.lower() == filename.lower()


def derive_current_url(past_url):
    parts = urlsplit(past_url)
    new_path, replacement_count = re.subn(
        r"country_jp_gyo_past\.js$",
        "country_jp_gyo.js",
        parts.path,
        count=1,
        flags=re.IGNORECASE,
    )

    if replacement_count != 1:
        raise UpdateError("无法从 past URL 派生 current URL：{}".format(past_url))

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            new_path,
            parts.query,
            parts.fragment,
        )
    )


def decode_js_string(value):
    result = []
    index = 0
    length = len(value)

    simple_escapes = {
        '"': '"',
        "'": "'",
        "\\": "\\",
        "/": "/",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "0": "\0",
    }

    while index < length:
        char = value[index]

        if char != "\\":
            result.append(char)
            index += 1
            continue

        index += 1
        if index >= length:
            raise UpdateError("JS 字符串以未完成的转义符结尾")

        escaped = value[index]

        if escaped in simple_escapes:
            result.append(simple_escapes[escaped])
            index += 1
            continue

        if escaped == "x":
            digits = value[index + 1:index + 3]
            if len(digits) != 2 or not re.fullmatch(r"[0-9A-Fa-f]{2}", digits):
                raise UpdateError("JS 字符串包含无效的 \\x 转义")
            result.append(chr(int(digits, 16)))
            index += 3
            continue

        if escaped == "u":
            digits = value[index + 1:index + 5]
            if len(digits) != 4 or not re.fullmatch(r"[0-9A-Fa-f]{4}", digits):
                raise UpdateError("JS 字符串包含无效的 \\u 转义")

            codepoint = int(digits, 16)
            index += 5

            if (
                0xD800 <= codepoint <= 0xDBFF
                and value[index:index + 2] == "\\u"
            ):
                low_digits = value[index + 2:index + 6]
                if (
                    len(low_digits) == 4
                    and re.fullmatch(r"[0-9A-Fa-f]{4}", low_digits)
                ):
                    low = int(low_digits, 16)
                    if 0xDC00 <= low <= 0xDFFF:
                        combined = (
                            0x10000
                            + ((codepoint - 0xD800) << 10)
                            + (low - 0xDC00)
                        )
                        result.append(chr(combined))
                        index += 6
                        continue

            result.append(chr(codepoint))
            continue

        if escaped in ("\n", "\r"):
            if escaped == "\r" and index + 1 < length and value[index + 1] == "\n":
                index += 1
            index += 1
            continue

        result.append(escaped)
        index += 1

    return "".join(result)


def extract_industry_name_map(text):
    pattern = re.compile(
        r"""
        \bGyo\s*
        \[\s*(?P<index>\d+)\s*\]\s*
        =\s*
        "(?P<value>(?:\\.|[^"\\])*)"
        \s*;?
        """,
        flags=re.VERBOSE | re.DOTALL,
    )

    result = {}

    for match in pattern.finditer(text):
        industry_index = int(match.group("index"))
        if not 0 <= industry_index < INDUSTRY_COUNT:
            continue

        name = decode_js_string(match.group("value")).strip()
        if not name:
            raise UpdateError("Gyo[{}] 的行业名为空".format(industry_index))

        existing = result.get(industry_index)
        if existing is not None and existing != name:
            raise UpdateError(
                "同一来源中 Gyo[{}] 存在冲突：{} / {}".format(
                    industry_index,
                    existing,
                    name,
                )
            )

        result[industry_index] = name

    return result


def merged_industry_name_map(texts):
    merged = {}

    for text in texts:
        source_map = extract_industry_name_map(text)
        for industry_index, name in source_map.items():
            if industry_index not in merged:
                merged[industry_index] = name

    return merged


def has_complete_industry_names(name_map):
    expected_indexes = set(range(INDUSTRY_COUNT))
    if set(name_map) != expected_indexes:
        return False

    names = [name_map[index].strip() for index in range(INDUSTRY_COUNT)]
    return len(set(names)) == INDUSTRY_COUNT and all(names)


def collect_industry_names(texts):
    name_map = merged_industry_name_map(texts)

    missing = [
        index
        for index in range(INDUSTRY_COUNT)
        if index not in name_map
    ]
    if missing:
        raise UpdateError(
            "行业名不足 {} 个，缺少索引：{}".format(
                INDUSTRY_COUNT,
                ", ".join(str(index) for index in missing),
            )
        )

    names = [name_map[index].strip() for index in range(INDUSTRY_COUNT)]

    if len(names) != INDUSTRY_COUNT:
        raise UpdateError("行业名数量不是 {}".format(INDUSTRY_COUNT))

    if any(not name for name in names):
        raise UpdateError("行业名中存在空值")

    if len(set(names)) != INDUSTRY_COUNT:
        raise UpdateError("行业名不是恰好 {} 个唯一值".format(INDUSTRY_COUNT))

    return names


def extract_scalar_assignment(text, variable_name):
    pattern = re.compile(
        r"""
        (?<![A-Za-z0-9_$])
        """
        + re.escape(variable_name)
        + r"""
        \s*=\s*
        "(?P<value>(?:\\.|[^"\\])*)"
        \s*;
        """,
        flags=re.VERBOSE | re.DOTALL,
    )

    matches = list(pattern.finditer(text))
    if not matches:
        raise UpdateError("current JS 缺少 {} 标量赋值".format(variable_name))

    return decode_js_string(matches[-1].group("value")).strip()


def parse_finite_number(value, context):
    cleaned = value.strip()
    cleaned = cleaned.replace("\u2212", "-")
    cleaned = cleaned.replace("\uff0b", "+")
    cleaned = cleaned.replace("\uff05", "%")

    if cleaned.endswith("%"):
        cleaned = cleaned[:-1].strip()

    if not cleaned:
        raise UpdateError("{} 包含空数值".format(context))

    try:
        number = float(cleaned)
    except ValueError as exc:
        raise UpdateError(
            "{} 包含无法解析的数值：{}".format(context, value)
        ) from exc

    if not math.isfinite(number):
        raise UpdateError("{} 包含非有限数值：{}".format(context, value))

    return number


def parse_number_list(value, context):
    parts = [
        item.strip()
        for item in re.split(r"[,_]", value)
        if item.strip()
    ]

    if len(parts) != INDUSTRY_COUNT:
        raise UpdateError(
            "{} 应包含 {} 个数值，实际为 {} 个".format(
                context,
                INDUSTRY_COUNT,
                len(parts),
            )
        )

    return [
        parse_finite_number(item, "{}[{}]".format(context, index))
        for index, item in enumerate(parts)
    ]


def normalize_date(value):
    raw = value.strip()

    japanese_match = re.fullmatch(
        r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
        raw,
    )
    if japanese_match:
        year, month, day = map(int, japanese_match.groups())
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError as exc:
            raise UpdateError("日期无效：{}".format(value)) from exc

    match = re.fullmatch(
        r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})",
        raw,
    )
    if not match:
        raise UpdateError("无法解析日期：{}".format(value))

    year, month, day = map(int, match.groups())
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError as exc:
        raise UpdateError("日期无效：{}".format(value)) from exc


def normalize_time(value):
    raw = value.strip()
    match = re.fullmatch(
        r"(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?",
        raw,
    )
    if not match:
        raise UpdateError("无法解析时间：{}".format(value))

    hour = int(match.group(1))
    minute = int(match.group(2))
    second_text = match.group(3)
    second = int(second_text) if second_text is not None else 0

    if not 0 <= hour <= 23:
        raise UpdateError("小时无效：{}".format(value))
    if not 0 <= minute <= 59:
        raise UpdateError("分钟无效：{}".format(value))
    if not 0 <= second <= 59:
        raise UpdateError("秒数无效：{}".format(value))

    if second_text is None:
        return "{:02d}:{:02d}".format(hour, minute)

    return "{:02d}:{:02d}:{:02d}".format(hour, minute, second)


def time_to_seconds(value):
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        hour, minute = parts
        second = 0
    elif len(parts) == 3:
        hour, minute, second = parts
    else:
        raise UpdateError("规范化时间格式异常：{}".format(value))

    return hour * 3600 + minute * 60 + second


def parse_current_js(text):
    mod_date = normalize_date(extract_scalar_assignment(text, "ModDate"))
    mod_time = normalize_time(extract_scalar_assignment(text, "ModTime"))

    current_points = parse_number_list(
        extract_scalar_assignment(text, "G1"),
        "G1",
    )
    change_points = parse_number_list(
        extract_scalar_assignment(text, "G2"),
        "G2",
    )

    previous_close = []
    daily_percent = []

    for index, (current, change) in enumerate(
        zip(current_points, change_points)
    ):
        previous = current - change
        if not math.isfinite(previous) or previous == 0:
            raise UpdateError(
                "行业 {} 的 previous close 无效：current={} change={}".format(
                    index,
                    current,
                    change,
                )
            )

        percent = change / previous * 100.0
        if not math.isfinite(percent):
            raise UpdateError("行业 {} 的当日涨跌百分比无效".format(index))

        previous_close.append(previous)
        daily_percent.append(percent)

    return {
        "date": mod_date,
        "time": mod_time,
        "points": current_points,
        "changes": change_points,
        "previousClose": previous_close,
        "percentages": daily_percent,
    }


def parse_past_js(text):
    pattern = re.compile(
        r"""
        \bGY\s*
        \[
            [^\]]+
        \]
        \s*=\s*
        "(?P<value>(?:\\.|[^"\\])*)"
        \s*;
        """,
        flags=re.VERBOSE | re.DOTALL,
    )

    matches = list(pattern.finditer(text))
    if not matches:
        raise UpdateError("past JS 中未找到 GY[...] 历史赋值")

    records_by_date = {}

    for record_index, match in enumerate(matches):
        decoded = decode_js_string(match.group("value")).strip()
        parts = decoded.split(",", 2)

        if len(parts) != 3:
            raise UpdateError(
                "past 记录 {} 不符合 日期,时间,33值 格式".format(record_index)
            )

        record_date = normalize_date(parts[0])
        record_time = normalize_time(parts[1])
        values = parse_number_list(
            parts[2],
            "past[{}]".format(record_index),
        )

        candidate = {
            "date": record_date,
            "time": record_time,
            "values": values,
        }

        existing = records_by_date.get(record_date)
        if (
            existing is None
            or time_to_seconds(record_time)
            > time_to_seconds(existing["time"])
        ):
            records_by_date[record_date] = candidate

    records = [
        records_by_date[record_date]
        for record_date in sorted(records_by_date)
    ]

    if not records:
        raise UpdateError("past JS 未产生任何有效历史记录")

    return records


def compound_return(records, industry_index, days, end_index):
    start_index = end_index - days + 1
    if start_index < 0:
        raise UpdateError(
            "计算 {} 日收益时历史数据不足".format(days)
        )

    growth = 1.0
    for record_index in range(start_index, end_index + 1):
        daily = records[record_index]["values"][industry_index]
        growth *= 1.0 + daily / 100.0

    result = (growth - 1.0) * 100.0
    if not math.isfinite(result):
        raise UpdateError(
            "行业 {} 的 {} 日收益不是有限数值".format(
                industry_index,
                days,
            )
        )

    return result


def calculate_snapshot(records, end_index):
    if end_index < 19:
        raise UpdateError("计算指标至少需要 20 个交易日")

    daily_values = list(records[end_index]["values"])
    return_5d = [
        compound_return(records, industry_index, 5, end_index)
        for industry_index in range(INDUSTRY_COUNT)
    ]
    return_20d = [
        compound_return(records, industry_index, 20, end_index)
        for industry_index in range(INDUSTRY_COUNT)
    ]

    daily_average = sum(daily_values) / INDUSTRY_COUNT
    average_5d = sum(return_5d) / INDUSTRY_COUNT
    average_20d = sum(return_20d) / INDUSTRY_COUNT

    rs_5 = [value - average_5d for value in return_5d]
    rs_20 = [value - average_20d for value in return_20d]

    strength = [
        0.15 * (daily_values[index] - daily_average)
        + 0.35 * rs_5[index]
        + 0.50 * rs_20[index]
        for index in range(INDUSTRY_COUNT)
    ]

    ranked_indexes = sorted(
        range(INDUSTRY_COUNT),
        key=lambda industry_index: (
            -strength[industry_index],
            industry_index,
        ),
    )

    ranks = [0] * INDUSTRY_COUNT
    for rank, industry_index in enumerate(ranked_indexes, start=1):
        ranks[industry_index] = rank

    return {
        "daily": daily_values,
        "return5d": return_5d,
        "return20d": return_20d,
        "rs5": rs_5,
        "rs20": rs_20,
        "strength": strength,
        "ranks": ranks,
    }


def calculate_streak(records, industry_index):
    direction = 0
    count = 0
    epsilon = 1e-12

    for record in reversed(records):
        average = sum(record["values"]) / INDUSTRY_COUNT
        relative = record["values"][industry_index] - average

        if relative > epsilon:
            current_direction = 1
        elif relative < -epsilon:
            current_direction = -1
        else:
            break

        if direction == 0:
            direction = current_direction

        if current_direction != direction:
            break

        count += 1

    return count * direction


def rotation_state(rs_5, rs_20):
    if rs_5 >= 0 and rs_20 >= 0:
        return "leading"
    if rs_5 >= 0 and rs_20 < 0:
        return "improving"
    if rs_5 < 0 and rs_20 >= 0:
        return "weakening"
    return "lagging"


def rounded_number(value, digits=6):
    if not math.isfinite(value):
        raise UpdateError("尝试输出非有限数值")

    rounded = round(float(value), digits)
    if rounded == 0:
        return 0.0
    return rounded


def calculate_metrics(records, industry_names, current_data):
    if len(records) < MIN_HISTORY_DAYS:
        raise UpdateError(
            "指标计算至少需要 {} 个交易日".format(MIN_HISTORY_DAYS)
        )

    latest_index = len(records) - 1
    current_snapshot = calculate_snapshot(records, latest_index)
    previous_snapshot = calculate_snapshot(records, latest_index - 1)

    sectors = []

    for industry_index in range(INDUSTRY_COUNT):
        rank = current_snapshot["ranks"][industry_index]
        previous_rank = previous_snapshot["ranks"][industry_index]

        sector = {
            "id": industry_index,
            "code": "Gyo{}".format(industry_index),
            "name": industry_names[industry_index],
            "current": rounded_number(
                current_data["points"][industry_index]
            ),
            "change": rounded_number(
                current_data["changes"][industry_index]
            ),
            "previousClose": rounded_number(
                current_data["previousClose"][industry_index]
            ),
            "dailyPercent": rounded_number(
                current_snapshot["daily"][industry_index]
            ),
            "return5d": rounded_number(
                current_snapshot["return5d"][industry_index]
            ),
            "return20d": rounded_number(
                current_snapshot["return20d"][industry_index]
            ),
            "rs5": rounded_number(
                current_snapshot["rs5"][industry_index]
            ),
            "rs20": rounded_number(
                current_snapshot["rs20"][industry_index]
            ),
            "rank": rank,
            "previousRank": previous_rank,
            "rankChange": previous_rank - rank,
            "streak": calculate_streak(records, industry_index),
            "strength": rounded_number(
                current_snapshot["strength"][industry_index]
            ),
            "rotationState": rotation_state(
                current_snapshot["rs5"][industry_index],
                current_snapshot["rs20"][industry_index],
            ),
            "fundFlow": None,
            "fundFlowRank": None,
        }
        sectors.append(sector)

    return sectors


def merge_history(past_records, current_data):
    records_by_date = {}

    for record in past_records:
        existing = records_by_date.get(record["date"])
        if (
            existing is None
            or time_to_seconds(record["time"])
            > time_to_seconds(existing["time"])
        ):
            records_by_date[record["date"]] = {
                "date": record["date"],
                "time": record["time"],
                "values": list(record["values"]),
            }

    later_dates = [
        record_date
        for record_date in records_by_date
        if record_date > current_data["date"]
    ]
    if later_dates:
        raise UpdateError(
            "past JS 含有晚于 current 日期 {} 的记录：{}".format(
                current_data["date"],
                ", ".join(sorted(later_dates)),
            )
        )

    records_by_date[current_data["date"]] = {
        "date": current_data["date"],
        "time": current_data["time"],
        "values": list(current_data["percentages"]),
    }

    records = [
        records_by_date[record_date]
        for record_date in sorted(records_by_date)
    ]

    if len(records) < MIN_HISTORY_DAYS:
        raise UpdateError(
            "合并后只有 {} 个交易日，至少需要 {} 个".format(
                len(records),
                MIN_HISTORY_DAYS,
            )
        )

    records = records[-MAX_HISTORY_DAYS:]

    if records[-1]["date"] != current_data["date"]:
        raise UpdateError("current 日期不是合并后历史中的最新日期")

    return records


def build_payload(
    industry_names,
    records,
    sectors,
    current_data,
    past_url,
    current_url,
):
    history = []

    for record in records:
        if len(record["values"]) != INDUSTRY_COUNT:
            raise UpdateError("历史记录行业数不一致")

        history.append(
            {
                "date": record["date"],
                "time": record["time"],
                "values": [
                    rounded_number(value)
                    for value in record["values"]
                ],
            }
        )

    return {
        "schemaVersion": 2,
        "generatedAt": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "latestDate": current_data["date"],
        "latestTime": current_data["time"],
        "tradingDayCount": len(history),
        "industryCount": INDUSTRY_COUNT,
        "industryNames": list(industry_names),
        "sectors": sectors,
        "history": history,
        "sources": {
            "entry": ENTRY_URL,
            "past": past_url,
            "current": current_url,
        },
        "fundFlow": None,
    }


def is_finite_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def validate_payload(payload):
    required_top_level = {
        "schemaVersion",
        "generatedAt",
        "latestDate",
        "latestTime",
        "tradingDayCount",
        "industryCount",
        "industryNames",
        "sectors",
        "history",
        "sources",
        "fundFlow",
    }

    missing = required_top_level - set(payload)
    if missing:
        raise UpdateError(
            "payload 缺少字段：{}".format(", ".join(sorted(missing)))
        )

    if payload["schemaVersion"] != 2:
        raise UpdateError("schemaVersion 必须为 2")

    if payload["industryCount"] != INDUSTRY_COUNT:
        raise UpdateError("industryCount 不一致")

    names = payload["industryNames"]
    if not isinstance(names, list) or len(names) != INDUSTRY_COUNT:
        raise UpdateError("industryNames 数量不正确")

    if len(set(names)) != INDUSTRY_COUNT or any(
        not isinstance(name, str) or not name.strip()
        for name in names
    ):
        raise UpdateError("industryNames 必须是 33 个非空唯一字符串")

    history = payload["history"]
    if not isinstance(history, list):
        raise UpdateError("history 必须是数组")

    if not MIN_HISTORY_DAYS <= len(history) <= MAX_HISTORY_DAYS:
        raise UpdateError("history 交易日数量超出允许范围")

    if payload["tradingDayCount"] != len(history):
        raise UpdateError("tradingDayCount 与 history 长度不一致")

    history_dates = []

    for record in history:
        if set(record) != {"date", "time", "values"}:
            raise UpdateError("history 记录字段不正确")

        normalize_date(record["date"])
        normalize_time(record["time"])

        values = record["values"]

        if not isinstance(values, list) or len(values) != INDUSTRY_COUNT:
            raise UpdateError("history.values 必须包含 33 个数值")

        if not all(is_finite_number(value) for value in values):
            raise UpdateError("history.values 含有无效数值")

        history_dates.append(record["date"])

    if history_dates != sorted(history_dates):
        raise UpdateError("history 未按日期升序排列")

    if len(set(history_dates)) != len(history_dates):
        raise UpdateError("history 中存在重复日期")

    if history_dates[-1] != payload["latestDate"]:
        raise UpdateError("latestDate 与 history 最新日期不一致")

    sectors = payload["sectors"]

    if not isinstance(sectors, list) or len(sectors) != INDUSTRY_COUNT:
        raise UpdateError("sectors 数量不正确")

    required_sector_fields = {
        "id",
        "code",
        "name",
        "current",
        "change",
        "previousClose",
        "dailyPercent",
        "return5d",
        "return20d",
        "rs5",
        "rs20",
        "rank",
        "previousRank",
        "rankChange",
        "streak",
        "strength",
        "rotationState",
        "fundFlow",
        "fundFlowRank",
    }

    ranks = []
    previous_ranks = []

    for industry_index, sector in enumerate(sectors):
        missing_sector_fields = required_sector_fields - set(sector)

        if missing_sector_fields:
            raise UpdateError(
                "sector {} 缺少字段：{}".format(
                    industry_index,
                    ", ".join(sorted(missing_sector_fields)),
                )
            )

        if sector["fundFlow"] is not None:
            raise UpdateError("sector fundFlow 必须为 null")

        if sector["fundFlowRank"] is not None:
            raise UpdateError("sector fundFlowRank 必须为 null")

        ranks.append(sector["rank"])
        previous_ranks.append(sector["previousRank"])

    expected_ranks = list(range(1, INDUSTRY_COUNT + 1))

    if sorted(ranks) != expected_ranks:
        raise UpdateError("rank 不是完整的 1 到 33 排名")

    if sorted(previous_ranks) != expected_ranks:
        raise UpdateError("previousRank 不是完整的 1 到 33 排名")

    if payload["fundFlow"] is not None:
        raise UpdateError("顶层 fundFlow 必须为 null")

    try:
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise UpdateError(
            "payload 无法安全序列化为 JSON：{}".format(exc)
        ) from exc


SCRIPT_BLOCK_PATTERN = re.compile(
    r"(?P<open><script\b[^>]*>)"
    r"(?P<body>.*?)"
    r"(?P<close></script\s*>)",
    flags=re.IGNORECASE | re.DOTALL,
)

ID_ATTRIBUTE_PATTERN = re.compile(
    r"""
    \bid\s*=\s*
    (?:
        (?P<quote>["'])
        (?P<quoted>.*?)
        (?P=quote)
        |
        (?P<unquoted>[^\s>]+)
    )
    """,
    flags=re.IGNORECASE | re.VERBOSE | re.DOTALL,
)


def get_script_id(opening_tag):
    match = ID_ATTRIBUTE_PATTERN.search(opening_tag)

    if not match:
        return None, None

    value = (
        match.group("quoted")
        if match.group("quoted") is not None
        else match.group("unquoted")
    )

    return value, match


def replace_script_id(opening_tag, id_match, new_id):
    if id_match.group("quoted") is not None:
        start = id_match.start("quoted")
        end = id_match.end("quoted")
    else:
        start = id_match.start("unquoted")
        end = id_match.end("unquoted")

    return opening_tag[:start] + new_id + opening_tag[end:]


def replace_embedded_data(index_html, json_text):
    blocks = []

    for block_match in SCRIPT_BLOCK_PATTERN.finditer(index_html):
        script_id, id_match = get_script_id(block_match.group("open"))
        if script_id is not None:
            blocks.append((block_match, script_id, id_match))

    primary = [
        item
        for item in blocks
        if item[1] == "daily-update-data"
    ]

    if len(primary) > 1:
        raise UpdateError(
            'index.html 中存在多个 id="daily-update-data" script'
        )

    use_fallback = False

    if primary:
        selected = primary[0]
    else:
        fallback = [
            item
            for item in blocks
            if item[1] == "embedded-data"
        ]

        if len(fallback) > 1:
            raise UpdateError(
                'index.html 中存在多个 id="embedded-data" script'
            )

        if not fallback:
            raise UpdateError(
                'index.html 中既没有 id="daily-update-data"，'
                '也没有 id="embedded-data"'
            )

        selected = fallback[0]
        use_fallback = True

    block_match, _script_id, id_match = selected
    opening_tag = block_match.group("open")

    if use_fallback:
        opening_tag = replace_script_id(
            opening_tag,
            id_match,
            "daily-update-data",
        )

    replacement = (
        opening_tag
        + "\n"
        + json_text
        + "\n"
        + block_match.group("close")
    )

    return (
        index_html[:block_match.start()]
        + replacement
        + index_html[block_match.end():]
    )


def atomic_write_text(path, content):
    path = Path(path)

    if not path.exists():
        raise UpdateError("目标文件不存在：{}".format(path))

    original_mode = stat.S_IMODE(path.stat().st_mode)
    file_descriptor = None
    temporary_name = None

    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".{}.".format(path.name),
            suffix=".tmp",
            dir=str(path.parent),
        )

        with os.fdopen(
            file_descriptor,
            "w",
            encoding="utf-8",
            newline=""
        ) as handle:
            file_descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        os.chmod(temporary_name, original_mode)
        os.replace(temporary_name, path)
        temporary_name = None

    except Exception as exc:
        raise UpdateError(
            "原子写入 {} 失败：{}".format(path, exc)
        ) from exc

    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass

        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def main():
    try:
        opener = build_opener(
            HTTPCookieProcessor(CookieJar())
        )

        entry_final_url, entry_text = fetch_text(
            opener,
            ENTRY_URL
        )
        print("入口页成功: {}".format(entry_final_url))

        direct_script_urls = discover_script_urls(
            entry_text,
            entry_final_url,
        )

        past_candidates = []

        for script_url in direct_script_urls:
            if is_target_script(
                script_url,
                "country_jp_gyo_past.js",
            ):
                past_candidates.append(script_url)

        for referenced_url in find_past_references(
            entry_text,
            entry_final_url,
        ):
            if referenced_url not in past_candidates:
                past_candidates.append(referenced_url)

        support_texts = []
        initial_references = list(direct_script_urls)

        for script_url in find_javascript_references(
            entry_text,
            entry_final_url,
        ):
            if script_url not in initial_references:
                initial_references.append(script_url)

        queue = []
        queued = set()

        for script_url in initial_references:
            if not is_same_site(script_url):
                continue

            if is_target_script(
                script_url,
                "country_jp_gyo_past.js",
            ):
                if script_url not in past_candidates:
                    past_candidates.append(script_url)
                continue

            if is_target_script(
                script_url,
                "country_jp_gyo.js",
            ):
                continue

            if script_url not in queued:
                queued.add(script_url)
                queue.append(script_url)

        fetched_support_count = 0

        while queue:
            current_name_map = merged_industry_name_map(
                [entry_text] + support_texts
            )

            if (
                past_candidates
                and has_complete_industry_names(current_name_map)
            ):
                break

            if fetched_support_count >= MAX_SUPPORT_SCRIPTS:
                break

            requested_url = queue.pop(0)

            script_final_url, script_text = fetch_text(
                opener,
                requested_url,
            )

            fetched_support_count += 1
            support_texts.append(script_text)

            for referenced_url in find_past_references(
                script_text,
                script_final_url,
            ):
                if (
                    is_same_site(referenced_url)
                    and referenced_url not in past_candidates
                ):
                    past_candidates.append(referenced_url)

            for nested_url in find_javascript_references(
                script_text,
                script_final_url,
            ):
                if not is_same_site(nested_url):
                    continue

                if is_target_script(
                    nested_url,
                    "country_jp_gyo_past.js",
                ):
                    if nested_url not in past_candidates:
                        past_candidates.append(nested_url)
                    continue

                if is_target_script(
                    nested_url,
                    "country_jp_gyo.js",
                ):
                    continue

                if nested_url not in queued:
                    queued.add(nested_url)
                    queue.append(nested_url)

        if not past_candidates:
            raise UpdateError(
                "未能从入口 HTML 或同站 JS 中发现 "
                "country_jp_gyo_past.js"
            )

        past_url = past_candidates[0]
        print("发现past URL: {}".format(past_url))

        current_url = derive_current_url(past_url)
        print("派生current URL: {}".format(current_url))

        _current_final_url, current_text = fetch_text(
            opener,
            current_url,
        )

        current_data = parse_current_js(current_text)

        print(
            "current解析成功: {} {}，{}个行业".format(
                current_data["date"],
                current_data["time"],
                len(current_data["percentages"]),
            )
        )

        _past_final_url, past_text = fetch_text(
            opener,
            past_url,
        )

        past_records = parse_past_js(past_text)

        print(
            "past解析成功: {}个交易日".format(
                len(past_records)
            )
        )

        industry_names = collect_industry_names(
            [entry_text]
            + support_texts
            + [current_text, past_text]
        )

        print(
            "行业名解析成功: {}个唯一行业".format(
                len(industry_names)
            )
        )

        records = merge_history(
            past_records,
            current_data,
        )

        sectors = calculate_metrics(
            records,
            industry_names,
            current_data,
        )

        print(
            "指标计算成功: {}个行业".format(
                len(sectors)
            )
        )

        payload = build_payload(
            industry_names=industry_names,
            records=records,
            sectors=sectors,
            current_data=current_data,
            past_url=past_url,
            current_url=current_url,
        )

        validate_payload(payload)

        try:
            index_html = INDEX_PATH.read_text(
                encoding="utf-8"
            )
        except Exception as exc:
            raise UpdateError(
                "读取 index.html 失败：{}".format(exc)
            ) from exc

        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ).replace("</", "<\\/")

        updated_html = replace_embedded_data(
            index_html,
            payload_json,
        )

        if updated_html == index_html:
            raise UpdateError(
                "index.html 内容未发生变化，拒绝写入"
            )

        atomic_write_text(
            INDEX_PATH,
            updated_html
        )

        print(
            "index.html写入成功: {}".format(
                INDEX_PATH
            )
        )

        return 0

    except UpdateError as exc:
        print(
            "ERROR: {}".format(exc),
            file=sys.stderr
        )
        return 1

    except Exception as exc:
        print(
            "ERROR: 未预期失败 {}: {}".format(
                type(exc).__name__,
                exc,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
