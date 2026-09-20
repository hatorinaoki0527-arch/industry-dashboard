#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import datetime as dt
import html
import json
import math
import os
import re
import stat
import sys
import tempfile
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path


ENTRY_URL = "https://nikkei225jp.com/chart/gyoushu.php"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = PROJECT_ROOT / "index.html"

INDUSTRY_COUNT = 33
MIN_TRADING_DAYS = 21
MAX_TRADING_DAYS = 90
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

PAST_FILENAME = "country_jp_gyo_past.js"
CURRENT_FILENAME = "country_jp_gyo.js"

ENTRY_PARTS = urllib.parse.urlsplit(ENTRY_URL)
ENTRY_HOSTNAME = (ENTRY_PARTS.hostname or "").lower()


class UpdateError(RuntimeError):
    pass


class ScriptSourceParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "script":
            return

        for key, value in attrs:
            if key.lower() == "src" and value:
                self.sources.append(value)
                break


def build_http_opener():
    cookie_jar = CookieJar()
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar)
    )


def is_same_site(url):
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False

    return (
        parts.scheme.lower() in ("http", "https")
        and (parts.hostname or "").lower() == ENTRY_HOSTNAME
    )


def normalize_resource_url(reference, base_url):
    if not reference:
        raise UpdateError("发现了空的资源 URL")

    reference = html.unescape(reference.strip())
    reference = reference.replace("\\/", "/")

    absolute = urllib.parse.urljoin(base_url, reference)
    parts = urllib.parse.urlsplit(absolute)

    if parts.scheme.lower() not in ("http", "https"):
        raise UpdateError("不支持的资源 URL 协议: {}".format(absolute))

    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, parts.query, "")
    )


def detect_declared_charset(raw, headers):
    charset = None

    try:
        charset = headers.get_content_charset()
    except (AttributeError, LookupError):
        charset = None

    if charset:
        return charset

    head = raw[:4096]
    match = re.search(
        br"""charset\s*=\s*["']?\s*([A-Za-z0-9._-]+)""",
        head,
        flags=re.IGNORECASE,
    )

    if match:
        try:
            return match.group(1).decode("ascii")
        except UnicodeDecodeError:
            return None

    return None


def decode_response(raw, headers, url):
    candidates = []

    declared = detect_declared_charset(raw, headers)
    if declared:
        candidates.append(declared)

    candidates.extend(
        [
            "utf-8-sig",
            "utf-8",
            "cp932",
            "shift_jis",
            "euc_jp",
        ]
    )

    tried = set()

    for encoding in candidates:
        normalized = encoding.lower()
        if normalized in tried:
            continue
        tried.add(normalized)

        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue

    raise UpdateError("无法解码网络响应: {}".format(url))


def fetch_text(opener, url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/javascript,text/javascript,"
                "application/xhtml+xml,*/*;q=0.8"
            ),
            "Referer": ENTRY_URL,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
        method="GET",
    )

    try:
        with opener.open(request, timeout=30) as response:
            status = getattr(response, "status", 200)

            if status != 200:
                raise UpdateError(
                    "HTTP 请求失败，状态码 {}: {}".format(status, url)
                )

            raw = response.read(MAX_DOWNLOAD_BYTES + 1)

            if len(raw) > MAX_DOWNLOAD_BYTES:
                raise UpdateError("网络响应过大: {}".format(url))

            return decode_response(raw, response.headers, url)

    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError(
            "网络请求失败 {}: {}".format(url, exc)
        ) from exc


def extract_same_site_script_urls(entry_html):
    parser = ScriptSourceParser()

    try:
        parser.feed(entry_html)
    except Exception as exc:
        raise UpdateError(
            "入口页 script 标签解析失败: {}".format(exc)
        ) from exc

    urls = []
    seen = set()

    for source in parser.sources:
        try:
            url = normalize_resource_url(source, ENTRY_URL)
        except UpdateError:
            continue

        if not is_same_site(url) or url in seen:
            continue

        seen.add(url)
        urls.append(url)

    return urls


