#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import datetime as dt
import gzip
import html
import json
import math
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zlib
from http.cookiejar import CookieJar
from html.parser import HTMLParser
from pathlib import Path


ENTRY_URL = "https://nikkei225jp.com/chart/gyoushu.php"
DATA_NODE_ID = "daily-update-data"
SECTOR_COUNT = 33
MIN_TRADING_DAYS = 21
MAX_TRADING_DAYS = 90
JST = dt.timezone(dt.timedelta(hours=9))

DEFAULT_SECTOR_NAMES = [
    "水産・農林業",
    "鉱業",
    "建設業",
    "食料品",
    "繊維製品",
    "パルプ・紙",
    "化学",
    "医薬品",
    "石油・石炭製品",
    "ゴム製品",
    "ガラス・土石製品",
    "鉄鋼",
    "非鉄金属",
    "金属製品",
    "機械",
    "電気機器",
    "輸送用機器",
    "精密機器",
    "その他製品",
    "電気・ガス業",
    "陸運業",
    "海運業",
    "空運業",
    "倉庫・運輸関連業",
    "情報・通信業",
    "卸売業",
    "小売業",
    "銀行業",
    "証券・商品先物取引業",
    "保険業",
    "その他金融業",
    "不動産業",
    "サービス業",
]

QUOTED_JS_STRING = r"""(?:"(?:\\[\s\S]|[^"\\])*"|'(?:\\[\s\S]|[^'\\])*')"""


class UpdateError(RuntimeError):
    pass


class ScriptCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sources = []
        self.inline_parts = []
        self._inside_script = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "script":
            return
        self._inside_script = True
        attributes = dict(attrs)
        src = attributes.get("src")
        if src:
            self.sources.append(src)

    def handle_endtag(self, tag):
        if tag.lower() == "script":
            self._inside_script = False

    def handle_data(self, data):
        if self._inside_script and data:
            self.inline_parts.append(data)


class HttpClient:
    def __init__(self, referer):
        self.referer = referer
        self.cookie_jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )
        self.cache = {}

    def get(self, url):
        url = canonicalize_url(url)
        if url in self.cache:
            return self.cache[url]

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
                "Accept": "*/*",
                "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
                "Accept-Encoding": "identity",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Referer": self.referer,
                "Connection": "close",
            },
            method="GET",
        )

        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read()
                encoding = (response.headers.get("Content-Encoding") or "").lower()

                if encoding == "gzip":
                    raw = gzip.decompress(raw)

                elif encoding == "deflate":
                    try:
                        raw = zlib.decompress(raw)
                    except zlib.error:
                        raw = zlib.decompress(
                            raw,
                            -zlib.MAX_WBITS
                        )

                final_url = canonicalize_url(
                    response.geturl()
                )

                charset = (
                    response.headers
                    .get_content_charset()
                )

                text = decode_bytes(
                    raw,
                    charset
                )

                result = (
                    final_url,
                    text
                )

                self.cache[url] = result
                self.cache[final_url] = result

                return result

        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            OSError
        ) as exc:

            raise UpdateError(
                "HTTP 获取失败：{} ({})".format(
                    url,
                    exc
                )
            ) from exc


def decode_bytes(
    raw,
    declared_charset=None
):
    encodings = []

    if declared_charset:
        encodings.append(
            declared_charset
        )

    head = raw[:4096]

    for pattern in (
        br"""charset\s*=\s*["']?\s*([A-Za-z0-9._-]+)""",
        br"""encoding\s*=\s*["']\s*([A-Za-z0-9._-]+)""",
    ):
        match = re.search(
            pattern,
            head,
            re.IGNORECASE
        )

        if match:
            encodings.append(
                match.group(1).decode(
                    "ascii",
                    "ignore"
                )
            )

    encodings.extend([
        "utf-8-sig",
        "cp932",
        "shift_jis",
        "euc_jp",
        "iso-8859-1"
    ])

    tried = set()

    for encoding in encodings:
        key = encoding.lower()

        if key in tried:
            continue

        tried.add(key)

        try:
            return raw.decode(
                encoding
            )

        except (
            UnicodeDecodeError,
            LookupError
        ):
            continue

    raise UpdateError(
        "无法识别远端响应字符编码"
    )


def canonicalize_url(url):
    parts = urllib.parse.urlsplit(
        url
    )

    return urllib.parse.urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path,
            parts.query,
            "",
        )
    )


def is_same_site(url):
    hostname = (
        urllib.parse
        .urlsplit(url)
        .hostname
        or ""
    ).lower().rstrip(".")

    return (
        hostname == "nikkei225jp.com"
        or hostname.endswith(
            ".nikkei225jp.com"
        )
    )


