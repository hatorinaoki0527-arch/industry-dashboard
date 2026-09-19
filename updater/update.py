#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import gzip
import html
import json
import math
import os
import re
import sys
import tempfile
import time
import zlib
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener


PAGE_URL = "https://nikkei225jp.com/chart/gyoushu.php"

FALLBACK_CURRENT_URL = (
    "https://nikkei225jp.com/_data/_nfsDATA/min/country_jp_gyo.js"
)

FALLBACK_PAST_URL = (
    "https://nikkei225jp.com/_data/_nfsDATA/min/country_jp_gyo_past.js"
)

MIN_HISTORY_DAYS = 21
HISTORY_KEEP_DAYS = 90

FETCH_RETRIES = 3
FETCH_TIMEOUT = 30

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class UpdateError(RuntimeError):
    pass


class ScriptSourceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: List[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        if tag.lower() != "script":
            return

        for key, value in attrs:
            if key.lower() == "src" and value:
                self.sources.append(value.strip())
                break


def build_session():
    cookie_jar = CookieJar()
    opener = build_opener(
        HTTPCookieProcessor(cookie_jar)
    )
    return opener, cookie_jar


def request_headers(
    referer: Optional[str],
    is_page: bool
) -> Dict[str, str]:

    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "close",
    }

    if is_page:
        headers.update({
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,"
                "image/avif,image/webp,"
                "image/apng,*/*;q=0.8"
            ),
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1",
        })
    else:
        headers.update({
            "Accept": "*/*",
            "Sec-Fetch-Dest": "script",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "same-origin",
        })

    if referer:
        headers["Referer"] = referer

    return headers


def decompress_body(
    data: bytes,
    content_encoding: str
) -> bytes:

    encoding = (content_encoding or "").lower().strip()

    if encoding == "gzip":
        return gzip.decompress(data)

    if encoding == "deflate":
        try:
            return zlib.decompress(data)
        except zlib.error:
            return zlib.decompress(
                data,
                -zlib.MAX_WBITS
            )

    return data


def normalize_charset_name(
    value: Optional[str]
) -> Optional[str]:

    if not value:
        return None

    name = value.strip().lower().replace("-", "_")

    aliases = {
        "utf8": "utf-8",
        "utf_8": "utf-8",
        "utf8_sig": "utf-8-sig",
        "utf_8_sig": "utf-8-sig",
        "shiftjis": "shift_jis",
        "shift_jis": "shift_jis",
        "sjis": "shift_jis",
        "x_sjis": "shift_jis",
        "windows_31j": "cp932",
        "ms932": "cp932",
        "cp932": "cp932",
        "eucjp": "euc_jp",
        "euc_jp": "euc_jp",
        "x_euc_jp": "euc_jp",
    }

    return aliases.get(name, name)


def detect_meta_charset(
    data: bytes
) -> Optional[str]:

    head = data[:8192]

    patterns = (
        rb"charset\s*=\s*[\"']?\s*([A-Za-z0-9._-]+)",
        rb"encoding\s*=\s*[\"']\s*([A-Za-z0-9._-]+)",
    )

    for pattern in patterns:
        match = re.search(
            pattern,
            head,
            flags=re.IGNORECASE
        )

        if match:
            try:
                return normalize_charset_name(
                    match.group(1).decode("ascii")
                )
            except UnicodeDecodeError:
                pass

    return None


def decode_text(
    data: bytes,
    header_charset: Optional[str]
) -> str:

    candidates: List[str] = []

    for candidate in (
        normalize_charset_name(header_charset),
        detect_meta_charset(data),
        "utf-8-sig",
        "utf-8",
        "euc_jp",
        "cp932",
        "shift_jis",
    ):
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    errors: List[str] = []

    for encoding in candidates:
        try:
            return data.decode(encoding)

        except (
            UnicodeDecodeError,
            LookupError
        ) as exc:

            errors.append(
                f"{encoding}: {exc}"
            )

    raise UpdateError(
        "响应内容无法使用 utf-8/cp932/"
        "shift_jis/euc_jp 解码；"
        + " | ".join(errors)
    )


