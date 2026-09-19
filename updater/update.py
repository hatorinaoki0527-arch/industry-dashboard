#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import math
import os
import re
import statistics
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


LANDING_URL = "https://nikkei225jp.com/chart/gyoushu.php"

EXPECTED_SECTOR_COUNT = 33
MIN_HISTORY_POINTS = 21
HISTORY_LIMIT = 120

SCRIPT_DIR = Path(__file__).resolve().parent
INDEX_PATH = SCRIPT_DIR.parent / "index.html"

HTTP_TIMEOUT = 30
HTTP_RETRIES = 3

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


def log(message: str) -> None:
    print(f"[updater] {message}", flush=True)


def fail(message: str) -> None:
    raise RuntimeError(message)


def finite(value: Optional[float]) -> bool:
    return value is not None and math.isfinite(value)


def rounded(
    value: Optional[float],
    digits: int = 4
) -> Optional[float]:

    if not finite(value):
        return None

    return round(float(value), digits)


class ScriptSrcParser(HTMLParser):

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: List[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs
    ) -> None:

        if tag.lower() != "script":
            return

        for key, value in attrs:
            if (
                key
                and key.lower() == "src"
                and value
            ):
                self.sources.append(
                    value.strip()
                )
                break


class HttpSession:

    def __init__(self) -> None:

        self.cookie_jar = CookieJar()

        self.opener = (
            urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(
                    self.cookie_jar
                )
            )
        )

    @staticmethod
    def decode(
        data: bytes,
        headers
    ) -> str:

        encodings = []

        try:
            charset = headers.get_content_charset()

            if charset:
                encodings.append(charset)

        except Exception:
            pass

        encodings.extend(
            [
                "utf-8",
                "cp932",
                "shift_jis",
                "euc_jp",
            ]
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
                UnicodeDecodeError,
                LookupError
            ):
                pass

        return data.decode(
            "utf-8",
            errors="replace"
        )

    def fetch(
        self,
        url: str,
        referer: Optional[str] = None,
    ) -> Tuple[str, str]:

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,"
                "application/xhtml+xml,"
                "application/javascript,"
                "text/javascript,"
                "*/*;q=0.8"
            ),
            "Accept-Language":
                "ja,en-US;q=0.8,en;q=0.6",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

        if referer:
            headers["Referer"] = referer

        last_error = None

        for attempt in range(
            1,
            HTTP_RETRIES + 1
        ):

            try:

                request = urllib.request.Request(
                    url,
                    headers=headers
                )

                with self.opener.open(
                    request,
                    timeout=HTTP_TIMEOUT
                ) as response:

                    data = response.read()

                    if not data:
                        fail(
                            f"服务器返回空内容：{url}"
                        )

                    return (
                        self.decode(
                            data,
                            response.headers
                        ),
                        response.geturl()
                    )

            except urllib.error.HTTPError as exc:

                last_error = exc

                if exc.code == 404:
                    break

            except (
                urllib.error.URLError,
                TimeoutError,
                OSError
            ) as exc:

                last_error = exc

            if attempt < HTTP_RETRIES:

                time.sleep(
                    attempt * 1.5
                )

        fail(
            f"抓取失败：{url}；{last_error}"
        )


def decode_js_string_body(
    body: str
) -> str:

    output = []

    i = 0

    escapes = {
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "b": "\b",
        "f": "\f",
        "v": "\v",
        "\\": "\\",
        "/": "/",
        '"': '"',
        "'": "'",
    }

    while i < len(body):

        char = body[i]

        if char != "\\":
            output.append(char)
            i += 1
            continue

        i += 1

        if i >= len(body):
            output.append("\\")
            break

        esc = body[i]
        i += 1

        if esc in escapes:
            output.append(
                escapes[esc]
            )
            continue

        if (
            esc == "x"
            and i + 2 <= len(body)
        ):
            token = body[i:i + 2]

            if re.fullmatch(
                r"[0-9A-Fa-f]{2}",
                token
            ):
                output.append(
                    chr(int(token, 16))
                )
                i += 2
                continue

        if (
            esc == "u"
            and i + 4 <= len(body)
        ):
            token = body[i:i + 4]

            if re.fullmatch(
                r"[0-9A-Fa-f]{4}",
                token
            ):
                output.append(
                    chr(int(token, 16))
                )
                i += 4
                continue

        output.append(esc)

    return "".join(output)


