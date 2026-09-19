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
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/127.0 Safari/537.36"
                )
            },
        )
        with self.opener.open(request, timeout=20) as resp:
            result = resp.read().decode("utf-8", errors="ignore")
            self.cache[url] = result
            return result


def canonicalize_url(url):
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path,
            parts.query,
            "",
        )
    )


def clean_discovered_url(value, base_url):
    value = html.unescape(value.strip())
    value = value.replace("\\/", "/")
    value = re.sub(
        r"\\u([0-9a-fA-F]{4})",
        lambda m: chr(int(m.group(1), 16)),
        value,
    )
    value = value.strip(" \t\r\n\"'`()[]{};,")
    if not value:
        return None

    return canonicalize_url(
        urllib.parse.urljoin(base_url, value)
    )


def extract_past_urls(text, base_url):
    patterns = [
        r"""(?P<url>(?:https?:)?//[^\s"'<>\\]+?country_jp_gyo_past\.js(?:\?[^\s"'<>\\)]*)?)""",
        r"""(?P<url>(?:\.\.?/|/)?[A-Za-z0-9_./-]*country_jp_gyo_past\.js(?:\?[^\s"'<>\\)]*)?)""",
    ]

    found = []
    seen = set()

    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            url = clean_discovered_url(match.group("url"), base_url)
            if url and url not in seen:
                seen.add(url)
                found.append(url)

    return found


def extract_javascript_urls(text, base_url):
    found = []
    seen = set()

    patterns = [
        r"""(?:src\s*=\s*|["'])(?P<url>(?:https?:)?//[^"'<>\\\s]+?\.js(?:\?[^"'<>\\\s]*)?)""",
        r"""["'](?P<url>(?:\.\.?/|/)[^"'<>\\\s]+?\.js(?:\?[^"'<>\\\s]*)?)["']""",
        r"""["'](?P<url>[A-Za-z0-9_./-]+\.js(?:\?[^"'<>\\\s]*)?)["']""",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            url = clean_discovered_url(match.group("url"), base_url)
            if url and url not in seen:
                seen.add(url)
                found.append(url)

    return found


def discover_data_urls(client):
    entry_html = client.get(ENTRY_URL)

    parser = ScriptCollector()
    parser.feed(entry_html)

    past_urls = []
    past_seen = set()

    def add_past(url):
        if url not in past_seen:
            past_seen.add(url)
            past_urls.append(url)

    for url in extract_past_urls(entry_html, ENTRY_URL):
        add_past(url)

    queue = []
    queued = set()

    for src in parser.sources:
        url = clean_discovered_url(src, ENTRY_URL)
        if url and url not in queued:
            queued.add(url)
            queue.append(url)

    for inline_text in parser.inline_parts:
        for url in extract_javascript_urls(inline_text, ENTRY_URL):
            if url not in queued:
                queued.add(url)
                queue.append(url)

        for url in extract_past_urls(inline_text, ENTRY_URL):
            add_past(url)

    processed = set()

    while queue and len(processed) < 80:
        script_url = queue.pop(0)

        if script_url in processed:
            continue

        processed.add(script_url)

        try:
            script_text = client.get(script_url)
        except UpdateError:
            continue

        if re.search(
            r"country_jp_gyo_past\.js(?:\?|$)",
            script_url,
            re.IGNORECASE,
        ):
            add_past(script_url)

        for url in extract_past_urls(script_text, script_url):
            add_past(url)

        for url in extract_javascript_urls(script_text, script_url):
            if url not in queued and url not in processed:
                queued.add(url)
                queue.append(url)

    if not past_urls:
        raise UpdateError(
            "未能从入口页及同站脚本中发现 country_jp_gyo_past.js"
        )

    return past_urls


def derive_current_url(past_url):
    parts = urllib.parse.urlsplit(past_url)

    new_path, count = re.subn(
        r"country_jp_gyo_past\.js$",
        "country_jp_gyo.js",
        parts.path,
        flags=re.IGNORECASE,
    )

    if count != 1:
        raise UpdateError(
            "无法从 past URL 派生 current URL：{}".format(past_url)
        )

    return urllib.parse.urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            new_path,
            parts.query,
            parts.fragment,
        )
    )