def fetch_text(
    opener,
    url: str,
    referer: Optional[str],
    is_page: bool = False
) -> str:

    last_error: Optional[BaseException] = None

    for attempt in range(
        1,
        FETCH_RETRIES + 1
    ):
        try:
            request = Request(
                url,
                headers=request_headers(
                    referer=referer,
                    is_page=is_page
                ),
                method="GET",
            )

            with opener.open(
                request,
                timeout=FETCH_TIMEOUT
            ) as response:

                status = getattr(
                    response,
                    "status",
                    response.getcode()
                )

                if status != 200:
                    raise UpdateError(
                        f"HTTP 状态码不是 200：{status}"
                    )

                raw = response.read()

                if not raw:
                    raise UpdateError(
                        "响应内容为空"
                    )

                raw = decompress_body(
                    raw,
                    response.headers.get(
                        "Content-Encoding",
                        ""
                    )
                )

                charset = (
                    response.headers
                    .get_content_charset()
                )

                return decode_text(
                    raw,
                    charset
                )

        except (
            HTTPError,
            URLError,
            TimeoutError,
            OSError,
            UpdateError
        ) as exc:

            last_error = exc

            if attempt < FETCH_RETRIES:
                print(
                    f"抓取重试 "
                    f"{attempt}/{FETCH_RETRIES - 1}："
                    f"{url}；原因：{exc}",
                    flush=True
                )

                time.sleep(
                    1.5 * attempt
                )

    raise UpdateError(
        f"抓取失败: {url}; {last_error}"
    )


def discover_script_urls(
    page_html: str
) -> Tuple[
    Optional[str],
    Optional[str]
]:

    parser = ScriptSourceParser()
    parser.feed(page_html)
    parser.close()

    current_url: Optional[str] = None
    past_url: Optional[str] = None

    for source in parser.sources:

        absolute_url = urljoin(
            PAGE_URL,
            html.unescape(source)
        )

        path = urlparse(
            absolute_url
        ).path.lower()

        if path.endswith(
            "/country_jp_gyo_past.js"
        ):
            if past_url is None:
                past_url = absolute_url

        elif path.endswith(
            "/country_jp_gyo.js"
        ):
            if current_url is None:
                current_url = absolute_url

    return current_url, past_url


def scan_js_quoted_string(
    text: str,
    quote_position: int
) -> Tuple[str, int]:

    if (
        quote_position >= len(text)
        or text[quote_position] not in ("'", '"')
    ):
        raise UpdateError(
            "JS 字符串起始引号无效"
        )

    quote = text[quote_position]

    escaped = False
    chars: List[str] = []
    index = quote_position + 1

    while index < len(text):

        char = text[index]

        if escaped:
            chars.append("\\")
            chars.append(char)
            escaped = False
            index += 1
            continue

        if char == "\\":
            escaped = True
            index += 1
            continue

        if char == quote:
            return (
                "".join(chars),
                index + 1
            )

        chars.append(char)
        index += 1

    raise UpdateError(
        "JS 字符串缺少结束引号"
    )


def unescape_js_string(
    value: str
) -> str:

    result: List[str] = []
    index = 0

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
        "0": "\0",
    }

    while index < len(value):

        char = value[index]

        if char != "\\":
            result.append(char)
            index += 1
            continue

        index += 1

        if index >= len(value):
            result.append("\\")
            break

        escape = value[index]

        if escape in simple_escapes:
            result.append(
                simple_escapes[escape]
            )
            index += 1
            continue

        if (
            escape == "u"
            and index + 4 < len(value)
        ):
            digits = value[
                index + 1:index + 5
            ]

            if re.fullmatch(
                r"[0-9A-Fa-f]{4}",
                digits
            ):
                result.append(
                    chr(int(digits, 16))
                )
                index += 5
                continue

        if (
            escape == "x"
            and index + 2 < len(value)
        ):
            digits = value[
                index + 1:index + 3
            ]

            if re.fullmatch(
                r"[0-9A-Fa-f]{2}",
                digits
            ):
                result.append(
                    chr(int(digits, 16))
                )
                index += 3
                continue

        if escape == "\r":
            index += 1

            if (
                index < len(value)
                and value[index] == "\n"
            ):
                index += 1

            continue

        if escape == "\n":
            index += 1
            continue

        result.append(escape)
        index += 1

    return "".join(result)