def clean_discovered_url(
    value,
    base_url
):
    value = html.unescape(
        value.strip()
    )

    value = value.replace(
        "\\/",
        "/"
    )

    value = re.sub(
        r"\\u([0-9a-fA-F]{4})",
        lambda m: chr(
            int(
                m.group(1),
                16
            )
        ),
        value,
    )

    value = value.strip(
        " \t\r\n\"'`()[]{};,"
    )

    if not value:
        return None

    url = canonicalize_url(
        urllib.parse.urljoin(
            base_url,
            value
        )
    )

    if (
        urllib.parse
        .urlsplit(url)
        .scheme
        not in ("http", "https")
    ):
        return None

    return url


def extract_past_urls(
    text,
    base_url
):
    patterns = [
        r"""(?P<url>(?:https?:)?//[^\s"'<>\\]+?country_jp_gyo_past\.js(?:\?[^\s"'<>\\)]*)?)""",
        r"""(?P<url>(?:\.\.?/|/)?[A-Za-z0-9_./-]*country_jp_gyo_past\.js(?:\?[^\s"'<>\\)]*)?)""",
    ]

    found = []
    seen = set()

    for pattern in patterns:
        for match in re.finditer(
            pattern,
            text,
            re.IGNORECASE
        ):
            url = clean_discovered_url(
                match.group("url"),
                base_url
            )

            if (
                url
                and is_same_site(url)
                and url not in seen
            ):
                seen.add(url)
                found.append(url)

    return found


def extract_javascript_urls(
    text,
    base_url
):
    found = []
    seen = set()

    patterns = [
        r"""(?:src\s*=\s*|["'])(?P<url>(?:https?:)?//[^"'<>\\\s]+?\.js(?:\?[^"'<>\\\s]*)?)""",
        r"""["'](?P<url>(?:\.\.?/|/)[^"'<>\\\s]+?\.js(?:\?[^"'<>\\\s]*)?)["']""",
        r"""["'](?P<url>[A-Za-z0-9_./-]+\.js(?:\?[^"'<>\\\s]*)?)["']""",
    ]

    for pattern in patterns:
        for match in re.finditer(
            pattern,
            text,
            re.IGNORECASE
        ):
            url = clean_discovered_url(
                match.group("url"),
                base_url
            )

            if (
                url
                and is_same_site(url)
                and url not in seen
                and url.lower()
                .split("?", 1)[0]
                .endswith(".js")
            ):
                seen.add(url)
                found.append(url)

    return found


def discover_data_urls(client):
    entry_final_url, entry_html = (
        client.get(
            ENTRY_URL
        )
    )

    parser = ScriptCollector()
    parser.feed(
        entry_html
    )

    past_urls = []
    past_seen = set()

    def add_past(url):
        if url not in past_seen:
            past_seen.add(url)
            past_urls.append(url)

    for url in extract_past_urls(
        entry_html,
        entry_final_url
    ):
        add_past(url)

    queue = []
    queued = set()

    for src in parser.sources:
        url = clean_discovered_url(
            src,
            entry_final_url
        )

        if (
            url
            and is_same_site(url)
            and url not in queued
        ):
            queued.add(url)
            queue.append(url)

    for inline_text in parser.inline_parts:
        for url in extract_javascript_urls(
            inline_text,
            entry_final_url
        ):
            if url not in queued:
                queued.add(url)
                queue.append(url)

        for url in extract_past_urls(
            inline_text,
            entry_final_url
        ):
            add_past(url)

    processed = set()

    while (
        queue
        and len(processed) < 80
    ):
        script_url = queue.pop(0)

        if script_url in processed:
            continue

        processed.add(
            script_url
        )

        try:
            (
                final_url,
                script_text
            ) = client.get(
                script_url
            )

        except UpdateError:
            continue

        if re.search(
            r"country_jp_gyo_past\.js(?:\?|$)",
            final_url,
            re.IGNORECASE
        ):
            add_past(
                final_url
            )

        for url in extract_past_urls(
            script_text,
            final_url
        ):
            add_past(url)

        for url in extract_javascript_urls(
            script_text,
            final_url
        ):
            if (
                url not in queued
                and url not in processed
            ):
                queued.add(url)
                queue.append(url)

    if not past_urls:
        raise UpdateError(
            "未能从入口页及同站脚本中发现 "
            "country_jp_gyo_past.js"
        )

    return past_urls


def derive_current_url(
    past_url
):
    parts = urllib.parse.urlsplit(
        past_url
    )

    new_path, count = re.subn(
        r"country_jp_gyo_past\.js$",
        "country_jp_gyo.js",
        parts.path,
        flags=re.IGNORECASE,
    )

    if count != 1:
        raise UpdateError(
            "无法从 past URL 派生 current URL：{}".format(
                past_url
            )
        )

    return urllib.parse.urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            new_path,
            parts.query,
            parts.fragment
        )
    )