def parse_js_string_at(
    text: str,
    position: int
):

    while (
        position < len(text)
        and text[position].isspace()
    ):
        position += 1

    if (
        position >= len(text)
        or text[position] not in ('"', "'")
    ):
        return None

    quote = text[position]

    position += 1

    body = []

    while position < len(text):

        char = text[position]

        if char == quote:

            return (
                decode_js_string_body(
                    "".join(body)
                ),
                position + 1
            )

        if (
            char == "\\"
            and position + 1 < len(text)
        ):
            body.append(char)
            body.append(
                text[position + 1]
            )

            position += 2
            continue

        body.append(char)

        position += 1

    return None


def extract_scalar_string(
    text: str,
    variable: str
) -> Optional[str]:

    pattern = re.compile(
        rf"\b{re.escape(variable)}\b\s*=",
        flags=re.IGNORECASE
    )

    for match in pattern.finditer(text):

        result = parse_js_string_at(
            text,
            match.end()
        )

        if result:
            return result[0]

    return None


def extract_indexed_strings(
    text: str,
    variable: str
) -> Dict[int, str]:

    pattern = re.compile(
        rf"\b{re.escape(variable)}\s*"
        rf"\[\s*(\d+)\s*\]\s*=",
        flags=re.IGNORECASE
    )

    values = {}

    for match in pattern.finditer(text):

        result = parse_js_string_at(
            text,
            match.end()
        )

        if result:
            values[
                int(match.group(1))
            ] = result[0]

    return values


def parse_number(
    value: str
) -> Optional[float]:

    value = (
        value.strip()
        .replace("−", "-")
        .replace("％", "%")
        .replace("\xa0", "")
    )

    if not value:
        return None

    if value in {
        "-",
        "--",
        "---",
        "N/A",
        "null",
        "undefined"
    }:
        return None

    value = (
        value
        .rstrip("%")
        .strip()
    )

    try:
        number = float(value)

    except ValueError:
        return None

    if not math.isfinite(number):
        return None

    return number


def parse_number_fields(
    value: str
) -> List[Optional[float]]:

    normalized = (
        value.strip()
        .replace("，", ",")
        .replace("｜", "|")
        .replace("\r", "")
        .replace("\n", ",")
    )

    if "," in normalized:
        parts = normalized.split(",")

    elif "|" in normalized:
        parts = normalized.split("|")

    elif ";" in normalized:
        parts = normalized.split(";")

    elif "\t" in normalized:
        parts = normalized.split("\t")

    else:
        parts = re.split(
            r"\s+",
            normalized
        )

    parsed = [
        parse_number(part)
        for part in parts
    ]

    if len(parsed) <= 1:

        tokens = re.findall(
            r"[-+]?(?:\d+(?:\.\d*)?|\.?\d+)",
            normalized
        )

        parsed = [
            parse_number(token)
            for token in tokens
        ]

    return parsed


def normalize_sector_name(
    name: str
) -> str:

    name = unescape(name)

    name = re.sub(
        r"<[^>]+>",
        "",
        name
    )

    name = re.sub(
        r"\s+",
        " ",
        name
    )

    return name.strip()


def same_source_host(
    url: str
) -> bool:

    hostname = (
        urllib.parse.urlparse(
            url
        ).hostname
        or ""
    ).lower()

    return (
        hostname == "nikkei225jp.com"
        or hostname.endswith(
            ".nikkei225jp.com"
        )
    )


def discover_script_urls(
    html: str,
    base_url: str
) -> List[str]:

    parser = ScriptSrcParser()

    parser.feed(html)

    sources = list(
        parser.sources
    )

    for match in re.finditer(
        r"""<script\b[^>]*\bsrc\s*=\s*(["'])(.*?)\1""",
        html,
        flags=re.IGNORECASE | re.DOTALL
    ):

        sources.append(
            unescape(
                match.group(2).strip()
            )
        )

    discovered = []

    seen = set()

    for source in sources:

        absolute = urllib.parse.urljoin(
            base_url,
            source
        )

        parsed = urllib.parse.urlsplit(
            absolute
        )

        absolute = urllib.parse.urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.query,
                ""
            )
        )

        if parsed.scheme not in {
            "http",
            "https"
        }:
            continue

        if not same_source_host(
            absolute
        ):
            continue

        if absolute in seen:
            continue

        seen.add(absolute)

        discovered.append(
            absolute
        )

    def priority(
        url: str
    ):

        lower = url.lower()

        score = 0

        if "gyo" in lower:
            score += 100

        if "country_jp" in lower:
            score += 50

        if "past" in lower:
            score += 30

        if "_data" in lower:
            score += 20

        if ".js" in lower:
            score += 10

        return (
            -score,
            lower
        )

    discovered.sort(
        key=priority
    )

    return discovered