def extract_assignment_strings(
    js_text: str,
    variable: str
) -> List[str]:

    pattern = re.compile(
        rf"\b{re.escape(variable)}\b"
        r"\s*=\s*(['\"])",
        flags=re.IGNORECASE
    )

    values: List[str] = []

    for match in pattern.finditer(
        js_text
    ):
        raw, _ = scan_js_quoted_string(
            js_text,
            match.end(1) - 1
        )

        values.append(
            unescape_js_string(
                raw
            ).strip()
        )

    return values


def extract_required_assignment(
    js_text: str,
    variable: str
) -> str:

    values = extract_assignment_strings(
        js_text,
        variable
    )

    if not values:
        raise UpdateError(
            f"当前 JS 中未找到 "
            f"{variable} 赋值"
        )

    return values[-1]


def parse_indexed_strings(
    js_text: str,
    variable: str
) -> Dict[int, str]:

    pattern = re.compile(
        rf"\b{re.escape(variable)}\s*"
        r"\[\s*(\d+)\s*\]"
        r"\s*=\s*(['\"])",
        flags=re.IGNORECASE
    )

    result: Dict[int, str] = {}

    for match in pattern.finditer(
        js_text
    ):
        raw, _ = scan_js_quoted_string(
            js_text,
            match.end(2) - 1
        )

        result[
            int(match.group(1))
        ] = html.unescape(
            unescape_js_string(
                raw
            ).strip()
        )

    return result


def parse_number(
    value: str
) -> float:

    cleaned = (
        value.strip()
        .replace("％", "")
        .replace("%", "")
        .replace("＋", "+")
        .replace("−", "-")
        .replace("－", "-")
        .replace("　", "")
    )

    if not cleaned:
        raise UpdateError(
            "发现空数值"
        )

    try:
        number = float(cleaned)

    except ValueError as exc:
        raise UpdateError(
            f"无法解析数值：{value!r}"
        ) from exc

    if not math.isfinite(number):
        raise UpdateError(
            f"发现非有限数值：{value!r}"
        )

    return number


def parse_number_list(
    value: str
) -> List[float]:

    parts = [
        part.strip()
        for part in re.split(
            r"[,_]",
            value
        )
        if part.strip()
    ]

    numbers = [
        parse_number(part)
        for part in parts
    ]

    if len(numbers) != 33:
        raise UpdateError(
            "数值数量错误："
            f"应为 33，实际为 {len(numbers)}"
        )

    return numbers


def parse_date(
    value: str
) -> date:

    text = value.strip()

    match = re.search(
        r"(?<!\d)"
        r"(20\d{2})\D+"
        r"(\d{1,2})\D+"
        r"(\d{1,2})"
        r"(?!\d)",
        text
    )

    if match:
        try:
            return date(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3))
            )

        except ValueError as exc:
            raise UpdateError(
                f"日期无效：{value!r}"
            ) from exc

    compact = re.search(
        r"(?<!\d)"
        r"(20\d{2})"
        r"(\d{2})"
        r"(\d{2})"
        r"(?!\d)",
        text
    )

    if compact:
        try:
            return date(
                int(compact.group(1)),
                int(compact.group(2)),
                int(compact.group(3))
            )

        except ValueError as exc:
            raise UpdateError(
                f"日期无效：{value!r}"
            ) from exc

    raise UpdateError(
        f"无法解析日期：{value!r}"
    )


