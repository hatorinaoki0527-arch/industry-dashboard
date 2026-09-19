#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener


ENTRY_URL = "https://nikkei225jp.com/chart/gyoushu.php"
SECTOR_COUNT = 33
MIN_TRADING_DAYS = 21
MAX_HISTORY_DAYS = 90
MAX_RESPONSE_BYTES = 20 * 1024 * 1024

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class UpdateError(RuntimeError):
    pass


class ScriptSrcParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "script":
            return
        attr_map = {str(k).lower(): v for k, v in attrs}
        src = attr_map.get("src")
        if src:
            self.sources.append(src.strip())


def log(message):
    print(message, flush=True)


def short_error(exc):
    text = str(exc).strip().replace("\n", " ")
    return text[:300] if text else exc.__class__.__name__


def decode_js_string(literal):
    if len(literal) < 2 or literal[0] not in "\"'" or literal[-1] != literal[0]:
        raise UpdateError("无效的 JavaScript 字符串")

    body = literal[1:-1]
    result = []
    i = 0

    escapes = {
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "b": "\b",
        "f": "\f",
        "v": "\v",
        "0": "\0",
        "\\": "\\",
        "/": "/",
        '"': '"',
        "'": "'",
    }

    while i < len(body):
        char = body[i]

        if char != "\\":
            result.append(char)
            i += 1
            continue

        i += 1

        if i >= len(body):
            result.append("\\")
            break

        escaped = body[i]

        if escaped == "\n":
            i += 1
            continue

        if escaped == "\r":
            i += 1
            if i < len(body) and body[i] == "\n":
                i += 1
            continue

        if escaped in escapes:
            result.append(escapes[escaped])
            i += 1
            continue

        if escaped == "x" and i + 2 < len(body):
            digits = body[i + 1:i + 3]

            if re.fullmatch(r"[0-9A-Fa-f]{2}", digits):
                result.append(chr(int(digits, 16)))
                i += 3
                continue

        if escaped == "u" and i + 4 < len(body):
            digits = body[i + 1:i + 5]

            if re.fullmatch(r"[0-9A-Fa-f]{4}", digits):
                codepoint = int(digits, 16)
                i += 5

                if (
                    0xD800 <= codepoint <= 0xDBFF
                    and i + 5 < len(body)
                    and body[i:i + 2] == "\\u"
                    and re.fullmatch(r"[0-9A-Fa-f]{4}", body[i + 2:i + 6])
                ):
                    low = int(body[i + 2:i + 6], 16)

                    if 0xDC00 <= low <= 0xDFFF:
                        codepoint = (
                            0x10000
                            + ((codepoint - 0xD800) << 10)
                            + (low - 0xDC00)
                        )
                        i += 6

                result.append(chr(codepoint))
                continue

        result.append(escaped)
        i += 1

    return "".join(result)


JS_LITERAL_PATTERN = r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')'''


def scalar_assignments(text, variable):
    pattern = re.compile(
        r"\b"
        + re.escape(variable)
        + r"\b\s*=\s*("
        + JS_LITERAL_PATTERN
        + r")",
        re.IGNORECASE | re.DOTALL,
    )

    values = []

    for match in pattern.finditer(text):
        values.append(
            decode_js_string(
                match.group(1)
            )
        )

    return values


def last_scalar_assignment(text, variable):
    values = scalar_assignments(
        text,
        variable
    )

    if not values:
        raise UpdateError(
            f"current JS 中未找到 {variable} 标量赋值"
        )

    return values[-1].strip()


def indexed_assignments(text, variable):
    pattern = re.compile(
        r"\b"
        + re.escape(variable)
        + r"\s*\[\s*(\d+)\s*\]\s*=\s*("
        + JS_LITERAL_PATTERN
        + r")",
        re.IGNORECASE | re.DOTALL,
    )

    results = []

    for match in pattern.finditer(text):
        results.append(
            (
                int(match.group(1)),
                decode_js_string(
                    match.group(2)
                )
            )
        )

    return results


def response_charset(headers):
    content_type = headers.get(
        "Content-Type",
        ""
    )

    match = re.search(
        r"charset\s*=\s*[\"']?([A-Za-z0-9._-]+)",
        content_type,
        re.I
    )

    return (
        match.group(1)
        if match
        else None
    )


def decode_response(data, charset=None):
    encodings = []

    if data.startswith(b"\xef\xbb\xbf"):
        encodings.append("utf-8-sig")

    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings.append("utf-16")

    if charset:
        encodings.append(charset)

    encodings.extend(
        (
            "utf-8",
            "cp932",
            "shift_jis",
            "euc_jp",
            "iso2022_jp",
        )
    )

    seen = set()

    for encoding in encodings:
        key = encoding.lower()

        if key in seen:
            continue

        seen.add(key)

        try:
            return data.decode(encoding)

        except (
            LookupError,
            UnicodeDecodeError
        ):
            continue

    raise UpdateError(
        "响应文本编码无法识别"
    )