def decode_js_string(token):
    if (
        len(token) < 2
        or token[0] not in ("'", '"')
        or token[-1] != token[0]
    ):
        raise UpdateError(
            "无效的 JavaScript 字符串"
        )

    source = token[1:-1]

    output = []

    i = 0

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

    while i < len(source):
        char = source[i]

        if char != "\\":
            output.append(char)
            i += 1
            continue

        i += 1

        if i >= len(source):
            output.append("\\")
            break

        escaped = source[i]

        if escaped in simple_escapes:
            output.append(
                simple_escapes[
                    escaped
                ]
            )
            i += 1

        elif (
            escaped == "u"
            and i + 4 < len(source)
        ):
            code = source[
                i + 1:i + 5
            ]

            if re.fullmatch(
                r"[0-9a-fA-F]{4}",
                code
            ):
                output.append(
                    chr(
                        int(
                            code,
                            16
                        )
                    )
                )
                i += 5

            else:
                output.append("u")
                i += 1

        elif (
            escaped == "x"
            and i + 2 < len(source)
        ):
            code = source[
                i + 1:i + 3
            ]

            if re.fullmatch(
                r"[0-9a-fA-F]{2}",
                code
            ):
                output.append(
                    chr(
                        int(
                            code,
                            16
                        )
                    )
                )
                i += 3

            else:
                output.append("x")
                i += 1

        elif escaped in (
            "\n",
            "\r"
        ):
            if (
                escaped == "\r"
                and i + 1 < len(source)
                and source[
                    i + 1
                ] == "\n"
            ):
                i += 1

            i += 1

        else:
            output.append(
                escaped
            )
            i += 1

    return "".join(
        output
    )


def parse_number(value):
    if value is None:
        raise UpdateError(
            "缺少数值"
        )

    text = str(value).strip()

    if not text:
        raise UpdateError(
            "空数值"
        )

    negative_marker = (
        text.startswith("▲")
    )

    text = (
        text.replace(",", "")
        .replace("，", "")
        .replace("＋", "+")
        .replace("−", "-")
        .replace("－", "-")
        .replace("―", "-")
        .replace("▲", "")
        .replace("△", "")
        .replace("円", "")
        .strip()
    )

    if (
        text.startswith("(")
        and text.endswith(")")
    ):
        negative_marker = True
        text = text[
            1:-1
        ].strip()

    match = re.search(
        r"[+-]?(?:\d+(?:\.\d*)?|\.?\d+)",
        text
    )

    if not match:
        raise UpdateError(
            "无法解析数值：{!r}".format(
                value
            )
        )

    number = float(
        match.group(0)
    )

    if (
        negative_marker
        and number > 0
    ):
        number = -number

    if not math.isfinite(
        number
    ):
        raise UpdateError(
            "出现非有限数值：{!r}".format(
                value
            )
        )

    return number


def normalize_date(value):
    text = str(value).strip()

    text = (
        text.replace("年", "/")
        .replace("月", "/")
        .replace("日", "")
        .replace(".", "/")
        .replace("-", "/")
    )

    text = re.sub(
        r"\s+",
        "",
        text
    )

    match = re.fullmatch(
        r"(\d{2,4})/"
        r"(\d{1,2})/"
        r"(\d{1,2})",
        text
    )

    if not match:
        raise UpdateError(
            "无法解析交易日期：{!r}".format(
                value
            )
        )

    year, month, day = map(
        int,
        match.groups()
    )

    if year < 100:
        year += (
            2000
            if year < 70
            else 1900
        )

    try:
        parsed = dt.date(
            year,
            month,
            day
        )

    except ValueError as exc:
        raise UpdateError(
            "无效交易日期：{!r}".format(
                value
            )
        ) from exc

    return parsed.isoformat()


def parse_past_rows(
    script_text
):
    assignment_pattern = re.compile(
        r"\bGY\s*"
        r"\[\s*[^\]]+\s*\]"
        r"\s*=\s*"
        r"(?P<value>{})\s*;".format(
            QUOTED_JS_STRING
        ),
        re.IGNORECASE,
    )

    rows_by_date = {}

    assignment_count = 0

    for match in assignment_pattern.finditer(
        script_text
    ):
        assignment_count += 1

        decoded = decode_js_string(
            match.group("value")
        )

        try:
            fields = next(
                csv.reader(
                    [decoded],
                    skipinitialspace=True
                )
            )

        except csv.Error as exc:
            raise UpdateError(
                "past 数据 CSV 解析失败"
            ) from exc

        fields = [
            field.strip()
            for field in fields
        ]

        if len(fields) != (
            2 + SECTOR_COUNT
        ):
            raise UpdateError(
                "past 数据字段数错误："
                "期望 {}，实际 {}，内容={!r}".format(
                    2 + SECTOR_COUNT,
                    len(fields),
                    decoded
                )
            )

        date_text = normalize_date(
            fields[0]
        )

        time_text = fields[1]

        values = [
            parse_number(
                field
            )
            for field in fields[2:]
        ]

        rows_by_date[
            date_text
        ] = {
            "date":
                date_text,

            "time":
                time_text,

            "values":
                values,
        }

    if assignment_count == 0:
        raise UpdateError(
            "未找到 "
            'GY[q] = "日期,时间,33值" '
            "格式的 past 数据"
        )

    rows = sorted(
        rows_by_date.values(),
        key=lambda row:
            row["date"]
    )

    if len(rows) < MIN_TRADING_DAYS:
        raise UpdateError(
            "past 数据不足："
            "至少需要 {} 个交易日，实际 {}".format(
                MIN_TRADING_DAYS,
                len(rows)
            )
        )

    return rows