def parse_time(
    value: str
) -> Tuple[
    str,
    Tuple[int, int, int]
]:

    text = value.strip()

    match = re.search(
        r"(?<!\d)"
        r"(\d{1,2})\D+"
        r"(\d{1,2})"
        r"(?:\D+(\d{1,2}))?"
        r"(?!\d)",
        text
    )

    if not match:
        raise UpdateError(
            f"无法解析时间：{value!r}"
        )

    hour = int(match.group(1))
    minute = int(match.group(2))
    second = int(
        match.group(3) or 0
    )

    if (
        hour > 23
        or minute > 59
        or second > 59
    ):
        raise UpdateError(
            f"时间无效：{value!r}"
        )

    normalized = (
        f"{hour:02d}:"
        f"{minute:02d}:"
        f"{second:02d}"
    )

    return (
        normalized,
        (hour, minute, second)
    )


def parse_industry_names(
    page_html: str,
    current_js: str
) -> List[str]:

    page_names = parse_indexed_strings(
        page_html,
        "Gyo"
    )

    current_names = parse_indexed_strings(
        current_js,
        "Gyo"
    )

    merged = dict(current_names)
    merged.update(page_names)

    expected_indexes = set(
        range(33)
    )

    actual_indexes = set(
        merged.keys()
    )

    if (
        actual_indexes
        != expected_indexes
    ):
        missing = sorted(
            expected_indexes
            - actual_indexes
        )

        extra = sorted(
            actual_indexes
            - expected_indexes
        )

        raise UpdateError(
            "行业名称必须完整包含 "
            "Gyo[0] 到 Gyo[32]；"
            f"缺少={missing}，"
            f"额外={extra}"
        )

    names = [
        merged[index].strip()
        for index in range(33)
    ]

    if any(
        not name
        for name in names
    ):
        raise UpdateError(
            "行业名称中存在空值"
        )

    if len(set(names)) != 33:
        raise UpdateError(
            "行业名称不是 33 个唯一值"
        )

    return names


def parse_current_js(
    current_js: str
) -> Tuple[
    date,
    str,
    List[float],
    List[float]
]:

    current_date = parse_date(
        extract_required_assignment(
            current_js,
            "ModDate"
        )
    )

    current_time, _ = parse_time(
        extract_required_assignment(
            current_js,
            "ModTime"
        )
    )

    current_values = parse_number_list(
        extract_required_assignment(
            current_js,
            "G1"
        )
    )

    current_changes = parse_number_list(
        extract_required_assignment(
            current_js,
            "G2"
        )
    )

    if any(
        value <= 0
        for value in current_values
    ):
        raise UpdateError(
            "G1 中存在小于或等于 0 "
            "的行业指数值"
        )

    return (
        current_date,
        current_time,
        current_values,
        current_changes
    )


def parse_past_js(
    past_js: str
) -> Tuple[
    List[Dict[str, object]],
    int
]:

    pattern = re.compile(
        r"\bGY\s*"
        r"\[\s*([^\]]+?)\s*\]"
        r"\s*=\s*(['\"])",
        flags=re.IGNORECASE
    )

    rows_by_date: Dict[
        date,
        Dict[str, object]
    ] = {}

    raw_count = 0

    for match in pattern.finditer(
        past_js
    ):
        raw_count += 1

        raw, _ = scan_js_quoted_string(
            past_js,
            match.end(2) - 1
        )

        record = unescape_js_string(
            raw
        ).strip()

        parts = record.split(",", 2)

        if len(parts) != 3:
            raise UpdateError(
                "历史记录格式错误，必须是 "
                "date,time,33values："
                f"{record!r}"
            )

        row_date = parse_date(
            parts[0]
        )

        normalized_time, time_key = (
            parse_time(
                parts[1]
            )
        )

        values = parse_number_list(
            parts[2]
        )

        if any(
            value <= 0
            for value in values
        ):
            raise UpdateError(
                "历史记录 "
                f"{row_date.isoformat()} "
                "中存在小于或等于 0 "
                "的指数值"
            )

        candidate: Dict[str, object] = {
            "date_obj": row_date,
            "date": row_date.isoformat(),
            "time": normalized_time,
            "time_key": time_key,
            "values": values,
        }

        previous = rows_by_date.get(
            row_date
        )

        if (
            previous is None
            or time_key
            > previous["time_key"]
        ):
            rows_by_date[
                row_date
            ] = candidate

    if raw_count == 0:
        raise UpdateError(
            "历史 JS 中未找到任何 "
            "GY[q] 记录"
        )

    rows = [
        rows_by_date[key]
        for key in sorted(
            rows_by_date
        )
    ]

    return rows, raw_count