class WebClient:
    def __init__(self):
        self.cookie_jar = CookieJar()

        self.opener = build_opener(
            HTTPCookieProcessor(
                self.cookie_jar
            )
        )

    def get_text(
        self,
        url,
        referer=None,
        retries=3
    ):
        headers = {
            "User-Agent": USER_AGENT,
            "Accept":
                "text/html,"
                "application/javascript,"
                "text/javascript,"
                "*/*;q=0.8",
            "Accept-Language":
                "ja,en-US;q=0.8,en;q=0.6",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Connection": "close",
        }

        if referer:
            headers["Referer"] = referer

        last_error = None

        for attempt in range(retries):
            request = Request(
                url,
                headers=headers,
                method="GET"
            )

            try:
                with self.opener.open(
                    request,
                    timeout=30
                ) as response:

                    status = getattr(
                        response,
                        "status",
                        response.getcode()
                    )

                    if status != 200:
                        raise UpdateError(
                            f"HTTP {status}: {url}"
                        )

                    content_length = (
                        response.headers.get(
                            "Content-Length"
                        )
                    )

                    if content_length:
                        try:
                            if int(content_length) > MAX_RESPONSE_BYTES:
                                raise UpdateError(
                                    f"响应过大: {url}"
                                )
                        except ValueError:
                            pass

                    data = response.read(
                        MAX_RESPONSE_BYTES + 1
                    )

                    if len(data) > MAX_RESPONSE_BYTES:
                        raise UpdateError(
                            f"响应超过限制: {url}"
                        )

                    content_encoding = (
                        response.headers.get(
                            "Content-Encoding",
                            ""
                        ).lower()
                    )

                    if content_encoding == "gzip":
                        data = gzip.decompress(data)

                    elif content_encoding == "deflate":
                        try:
                            data = zlib.decompress(data)
                        except zlib.error:
                            data = zlib.decompress(
                                data,
                                -zlib.MAX_WBITS
                            )

                    return decode_response(
                        data,
                        response_charset(
                            response.headers
                        )
                    )

            except HTTPError as exc:
                last_error = exc

                if (
                    exc.code < 500
                    or attempt + 1 >= retries
                ):
                    raise UpdateError(
                        f"HTTP {exc.code}: {url}"
                    ) from exc

            except (
                URLError,
                TimeoutError,
                OSError,
                UpdateError
            ) as exc:
                last_error = exc

                if (
                    isinstance(
                        exc,
                        UpdateError
                    )
                    or attempt + 1 >= retries
                ):
                    if isinstance(
                        exc,
                        UpdateError
                    ):
                        raise

                    raise UpdateError(
                        f"抓取失败 {url}: "
                        f"{short_error(exc)}"
                    ) from exc

            time.sleep(
                1.0 * (attempt + 1)
            )

        raise UpdateError(
            f"抓取失败 {url}: "
            f"{short_error(last_error)}"
        )


def same_site(url, entry_url):
    candidate = urlsplit(url)
    entry = urlsplit(entry_url)

    candidate_host = (
        candidate.hostname
        or ""
    ).lower()

    entry_host = (
        entry.hostname
        or ""
    ).lower()

    if candidate_host.startswith("www."):
        candidate_host = candidate_host[4:]

    if entry_host.startswith("www."):
        entry_host = entry_host[4:]

    return (
        candidate.scheme in (
            "http",
            "https"
        )
        and candidate_host == entry_host
    )


def normalize_url(url):
    parts = urlsplit(
        html.unescape(
            url.strip()
        )
    )

    if parts.scheme not in (
        "http",
        "https"
    ):
        raise UpdateError(
            f"无效 URL: {url}"
        )

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            parts.query,
            ""
        )
    )


def deduplicate(items):
    result = []
    seen = set()

    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)

    return result


def discover_script_urls(entry_html):
    parser = ScriptSrcParser()

    try:
        parser.feed(entry_html)

    except Exception as exc:
        raise UpdateError(
            f"入口页 script 标签解析失败: "
            f"{short_error(exc)}"
        ) from exc

    urls = []

    for src in parser.sources:
        absolute = normalize_url(
            urljoin(
                ENTRY_URL,
                src
            )
        )

        if same_site(
            absolute,
            ENTRY_URL
        ):
            urls.append(absolute)

    return deduplicate(urls)


def normalize_reference_text(text):
    return (
        text
        .replace("\\/", "/")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\x2F", "/")
        .replace("\\x2f", "/")
    )