def extract_direct_gyo_properties(
    script_text
):
    properties = {
        index: {}
        for index
        in range(SECTOR_COUNT)
    }

    pattern = re.compile(
        r"""
        \bGyo\s*\[\s*(?P<index>\d+)\s*\]\s*
        (?:
            \.\s*(?P<dot_name>G[012]|name|Name|NAME)
            |
            \[\s*["'](?P<bracket_name>G[012]|name|Name|NAME)["']\s*\]
        )
        \s*=\s*(?P<value>"""
        + QUOTED_JS_STRING
        + r""")\s*;
        """,
        re.IGNORECASE
        | re.VERBOSE,
    )

    for match in pattern.finditer(
        script_text
    ):
        index = int(
            match.group(
                "index"
            )
        )

        if index not in properties:
            continue

        name = (
            match.group(
                "dot_name"
            )
            or match.group(
                "bracket_name"
            )
        )

        name = (
            name.upper()
            if name.upper().startswith("G")
            else "NAME"
        )

        properties[index][
            name
        ] = decode_js_string(
            match.group(
                "value"
            )
        )

    return properties


def extract_object_or_array_properties(
    script_text,
    properties
):
    assignment_pattern = re.compile(
        r"\bGyo\s*"
        r"\[\s*(?P<index>\d+)\s*\]"
        r"\s*=\s*(?P<body>.*?);",
        re.IGNORECASE
        | re.DOTALL,
    )

    object_property_pattern = re.compile(
        r"""(?:["']?(?P<name>G[012]|name)["']?)
        \s*:\s*
        (?P<value>"""
        + QUOTED_JS_STRING
        + r""")""",
        re.IGNORECASE
        | re.VERBOSE,
    )

    string_pattern = re.compile(
        QUOTED_JS_STRING,
        re.DOTALL
    )

    for match in assignment_pattern.finditer(
        script_text
    ):
        index = int(
            match.group(
                "index"
            )
        )

        if index not in properties:
            continue

        body = match.group(
            "body"
        )

        for prop_match in object_property_pattern.finditer(
            body
        ):
            name = prop_match.group(
                "name"
            )

            name = (
                name.upper()
                if name.upper().startswith("G")
                else "NAME"
            )

            properties[
                index
            ].setdefault(
                name,
                decode_js_string(
                    prop_match.group(
                        "value"
                    )
                )
            )

        if (
            "G1" in properties[index]
            and "G2" in properties[index]
        ):
            continue

        strings = [
            decode_js_string(
                item.group(0)
            )
            for item
            in string_pattern.finditer(
                body
            )
        ]

        numeric_strings = []

        for value in strings:
            try:
                parse_number(
                    value
                )

            except UpdateError:
                continue

            numeric_strings.append(
                value
            )

        if len(
            numeric_strings
        ) >= 2:
            properties[index].setdefault(
                "G1",
                numeric_strings[0]
            )

            properties[index].setdefault(
                "G2",
                numeric_strings[1]
            )

        for value in strings:
            if (
                value not in numeric_strings
                and value.strip()
            ):
                properties[
                    index
                ].setdefault(
                    "NAME",
                    value.strip()
                )
                break


def parse_current_data(
    script_text
):
    properties = (
        extract_direct_gyo_properties(
            script_text
        )
    )

    extract_object_or_array_properties(
        script_text,
        properties
    )

    sectors = []

    for index in range(
        SECTOR_COUNT
    ):
        item = properties[
            index
        ]

        if (
            "G1" not in item
            or "G2" not in item
        ):
            raise UpdateError(
                "current 数据缺少 "
                "Gyo[{}] 的标量字符串 "
                "G1/G2".format(
                    index
                )
            )

        current = parse_number(
            item["G1"]
        )

        change = parse_number(
            item["G2"]
        )

        previous_close = (
            current
            - change
        )

        if previous_close == 0:
            raise UpdateError(
                "Gyo[{}] 的前收盘值为零，"
                "无法计算真实涨跌幅".format(
                    index
                )
            )

        daily_percent = (
            change
            / previous_close
            * 100.0
        )

        name = (
            item.get("NAME")
            or item.get("G0")
            or DEFAULT_SECTOR_NAMES[
                index
            ]
        )

        name = (
            str(name).strip()
            or DEFAULT_SECTOR_NAMES[
                index
            ]
        )

        sectors.append(
            {
                "index":
                    index,

                "name":
                    name,

                "current":
                    current,

                "change":
                    change,

                "previousClose":
                    previous_close,

                "dailyPercent":
                    daily_percent,
            }
        )

    return sectors