PAST_REFERENCE_RE = re.compile(
    r"""
    (?P<ref>
        (?:
            (?:https?:)?//
            |
            (?:\.\.?/)+
            |
            /
        )?
        [A-Za-z0-9_%~./:@+\-]*
        country_jp_gyo_past\.js
        (?:\?[^\\'"<>\s\)]*)?
    )
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)


def find_past_references(text, base_url):
    searchable = text.replace("\\/", "/")
    results = []
    seen = set()

    for match in PAST_REFERENCE_RE.finditer(searchable):
        reference = match.group("ref")

        try:
            url = normalize_resource_url(reference, base_url)
        except UpdateError:
            continue

        if not is_same_site(url):
            continue

        path = urllib.parse.urlsplit(url).path.lower()

        if not path.endswith("/" + PAST_FILENAME) and not path.endswith(
            PAST_FILENAME
        ):
            continue

        if url not in seen:
            seen.add(url)
            results.append(url)

    return results


def discover_past_url(entry_html, fetched_scripts):
    candidates = []
    seen = set()

    def add_candidates(items):
        for item in items:
            if item not in seen:
                seen.add(item)
                candidates.append(item)

    add_candidates(find_past_references(entry_html, ENTRY_URL))

    for script_url, script_text in fetched_scripts.items():
        add_candidates(find_past_references(script_text, script_url))

    if not candidates:
        raise UpdateError(
            "未能从入口页或同站脚本中发现 {}".format(PAST_FILENAME)
        )

    return candidates[0]


def derive_current_url(past_url):
    parts = urllib.parse.urlsplit(past_url)

    new_path, replacement_count = re.subn(
        re.escape(PAST_FILENAME) + r"$",
        CURRENT_FILENAME,
        parts.path,
        count=1,
        flags=re.IGNORECASE,
    )

    if replacement_count != 1:
        raise UpdateError(
            "无法从 past URL 派生 current URL: {}".format(past_url)
        )

    current_url = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, new_path, parts.query, parts.fragment)
    )

    if not is_same_site(current_url):
        raise UpdateError("派生出的 current URL 不是同站 URL")

    return current_url


def decode_javascript_string(value):
    output = []
    index = 0
    length = len(value)

    simple_escapes = {
        "'": "'",
        '"': '"',
        "\\": "\\",
        "/": "/",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
    }

    while index < length:
        char = value[index]

        if char != "\\":
            output.append(char)
            index += 1
            continue

        index += 1

        if index >= length:
            raise UpdateError("JavaScript 字符串末尾存在无效转义")

        escape = value[index]
        index += 1

        if escape in simple_escapes:
            output.append(simple_escapes[escape])
            continue

        if escape == "u":
            digits = value[index:index + 4]

            if len(digits) != 4 or not re.fullmatch(
                r"[0-9A-Fa-f]{4}", digits
            ):
                raise UpdateError(
                    "JavaScript 字符串包含无效的 Unicode 转义"
                )

            output.append(chr(int(digits, 16)))
            index += 4
            continue

        if escape == "x":
            digits = value[index:index + 2]

            if len(digits) != 2 or not re.fullmatch(
                r"[0-9A-Fa-f]{2}", digits
            ):
                raise UpdateError(
                    "JavaScript 字符串包含无效的十六进制转义"
                )

            output.append(chr(int(digits, 16)))
            index += 2
            continue

        output.append(escape)

    return "".join(output)


def extract_scalar_string(text, variable_name):
    pattern = re.compile(
        r"""
        \b%s\b
        \s*=\s*
        (?P<quote>["'])
        (?P<value>(?:\\.|[^"'\\])*)
        (?P=quote)
        \s*;
        """
        % re.escape(variable_name),
        flags=re.DOTALL | re.VERBOSE,
    )

    matches = list(pattern.finditer(text))

    if not matches:
        raise UpdateError(
            "current JS 缺少标量字符串 {}".format(variable_name)
        )

    return decode_javascript_string(
        matches[-1].group("value")
    ).strip()


def parse_number(value, label):
    cleaned = value.strip()

    if not cleaned:
        raise UpdateError("{} 包含空数值".format(label))

    try:
        number = float(cleaned)
    except ValueError as exc:
        raise UpdateError(
            "{} 包含无效数值: {}".format(label, cleaned)
        ) from exc

    if not math.isfinite(number):
        raise UpdateError("{} 包含非有限数值".format(label))

    return number


def parse_number_list(value, label):
    parts = [
        item.strip()
        for item in re.split(r"[,_]", value)
        if item.strip() != ""
    ]

    if len(parts) != INDUSTRY_COUNT:
        raise UpdateError(
            "{} 必须恰好包含 {} 个值，实际为 {}".format(
                label,
                INDUSTRY_COUNT,
                len(parts),
            )
        )

    return [
        parse_number(item, "{}[{}]".format(label, index))
        for index, item in enumerate(parts)
    ]


def normalize_date(value):
    cleaned = value.strip()

    match = re.fullmatch(
        r"(\d{4})\s*(?:[-/.]|年)\s*(\d{1,2})\s*(?:[-/.]|月)\s*(\d{1,2})\s*日?",
        cleaned,
    )

    if not match:
        raise UpdateError("无效日期: {}".format(value))

    year, month, day = (int(part) for part in match.groups())

    try:
        parsed = dt.date(year, month, day)
    except ValueError as exc:
        raise UpdateError("无效日期: {}".format(value)) from exc

    return parsed.isoformat()


def normalize_time(value):
    cleaned = value.strip()

    match = re.fullmatch(
        r"(\d{1,2}):(\d{2})(?::(\d{2}))?",
        cleaned,
    )

    if not match:
        raise UpdateError("无效时间: {}".format(value))

    hour = int(match.group(1))
    minute = int(match.group(2))
    second_text = match.group(3)
    second = int(second_text) if second_text is not None else 0

    if hour > 23 or minute > 59 or second > 59:
        raise UpdateError("无效时间: {}".format(value))

    if second_text is not None:
        return "{:02d}:{:02d}:{:02d}".format(
            hour, minute, second
        )

    return "{:02d}:{:02d}".format(hour, minute)


def time_sort_key(value):
    normalized = normalize_time(value)
    parts = [int(part) for part in normalized.split(":")]

    if len(parts) == 2:
        parts.append(0)

    return tuple(parts)


def parse_current_js(text):
    mod_date = normalize_date(
        extract_scalar_string(text, "ModDate")
    )

    mod_time = normalize_time(
        extract_scalar_string(text, "ModTime")
    )

    current_points = parse_number_list(
        extract_scalar_string(text, "G1"),
        "G1",
    )

    change_points = parse_number_list(
        extract_scalar_string(text, "G2"),
        "G2",
    )

    previous_closes = []
    daily_percents = []

    for index, (current_point, change_point) in enumerate(
        zip(current_points, change_points)
    ):
        previous_close = current_point - change_point

        if previous_close <= 0:
            raise UpdateError(
                "行业 {} 的 previousClose 必须大于 0".format(index)
            )

        daily_percent = (
            change_point / previous_close * 100.0
        )

        previous_closes.append(previous_close)
        daily_percents.append(daily_percent)

    return {
        "date": mod_date,
        "time": mod_time,
        "points": current_points,
        "changes": change_points,
        "previousCloses": previous_closes,
        "dailyPercents": daily_percents,
    }


PAST_ASSIGNMENT_RE = re.compile(
    r"""
    \bGY
    \s*\[
        [^\]]+
    \]
    \s*=\s*
    (?P<quote>["'])
    (?P<value>(?:\\.|[^"'\\])*)
    (?P=quote)
    \s*;
    (?:\s*q\s*\+\+\s*;)?
    """,
    flags=re.DOTALL | re.VERBOSE,
)


def parse_past_js(text):
    records_by_date = {}
    matches = list(PAST_ASSIGNMENT_RE.finditer(text))

    if not matches:
        raise UpdateError(
            "past JS 中未找到 GY[...] 历史点位记录"
        )

    for sequence, match in enumerate(matches):
        decoded = decode_javascript_string(
            match.group("value")
        )

        parts = [
            item.strip()
            for item in decoded.split(",")
        ]

        expected_count = 2 + INDUSTRY_COUNT

        if len(parts) != expected_count:
            raise UpdateError(
                "past JS 的 GY 记录必须包含 {} 项，实际为 {}".format(
                    expected_count,
                    len(parts),
                )
            )

        record_date = normalize_date(parts[0])
        record_time = normalize_time(parts[1])

        points = [
            parse_number(
                item,
                "past {} industry {}".format(
                    record_date,
                    index,
                ),
            )
            for index, item in enumerate(parts[2:])
        ]

        record = {
            "date": record_date,
            "time": record_time,
            "points": points,
            "_sequence": sequence,
        }

        old_record = records_by_date.get(record_date)

        if old_record is None:
            records_by_date[record_date] = record
            continue

        old_key = (
            time_sort_key(old_record["time"]),
            old_record["_sequence"],
        )

        new_key = (
            time_sort_key(record_time),
            sequence,
        )

        if new_key >= old_key:
            records_by_date[record_date] = record

    records = sorted(
        records_by_date.values(),
        key=lambda item: item["date"],
    )

    for record in records:
        record.pop("_sequence", None)

    return records


GY_NAME_RE = re.compile(
    r"""
    \bGyo
    \s*\[
        \s*(?P<index>\d+)\s*
    \]
    \s*=\s*
    (?P<quote>["'])
    (?P<value>(?:\\.|[^"'\\])*)
    (?P=quote)
    \s*;
    """,
    flags=re.DOTALL | re.VERBOSE,
)


def parse_industry_names(text_sources):
    names_by_index = {}

    for _, text in text_sources:
        for match in GY_NAME_RE.finditer(text):
            index = int(match.group("index"))

            if index < 0 or index >= INDUSTRY_COUNT:
                continue

            if index in names_by_index:
                continue

            name = decode_javascript_string(
                match.group("value")
            ).strip()

            if not name:
                raise UpdateError(
                    "Gyo[{}] 行业名为空".format(index)
                )

            names_by_index[index] = name

    expected_indexes = set(range(INDUSTRY_COUNT))
    actual_indexes = set(names_by_index)

    if actual_indexes != expected_indexes:
        missing = sorted(
            expected_indexes - actual_indexes
        )

        raise UpdateError(
            "行业名必须覆盖 Gyo[0] 至 Gyo[32]；缺失={}".format(
                missing
            )
        )

    names = [
        names_by_index[index]
        for index in range(INDUSTRY_COUNT)
    ]

    if len(set(names)) != INDUSTRY_COUNT:
        raise UpdateError(
            "33 个行业名必须唯一"
        )

    return names


def merge_point_records(
    past_records,
    current,
):
    records_by_date = {
        record["date"]: {
            "date": record["date"],
            "time": record["time"],
            "points": list(record["points"]),
        }
        for record in past_records
    }

    records_by_date[current["date"]] = {
        "date": current["date"],
        "time": current["time"],
        "points": list(current["points"]),
    }

    records = sorted(
        records_by_date.values(),
        key=lambda item: item["date"],
    )

    if records[-1]["date"] != current["date"]:
        raise UpdateError(
            "current 日期不是最新交易日"
        )

    if len(records) < MIN_TRADING_DAYS + 1:
        raise UpdateError(
            "点位历史不足：至少需要 {} 个日期".format(
                MIN_TRADING_DAYS + 1
            )
        )

    return records


def percent_change(latest, base, label):
    if base <= 0:
        raise UpdateError(
            "{} 的基准点位必须大于 0".format(label)
        )

    result = (
        latest / base - 1.0
    ) * 100.0

    if not math.isfinite(result):
        raise UpdateError(
            "{} 计算结果无效".format(label)
        )

    return result


def compute_daily_values(
    records,
    record_index,
    latest_override=None,
):
    current_points = records[record_index]["points"]
    previous_points = records[record_index - 1]["points"]

    values = [
        percent_change(
            current_points[index],
            previous_points[index],
            "dailyPercent",
        )
        for index in range(INDUSTRY_COUNT)
    ]

    if (
        latest_override is not None
        and record_index == len(records) - 1
    ):
        values = list(latest_override)

    return values


def compute_snapshot(
    records,
    record_index,
    daily_override=None,
):
    if record_index < 20:
        raise UpdateError(
            "计算 20 日收益所需的点位历史不足"
        )

    daily_values = compute_daily_values(
        records,
        record_index,
        latest_override=daily_override,
    )

    return_5d = []
    return_20d = []

    for industry_index in range(INDUSTRY_COUNT):
        latest_point = records[record_index]["points"][
            industry_index
        ]

        return_5d.append(
            percent_change(
                latest_point,
                records[record_index - 5]["points"][
                    industry_index
                ],
                "return5d",
            )
        )

        return_20d.append(
            percent_change(
                latest_point,
                records[record_index - 20]["points"][
                    industry_index
                ],
                "return20d",
            )
        )

    average_daily = (
        sum(daily_values) / INDUSTRY_COUNT
    )

    average_5d = (
        sum(return_5d) / INDUSTRY_COUNT
    )

    average_20d = (
        sum(return_20d) / INDUSTRY_COUNT
    )

    relative_daily = [
        value - average_daily
        for value in daily_values
    ]

    rs5 = [
        value - average_5d
        for value in return_5d
    ]

    rs20 = [
        value - average_20d
        for value in return_20d
    ]

    strengths = [
        0.15 * relative_daily[index]
        + 0.35 * rs5[index]
        + 0.50 * rs20[index]
        for index in range(INDUSTRY_COUNT)
    ]

    rank_order = sorted(
        range(INDUSTRY_COUNT),
        key=lambda index: (
            -strengths[index],
            index,
        ),
    )

    ranks = {
        industry_index: rank_position + 1
        for rank_position, industry_index
        in enumerate(rank_order)
    }

    return {
        "daily": daily_values,
        "return5d": return_5d,
        "return20d": return_20d,
        "rs5": rs5,
        "rs20": rs20,
        "strength": strengths,
        "ranks": ranks,
    }


def compute_streaks(
    records,
    latest_daily_override,
    first_output_index,
):
    relative_history = []

    for record_index in range(
        first_output_index,
        len(records),
    ):
        override = (
            latest_daily_override
            if record_index == len(records) - 1
            else None
        )

        daily_values = compute_daily_values(
            records,
            record_index,
            latest_override=override,
        )

        average_daily = (
            sum(daily_values)
            / INDUSTRY_COUNT
        )

        relative_history.append(
            [
                value - average_daily
                for value in daily_values
            ]
        )

    streaks = []

    for industry_index in range(INDUSTRY_COUNT):
        latest_relative = (
            relative_history[-1][industry_index]
        )

        if abs(latest_relative) <= 1e-12:
            streaks.append(0)
            continue

        direction = (
            1
            if latest_relative > 0
            else -1
        )

        count = 0

        for daily_relative in reversed(
            relative_history
        ):
            value = (
                daily_relative[industry_index]
            )

            if value > 1e-12:
                current_direction = 1
            elif value < -1e-12:
                current_direction = -1
            else:
                current_direction = 0

            if current_direction != direction:
                break

            count += 1

        streaks.append(
            direction * count
        )

    return streaks


def classify_rotation(rs5, rs20):
    if rs5 >= 0 and rs20 >= 0:
        return "leading"

    if rs5 >= 0 and rs20 < 0:
        return "improving"

    if rs5 < 0 and rs20 >= 0:
        return "weakening"

    return "lagging"


def clean_float(value):
    if not math.isfinite(value):
        raise UpdateError(
            "输出中出现非有限数值"
        )

    rounded = round(
        float(value),
        6,
    )

    if rounded == 0:
        return 0.0

    return rounded


def build_payload(
    industry_names,
    records,
    current,
    sources,
):
    latest_index = len(records) - 1
    previous_index = latest_index - 1

    latest_snapshot = compute_snapshot(
        records,
        latest_index,
        daily_override=current["dailyPercents"],
    )

    previous_snapshot = compute_snapshot(
        records,
        previous_index,
        daily_override=None,
    )

    first_output_index = max(
        1,
        len(records) - MAX_TRADING_DAYS,
    )

    output_indexes = list(
        range(
            first_output_index,
            len(records),
        )
    )

    if len(output_indexes) < MIN_TRADING_DAYS:
        raise UpdateError(
            "完整交易日不足 {} 天".format(
                MIN_TRADING_DAYS
            )
        )

    streaks = compute_streaks(
        records,
        current["dailyPercents"],
        first_output_index,
    )

    sectors = []

    for index, name in enumerate(
        industry_names
    ):
        rank = latest_snapshot["ranks"][index]
        previous_rank = (
            previous_snapshot["ranks"][index]
        )

        sectors.append(
            {
                "index": index,
                "name": name,
                "currentPoint": clean_float(
                    current["points"][index]
                ),
                "changePoint": clean_float(
                    current["changes"][index]
                ),
                "previousClose": clean_float(
                    current["previousCloses"][index]
                ),
                "dailyPercent": clean_float(
                    latest_snapshot["daily"][index]
                ),
                "return5d": clean_float(
                    latest_snapshot["return5d"][index]
                ),
                "return20d": clean_float(
                    latest_snapshot["return20d"][index]
                ),
                "rs5": clean_float(
                    latest_snapshot["rs5"][index]
                ),
                "rs20": clean_float(
                    latest_snapshot["rs20"][index]
                ),
                "rank": int(rank),
                "previousRank": int(previous_rank),
                "rankChange": int(
                    previous_rank - rank
                ),
                "streak": int(
                    streaks[index]
                ),
                "strength": clean_float(
                    latest_snapshot["strength"][index]
                ),
                "rotationState": classify_rotation(
                    latest_snapshot["rs5"][index],
                    latest_snapshot["rs20"][index],
                ),
                "fundFlow": None,
                "fundFlowRank": None,
            }
        )

    history = []
    point_history = []

    for record_index in output_indexes:
        record = records[record_index]

        override = (
            current["dailyPercents"]
            if record_index == latest_index
            else None
        )

        daily_values = compute_daily_values(
            records,
            record_index,
            latest_override=override,
        )

        history.append(
            {
                "date": record["date"],
                "time": record["time"],
                "values": [
                    clean_float(value)
                    for value in daily_values
                ],
            }
        )

        point_history.append(
            {
                "date": record["date"],
                "time": record["time"],
                "values": [
                    clean_float(value)
                    for value in record["points"]
                ],
            }
        )

    payload = {
        "schemaVersion": 2,
        "generatedAt": (
            dt.datetime.now(
                dt.timezone.utc
            )
            .isoformat(
                timespec="seconds"
            )
            .replace(
                "+00:00",
                "Z"
            )
        ),
        "latestDate": current["date"],
        "latestTime": current["time"],
        "tradingDayCount": len(output_indexes),
        "industryCount": INDUSTRY_COUNT,
        "industryNames": list(
            industry_names
        ),
        "sectors": sectors,
        "history": history,
        "pointHistory": point_history,
        "sources": {
            "entry": sources["entry"],
            "past": sources["past"],
            "current": sources["current"],
        },
        "fundFlow": None,
    }

    validate_payload(payload)

    return payload


def validate_payload(payload):
    if payload["schemaVersion"] != 2:
        raise UpdateError(
            "schemaVersion 必须为 2"
        )

    if payload["industryCount"] != INDUSTRY_COUNT:
        raise UpdateError(
            "industryCount 必须为 33"
        )

    if len(payload["industryNames"]) != INDUSTRY_COUNT:
        raise UpdateError(
            "industryNames 数量错误"
        )

    if len(set(payload["industryNames"])) != INDUSTRY_COUNT:
        raise UpdateError(
            "industryNames 必须唯一"
        )

    if not (
        MIN_TRADING_DAYS
        <= payload["tradingDayCount"]
        <= MAX_TRADING_DAYS
    ):
        raise UpdateError(
            "tradingDayCount 不在允许范围内"
        )

    if len(payload["sectors"]) != INDUSTRY_COUNT:
        raise UpdateError(
            "sectors 数量错误"
        )

    if payload["fundFlow"] is not None:
        raise UpdateError(
            "fundFlow 必须为 null"
        )

    if len(payload["history"]) != payload["tradingDayCount"]:
        raise UpdateError(
            "history 长度错误"
        )

    if len(payload["pointHistory"]) != payload["tradingDayCount"]:
        raise UpdateError(
            "pointHistory 长度错误"
        )

    ranks = []
    previous_ranks = []

    for sector in payload["sectors"]:
        ranks.append(sector["rank"])
        previous_ranks.append(
            sector["previousRank"]
        )

        if sector["fundFlow"] is not None:
            raise UpdateError(
                "sector fundFlow 必须为 null"
            )

        if sector["fundFlowRank"] is not None:
            raise UpdateError(
                "sector fundFlowRank 必须为 null"
            )

        if (
            sector["rankChange"]
            != sector["previousRank"]
            - sector["rank"]
        ):
            raise UpdateError(
                "rankChange 计算错误"
            )

    expected = list(
        range(
            1,
            INDUSTRY_COUNT + 1,
        )
    )

    if sorted(ranks) != expected:
        raise UpdateError(
            "rank 必须完整覆盖 1~33"
        )

    if sorted(previous_ranks) != expected:
        raise UpdateError(
            "previousRank 必须完整覆盖 1~33"
        )

    json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
    )


SCRIPT_BLOCK_RE = re.compile(
    r"<script\b[^>]*>.*?</script\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)

SCRIPT_ID_RE = re.compile(
    r"""\bid\s*=\s*(?P<quote>["'])(?P<value>[^"']+)(?P=quote)""",
    flags=re.IGNORECASE,
)


def replace_embedded_json(
    index_html,
    payload_json,
):
    daily_blocks = []
    embedded_blocks = []

    for match in SCRIPT_BLOCK_RE.finditer(
        index_html
    ):
        block = match.group(0)
        opening_end = block.find(">")

        if opening_end < 0:
            continue

        opening_tag = block[
            :opening_end + 1
        ]

        id_match = SCRIPT_ID_RE.search(
            opening_tag
        )

        if not id_match:
            continue

        script_id = id_match.group(
            "value"
        )

        if script_id == "daily-update-data":
            daily_blocks.append(
                (
                    match,
                    opening_tag,
                    id_match,
                )
            )
        elif script_id == "embedded-data":
            embedded_blocks.append(
                (
                    match,
                    opening_tag,
                    id_match,
                )
            )

    if len(daily_blocks) > 1:
        raise UpdateError(
            "存在多个 daily-update-data"
        )

    if daily_blocks:
        match, opening_tag, id_match = (
            daily_blocks[0]
        )
        change_id = False
    else:
        if len(embedded_blocks) != 1:
            raise UpdateError(
                "找不到唯一的数据 script"
            )

        match, opening_tag, id_match = (
            embedded_blocks[0]
        )

        change_id = True

    if change_id:
        start = id_match.start("value")
        end = id_match.end("value")

        opening_tag = (
            opening_tag[:start]
            + "daily-update-data"
            + opening_tag[end:]
        )

    replacement = (
        opening_tag
        + "\n"
        + payload_json
        + "\n</script>"
    )

    return (
        index_html[:match.start()]
        + replacement
        + index_html[match.end():]
    )


def atomic_write_text(
    path,
    text,
):
    path = Path(path)

    old_mode = stat.S_IMODE(
        path.stat().st_mode
    )

    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=str(path.parent),
            prefix=".index.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(
                temp_file.name
            )

            temp_file.write(text)
            temp_file.flush()
            os.fsync(
                temp_file.fileno()
            )

        os.chmod(
            temp_path,
            old_mode,
        )

        os.replace(
            temp_path,
            path,
        )

        temp_path = None

    finally:
        if (
            temp_path is not None
            and temp_path.exists()
        ):
            temp_path.unlink()


def main():
    opener = build_http_opener()

    entry_html = fetch_text(
        opener,
        ENTRY_URL,
    )

    print("入口页成功")

    script_urls = extract_same_site_script_urls(
        entry_html
    )

    fetched_scripts = {}

    for script_url in script_urls:
        try:
            fetched_scripts[
                script_url
            ] = fetch_text(
                opener,
                script_url,
            )
        except UpdateError:
            continue

    past_url = discover_past_url(
        entry_html,
        fetched_scripts,
    )

    print(
        "发现past URL: {}".format(
            past_url
        )
    )

    current_url = derive_current_url(
        past_url
    )

    print(
        "派生current URL: {}".format(
            current_url
        )
    )

    current_text = fetch_text(
        opener,
        current_url,
    )

    current = parse_current_js(
        current_text
    )

    print("current解析成功")

    past_text = (
        fetched_scripts.get(
            past_url
        )
        or fetch_text(
            opener,
            past_url,
        )
    )

    past_records = parse_past_js(
        past_text
    )

    print(
        "past解析成功: {}个交易日".format(
            len(past_records)
        )
    )

    name_sources = [
        ("entry", entry_html)
    ]

    name_sources.extend(
        fetched_scripts.items()
    )

    name_sources.append(
        (
            "current",
            current_text,
        )
    )

    name_sources.append(
        (
            "past",
            past_text,
        )
    )

    industry_names = parse_industry_names(
        name_sources
    )

    print(
        "行业名解析成功: 33个"
    )

    records = merge_point_records(
        past_records,
        current,
    )

    payload = build_payload(
        industry_names,
        records,
        current,
        {
            "entry": ENTRY_URL,
            "past": past_url,
            "current": current_url,
        },
    )

    print("指标计算成功")

    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    ).replace(
        "</",
        "<\\/",
    )

    try:
        old_html = INDEX_PATH.read_text(
            encoding="utf-8"
        )
    except Exception as exc:
        raise UpdateError(
            "读取 index.html 失败: {}".format(
                exc
            )
        ) from exc

    new_html = replace_embedded_json(
        old_html,
        payload_json,
    )

    if new_html == old_html:
        raise UpdateError(
            "index.html 未产生变化"
        )

    atomic_write_text(
        INDEX_PATH,
        new_html,
    )

    print(
        "index.html写入成功"
    )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except UpdateError as exc:
        print(
            "更新失败: {}".format(exc),
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:
        print(
            "更新失败: {}".format(exc),
            file=sys.stderr,
        )
        sys.exit(1)