def decode_js_string(token):
    if (
        len(token) < 2
        or token[0] not in ("'", '"')
        or token[-1] != token[0]
    ):
        raise UpdateError("无效的 JavaScript 字符串")

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
            output.append(simple_escapes[escaped])
            i += 1

        elif escaped == "u" and i + 4 < len(source):
            code = source[i + 1:i + 5]

            if re.fullmatch(r"[0-9a-fA-F]{4}", code):
                output.append(chr(int(code, 16)))
                i += 5
            else:
                output.append("u")
                i += 1

        elif escaped == "x" and i + 2 < len(source):
            code = source[i + 1:i + 3]

            if re.fullmatch(r"[0-9a-fA-F]{2}", code):
                output.append(chr(int(code, 16)))
                i += 3
            else:
                output.append("x")
                i += 1

        else:
            output.append(escaped)
            i += 1

    return "".join(output)


def extract_scalar_string(script_text, variable_name):
    pattern = re.compile(
        rf"\b{re.escape(variable_name)}\b\s*=\s*"
        rf'(?:"(?P<dq>(?:\\[\s\S]|[^"\\])*)"'
        rf"|'(?P<sq>(?:\\[\s\S]|[^'\\])*)')",
        re.MULTILINE,
    )

    matches = list(pattern.finditer(script_text))
    if not matches:
        raise UpdateError(
            f"current JS 缺少标量字符串 {variable_name}"
        )

    match = matches[-1]
    raw_value = match.group("dq")
    if raw_value is None:
        raw_value = match.group("sq")

    return decode_js_string(
        '"' + raw_value + '"'
    )


def normalize_date(value):
    value = value.strip()

    match = re.search(
        r"(?<!\d)(\d{4})[/.-](\d{1,2})[/.-](\d{1,2})(?!\d)",
        value,
    )

    if not match:
        raise UpdateError(
            f"无法识别日期: {value!r}"
        )

    year, month, day_value = map(
        int,
        match.groups()
    )

    return dt.date(
        year,
        month,
        day_value
    ).isoformat()


def normalize_time(value):
    value = value.strip()

    match = re.search(
        r"(?<!\d)(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?",
        value,
    )

    if not match:
        raise UpdateError(
            f"无法识别时间: {value!r}"
        )

    hour = int(match.group(1))
    minute = int(match.group(2))
    second = int(match.group(3) or 0)

    return (
        f"{hour:02d}:{minute:02d}",
        (hour, minute, second),
    )


def parse_number(value, context):
    value = value.strip().replace("−", "-")

    try:
        number = float(value)
    except ValueError as exc:
        raise UpdateError(
            f"{context} 包含无效数值: {value!r}"
        ) from exc

    if not math.isfinite(number):
        raise UpdateError(
            f"{context} 包含非有限数值: {value!r}"
        )

    return number


def split_current_series(
    value,
    variable_name
):
    parts = [
        part.strip()
        for part in re.split(
            r"[,_]",
            value
        )
        if part.strip()
    ]

    if len(parts) != INDUSTRY_COUNT:
        raise UpdateError(
            f"{variable_name} 应有 "
            f"{INDUSTRY_COUNT} 个值，"
            f"实际为 {len(parts)} 个"
        )

    return [
        parse_number(
            part,
            f"{variable_name}[{index}]"
        )
        for index, part
        in enumerate(parts)
    ]


def parse_current(
    script_text
):
    mod_date_raw = (
        extract_scalar_string(
            script_text,
            "ModDate"
        )
    )

    mod_time_raw = (
        extract_scalar_string(
            script_text,
            "ModTime"
        )
    )

    g1_raw = (
        extract_scalar_string(
            script_text,
            "G1"
        )
    )

    g2_raw = (
        extract_scalar_string(
            script_text,
            "G2"
        )
    )

    current_date = (
        normalize_date(
            mod_date_raw
        )
    )

    current_time, current_time_key = (
        normalize_time(
            mod_time_raw
        )
    )

    current_values = (
        split_current_series(
            g1_raw,
            "G1"
        )
    )

    change_values = (
        split_current_series(
            g2_raw,
            "G2"
        )
    )

    percentages = []

    for index, (
        current_value,
        change_value
    ) in enumerate(
        zip(
            current_values,
            change_values
        )
    ):
        previous_value = (
            current_value
            - change_value
        )

        if previous_value <= 0:
            raise UpdateError(
                f"G1/G2 第 {index} 项"
                "无法计算百分比"
            )

        percentage = (
            change_value
            / previous_value
            * 100.0
        )

        percentages.append(
            percentage
        )

    return {
        "date": current_date,
        "time": current_time,
        "timeKey": current_time_key,
        "values": percentages,
    }