def extract_current_date(
    script_text,
    latest_past_date
):
    candidates = []

    for match in re.finditer(
        r"(?<!\d)"
        r"(\d{2,4}[./-]"
        r"\d{1,2}[./-]"
        r"\d{1,2})"
        r"(?!\d)",
        script_text,
    ):
        try:
            candidates.append(
                normalize_date(
                    match.group(1)
                )
            )

        except UpdateError:
            continue

    latest_past = dt.date.fromisoformat(
        latest_past_date
    )

    today_jst = dt.datetime.now(
        JST
    ).date()

    valid = []

    for value in candidates:
        parsed = dt.date.fromisoformat(
            value
        )

        if (
            abs(
                (
                    parsed
                    - latest_past
                ).days
            )
            <= 10
            and parsed
            <= today_jst
            + dt.timedelta(days=1)
        ):
            valid.append(
                value
            )

    return (
        max(valid)
        if valid
        else latest_past_date
    )


def merge_current_row(
    past_rows,
    current_sectors,
    current_date
):
    rows_by_date = {
        row["date"]: {
            "date":
                row["date"],

            "time":
                row.get(
                    "time",
                    ""
                ),

            "values":
                list(
                    row["values"]
                ),
        }
        for row in past_rows
    }

    rows_by_date[
        current_date
    ] = {
        "date":
            current_date,

        "time":
            dt.datetime.now(
                JST
            ).strftime(
                "%H:%M"
            ),

        "values": [
            sector["current"]
            for sector
            in current_sectors
        ],
    }

    rows = sorted(
        rows_by_date.values(),
        key=lambda row:
            row["date"]
    )

    rows = rows[
        -MAX_TRADING_DAYS:
    ]

    if len(rows) < MIN_TRADING_DAYS:
        raise UpdateError(
            "合并 current 后交易日不足："
            "至少 {}，实际 {}".format(
                MIN_TRADING_DAYS,
                len(rows)
            )
        )

    if rows[-1]["date"] != current_date:
        raise UpdateError(
            "current 日期不是合并后最新交易日"
        )

    for row in rows:
        if len(
            row["values"]
        ) != SECTOR_COUNT:
            raise UpdateError(
                "合并后的历史数据不是 33 个行业"
            )

    return rows


def pct_change(
    current,
    previous
):
    if previous == 0:
        raise UpdateError(
            "历史基准值为零，无法计算涨跌幅"
        )

    result = (
        current
        / previous
        - 1.0
    ) * 100.0

    if not math.isfinite(
        result
    ):
        raise UpdateError(
            "计算得到非有限涨跌幅"
        )

    return result


def calculate_snapshot(
    rows,
    cutoff,
    latest_daily_override=None
):
    if cutoff < 1:
        raise UpdateError(
            "历史数据不足以计算快照"
        )

    daily = []
    change5d = []
    change20d = []

    for sector_index in range(
        SECTOR_COUNT
    ):
        current = (
            rows[cutoff]
            ["values"]
            [sector_index]
        )

        if latest_daily_override is not None:
            day_value = (
                latest_daily_override[
                    sector_index
                ]
            )

        else:
            day_value = pct_change(
                current,
                rows[
                    cutoff - 1
                ]
                ["values"]
                [sector_index]
            )

        base5_index = max(
            0,
            cutoff - 5
        )

        base20_index = max(
            0,
            cutoff - 20
        )

        daily.append(
            day_value
        )

        change5d.append(
            pct_change(
                current,
                rows[
                    base5_index
                ]
                ["values"]
                [sector_index]
            )
        )

        change20d.append(
            pct_change(
                current,
                rows[
                    base20_index
                ]
                ["values"]
                [sector_index]
            )
        )

    mean5 = (
        sum(change5d)
        / SECTOR_COUNT
    )

    mean20 = (
        sum(change20d)
        / SECTOR_COUNT
    )

    rs5 = [
        value - mean5
        for value in change5d
    ]

    rs20 = [
        value - mean20
        for value in change20d
    ]

    strength = [
        0.20
        * daily[index]
        + 0.35
        * rs5[index]
        + 0.45
        * rs20[index]
        for index
        in range(SECTOR_COUNT)
    ]

    order = sorted(
        range(SECTOR_COUNT),
        key=lambda index: (
            -strength[index],
            index
        ),
    )

    ranks = [
        0
    ] * SECTOR_COUNT

    for rank, sector_index in enumerate(
        order,
        start=1
    ):
        ranks[
            sector_index
        ] = rank

    return {
        "dailyPercent":
            daily,

        "change5d":
            change5d,

        "change20d":
            change20d,

        "RS5":
            rs5,

        "RS20":
            rs20,

        "strength":
            strength,

        "rank":
            ranks,
    }