def extract_file_references(
    text,
    filename,
    base_url
):
    text = normalize_reference_text(
        html.unescape(text)
    )

    escaped_filename = re.escape(
        filename
    )

    pattern = re.compile(
        r"""
        (
            (?:(?:https?:)?//[^\s"'<>\\]*?"""
        + escaped_filename
        + r"""(?:\?[A-Za-z0-9._~%=&+\-]+)?)
            |
            (?:(?:/|\.\.?/)?[A-Za-z0-9._~%/\-]*"""
        + escaped_filename
        + r"""(?:\?[A-Za-z0-9._~%=&+\-]+)?)
        )
        """,
        re.IGNORECASE
        | re.VERBOSE,
    )

    urls = []

    for match in pattern.finditer(text):
        reference = (
            match.group(1)
            .rstrip("),;]")
        )

        if reference.startswith("//"):
            reference = (
                urlsplit(
                    base_url
                ).scheme
                + ":"
                + reference
            )

        absolute = normalize_url(
            urljoin(
                base_url,
                reference
            )
        )

        if same_site(
            absolute,
            ENTRY_URL
        ):
            urls.append(
                absolute
            )

    return deduplicate(urls)


def derive_current_url(past_url):
    parts = urlsplit(past_url)

    old_name = (
        "country_jp_gyo_past.js"
    )

    new_name = (
        "country_jp_gyo.js"
    )

    if not parts.path.lower().endswith(
        old_name
    ):
        raise UpdateError(
            "无法从 past URL 派生 "
            f"current URL: {past_url}"
        )

    new_path = (
        parts.path[:-len(old_name)]
        + new_name
    )

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            new_path,
            parts.query,
            ""
        )
    )


def normalize_date(value):
    value = value.strip()

    value = (
        value
        .replace("年", "/")
        .replace("月", "/")
        .replace("日", "")
        .replace(".", "/")
        .replace("-", "/")
    )

    match = re.search(
        r"(\d{4})\s*/\s*"
        r"(\d{1,2})\s*/\s*"
        r"(\d{1,2})",
        value
    )

    if match:
        year, month, day = map(
            int,
            match.groups()
        )

    else:
        match = re.search(
            r"(?<!\d)"
            r"(\d{4})(\d{2})(\d{2})"
            r"(?!\d)",
            value
        )

        if not match:
            raise UpdateError(
                f"无效日期: {value!r}"
            )

        year, month, day = map(
            int,
            match.groups()
        )

    try:
        return date(
            year,
            month,
            day
        ).isoformat()

    except ValueError as exc:
        raise UpdateError(
            f"无效日期: {value!r}"
        ) from exc


def normalize_time(value):
    value = value.strip()

    match = re.search(
        r"(?<!\d)"
        r"(\d{1,2})\s*:\s*"
        r"(\d{1,2})"
        r"(?:\s*:\s*(\d{1,2}))?",
        value
    )

    if match:
        hour = int(
            match.group(1)
        )

        minute = int(
            match.group(2)
        )

        second = int(
            match.group(3)
            or 0
        )

    else:
        digits = re.sub(
            r"\D",
            "",
            value
        )

        if len(digits) == 4:
            hour = int(
                digits[:2]
            )

            minute = int(
                digits[2:]
            )

            second = 0

        elif len(digits) == 6:
            hour = int(
                digits[:2]
            )

            minute = int(
                digits[2:4]
            )

            second = int(
                digits[4:]
            )

        else:
            raise UpdateError(
                f"无效时间: {value!r}"
            )

    if not (
        0 <= hour <= 23
        and 0 <= minute <= 59
        and 0 <= second <= 59
    ):
        raise UpdateError(
            f"无效时间: {value!r}"
        )

    if second:
        return (
            f"{hour:02d}:"
            f"{minute:02d}:"
            f"{second:02d}"
        )

    return (
        f"{hour:02d}:"
        f"{minute:02d}"
    )


def time_key(value):
    normalized = normalize_time(
        value
    )

    parts = [
        int(part)
        for part in normalized.split(":")
    ]

    while len(parts) < 3:
        parts.append(0)

    return (
        parts[0] * 3600
        + parts[1] * 60
        + parts[2]
    )


def parse_number(value):
    value = (
        value.strip()
        .replace("\u3000", "")
        .replace(" ", "")
        .replace("−", "-")
        .replace("－", "-")
        .replace("＋", "+")
        .replace("％", "")
        .replace("%", "")
        .replace("円", "")
    )

    if not value:
        raise UpdateError(
            "发现空数值"
        )

    try:
        number = float(value)

    except ValueError as exc:
        raise UpdateError(
            f"无效数值: {value!r}"
        ) from exc

    if not math.isfinite(number):
        raise UpdateError(
            f"非有限数值: {value!r}"
        )

    return number


def split_33_values(
    value,
    label
):
    parts = [
        item.strip()
        for item in re.split(
            r"[,_]",
            value
        )
        if item.strip()
    ]

    if len(parts) != SECTOR_COUNT:
        raise UpdateError(
            f"{label} 应有 "
            f"{SECTOR_COUNT} 个值，"
            f"实际为 {len(parts)} 个"
        )

    return [
        parse_number(item)
        for item in parts
    ]