def merge_history(
    past_rows: List[
        Dict[str, object]
    ],
    current_date: date,
    current_time: str,
    current_values: List[float]
) -> List[Dict[str, object]]:

    rows_by_date: Dict[
        date,
        Dict[str, object]
    ] = {}

    for row in past_rows:

        row_date = row["date_obj"]

        if not isinstance(
            row_date,
            date
        ):
            raise UpdateError(
                "历史日期内部格式错误"
            )

        if row_date > current_date:
            raise UpdateError(
                "历史 JS 出现晚于当前日期的记录："
                f"{row_date.isoformat()} > "
                f"{current_date.isoformat()}"
            )

        rows_by_date[
            row_date
        ] = row

    normalized_time, time_key = (
        parse_time(
            current_time
        )
    )

    rows_by_date[
        current_date
    ] = {
        "date_obj": current_date,
        "date": current_date.isoformat(),
        "time": normalized_time,
        "time_key": time_key,
        "values": list(
            current_values
        ),
    }

    merged = [
        rows_by_date[key]
        for key in sorted(
            rows_by_date
        )
    ]

    if len(merged) < MIN_HISTORY_DAYS:
        raise UpdateError(
            "交易日历史不足："
            f"至少需要 {MIN_HISTORY_DAYS} 日，"
            f"实际只有 {len(merged)} 日"
        )

    return merged[
        -HISTORY_KEEP_DAYS:
    ]


def percentage_return(
    current: float,
    previous: float
) -> float:

    if previous <= 0:
        raise UpdateError(
            "计算收益率时发现前值"
            "小于或等于 0"
        )

    return (
        current / previous
        - 1.0
    ) * 100.0


def average(
    values: Sequence[float]
) -> float:

    if not values:
        raise UpdateError(
            "无法计算空列表平均值"
        )

    return (
        sum(values)
        / len(values)
    )


def ranking(
    values: Sequence[float]
) -> List[int]:

    if len(values) != 33:
        raise UpdateError(
            "排名输入必须有 33 个数值"
        )

    order = sorted(
        range(33),
        key=lambda index: (
            -values[index],
            index
        )
    )

    ranks = [0] * 33

    for rank_value, index in enumerate(
        order,
        start=1
    ):
        ranks[index] = rank_value

    return ranks


def rounded(
    value: Optional[float],
    digits: int = 4
):

    if value is None:
        return None

    result = round(
        float(value),
        digits
    )

    if result == 0:
        return 0.0

    return result