def endpoint_distance(
    series,
    current,
    previous
):

    last = next(
        (
            value
            for value in reversed(series)
            if finite(value)
        ),
        None
    )

    if not finite(last):
        return None

    return min(
        abs(last-current)
        / max(abs(current),1e-9),

        abs(last-previous)
        / max(abs(previous),1e-9)
    )


def matrix_score(
    histories,
    current_values,
    changes
):

    distances = []

    for index in range(
        EXPECTED_SECTOR_COUNT
    ):

        previous = (
            current_values[index]
            - changes[index]
        )

        distance = endpoint_distance(
            histories[index],
            current_values[index],
            previous
        )

        if distance is not None:
            distances.append(
                distance
            )

    if len(distances) < 30:
        return float("inf")

    return statistics.median(
        distances
    )


def orient_past_history(
    indexed_history,
    current_values,
    changes
):

    ordered_rows = [
        parse_number_fields(
            indexed_history[key]
        )
        for key in sorted(
            indexed_history
        )
    ]

    candidates = []

    if len(ordered_rows) >= 33:

        sector_major = [
            list(row)
            for row in ordered_rows[:33]
        ]

        if all(
            len(row) >= 20
            for row in sector_major
        ):
            candidates.append(
                (
                    "sector-major",
                    sector_major
                )
            )

    day_rows = [
        row
        for row in ordered_rows
        if len(row) >= 33
    ]

    if len(day_rows) >= 20:

        day_major = [
            [
                row[sector]
                for row in day_rows
            ]
            for sector in range(33)
        ]

        candidates.append(
            (
                "day-major",
                day_major
            )
        )

    if not candidates:
        fail(
            "无法识别 past GY 历史结构"
        )

    oriented = []

    for label, histories in candidates:

        normal = [
            list(series)
            for series in histories
        ]

        reverse = [
            list(
                reversed(series)
            )
            for series in histories
        ]

        oriented.append(
            (
                matrix_score(
                    normal,
                    current_values,
                    changes
                ),
                label + "/oldest-to-newest",
                normal
            )
        )

        oriented.append(
            (
                matrix_score(
                    reverse,
                    current_values,
                    changes
                ),
                label + "/reversed",
                reverse
            )
        )

    score, orientation, histories = min(
        oriented,
        key=lambda item: item[0]
    )

    if (
        not math.isfinite(score)
        or score > 0.30
    ):
        fail(
            "past 历史与当前数据不匹配："
            f"{orientation} / {score:.4f}"
        )

    return (
        histories,
        orientation,
        score
    )


def strip_missing_edges(
    series
):

    values = list(series)

    while (
        values
        and not finite(values[0])
    ):
        values.pop(0)

    while (
        values
        and not finite(values[-1])
    ):
        values.pop()

    return values


def append_current_values(
    histories,
    current_values,
    changes
):

    normalized = []

    for index in range(33):

        series = strip_missing_edges(
            histories[index]
        )

        if not series:
            fail(
                f"行业 {index} 没有历史数据"
            )

        current = current_values[index]

        previous = (
            current
            - changes[index]
        )

        last = series[-1]

        if not finite(last):
            fail(
                f"行业 {index} 历史末值无效"
            )

        distance_current = (
            abs(last-current)
            / max(abs(current),1e-9)
        )

        distance_previous = (
            abs(last-previous)
            / max(abs(previous),1e-9)
        )

        if (
            distance_current
            <= distance_previous
            and distance_current <= 0.02
        ):
            series[-1] = current

        else:
            series.append(current)

        if len(series) < 21:
            fail(
                f"行业 {index} 历史不足21个交易点"
            )

        normalized.append(
            series[-HISTORY_LIMIT:]
        )

    return normalized


def value_n_sessions_ago(
    series,
    sessions
):

    position = (
        len(series)
        - 1
        - sessions
    )

    if position < 0:
        return None

    value = series[position]

    return (
        value
        if finite(value)
        else None
    )


def period_return(
    series,
    sessions
):

    if (
        not series
        or not finite(series[-1])
    ):
        return None

    base = value_n_sessions_ago(
        series,
        sessions
    )

    if (
        not finite(base)
        or base == 0
    ):
        return None

    return (
        series[-1]
        / base
        - 1
    ) * 100