def parse_current_js(text):
    for field in (
        "ModDate",
        "ModTime",
        "G1",
        "G2"
    ):
        if not re.search(
            r"\b"
            + re.escape(field)
            + r"\b",
            text
        ):
            raise UpdateError(
                f"current JS 缺少 {field}"
            )

    current_date = normalize_date(
        last_scalar_assignment(
            text,
            "ModDate"
        )
    )

    current_time = normalize_time(
        last_scalar_assignment(
            text,
            "ModTime"
        )
    )

    current_values = split_33_values(
        last_scalar_assignment(
            text,
            "G1"
        ),
        "G1"
    )

    changes = split_33_values(
        last_scalar_assignment(
            text,
            "G2"
        ),
        "G2"
    )

    for index, (
        current,
        change
    ) in enumerate(
        zip(
            current_values,
            changes
        )
    ):
        previous_close = (
            current - change
        )

        if (
            current <= 0
            or previous_close <= 0
        ):
            raise UpdateError(
                f"G1/G2 第 {index} 项"
                "无法计算真实涨跌幅"
            )

    return {
        "date": current_date,
        "time": current_time,
        "values": current_values,
        "changes": changes,
    }


def parse_past_js(text):
    assignments = indexed_assignments(
        text,
        "GY"
    )

    if not assignments:
        raise UpdateError(
            "past JS 中未找到 "
            "GY[q] 行式数据"
        )

    by_date = {}
    skipped = 0

    for _, raw_value in assignments:
        parts = [
            item.strip()
            for item in re.split(
                r"[,_]",
                raw_value
            )
            if item.strip()
        ]

        if len(parts) != (
            SECTOR_COUNT + 2
        ):
            skipped += 1
            continue

        try:
            row_date = normalize_date(
                parts[0]
            )

            row_time = normalize_time(
                parts[1]
            )

            values = [
                parse_number(item)
                for item in parts[2:]
            ]

            if (
                len(values)
                != SECTOR_COUNT
                or any(
                    value <= 0
                    for value in values
                )
            ):
                raise UpdateError(
                    "历史行业值数量"
                    "或范围无效"
                )

        except UpdateError:
            skipped += 1
            continue

        previous = by_date.get(
            row_date
        )

        if (
            previous is None
            or time_key(row_time)
            >= time_key(
                previous["time"]
            )
        ):
            by_date[
                row_date
            ] = {
                "date": row_date,
                "time": row_time,
                "values": values,
                "is_current": False,
                "source_changes": None,
            }

    if len(by_date) < (
        MIN_TRADING_DAYS - 1
    ):
        raise UpdateError(
            "past JS 仅解析出 "
            f"{len(by_date)} 个有效交易日"
        )

    rows = [
        by_date[key]
        for key in sorted(by_date)
    ]

    return rows, skipped


def parse_sector_names(
    text_sources
):
    names = {}

    for _, text in text_sources:
        for index, raw_name in (
            indexed_assignments(
                text,
                "Gyo"
            )
        ):
            if not (
                0 <= index < SECTOR_COUNT
            ):
                continue

            name = html.unescape(
                raw_name
            ).strip()

            if (
                name
                and index not in names
            ):
                names[index] = name

    missing = [
        index
        for index in range(
            SECTOR_COUNT
        )
        if index not in names
    ]

    if missing:
        raise UpdateError(
            "未能解析全部 "
            "Gyo[0..32] 行业名，缺少: "
            + ", ".join(
                map(str, missing)
            )
        )

    result = [
        names[index]
        for index in range(
            SECTOR_COUNT
        )
    ]

    if len(set(result)) != SECTOR_COUNT:
        raise UpdateError(
            "行业名存在重复，拒绝更新"
        )

    return result


def merge_history(
    past_rows,
    current
):
    rows_by_date = {}

    for row in past_rows:
        previous = rows_by_date.get(
            row["date"]
        )

        if (
            previous is None
            or time_key(
                row["time"]
            )
            >= time_key(
                previous["time"]
            )
        ):
            rows_by_date[
                row["date"]
            ] = row

    historical_dates_after_current = [
        row_date
        for row_date in rows_by_date
        if row_date > current["date"]
    ]

    if historical_dates_after_current:
        raise UpdateError(
            "past JS 存在晚于 "
            "current 日期的记录: "
            + ", ".join(
                sorted(
                    historical_dates_after_current
                )[:3]
            )
        )

    rows_by_date[
        current["date"]
    ] = {
        "date": current["date"],
        "time": current["time"],
        "values": current["values"],
        "source_changes":
            current["changes"],
        "is_current": True,
    }

    rows = [
        rows_by_date[key]
        for key in sorted(
            rows_by_date
        )
    ]

    if len(rows) < MIN_TRADING_DAYS:
        raise UpdateError(
            "合并后仅有 "
            f"{len(rows)} 个交易日，"
            f"至少需要 {MIN_TRADING_DAYS} 个"
        )

    if (
        rows[-1]["date"]
        != current["date"]
    ):
        raise UpdateError(
            "current 日期不是最新交易日"
        )

    return rows