def compute_metrics(
    names: List[str],
    history_rows: List[
        Dict[str, object]
    ],
    current_changes: List[float]
) -> Tuple[
    List[Dict[str, object]],
    List[Dict[str, object]]
]:

    if (
        len(names) != 33
        or len(current_changes) != 33
    ):
        raise UpdateError(
            "指标计算输入必须包含 "
            "33 个行业"
        )

    if (
        len(history_rows)
        < MIN_HISTORY_DAYS
    ):
        raise UpdateError(
            "指标计算时历史交易日不足"
        )

    daily_returns: List[
        Optional[List[float]]
    ] = [None]

    for row_index in range(
        1,
        len(history_rows)
    ):

        previous_values = (
            history_rows[
                row_index - 1
            ]["values"]
        )

        current_values = (
            history_rows[
                row_index
            ]["values"]
        )

        if (
            not isinstance(
                previous_values,
                list
            )
            or not isinstance(
                current_values,
                list
            )
        ):
            raise UpdateError(
                "历史 values 格式错误"
            )

        if (
            len(previous_values) != 33
            or len(current_values) != 33
        ):
            raise UpdateError(
                "历史记录的行业指数数量"
                "不是 33"
            )

        returns = [
            percentage_return(
                current_values[i],
                previous_values[i]
            )
            for i in range(33)
        ]

        daily_returns.append(
            returns
        )

    daily_returns[-1] = list(
        current_changes
    )

    current_values = (
        history_rows[-1]["values"]
    )

    five_day_values = (
        history_rows[-6]["values"]
    )

    twenty_day_values = (
        history_rows[-21]["values"]
    )

    if not isinstance(
        current_values,
        list
    ):
        raise UpdateError(
            "当前 values 格式错误"
        )

    if not isinstance(
        five_day_values,
        list
    ):
        raise UpdateError(
            "5 日前 values 格式错误"
        )

    if not isinstance(
        twenty_day_values,
        list
    ):
        raise UpdateError(
            "20 日前 values 格式错误"
        )

    five_day_returns = [
        percentage_return(
            current_values[i],
            five_day_values[i]
        )
        for i in range(33)
    ]

    twenty_day_returns = [
        percentage_return(
            current_values[i],
            twenty_day_values[i]
        )
        for i in range(33)
    ]

    current_average = average(
        current_changes
    )

    five_day_average = average(
        five_day_returns
    )

    twenty_day_average = average(
        twenty_day_returns
    )

    current_rs = [
        current_changes[i]
        - current_average
        for i in range(33)
    ]

    five_day_rs = [
        five_day_returns[i]
        - five_day_average
        for i in range(33)
    ]

    twenty_day_rs = [
        twenty_day_returns[i]
        - twenty_day_average
        for i in range(33)
    ]

    current_ranks = ranking(
        current_rs
    )

    five_day_ranks = ranking(
        five_day_rs
    )

    daily_relative_strength: List[
        Optional[List[float]]
    ] = []

    for returns in daily_returns:

        if returns is None:
            daily_relative_strength.append(
                None
            )
            continue

        daily_average = average(
            returns
        )

        daily_relative_strength.append([
            returns[i]
            - daily_average
            for i in range(33)
        ])

    summary: List[
        Dict[str, object]
    ] = []

    for index, name in enumerate(
        names
    ):

        strong_days: Optional[int]
        weak_days: Optional[int]

        reliable_days = [
            row[index]
            for row
            in daily_relative_strength
            if row is not None
        ]

        if not reliable_days:

            strong_days = None
            weak_days = None

        elif reliable_days[-1] > 0:

            count = 0

            for value in reversed(
                reliable_days
            ):
                if value > 0:
                    count += 1
                else:
                    break

            strong_days = count
            weak_days = 0

        else:

            count = 0

            for value in reversed(
                reliable_days
            ):
                if value <= 0:
                    count += 1
                else:
                    break

            strong_days = 0
            weak_days = count

        day_rs = current_rs[index]
        day5_rs = five_day_rs[index]

        if (
            day_rs > 0
            and day5_rs > 0
        ):
            rotation_state: Optional[
                str
            ] = "强势延续"

        elif (
            day_rs > 0
            and day5_rs <= 0
        ):
            rotation_state = "转强"

        elif (
            day_rs <= 0
            and day5_rs > 0
        ):
            rotation_state = "转弱"

        else:
            rotation_state = "弱势延续"

        summary.append({
            "日期":
                history_rows[-1]["date"],

            "时间":
                history_rows[-1]["time"],

            "市场":
                "日本",

            "分类体系":
                "东证33",

            "一级行业":
                name,

            "行业指数名称":
                name,

            "行业指数值":
                rounded(
                    current_values[index],
                    6
                ),

            "行业指数涨跌幅(%)":
                rounded(
                    current_changes[index]
                ),

            "33行业当日平均涨跌幅(%)":
                rounded(
                    current_average
                ),

            "当日相对强度(%)":
                rounded(
                    day_rs
                ),

            "5日收益率(%)":
                rounded(
                    five_day_returns[
                        index
                    ]
                ),

            "20日收益率(%)":
                rounded(
                    twenty_day_returns[
                        index
                    ]
                ),

            "5日相对强度(%)":
                rounded(
                    day5_rs
                ),

            "20日相对强度(%)":
                rounded(
                    twenty_day_rs[
                        index
                    ]
                ),

            "当日强弱排名":
                current_ranks[index],

            "5日排名":
                five_day_ranks[index],

            "排名变化":
                five_day_ranks[index]
                - current_ranks[index],

            "连续强势天数":
                strong_days,

            "连续弱势天数":
                weak_days,

            "轮动状态":
                rotation_state,

            "数据状态":
                "来源：nikkei225jp.com",
        })

    history_payload: List[
        Dict[str, object]
    ] = []

    for row_index, row in enumerate(
        history_rows
    ):

        values = row["values"]

        returns = daily_returns[
            row_index
        ]

        if not isinstance(
            values,
            list
        ):
            raise UpdateError(
                "输出历史时发现 values "
                "格式错误"
            )

        history_payload.append({
            "日期":
                row["date"],

            "时间":
                row["time"],

            "行业指数值":{
                names[i]:
                    rounded(
                        values[i],
                        6
                    )
                for i in range(33)
            },

            "当日收益率(%)":
                None
                if returns is None
                else {
                    names[i]:
                        rounded(
                            returns[i]
                        )
                    for i in range(33)
                }
        })

    return (
        summary,
        history_payload
    )