def one_day_return(
    series,
    lag
):

    end_position = (
        len(series)
        - 1
        - lag
    )

    start_position = (
        end_position
        - 1
    )

    if start_position < 0:
        return None

    start = series[
        start_position
    ]

    end = series[
        end_position
    ]

    if (
        not finite(start)
        or not finite(end)
        or start == 0
    ):
        return None

    return (
        end / start
        - 1
    ) * 100


def calculate_streaks(
    histories
):

    relative_by_lag = []

    for lag in range(20):

        daily_returns = [
            one_day_return(
                series,
                lag
            )
            for series in histories
        ]

        valid = [
            value
            for value in daily_returns
            if finite(value)
        ]

        if not valid:
            break

        benchmark = statistics.fmean(
            valid
        )

        relative_by_lag.append(
            [
                (
                    value - benchmark
                    if finite(value)
                    else None
                )
                for value in daily_returns
            ]
        )

    streaks = []

    for sector in range(33):

        direction = 0
        count = 0

        for row in relative_by_lag:

            value = row[sector]

            if (
                not finite(value)
                or abs(value) < 1e-12
            ):
                break

            current_direction = (
                1
                if value > 0
                else -1
            )

            if direction == 0:

                direction = (
                    current_direction
                )

                count = 1

            elif (
                current_direction
                == direction
            ):
                count += 1

            else:
                break

        streaks.append(
            direction * count
        )

    return streaks


def assign_rank(
    records,
    field,
    output_field
):

    eligible = [
        record
        for record in records
        if finite(
            record.get(field)
        )
    ]

    eligible.sort(
        key=lambda r:r[field],
        reverse=True
    )

    for rank, record in enumerate(
        eligible,
        start=1
    ):
        record[
            output_field
        ] = rank

    for record in records:
        record.setdefault(
            output_field,
            None
        )


def calculate_sector_records(
    names,
    current_values,
    changes,
    histories
):

    pct1 = []

    pct5_values = []

    pct20_values = []

    for index in range(33):

        current = current_values[
            index
        ]

        change = changes[
            index
        ]

        previous = (
            current - change
        )

        if previous <= 0:
            fail(
                f"行业 {index} 前收盘无效"
            )

        pct1.append(
            change
            / previous
            * 100
        )

        pct5_values.append(
            period_return(
                histories[index],
                5
            )
        )

        pct20_values.append(
            period_return(
                histories[index],
                20
            )
        )

    valid5 = [
        x for x in pct5_values
        if finite(x)
    ]

    valid20 = [
        x for x in pct20_values
        if finite(x)
    ]

    if len(valid5) < 30:
        fail(
            "5日收益有效行业不足"
        )

    if len(valid20) < 30:
        fail(
            "20日收益有效行业不足"
        )

    mean1 = statistics.fmean(
        pct1
    )

    mean5 = statistics.fmean(
        valid5
    )

    mean20 = statistics.fmean(
        valid20
    )

    streaks = calculate_streaks(
        histories
    )

    records = []

    for index in range(33):

        ret5 = pct5_values[index]

        ret20 = pct20_values[index]

        rs1 = (
            pct1[index]
            - mean1
        )

        rs5 = (
            ret5 - mean5
            if finite(ret5)
            else None
        )

        rs20 = (
            ret20 - mean20
            if finite(ret20)
            else None
        )

        if (
            finite(rs5)
            and finite(rs20)
        ):

            if (
                rs5 >= 0
                and rs20 >= 0
            ):
                rotation = "领先"

            elif (
                rs5 < 0
                and rs20 >= 0
            ):
                rotation = "转弱"

            elif (
                rs5 < 0
                and rs20 < 0
            ):
                rotation = "落后"

            else:
                rotation = "改善"

        else:
            rotation = None

        streak = streaks[index]

        if streak > 0:
            strong_days = streak
            weak_days = None

        elif streak < 0:
            strong_days = None
            weak_days = abs(
                streak
            )

        else:
            strong_days = None
            weak_days = None

        records.append(
            {
                "日期": mod_date_global,
                "时间": mod_time_global,

                "市场": "日本",
                "分类体系": "东证33",

                "一级行业":
                    names[index],

                "行业指数名称":
                    names[index],

                "行业指数值":
                    rounded(
                        current_values[index],
                        6
                    ),

                "行业指数点数涨跌":
                    rounded(
                        changes[index],
                        6
                    ),

                "行业指数涨跌幅(%)":
                    rounded(
                        pct1[index]
                    ),

                "33行业当日平均涨跌幅(%)":
                    rounded(
                        mean1
                    ),

                "当日相对强度(%)":
                    rounded(
                        rs1
                    ),

                "5日收益率(%)":
                    rounded(
                        ret5
                    ),

                "20日收益率(%)":
                    rounded(
                        ret20
                    ),

                "5日相对强度(%)":
                    rounded(
                        rs5
                    ),

                "20日相对强度(%)":
                    rounded(
                        rs20
                    ),

                "连续强势天数":
                    strong_days,

                "连续弱势天数":
                    weak_days,

                "轮动状态":
                    rotation,

                "数据状态":
                    "来源：nikkei225jp.com",
            }
        )

    assign_rank(
        records,
        "行业指数涨跌幅(%)",
        "上涨排名"
    )

    assign_rank(
        records,
        "5日收益率(%)",
        "5日排名"
    )

    assign_rank(
        records,
        "20日收益率(%)",
        "20日排名"
    )

    return (
        records,
        mean1,
        mean5,
        mean20
    )