def rounded(value):
    if value is None:
        return None

    if not math.isfinite(value):
        raise UpdateError(
            "指标计算产生非有限数值"
        )

    result = round(
        float(value),
        6
    )

    return (
        0.0
        if result == -0.0
        else result
    )


def period_return(
    rows,
    day_index,
    sector_index,
    period
):
    if day_index < period:
        return None

    current = (
        rows[day_index]
        ["values"]
        [sector_index]
    )

    previous = (
        rows[
            day_index - period
        ]
        ["values"]
        [sector_index]
    )

    if previous <= 0:
        return None

    return (
        current / previous
        - 1.0
    ) * 100.0


def daily_change_and_return(
    rows,
    day_index,
    sector_index
):
    row = rows[
        day_index
    ]

    current = (
        row["values"]
        [sector_index]
    )

    if (
        row["source_changes"]
        is not None
    ):
        change = (
            row["source_changes"]
            [sector_index]
        )

        previous = (
            current - change
        )

        if previous <= 0:
            raise UpdateError(
                "current G2 导致"
                "前收盘价小于或等于零"
            )

        return (
            change,
            change / previous * 100.0
        )

    if day_index == 0:
        return None, None

    previous = (
        rows[
            day_index - 1
        ]
        ["values"]
        [sector_index]
    )

    if previous <= 0:
        return None, None

    change = (
        current - previous
    )

    return (
        change,
        change / previous * 100.0
    )


def cross_section_rs(values):
    valid = [
        value
        for value in values
        if value is not None
    ]

    if not valid:
        return [
            None
        ] * len(values)

    average = (
        sum(valid)
        / len(valid)
    )

    return [
        (
            None
            if value is None
            else value - average
        )
        for value in values
    ]


def descending_ranks(values):
    available = [
        (
            index,
            value
        )
        for index, value
        in enumerate(values)
        if value is not None
    ]

    available.sort(
        key=lambda item: (
            -item[1],
            item[0]
        )
    )

    ranks = [
        None
    ] * len(values)

    for rank, (
        index,
        _
    ) in enumerate(
        available,
        start=1
    ):
        ranks[index] = rank

    return ranks


def rotation_status(
    rs_1d,
    rs_5d
):
    if (
        rs_1d is None
        or rs_5d is None
    ):
        return None

    if (
        rs_1d > 0
        and rs_5d > 0
    ):
        return "强势延续"

    if (
        rs_1d > 0
        and rs_5d <= 0
    ):
        return "转强"

    if (
        rs_1d <= 0
        and rs_5d > 0
    ):
        return "转弱"

    return "弱势延续"