def parse_past(
    script_text
):
    pattern = re.compile(
        r"\bGY\s*\[\s*[^\]]+\s*\]\s*=\s*"
        r'(?:"(?P<dq>(?:\\[\s\S]|[^"\\])*)"'
        r"|'(?P<sq>(?:\\[\s\S]|[^'\\])*)')"
        r"\s*;?",
        re.MULTILINE,
    )

    matches = list(
        pattern.finditer(
            script_text
        )
    )

    if not matches:
        raise UpdateError(
            "past JS 中未找到 GY[...] 日期记录"
        )

    records = {}

    for row_index, match in enumerate(
        matches
    ):
        raw_value = match.group("dq")
        if raw_value is None:
            raw_value = match.group("sq")

        payload = raw_value

        parts = [
            part.strip()
            for part in payload.split(",")
        ]

        expected_count = (
            INDUSTRY_COUNT + 2
        )

        if len(parts) != expected_count:
            raise UpdateError(
                f"past 第 {row_index + 1} 条记录"
                f"应有 {expected_count} 项，"
                f"实际为 {len(parts)} 项"
            )

        record_date = (
            normalize_date(
                parts[0]
            )
        )

        record_time, time_key = (
            normalize_time(
                parts[1]
            )
        )

        values = [
            parse_number(
                parts[index + 2],
                f"past {record_date} 第 {index} 项",
            )
            for index in range(
                INDUSTRY_COUNT
            )
        ]

        record = {
            "date": record_date,
            "time": record_time,
            "timeKey": time_key,
            "values": values,
        }

        old_record = records.get(
            record_date
        )

        if (
            old_record is None
            or time_key
            >= old_record["timeKey"]
        ):
            records[
                record_date
            ] = record

    return records


def extract_industry_names(text):
    pattern = re.compile(
        r"\bGyo\s*\[\s*(?P<index>\d+)\s*\]\s*=\s*"
        r'(?:"(?P<dq>(?:\\[\s\S]|[^"\\])*)"'
        r"|'(?P<sq>(?:\\[\s\S]|[^'\\])*)')"
        r"\s*;?",
        re.MULTILINE,
    )

    names = {}

    for match in pattern.finditer(text):
        index = int(
            match.group("index")
        )

        if (
            index < 0
            or index >= INDUSTRY_COUNT
        ):
            continue

        raw_value = match.group("dq")

        if raw_value is None:
            raw_value = match.group("sq")

        name = raw_value.strip()

        if name:
            names[
                index
            ] = name

    return names


def merge_industry_names(
    target,
    source
):
    for index, name in source.items():
        target[index] = name


def collect_industry_names(
    opener,
    entry_html,
    current_text,
    past_text,
    script_urls,
    current_url,
    past_url,
):
    names = {}

    merge_industry_names(
        names,
        extract_industry_names(
            entry_html
        )
    )

    merge_industry_names(
        names,
        extract_industry_names(
            current_text
        )
    )

    merge_industry_names(
        names,
        extract_industry_names(
            past_text
        )
    )

    if len(names) < INDUSTRY_COUNT:

        for script_url in script_urls:

            if script_url in (
                current_url,
                past_url
            ):
                continue

            try:
                script_text = (
                    fetch_text(
                        opener,
                        script_url,
                        ENTRY_URL
                    )
                )

            except UpdateError:
                continue

            merge_industry_names(
                names,
                extract_industry_names(
                    script_text
                )
            )

            if (
                len(names)
                == INDUSTRY_COUNT
            ):
                break

    missing = [
        index
        for index in range(
            INDUSTRY_COUNT
        )
        if index not in names
    ]

    if missing:
        raise UpdateError(
            "未能解析完整的 33 个行业名"
        )

    return [
        names[index]
        for index in range(
            INDUSTRY_COUNT
        )
    ]


def clean_number(
    value,
    digits=4
):
    if not math.isfinite(
        value
    ):
        raise UpdateError(
            "指标计算产生非有限数值"
        )

    return round(
        value,
        digits
    )


def pct_change(
    current,
    previous
):
    if previous == 0:
        raise UpdateError(
            "历史基准值为零"
        )

    return (
        current / previous
        - 1.0
    ) * 100.0