def replace_embedded_data(
    index_text: str,
    payload_json: str
) -> str:

    pattern = re.compile(
        r"("
        r"<script\b"
        r"(?=[^>]*\s+id\s*=\s*"
        r"[\"']embedded-data[\"'])"
        r"[^>]*>"
        r")"
        r"(.*?)"
        r"(</script\s*>)",
        flags=
            re.IGNORECASE
            | re.DOTALL
    )

    matches = list(
        pattern.finditer(
            index_text
        )
    )

    if len(matches) != 1:
        raise UpdateError(
            "index.html 中必须且只能存在一个 "
            'id="embedded-data" 的 script，'
            f"实际找到 {len(matches)} 个"
        )

    return pattern.sub(
        lambda match:
            match.group(1)
            + "\n"
            + payload_json
            + "\n"
            + match.group(3),
        index_text,
        count=1
    )


def atomic_write(
    path: Path,
    content: str
) -> None:

    if not path.exists():
        raise UpdateError(
            f"文件不存在：{path}"
        )

    original_mode = (
        path.stat().st_mode
    )

    temp_path: Optional[
        Path
    ] = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            delete=False,
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as temp_file:

            temp_path = Path(
                temp_file.name
            )

            temp_file.write(
                content
            )

            temp_file.flush()

            os.fsync(
                temp_file.fileno()
            )

        os.chmod(
            temp_path,
            original_mode
        )

        os.replace(
            temp_path,
            path
        )

        temp_path = None

    finally:
        if (
            temp_path is not None
            and temp_path.exists()
        ):
            temp_path.unlink()