def calculate_metrics(
    rows,
    sector_names
):
    day_metrics = []

    for day_index, row in enumerate(
        rows
    ):
        changes = []
        returns_1d = []
        returns_5d = []
        returns_20d = []

        for sector_index in range(
            SECTOR_COUNT
        ):
            (
                change,
                return_1d
            ) = daily_change_and_return(
                rows,
                day_index,
                sector_index
            )

            changes.append(
                change
            )

            returns_1d.append(
                return_1d
            )

            returns_5d.append(
                period_return(
                    rows,
                    day_index,
                    sector_index,
                    5
                )
            )

            returns_20d.append(
                period_return(
                    rows,
                    day_index,
                    sector_index,
                    20
                )
            )

        rs_1d = cross_section_rs(
            returns_1d
        )

        rs_5d = cross_section_rs(
            returns_5d
        )

        rs_20d = cross_section_rs(
            returns_20d
        )

        ranks_1d = descending_ranks(
            returns_1d
        )

        ranks_5d = descending_ranks(
            returns_5d
        )

        day_metrics.append(
            {
                "changes":
                    changes,

                "returns_1d":
                    returns_1d,

                "returns_5d":
                    returns_5d,

                "returns_20d":
                    returns_20d,

                "rs_1d":
                    rs_1d,

                "rs_5d":
                    rs_5d,

                "rs_20d":
                    rs_20d,

                "ranks_1d":
                    ranks_1d,

                "ranks_5d":
                    ranks_5d,
            }
        )

    history = []

    for day_index, row in enumerate(
        rows
    ):
        metrics = day_metrics[
            day_index
        ]

        items = []

        for (
            sector_index,
            sector_name
        ) in enumerate(
            sector_names
        ):
            current_rank = (
                metrics["ranks_1d"]
                [sector_index]
            )

            previous_rank = (
                day_metrics[
                    day_index - 1
                ]
                ["ranks_1d"]
                [sector_index]
                if day_index > 0
                else None
            )

            rank_5d = (
                metrics["ranks_5d"]
                [sector_index]
            )

            rank_change = (
                previous_rank
                - current_rank
                if (
                    previous_rank
                    is not None
                    and current_rank
                    is not None
                )
                else None
            )

            rank_change_vs_5d = (
                rank_5d
                - current_rank
                if (
                    rank_5d
                    is not None
                    and current_rank
                    is not None
                )
                else None
            )

            current_rs = (
                metrics["rs_1d"]
                [sector_index]
            )

            if current_rs is None:
                strong_streak = None
                weak_streak = None

            elif current_rs > 0:
                strong_streak = 0

                for previous_day in range(
                    day_index,
                    -1,
                    -1
                ):
                    value = (
                        day_metrics[
                            previous_day
                        ]
                        ["rs_1d"]
                        [sector_index]
                    )

                    if (
                        value is None
                        or value <= 0
                    ):
                        break

                    strong_streak += 1

                weak_streak = 0

            else:
                weak_streak = 0

                for previous_day in range(
                    day_index,
                    -1,
                    -1
                ):
                    value = (
                        day_metrics[
                            previous_day
                        ]
                        ["rs_1d"]
                        [sector_index]
                    )

                    if (
                        value is None
                        or value > 0
                    ):
                        break

                    weak_streak += 1

                strong_streak = 0

            change_points = (
                metrics["changes"]
                [sector_index]
            )

            return_1d = (
                metrics["returns_1d"]
                [sector_index]
            )

            return_5d = (
                metrics["returns_5d"]
                [sector_index]
            )

            return_20d = (
                metrics["returns_20d"]
                [sector_index]
            )

            sector_rs_1d = (
                metrics["rs_1d"]
                [sector_index]
            )

            sector_rs_5d = (
                metrics["rs_5d"]
                [sector_index]
            )

            sector_rs_20d = (
                metrics["rs_20d"]
                [sector_index]
            )

            item = {
                "index":
                    sector_index,

                "name":
                    sector_name,

                "industry":
                    sector_name,

                "date":
                    row["date"],

                "time":
                    row["time"],

                "value":
                    rounded(
                        row["values"]
                        [sector_index]
                    ),

                "current":
                    rounded(
                        row["values"]
                        [sector_index]
                    ),

                "change":
                    rounded(
                        change_points
                    ),

                "change_points":
                    rounded(
                        change_points
                    ),

                "change_percent":
                    rounded(
                        return_1d
                    ),

                "return_1d":
                    rounded(
                        return_1d
                    ),

                "return_5d":
                    rounded(
                        return_5d
                    ),

                "return_20d":
                    rounded(
                        return_20d
                    ),

                "rs_1d":
                    rounded(
                        sector_rs_1d
                    ),

                "rs_5d":
                    rounded(
                        sector_rs_5d
                    ),

                "rs_20d":
                    rounded(
                        sector_rs_20d
                    ),

                "rank_1d":
                    current_rank,

                "rank_5d":
                    rank_5d,

                "rank_change":
                    rank_change,

                "rank_change_vs_5d":
                    rank_change_vs_5d,

                "strong_streak":
                    strong_streak,

                "weak_streak":
                    weak_streak,

                "rotation_status":
                    rotation_status(
                        sector_rs_1d,
                        sector_rs_5d
                    ),
            }

            items.append(
                item
            )

        history.append(
            {
                "date":
                    row["date"],

                "time":
                    row["time"],

                "is_current":
                    bool(
                        row["is_current"]
                    ),

                "items":
                    items,
            }
        )

    if not history:
        raise UpdateError(
            "指标计算结果为空"
        )

    retained_history = (
        history[
            -MAX_HISTORY_DAYS:
        ]
    )

    summary = (
        retained_history[
            -1
        ]["items"]
    )

    if (
        len(summary)
        != SECTOR_COUNT
    ):
        raise UpdateError(
            "最新 summary "
            "行业数量不是 33"
        )

    return (
        summary,
        retained_history
    )


def build_payload(
    summary,
    history,
    sector_names,
    past_url,
    current_url,
    total_days,
):
    latest = history[-1]

    generated_at = (
        datetime.now(
            timezone.utc
        )
        .replace(
            microsecond=0
        )
        .isoformat()
        .replace(
            "+00:00",
            "Z"
        )
    )

    return {
        "summary":
            summary,

        "rep":
            [],

        "act":
            [],

        "detail":
            [],

        "history":
            history,

        "meta":{
            "generated_at":
                generated_at,

            "latest_date":
                latest["date"],

            "latest_time":
                latest["time"],

            "sector_count":
                len(
                    sector_names
                ),

            "trading_days_total":
                total_days,

            "trading_days_retained":
                len(history),

            "history_limit":
                MAX_HISTORY_DAYS,

            "return_unit":
                "percent",

            "change_unit":
                "points",

            "rs_definition":
                "sector_return_minus_equal_weight_sector_average",

            "rank_change_definition":
                "previous_day_rank_minus_current_day_rank",
        },

        "source":{
            "entry":
                ENTRY_URL,

            "past":
                past_url,

            "current":
                current_url,
        },
    }