def calculate_snapshot(
    rows,
    cutoff
):
    if cutoff < 20:
        raise UpdateError(
            "历史数据不足"
        )

    daily = []
    change5d = []
    change20d = []

    for sector_index in range(
        INDUSTRY_COUNT
    ):
        current = (
            rows[cutoff]
            ["values"]
            [sector_index]
        )

        day_value = (
            current
        )

        change5d.append(
            sum(
                row["values"][
                    sector_index
                ]
                for row
                in rows[
                    cutoff - 4:
                    cutoff + 1
                ]
            )
        )

        change20d.append(
            sum(
                row["values"][
                    sector_index
                ]
                for row
                in rows[
                    cutoff - 19:
                    cutoff + 1
                ]
            )
        )

        daily.append(
            day_value
        )

    mean5 = sum(
        change5d
    ) / INDUSTRY_COUNT

    mean20 = sum(
        change20d
    ) / INDUSTRY_COUNT

    rs5 = [
        value - mean5
        for value in change5d
    ]

    rs20 = [
        value - mean20
        for value in change20d
    ]

    strength = [
        rs5[index] * 0.4
        + rs20[index] * 0.6
        for index in range(
            INDUSTRY_COUNT
        )
    ]

    order = sorted(
        range(
            INDUSTRY_COUNT
        ),
        key=lambda index: (
            -strength[index],
            index
        )
    )

    ranks = [
        0
    ] * INDUSTRY_COUNT

    for rank, sector_index in enumerate(
        order,
        start=1
    ):
        ranks[
            sector_index
        ] = rank

    return {
        "dailyPercent": daily,
        "change5d": change5d,
        "change20d": change20d,
        "RS5": rs5,
        "RS20": rs20,
        "strength": strength,
        "rank": ranks,
    }


def calculate_streak(
    rows,
    sector_index
):
    latest = (
        rows[-1]
        ["values"]
        [sector_index]
    )

    if latest == 0:
        return 0

    direction = (
        1
        if latest > 0
        else -1
    )

    count = 0

    for row in reversed(
        rows
    ):
        value = (
            row["values"]
            [sector_index]
        )

        if (
            direction > 0
            and value > 0
        ):
            count += 1

        elif (
            direction < 0
            and value < 0
        ):
            count += 1

        else:
            break

    return (
        count
        if direction > 0
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


def build_payload(
    rows,
    industry_names,
    current_url,
    past_url,
):
    latest_index = (
        len(rows) - 1
    )

    current_snapshot = (
        calculate_snapshot(
            rows,
            latest_index
        )
    )

    previous_snapshot = (
        calculate_snapshot(
            rows,
            latest_index - 1
        )
    )

    sectors = []

    for index in range(
        INDUSTRY_COUNT
    ):
        rank = (
            current_snapshot[
                "rank"
            ][index]
        )

        previous_rank = (
            previous_snapshot[
                "rank"
            ][index]
        )

        sectors.append(
            {
                "index": index,
                "name": industry_names[index],

                "dailyPercent":
                    clean_number(
                        current_snapshot[
                            "dailyPercent"
                        ][index]
                    ),

                "return5d":
                    clean_number(
                        current_snapshot[
                            "change5d"
                        ][index]
                    ),

                "return20d":
                    clean_number(
                        current_snapshot[
                            "change20d"
                        ][index]
                    ),

                "rs5":
                    clean_number(
                        current_snapshot[
                            "RS5"
                        ][index]
                    ),

                "rs20":
                    clean_number(
                        current_snapshot[
                            "RS20"
                        ][index]
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
                        index
                    ),

                "strength":
                    clean_number(
                        current_snapshot[
                            "strength"
                        ][index]
                    ),

                "rotationState":
                    rotation_state(
                        current_snapshot[
                            "RS5"
                        ][index],

                        current_snapshot[
                            "RS20"
                        ][index],
                    ),

                "fundFlow":
                    None,

                "fundFlowRank":
                    None,
            }
        )

    history = [
        {
            "date":
                row["date"],

            "time":
                row["time"],

            "values":[
                clean_number(
                    value
                )
                for value
                in row["values"]
            ],
        }
        for row in rows
    ]

    return {
        "schemaVersion": 2,

        "generatedAt":
            dt.datetime.now(
                dt.timezone.utc
            ).isoformat(
                timespec="seconds"
            ),

        "latestDate":
            rows[-1]["date"],

        "latestTime":
            rows[-1]["time"],

        "tradingDayCount":
            len(rows),

        "industryCount":
            INDUSTRY_COUNT,

        "industryNames":
            industry_names,

        "sectors":
            sectors,

        "history":
            history,

        "sources":{
            "entry":
                ENTRY_URL,
            "past":
                past_url,
            "current":
                current_url,
        },

        "fundFlow":
            None,
    }


def replace_data_node(
    index_html,
    payload
):
    json_text = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(
            ",",
            ":"
        ),
        allow_nan=False
    )

    json_text = (
        json_text
        .replace(
            "</",
            "<\\/"
        )
    )

    pattern = re.compile(
        r'(<script\b[^>]*'
        r'id=["\']daily-update-data["\']'
        r'[^>]*>)'
        r'.*?'
        r'(</script>)',
        re.IGNORECASE
        | re.DOTALL
    )

    if not pattern.search(
        index_html
    ):
        raise UpdateError(
            'index.html 中未找到 '
            'id="daily-update-data"'
        )

    return pattern.sub(
        lambda match:
            match.group(1)
            + "\n"
            + json_text
            + "\n"
            + match.group(2),
        index_html,
        count=1
    )