def build_history_payload(
    names,
    histories
):

    max_len = max(
        len(series)
        for series in histories
    )

    output = {}

    for index, name in enumerate(
        names
    ):

        series = histories[index]

        output[name] = []

        offset = (
            max_len
            - len(series)
        )

        for i, value in enumerate(
            series
        ):

            output[name].append(
                {
                    "序号":
                        offset+i,

                    "行业指数值":
                        rounded(
                            value,
                            6
                        )
                }
            )

    return output


def locate_embedded_region(
    html: str
):

    match = re.search(
        r"""<script\b"""
        r"""(?=[^>]*\bid\s*=\s*["']embedded-data["'])"""
        r"""[^>]*>""",
        html,
        flags=
            re.IGNORECASE
            | re.DOTALL
    )

    if not match:
        fail(
            'index.html 中没有 id="embedded-data"'
        )

    close = re.search(
        r"</script\s*>",
        html[match.end():],
        flags=re.IGNORECASE
    )

    if not close:
        fail(
            "embedded-data 缺少 </script>"
        )

    start = match.end()

    end = (
        start
        + close.start()
    )

    return start, end


def inject_payload(
    index_html,
    payload
):

    start, end = (
        locate_embedded_region(
            index_html
        )
    )

    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False
    )

    serialized = serialized.replace(
        "<",
        "\\u003c"
    )

    return (
        index_html[:start]
        + "\n"
        + serialized
        + "\n"
        + index_html[end:]
    )