def replace_embedded_data(
    index_path,
    payload
):
    try:
        original = (
            index_path
            .read_text(
                encoding="utf-8"
            )
        )

    except OSError as exc:
        raise UpdateError(
            "读取 index.html 失败: "
            f"{short_error(exc)}"
        ) from exc

    except UnicodeDecodeError as exc:
        raise UpdateError(
            "index.html 不是有效 UTF-8"
        ) from exc

    pattern = re.compile(
        r"""(
            <script\b
            (?=[^>]*\bid\s*=\s*(?:"embedded-data"|'embedded-data'|embedded-data\b))
            [^>]*>
        )(.*?)(
            </script\s*>
        )""",
        re.IGNORECASE
        | re.DOTALL
        | re.VERBOSE,
    )

    matches = list(
        pattern.finditer(
            original
        )
    )

    if len(matches) != 1:
        raise UpdateError(
            "index.html 中 "
            "id=embedded-data 的 "
            f"script 数量为 {len(matches)}，应为 1"
        )

    try:
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )

    except (
        TypeError,
        ValueError
    ) as exc:
        raise UpdateError(
            "payload JSON 序列化失败: "
            f"{short_error(exc)}"
        ) from exc

    payload_json = (
        payload_json
        .replace(
            "</",
            "<\\/"
        )
        .replace(
            "\u2028",
            "\\u2028"
        )
        .replace(
            "\u2029",
            "\\u2029"
        )
    )

    match = matches[0]

    replacement_content = (
        "\n"
        + payload_json
        + "\n"
    )

    updated = (
        original[
            :match.start(2)
        ]
        + replacement_content
        + original[
            match.end(2):
        ]
    )

    try:
        old_stat = (
            index_path.stat()
        )

        (
            temp_fd,
            temp_name
        ) = tempfile.mkstemp(
            prefix=".index.html.",
            suffix=".tmp",
            dir=str(
                index_path.parent
            ),
        )

        try:
            with os.fdopen(
                temp_fd,
                "w",
                encoding="utf-8",
                newline=""
            ) as handle:

                handle.write(
                    updated
                )

                handle.flush()

                os.fsync(
                    handle.fileno()
                )

            os.chmod(
                temp_name,
                old_stat.st_mode
                & 0o7777
            )

            os.replace(
                temp_name,
                index_path
            )

        except Exception:
            try:
                os.unlink(
                    temp_name
                )

            except OSError:
                pass

            raise

    except OSError as exc:
        raise UpdateError(
            "写入 index.html 失败: "
            f"{short_error(exc)}"
        ) from exc