def atomic_write(
    path,
    content
):
    mode = (
        path.stat().st_mode
    )

    temp_path = None

    try:

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(
                path.parent
            ),
            prefix=".index.",
            suffix=".tmp",
            delete=False,
        ) as handle:

            handle.write(
                content
            )

            handle.flush()

            os.fsync(
                handle.fileno()
            )

            temp_path = Path(
                handle.name
            )

        os.chmod(
            temp_path,
            mode
        )

        os.replace(
            temp_path,
            path
        )

        temp_path = None

    finally:

        if (
            temp_path
            and temp_path.exists()
        ):
            temp_path.unlink()


def main():

    root = (
        Path(__file__)
        .resolve()
        .parents[1]
    )

    index_path = (
        root
        / "index.html"
    )

    cookie_jar = CookieJar()

    opener = build_opener(
        urllib.request
        .HTTPCookieProcessor(
            cookie_jar
        )
    )

    print(
        "[1/8] 抓取入口页"
    )

    entry_html = fetch_text(
        opener,
        ENTRY_URL,
        ENTRY_URL
    )

    script_urls = (
        discover_script_urls(
            entry_html
        )
    )

    print(
        f"[2/8] 自动发现 "
        f"{len(script_urls)} 个脚本"
    )

    past_candidates = [
        url
        for url in script_urls
        if (
            "country_jp_gyo_past.js"
            in urllib.parse
            .urlsplit(url)
            .path.lower()
        )
    ]

    if not past_candidates:
        raise UpdateError(
            "未找到 past JS"
        )

    past_url = (
        past_candidates[0]
    )

    print(
        f"[3/8] past URL: "
        f"{past_url}"
    )

    current_url = (
        derive_current_url(
            past_url
        )
    )

    print(
        f"[4/8] current URL: "
        f"{current_url}"
    )

    past_text = fetch_text(
        opener,
        past_url,
        ENTRY_URL
    )

    current_text = fetch_text(
        opener,
        current_url,
        ENTRY_URL
    )

    current = (
        parse_current(
            current_text
        )
    )

    print(
        "[5/8] current解析成功"
    )

    past_records = (
        parse_past(
            past_text
        )
    )

    print(
        f"[6/8] past解析成功: "
        f"{len(past_records)} 天"
    )

    industry_names = (
        collect_industry_names(
            opener,
            entry_html,
            current_text,
            past_text,
            script_urls,
            current_url,
            past_url,
        )
    )

    merged = dict(
        past_records
    )

    merged[
        current["date"]
    ] = current

    rows = [
        merged[key]
        for key in sorted(
            merged.keys()
        )
    ]

    rows = rows[
        -MAX_TRADING_DAYS:
    ]

    if len(rows) < (
        MIN_TRADING_DAYS + 1
    ):
        raise UpdateError(
            "交易日不足"
        )

    payload = build_payload(
        rows,
        industry_names,
        current_url,
        past_url,
    )

    print(
        "[7/8] 指标计算成功"
    )

    original_html = (
        index_path.read_text(
            encoding="utf-8"
        )
    )

    updated_html = (
        replace_data_node(
            original_html,
            payload
        )
    )

    atomic_write(
        index_path,
        updated_html
    )

    print(
        "[8/8] index.html写入成功"
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        print(
            "更新失败，旧 index.html 未被覆盖："
            f"{exc}",
            file=sys.stderr,
            flush=True
        )
        sys.exit(1)