def calculate_streak(
    rows,
    sector_index,
    latest_daily_percent
):
    changes = [
        latest_daily_percent
    ]

    for row_index in range(
        len(rows) - 2,
        0,
        -1
    ):
        changes.append(
            pct_change(
                rows[
                    row_index
                ]
                ["values"]
                [sector_index],

                rows[
                    row_index - 1
                ]
                ["values"]
                [sector_index],
            )
        )

    if (
        not changes
        or abs(
            changes[0]
        ) < 1e-12
    ):
        return 0

    positive = (
        changes[0] > 0
    )

    count = 0

    for value in changes:
        if abs(
            value
        ) < 1e-12:
            break

        if (
            value > 0
        ) != positive:
            break

        count += 1

    return (
        count
        if positive
        else -count
    )


def rotation_state(
    rs5,
    rs20
):
    if (
        rs5 >= 0
        and rs20 >= 0
    ):
        return "leading"

    if (
        rs5 >= 0
        and rs20 < 0
    ):
        return "improving"

    if (
        rs5 < 0
        and rs20 >= 0
    ):
        return "weakening"

    return "lagging"


def rounded(
    value,
    digits=6
):
    if value is None:
        return None

    if not math.isfinite(
        float(value)
    ):
        raise UpdateError(
            "输出数据含非有限数值"
        )

    result = round(
        float(value),
        digits
    )

    return (
        0.0
        if result == -0.0
        else result
    )


def build_payload(
    rows,
    current_sectors,
    current_url,
    past_url
):
    latest_index = (
        len(rows) - 1
    )

    latest_daily = [
        sector[
            "dailyPercent"
        ]
        for sector
        in current_sectors
    ]

    current_snapshot = (
        calculate_snapshot(
            rows,
            latest_index,
            latest_daily_override=
                latest_daily,
        )
    )

    previous_snapshot = (
        calculate_snapshot(
            rows,
            latest_index - 1
        )
    )

    dates = [
        row["date"]
        for row in rows
    ]

    sectors = []

    for index, current_sector in enumerate(
        current_sectors
    ):
        history = []

        for row_index, row in enumerate(
            rows
        ):
            if row_index == 0:
                day_percent = None

            elif row_index == latest_index:
                day_percent = (
                    latest_daily[
                        index
                    ]
                )

            else:
                day_percent = pct_change(
                    row["values"][index],

                    rows[
                        row_index - 1
                    ]
                    ["values"]
                    [index],
                )

            history.append(
                {
                    "date":
                        row["date"],

                    "value":
                        rounded(
                            row[
                                "values"
                            ][
                                index
                            ]
                        ),

                    "dailyPercent":
                        (
                            None
                            if day_percent
                            is None
                            else rounded(
                                day_percent
                            )
                        ),
                }
            )

        rank = (
            current_snapshot[
                "rank"
            ][
                index
            ]
        )

        previous_rank = (
            previous_snapshot[
                "rank"
            ][
                index
            ]
        )

        sectors.append(
            {
                "id":
                    "sector-{:02d}".format(
                        index + 1
                    ),

                "index":
                    index,

                "name":
                    current_sector[
                        "name"
                    ],

                "current":
                    rounded(
                        current_sector[
                            "current"
                        ]
                    ),

                "change":
                    rounded(
                        current_sector[
                            "change"
                        ]
                    ),

                "previousClose":
                    rounded(
                        current_sector[
                            "previousClose"
                        ]
                    ),

                "dailyPercent":
                    rounded(
                        current_snapshot[
                            "dailyPercent"
                        ][
                            index
                        ]
                    ),

                "change5d":
                    rounded(
                        current_snapshot[
                            "change5d"
                        ][
                            index
                        ]
                    ),

                "change20d":
                    rounded(
                        current_snapshot[
                            "change20d"
                        ][
                            index
                        ]
                    ),

                "RS5":
                    rounded(
                        current_snapshot[
                            "RS5"
                        ][
                            index
                        ]
                    ),

                "RS20":
                    rounded(
                        current_snapshot[
                            "RS20"
                        ][
                            index
                        ]
                    ),

                "rank":
                    rank,

                "previousRank":
                    previous_rank,

                "rankChange":
                    previous_rank
                    - rank,

                "streak":
                    calculate_streak(
                        rows,
                        index,
                        current_snapshot[
                            "dailyPercent"
                        ][
                            index
                        ]
                    ),

                "strength":
                    rounded(
                        current_snapshot[
                            "strength"
                        ][
                            index
                        ]
                    ),

                "rotationState":
                    rotation_state(
                        current_snapshot[
                            "RS5"
                        ][
                            index
                        ],

                        current_snapshot[
                            "RS20"
                        ][
                            index
                        ],
                    ),

                "fundFlow":
                    None,

                "fundFlowRank":
                    None,

                "history":
                    history,

                "values": [
                    rounded(
                        row[
                            "values"
                        ][
                            index
                        ]
                    )
                    for row
                    in rows
                ],
            }
        )

    payload = {
        "schemaVersion":
            2,

        "updatedAt":
            dt.datetime.now(
                dt.timezone.utc
            )
            .replace(
                microsecond=0
            )
            .isoformat()
            .replace(
                "+00:00",
                "Z"
            ),

        "marketDate":
            rows[-1][
                "date"
            ],

        "tradingDays":
            len(rows),

        "dates":
            dates,

        "source":{
            "entry":
                ENTRY_URL,

            "current":
                current_url,

            "past":
                past_url,
        },

        "sectors":
            sectors,
    }

    validate_payload(
        payload
    )

    return payload