def main():
    project_root = (
        Path(__file__)
        .resolve()
        .parents[1]
    )

    index_path = (
        project_root
        / "index.html"
    )

    client = WebClient()

    entry_html = client.get_text(
        ENTRY_URL
    )

    log(
        f"[成功] 入口页抓取成功: "
        f"{ENTRY_URL}"
    )

    script_urls = discover_script_urls(
        entry_html
    )

    log(
        "[成功] 自动发现 "
        f"{len(script_urls)} 个同站脚本"
    )

    script_texts = {}

    for script_url in script_urls:
        try:
            script_texts[
                script_url
            ] = client.get_text(
                script_url,
                referer=ENTRY_URL,
            )

        except UpdateError as exc:
            log(
                "[警告] 非必要脚本抓取失败，继续扫描: "
                f"{script_url} "
                f"({short_error(exc)})"
            )

    past_candidates = [
        url
        for url in script_urls
        if "country_jp_gyo_past.js"
        in urlsplit(
            url
        ).path.lower()
    ]

    scan_sources = [
        (
            ENTRY_URL,
            entry_html
        )
    ]

    scan_sources.extend(
        script_texts.items()
    )

    for (
        base_url,
        text
    ) in scan_sources:

        past_candidates.extend(
            extract_file_references(
                text,
                "country_jp_gyo_past.js",
                base_url,
            )
        )

    past_candidates = deduplicate(
        past_candidates
    )

    past_candidates.sort(
        key=lambda url: (
            0
            if urlsplit(
                url
            ).query
            else 1
        )
    )

    if not past_candidates:
        raise UpdateError(
            "未发现 "
            "country_jp_gyo_past.js"
        )

    past_url = None
    past_text = None
    past_rows = None
    skipped_past_rows = 0
    past_errors = []

    for candidate in past_candidates:
        try:
            candidate_text = (
                script_texts.get(
                    candidate
                )
            )

            if candidate_text is None:
                candidate_text = (
                    client.get_text(
                        candidate,
                        referer=ENTRY_URL,
                    )
                )

                script_texts[
                    candidate
                ] = candidate_text

            (
                candidate_rows,
                skipped
            ) = parse_past_js(
                candidate_text
            )

            past_url = candidate
            past_text = candidate_text
            past_rows = candidate_rows
            skipped_past_rows = skipped

            break

        except UpdateError as exc:
            past_errors.append(
                f"{candidate}: "
                f"{short_error(exc)}"
            )

    if past_url is None:
        raise UpdateError(
            "所有 past JS 候选均失败: "
            + " | ".join(
                past_errors
            )
        )

    log(
        "[成功] 发现 past JS: "
        f"{past_url}"
    )

    log(
        "[成功] past JS 解析成功: "
        f"{len(past_rows)} 个交易日"
        + (
            f"，跳过 {skipped_past_rows} 行无效数据"
            if skipped_past_rows
            else ""
        )
    )

    derived_current_url = (
        derive_current_url(
            past_url
        )
    )

    log(
        "[成功] 已由 past URL "
        "派生 current URL: "
        f"{derived_current_url}"
    )

    current_url = None
    current_text = None
    current_data = None
    current_errors = []

    try:
        derived_text = (
            client.get_text(
                derived_current_url,
                referer=ENTRY_URL,
            )
        )

        derived_data = (
            parse_current_js(
                derived_text
            )
        )

        current_url = (
            derived_current_url
        )

        current_text = (
            derived_text
        )

        current_data = (
            derived_data
        )

    except UpdateError as exc:
        current_errors.append(
            f"{derived_current_url}: "
            f"{short_error(exc)}"
        )

        log(
            "[警告] 派生 current URL "
            "抓取或校验失败，开始扫描回退地址: "
            f"{short_error(exc)}"
        )

    if current_data is None:
        fallback_candidates = []

        fallback_sources = [
            (
                ENTRY_URL,
                entry_html
            )
        ]

        fallback_sources.extend(
            script_texts.items()
        )

        for (
            base_url,
            text
        ) in fallback_sources:

            fallback_candidates.extend(
                extract_file_references(
                    text,
                    "country_jp_gyo.js",
                    base_url,
                )
            )

        fallback_candidates = [
            url
            for url in deduplicate(
                fallback_candidates
            )
            if url
            != derived_current_url
        ]

        fallback_candidates.sort(
            key=lambda url:
                0
                if urlsplit(
                    url
                ).query
                else 1
        )

        for candidate in fallback_candidates:
            try:
                candidate_text = (
                    client.get_text(
                        candidate,
                        referer=ENTRY_URL,
                    )
                )

                candidate_data = (
                    parse_current_js(
                        candidate_text
                    )
                )

                current_url = candidate
                current_text = candidate_text
                current_data = candidate_data

                break

            except UpdateError as exc:
                current_errors.append(
                    f"{candidate}: "
                    f"{short_error(exc)}"
                )

    if current_data is None:
        raise UpdateError(
            "所有 current JS 候选均失败: "
            + " | ".join(
                current_errors
            )
        )

    log(
        "[成功] current JS 解析成功: "
        f"{current_url} "
        f"({current_data['date']} "
        f"{current_data['time']})"
    )

    name_sources = [
        (
            ENTRY_URL,
            entry_html
        )
    ]

    name_sources.extend(
        script_texts.items()
    )

    name_sources.append(
        (
            past_url,
            past_text
        )
    )

    name_sources.append(
        (
            current_url,
            current_text
        )
    )

    sector_names = parse_sector_names(
        name_sources
    )

    log(
        "[成功] Gyo[0..32] "
        "行业名解析成功"
    )

    merged_rows = merge_history(
        past_rows,
        current_data
    )

    (
        summary,
        retained_history
    ) = calculate_metrics(
        merged_rows,
        sector_names,
    )

    log(
        "[成功] 指标计算成功: "
        f"{len(summary)} 个行业，"
        f"保留最近 "
        f"{len(retained_history)} "
        "个交易日"
    )

    payload = build_payload(
        summary=summary,
        history=retained_history,
        sector_names=sector_names,
        past_url=past_url,
        current_url=current_url,
        total_days=len(
            merged_rows
        ),
    )

    replace_embedded_data(
        index_path,
        payload
    )

    log(
        "[成功] index.html "
        f"写入成功: {index_path}"
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        if isinstance(
            exc,
            UpdateError
        ):
            message = str(exc)

        else:
            message = (
                f"{exc.__class__.__name__}: "
                f"{short_error(exc)}"
            )

        print(
            "[错误] 更新失败，"
            "旧 index.html 未被覆盖: "
            f"{message}",
            file=sys.stderr
        )

        sys.exit(1)