def atomic_write(
    path,
    content
):

    original_mode = (
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
            delete=False
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
            original_mode
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


mod_date_global = ""
mod_time_global = ""


def main():

    global mod_date_global
    global mod_time_global

    if not INDEX_PATH.exists():
        fail(
            f"找不到 index.html：{INDEX_PATH}"
        )

    session = HttpSession()

    log(
        f"抓取入口页面：{LANDING_URL}"
    )

    landing_html, landing_final = (
        session.fetch(
            LANDING_URL
        )
    )

    log(
        "入口页面抓取成功"
    )

    script_urls = discover_script_urls(
        landing_html,
        landing_final
    )

    if not script_urls:
        fail(
            "gyoushu.php 中没有发现脚本"
        )

    log(
        f"自动发现 {len(script_urls)} 个同站脚本"
    )

    sources = [
        (
            landing_final,
            landing_html
        )
    ]

    for script_url in script_urls:

        try:

            log(
                f"尝试抓取：{script_url}"
            )

            script_text, final_url = (
                session.fetch(
                    script_url,
                    referer=landing_final
                )
            )

            sources.append(
                (
                    final_url,
                    script_text
                )
            )

        except Exception as exc:

            log(
                f"跳过：{script_url}；{exc}"
            )

    best_names = {}

    for _, source_text in sources:

        names = (
            extract_indexed_strings(
                source_text,
                "Gyo"
            )
        )

        if (
            len(names)
            > len(best_names)
        ):
            best_names = names

    missing = [
        i
        for i in range(33)
        if i not in best_names
    ]

    if missing:
        fail(
            "行业名称不完整："
            + ",".join(
                map(str,missing)
            )
        )

    sector_names = [
        normalize_sector_name(
            best_names[i]
        )
        for i in range(33)
    ]

    current_candidates = []

    for source_url, source_text in sources:

        g1 = extract_scalar_string(
            source_text,
            "G1"
        )

        g2 = extract_scalar_string(
            source_text,
            "G2"
        )

        if (
            g1 is None
            or g2 is None
        ):
            continue

        mod_date = (
            extract_scalar_string(
                source_text,
                "ModDate"
            )
        )

        mod_time = (
            extract_scalar_string(
                source_text,
                "ModTime"
            )
        )

        score = 0

        lower = source_url.lower()

        if mod_date:
            score += 100

        if mod_time:
            score += 20

        if "gyo" in lower:
            score += 20

        if "country_jp" in lower:
            score += 10

        current_candidates.append(
            (
                score,
                source_url,
                g1,
                g2,
                mod_date,
                mod_time
            )
        )

    if not current_candidates:
        fail(
            "没有找到包含 G1/G2 的 current JS"
        )

    (
        _,
        current_url,
        g1_text,
        g2_text,
        mod_date_global,
        mod_time_global
    ) = max(
        current_candidates,
        key=lambda item:item[0]
    )

    if not mod_date_global:
        fail(
            "current JS 没有 ModDate"
        )

    if not mod_time_global:
        mod_time_global = ""

    g1_raw = parse_number_fields(
        g1_text
    )

    g2_raw = parse_number_fields(
        g2_text
    )

    if len(g1_raw) < 33:
        fail(
            f"G1 只有 {len(g1_raw)} 个值"
        )

    if len(g2_raw) < 33:
        fail(
            f"G2 只有 {len(g2_raw)} 个值"
        )

    current_values = []

    changes = []

    for i in range(33):

        current = g1_raw[i]

        change = g2_raw[i]

        if not finite(current):
            fail(
                f"G1[{i}] 无效"
            )

        if not finite(change):
            fail(
                f"G2[{i}] 无效"
            )

        if current <= 0:
            fail(
                f"G1[{i}] 非正数"
            )

        if (
            current
            - change
            <= 0
        ):
            fail(
                f"行业 {i} 前收盘无效"
            )

        current_values.append(
            float(current)
        )

        changes.append(
            float(change)
        )

    log(
        "current JS 解析成功"
    )

    past_candidates = []

    for source_url, source_text in sources:

        indexed = (
            extract_indexed_strings(
                source_text,
                "GY"
            )
        )

        if not indexed:
            continue

        score = len(indexed)

        lower = source_url.lower()

        if "past" in lower:
            score += 100

        if "gyo" in lower:
            score += 20

        past_candidates.append(
            (
                score,
                source_url,
                indexed
            )
        )

    if not past_candidates:
        fail(
            "没有找到 past GY 历史"
        )

    (
        _,
        past_url,
        indexed_history
    ) = max(
        past_candidates,
        key=lambda item:item[0]
    )

    (
        histories,
        history_orientation,
        history_score
    ) = orient_past_history(
        indexed_history,
        current_values,
        changes
    )

    histories = append_current_values(
        histories,
        current_values,
        changes
    )

    log(
        "历史数据解析成功"
    )

    (
        summary,
        avg1,
        avg5,
        avg20
    ) = calculate_sector_records(
        sector_names,
        current_values,
        changes,
        histories
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
            build_history_payload(
                sector_names,
                histories
            ),

        "meta":{
            "market":
                "日本",

            "classification":
                "东证33",

            "generated_at":
                datetime.now(
                    timezone.utc
                ).isoformat(
                    timespec="seconds"
                ),

            "source_date":
                mod_date_global,

            "source_time":
                mod_time_global,

            "current_url":
                current_url,

            "past_url":
                past_url,

            "history_orientation":
                history_orientation,

            "history_endpoint_score":
                rounded(
                    history_score,
                    6
                ),

            "average_change_pct":
                rounded(avg1),

            "average_return_5d":
                rounded(avg5),

            "average_return_20d":
                rounded(avg20),

            "notes":[
                "G2 为指数点数涨跌额",
                "涨跌百分比按 change/(current-change)*100 换算",
                "轮动为价格相对强弱，不代表真实资金净流"
            ]
        }
    }

    index_html = (
        INDEX_PATH.read_text(
            encoding="utf-8"
        )
    )

    updated_html = (
        inject_payload(
            index_html,
            payload
        )
    )

    atomic_write(
        INDEX_PATH,
        updated_html
    )

    log(
        "index.html 写入成功"
    )

    log(
        f"current JS：{current_url}"
    )

    log(
        f"past JS：{past_url}"
    )

    return 0


if __name__ == "__main__":

    try:
        sys.exit(
            main()
        )

    except Exception as exc:

        print(
            f"[updater] ERROR：{exc}",
            file=sys.stderr,
            flush=True
        )

        sys.exit(1)