def validate_payload(
    payload
):
    if (
        payload.get(
            "schemaVersion"
        ) != 2
    ):
        raise UpdateError(
            "schemaVersion 必须为 2"
        )

    sectors = payload.get(
        "sectors"
    )

    dates = payload.get(
        "dates"
    )

    if (
        not isinstance(
            sectors,
            list
        )
        or len(
            sectors
        ) != SECTOR_COUNT
    ):
        raise UpdateError(
            "输出行业数不是 33"
        )

    if (
        not isinstance(
            dates,
            list
        )
        or not (
            MIN_TRADING_DAYS
            <= len(dates)
            <= MAX_TRADING_DAYS
        )
    ):
        raise UpdateError(
            "输出交易日数量不符合要求"
        )

    ranks = sorted(
        sector.get(
            "rank"
        )
        for sector
        in sectors
    )

    previous_ranks = sorted(
        sector.get(
            "previousRank"
        )
        for sector
        in sectors
    )

    expected_ranks = list(
        range(
            1,
            SECTOR_COUNT + 1
        )
    )

    if (
        ranks != expected_ranks
        or previous_ranks
        != expected_ranks
    ):
        raise UpdateError(
            "rank 或 previousRank 不完整"
        )

    required = (
        "dailyPercent",
        "change5d",
        "change20d",
        "RS5",
        "RS20",
        "rank",
        "previousRank",
        "rankChange",
        "streak",
        "strength",
        "rotationState",
        "fundFlow",
        "fundFlowRank",
    )

    for sector in sectors:
        for field in required:
            if field not in sector:
                raise UpdateError(
                    "输出缺少字段：{}".format(
                        field
                    )
                )

        if (
            sector[
                "fundFlow"
            ] is not None
            or sector[
                "fundFlowRank"
            ] is not None
        ):
            raise UpdateError(
                "fundFlow/fundFlowRank 必须为 null"
            )

        if len(
            sector.get(
                "history",
                []
            )
        ) != len(dates):
            raise UpdateError(
                "行业历史数据长度与 dates 不一致"
            )

    def check_finite(
        value
    ):
        if (
            isinstance(
                value,
                float
            )
            and not math.isfinite(
                value
            )
        ):
            raise UpdateError(
                "输出 JSON 含 NaN 或 Infinity"
            )

        if isinstance(
            value,
            dict
        ):
            for child in value.values():
                check_finite(
                    child
                )

        elif isinstance(
            value,
            list
        ):
            for child in value:
                check_finite(
                    child
                )

    check_finite(
        payload
    )