def main() -> None:

    repo_root = (
        Path(__file__)
        .resolve()
        .parents[1]
    )

    index_path = (
        repo_root
        / "index.html"
    )

    opener, _cookie_jar = (
        build_session()
    )

    page_html = fetch_text(
        opener,
        PAGE_URL,
        referer="https://nikkei225jp.com/",
        is_page=True
    )

    print(
        f"页面抓取成功：{PAGE_URL}",
        flush=True
    )

    discovered_current, discovered_past = (
        discover_script_urls(
            page_html
        )
    )

    if (
        discovered_current is None
        or discovered_past is None
    ):

        missing: List[str] = []

        if discovered_current is None:
            missing.append(
                "country_jp_gyo.js"
            )

        if discovered_past is None:
            missing.append(
                "country_jp_gyo_past.js"
            )

        print(
            "自动发现 URL 失败："
            "页面中未找到 "
            + "、".join(missing)
            + "；使用回退 URL",
            flush=True
        )

        current_url = (
            discovered_current
            or FALLBACK_CURRENT_URL
        )

        past_url = (
            discovered_past
            or FALLBACK_PAST_URL
        )

    else:
        current_url = (
            discovered_current
        )

        past_url = (
            discovered_past
        )

    print(
        f"当前数据 URL：{current_url}",
        flush=True
    )

    print(
        f"历史数据 URL：{past_url}",
        flush=True
    )

    current_js = fetch_text(
        opener,
        current_url,
        referer=PAGE_URL,
        is_page=False
    )

    past_js = fetch_text(
        opener,
        past_url,
        referer=PAGE_URL,
        is_page=False
    )

    names = parse_industry_names(
        page_html,
        current_js
    )

    (
        current_date,
        current_time,
        current_values,
        current_changes
    ) = parse_current_js(
        current_js
    )

    print(
        "当前数据解析成功："
        "行业名称=33，"
        "G1=33，"
        "G2=33，"
        f"日期={current_date.isoformat()}，"
        f"时间={current_time}",
        flush=True
    )

    past_rows, raw_past_count = (
        parse_past_js(
            past_js
        )
    )

    print(
        "历史数据解析成功："
        f"原始记录={raw_past_count}，"
        f"按日期去重后={len(past_rows)}",
        flush=True
    )

    history_rows = merge_history(
        past_rows,
        current_date,
        current_time,
        current_values
    )

    (
        summary,
        history_payload
    ) = compute_metrics(
        names,
        history_rows,
        current_changes
    )

    print(
        "指标计算成功："
        f"行业=33，"
        f"保留交易日={len(history_payload)}",
        flush=True
    )

    jst = timezone(
        timedelta(hours=9)
    )

    updated_at = (
        datetime.now(jst)
        .replace(microsecond=0)
        .isoformat()
    )

    payload = {
        "summary":
            summary,

        "rep":
            [],

        "act":
            [],

        "detail":
            [],

        "history":
            history_payload,

        "industries":
            names,

        "updated_at":
            updated_at,

        "source":{
            "page":
                PAGE_URL,

            "current":
                current_url,

            "past":
                past_url
        }
    }

    try:
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False
        )

    except (
        TypeError,
        ValueError
    ) as exc:

        raise UpdateError(
            "payload JSON 序列化失败："
            f"{exc}"
        ) from exc

    if not index_path.exists():
        raise UpdateError(
            "未找到 index.html："
            f"{index_path}"
        )

    try:
        index_text = (
            index_path
            .read_text(
                encoding="utf-8"
            )
        )

    except (
        OSError,
        UnicodeDecodeError
    ) as exc:

        raise UpdateError(
            "读取 index.html 失败："
            f"{exc}"
        ) from exc

    new_index_text = (
        replace_embedded_data(
            index_text,
            payload_json
        )
    )

    if new_index_text == index_text:
        raise UpdateError(
            "embedded-data 替换后"
            "内容没有变化"
        )

    atomic_write(
        index_path,
        new_index_text
    )

    print(
        "index.html 写入成功："
        f"{index_path}",
        flush=True
    )


if __name__ == "__main__":

    try:
        main()

    except Exception as exc:

        print(
            "更新失败，旧 index.html 保持不变："
            f"{exc}",
            file=sys.stderr,
            flush=True
        )

        sys.exit(1)