def serialize_payload(
    payload
):
    text = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        separators=(
            ",",
            ": "
        ),
    )

    return (
        text
        .replace(
            "<",
            "\\u003c"
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


def replace_data_node(
    index_html,
    json_text
):
    node = (
        '<script id="{}" '
        'type="application/json">\n'
        '{}\n'
        '</script>'.format(
            DATA_NODE_ID,
            json_text
        )
    )

    quoted_id_pattern = (
        r"""\bid\s*=\s*(?P<quote>["'])"""
        + re.escape(
            DATA_NODE_ID
        )
        + r"""(?P=quote)"""
    )

    unquoted_id_pattern = (
        r"""\bid\s*=\s*"""
        + re.escape(
            DATA_NODE_ID
        )
        + r"""(?=[\s>])"""
    )

    element_patterns = [
        re.compile(
            r"""<
                (?P<tag>[A-Za-z][A-Za-z0-9:_-]*)
                \b
                (?=[^>]*"""
                + quoted_id_pattern
                + r""")
                [^>]*>
                .*?
                </(?P=tag)\s*>
            """,
            re.IGNORECASE
            | re.DOTALL
            | re.VERBOSE,
        ),

        re.compile(
            r"""<
                (?P<tag>[A-Za-z][A-Za-z0-9:_-]*)
                \b
                (?=[^>]*"""
                + unquoted_id_pattern
                + r""")
                [^>]*>
                .*?
                </(?P=tag)\s*>
            """,
            re.IGNORECASE
            | re.DOTALL
            | re.VERBOSE,
        ),
    ]

    updated = index_html

    total_replacements = 0

    for pattern in element_patterns:
        (
            updated,
            count
        ) = pattern.subn(
            node,
            updated
        )

        total_replacements += (
            count
        )

        if count:
            break

    if total_replacements > 1:
        raise UpdateError(
            'index.html 中存在多个 '
            'id="{}" 节点'.format(
                DATA_NODE_ID
            )
        )

    if total_replacements == 0:
        body_matches = list(
            re.finditer(
                r"</body\s*>",
                updated,
                re.IGNORECASE
            )
        )

        if not body_matches:
            raise UpdateError(
                "index.html 中既无 "
                'id="{}" 节点，'
                "也无 </body>".format(
                    DATA_NODE_ID
                )
            )

        insertion = (
            body_matches[-1]
            .start()
        )

        updated = (
            updated[
                :insertion
            ]
            + node
            + "\n"
            + updated[
                insertion:
            ]
        )

    if updated.count(
        'id="{}"'.format(
            DATA_NODE_ID
        )
    ) != 1:
        raise UpdateError(
            "daily-update-data 节点写入验证失败"
        )

    return updated


def atomic_write(
    path,
    content
):
    path = Path(
        path
    )

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    original_mode = None

    if path.exists():
        original_mode = (
            path.stat().st_mode
        )

    (
        file_descriptor,
        temporary_name
    ) = tempfile.mkstemp(
        prefix=".{}.".format(
            path.name
        ),
        suffix=".tmp",
        dir=str(
            path.parent
        ),
    )

    try:
        with os.fdopen(
            file_descriptor,
            "w",
            encoding="utf-8",
            newline=""
        ) as handle:
            handle.write(
                content
            )

            handle.flush()

            os.fsync(
                handle.fileno()
            )

        if original_mode is not None:
            os.chmod(
                temporary_name,
                original_mode
            )

        os.replace(
            temporary_name,
            path
        )

        try:
            directory_fd = os.open(
                str(
                    path.parent
                ),
                os.O_RDONLY
            )

        except OSError:
            directory_fd = None

        if directory_fd is not None:
            try:
                os.fsync(
                    directory_fd
                )

            finally:
                os.close(
                    directory_fd
                )

    except Exception:
        try:
            os.unlink(
                temporary_name
            )

        except OSError:
            pass

        raise


def load_index(
    path
):
    try:
        return Path(
            path
        ).read_text(
            encoding="utf-8"
        )

    except (
        OSError,
        UnicodeError
    ) as exc:
        raise UpdateError(
            "无法读取 index.html：{}".format(
                exc
            )
        ) from exc


def update(
    index_path
):
    client = HttpClient(
        ENTRY_URL
    )

    past_urls = (
        discover_data_urls(
            client
        )
    )

    errors = []

    selected = None

    for past_url in past_urls:
        try:
            current_url = (
                derive_current_url(
                    past_url
                )
            )

            (
                _,
                past_script
            ) = client.get(
                past_url
            )

            (
                _,
                current_script
            ) = client.get(
                current_url
            )

            past_rows = (
                parse_past_rows(
                    past_script
                )
            )

            current_sectors = (
                parse_current_data(
                    current_script
                )
            )

            current_date = (
                extract_current_date(
                    current_script,
                    past_rows[
                        -1
                    ][
                        "date"
                    ],
                )
            )

            rows = (
                merge_current_row(
                    past_rows,
                    current_sectors,
                    current_date,
                )
            )

            payload = (
                build_payload(
                    rows,
                    current_sectors,
                    current_url,
                    past_url,
                )
            )

            selected = (
                payload,
                current_url,
                past_url
            )

            break

        except UpdateError as exc:
            errors.append(
                "{}: {}".format(
                    past_url,
                    exc
                )
            )

    if selected is None:
        detail = (
            "\n".join(
                errors
            )
            if errors
            else "没有可用候选 URL"
        )

        raise UpdateError(
            "所有数据源候选均处理失败：\n{}".format(
                detail
            )
        )

    (
        payload,
        current_url,
        past_url
    ) = selected

    json_text = serialize_payload(
        payload
    )

    original_html = load_index(
        index_path
    )

    updated_html = (
        replace_data_node(
            original_html,
            json_text
        )
    )

    if updated_html == original_html:
        raise UpdateError(
            "index.html 内容未发生变化"
        )

    atomic_write(
        index_path,
        updated_html
    )

    print(
        "更新完成：{}，"
        "交易日 {}，"
        "行业 {}，"
        "数据源 {}".format(
            index_path,
            payload[
                "tradingDays"
            ],
            len(
                payload[
                    "sectors"
                ]
            ),
            current_url,
        )
    )


def default_index_path():
    return (
        Path(__file__)
        .resolve()
        .parent
        .parent
        / "index.html"
    )


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "抓取日经33行业数据并原子更新 index.html"
        )
    )

    parser.add_argument(
        "--index",
        default=str(
            default_index_path()
        ),
        help="index.html 路径",
    )

    return parser.parse_args()


def main():
    arguments = parse_arguments()

    try:
        update(
            Path(
                arguments.index
            ).resolve()
        )

    except UpdateError as exc:
        print(
            "更新失败：{}".format(
                exc
            ),
            file=sys.stderr
        )

        return 1

    except Exception as exc:
        print(
            "更新失败：{}".format(
                exc
            ),
            file=sys.stderr
        )

        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
