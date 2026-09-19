#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import html
import json
import math
import os
import re
import statistics
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


# ============================================================
# 基本配置
# ============================================================

GYOUSHU_URLS = (
    "https://nikkei225jp.com/chart/gyoushu.php",
)

CURRENT_JS_URL = (
    "https://nikkei225jp.com/_data/_nfsDATA/min/country_jp_gyo.js"
)

PAST_JS_URL = (
    "https://nikkei225jp.com/_data/_nfsDATA/min/country_jp_gyo_past.js"
)

REQUEST_TIMEOUT = 30
REQUEST_RETRIES = 3
REQUEST_RETRY_WAIT = 2

EXPECTED_INDUSTRY_COUNT = 33
MIN_HISTORY_DAYS = 21
HISTORY_KEEP_DAYS = 90

JST = timezone(timedelta(hours=9))

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = PROJECT_ROOT / "index.html"

USER_AGENT = (
    "Mozilla/5.0 (compatible; TOPIX33Updater/2.0; "
    "+https://github.com/)"
)


# ============================================================
# 东证33行业标准顺序
# ============================================================

INDUSTRY_NAMES = [
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
    "証券業",
    "保険業",
    "その他金融業",
    "不動産業",
    "サービス業",
]


class UpdateError(RuntimeError):
    pass


class TableTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.all_text = []
        self.rows = []

        self._in_row = False
        self._in_cell = False
        self._current_row = []
        self._current_cell = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()

        if tag == "tr":
            self._in_row = True
            self._current_row = []

        elif tag in ("td", "th") and self._in_row:
            self._in_cell = True
            self._current_cell = []

    def handle_endtag(self, tag):
        tag = tag.lower()

        if tag in ("td", "th") and self._in_cell:
            cell = normalize_whitespace(
                "".join(self._current_cell)
            )
            self._current_row.append(cell)
            self._current_cell = []
            self._in_cell = False

        elif tag == "tr" and self._in_row:
            if self._current_row:
                self.rows.append(self._current_row)

            self._current_row = []
            self._in_row = False
            self._in_cell = False

    def handle_data(self, data):
        if data:
            self.all_text.append(data)

            if self._in_cell:
                self._current_cell.append(data)


def log(message):
    print(f"[update.py] {message}", flush=True)


def normalize_whitespace(value):
    return re.sub(
        r"\s+",
        " ",
        html.unescape(value or ""),
    ).strip()


def normalize_date(value):
    if not value:
        return None

    text = str(value).strip()

    patterns = (
        r"(20\d{2})[年/\-.](\d{1,2})[月/\-.](\d{1,2})日?",
        r"\b(20\d{2})(\d{2})(\d{2})\b",
    )

    for pattern in patterns:
        match = re.search(pattern, text)

        if not match:
            continue

        try:
            parsed = date(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
            )
        except ValueError:
            continue

        return parsed.isoformat()

    return None


def normalize_time(value):
    if not value:
        return None

    match = re.search(
        r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?(?!\d)",
        str(value),
    )

    if not match:
        return None

    return f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"


def is_finite_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def clean_float(value, digits=6):
    result = round(float(value), digits)

    if result == 0:
        return 0.0

    return result


def clean_percent(value):
    if value is None:
        return None

    if not math.isfinite(value):
        return None

    return round(float(value), 4)


# ============================================================
# 网络
# ============================================================

def fetch_text(url):
    last_error = None

    for attempt in range(1, REQUEST_RETRIES + 1):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": (
                    "text/html,application/javascript,"
                    "text/javascript,*/*;q=0.8"
                ),
                "Cache-Control": "no-cache",
            },
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=REQUEST_TIMEOUT,
            ) as response:

                raw = response.read()

                if not raw:
                    raise UpdateError(
                        f"{url} 返回空内容"
                    )

                charset = (
                    response.headers.get_content_charset()
                )

                for encoding in (
                    charset,
                    "utf-8",
                    "cp932",
                    "shift_jis",
                    "euc_jp",
                ):
                    if not encoding:
                        continue

                    try:
                        return raw.decode(encoding)
                    except (
                        UnicodeDecodeError,
                        LookupError,
                    ):
                        pass

                return raw.decode(
                    "utf-8",
                    errors="replace",
                )

        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            OSError,
        ) as exc:

            last_error = exc

            if attempt < REQUEST_RETRIES:
                time.sleep(
                    REQUEST_RETRY_WAIT * attempt
                )

    raise UpdateError(
        f"抓取失败：{url}；{last_error}"
    )


# ============================================================
# JS解析
# ============================================================

def js_unescape(value):
    def replace_unicode(match):
        return chr(int(match.group(1), 16))

    value = re.sub(
        r"\\u([0-9a-fA-F]{4})",
        replace_unicode,
        value,
    )

    return (
        value
        .replace(r"\/", "/")
        .replace(r"\"", '"')
        .replace(r"\'", "'")
        .replace(r"\\", "\\")
    )


def parse_number_list(value, label):
    parts = [
        x.strip()
        for x in re.split(r"[,_]", value)
        if x.strip()
    ]

    if len(parts) != 33:
        raise UpdateError(
            f"{label} 应包含33个数值，"
            f"实际为 {len(parts)} 个"
        )

    numbers = []

    for i, part in enumerate(parts):
        try:
            number = float(part)
        except ValueError as exc:
            raise UpdateError(
                f"{label} 第{i + 1}个值不是数字："
                f"{part}"
            ) from exc

        if not math.isfinite(number):
            raise UpdateError(
                f"{label} 第{i + 1}个值无效"
            )

        numbers.append(number)

    return numbers


def extract_js_assignment(text, variable):
    pattern = re.compile(
        rf"""
        \b{re.escape(variable)}
        \s*=\s*
        (?P<quote>["'])
        (?P<value>.*?)
        (?P=quote)\s*;
        """,
        re.VERBOSE | re.DOTALL,
    )

    matches = list(pattern.finditer(text))

    if len(matches) != 1:
        raise UpdateError(
            f"{variable} 应出现1次，"
            f"实际 {len(matches)} 次"
        )

    return js_unescape(
        matches[0].group("value")
    ).strip()


# ============================================================
# 行业名称
# ============================================================

def parse_industry_names(text):
    pattern = re.compile(
        r"""
        \bGyo\s*
        \[\s*(\d{1,2})\s*\]
        \s*=\s*
        (?P<quote>["'])
        (?P<value>.*?)
        (?P=quote)\s*;
        """,
        re.VERBOSE | re.DOTALL,
    )

    matches = list(pattern.finditer(text))

    if len(matches) != 33:
        raise UpdateError(
            f"应有33条Gyo定义，"
            f"实际 {len(matches)} 条"
        )

    names_by_index = {}

    for match in matches:
        index = int(match.group(1))

        name = html.unescape(
            js_unescape(
                match.group("value")
            )
        ).strip()

        names_by_index[index] = name

    if set(names_by_index) != set(range(33)):
        raise UpdateError(
            "Gyo索引必须连续为0-32"
        )

    return [
        names_by_index[i]
        for i in range(33)
    ]


def load_industry_names():
    errors = []

    for url in GYOUSHU_URLS:
        try:
            return parse_industry_names(
                fetch_text(url)
            )
        except Exception as exc:
            errors.append(str(exc))

    raise UpdateError(
        "无法取得行业名称："
        + " | ".join(errors)
    )


# ============================================================
# 当前行情
# ============================================================

def parse_current_js(text):
    mod_date = extract_js_assignment(
        text,
        "ModDate",
    )

    mod_time = extract_js_assignment(
        text,
        "ModTime",
    )

    current_values = parse_number_list(
        extract_js_assignment(
            text,
            "G1",
        ),
        "G1",
    )

    changes = parse_number_list(
        extract_js_assignment(
            text,
            "G2",
        ),
        "G2",
    )

    parsed_date = datetime.strptime(
        mod_date,
        "%Y/%m/%d",
    )

    datetime.strptime(
        mod_time,
        "%H:%M",
    )

    return {
        "date": parsed_date.strftime(
            "%Y-%m-%d"
        ),
        "time": mod_time,
        "values": current_values,
        "changes": changes,
    }


# ============================================================
# 历史行情
# ============================================================

def parse_past_js(text):
    pattern = re.compile(
        r"""
        \bGY\s*
        \[\s*[^\]]+\s*\]
        \s*=\s*
        (?P<quote>["'])
        (?P<value>.*?)
        (?P=quote)\s*;
        """,
        re.VERBOSE | re.DOTALL,
    )

    matches = list(pattern.finditer(text))

    if not matches:
        raise UpdateError(
            "past.js 没找到GY历史记录"
        )

    rows = []

    for row_number, match in enumerate(
        matches,
        start=1,
    ):
        raw = js_unescape(
            match.group("value")
        )

        parts = [
            x.strip()
            for x in raw.split(",")
        ]

        if len(parts) != 35:
            continue

        date_text = parts[0]
        time_text = parts[1]

        try:
            parsed = datetime.strptime(
                f"{date_text} {time_text}",
                "%Y/%m/%d %H:%M",
            )
        except ValueError:
            continue

        values = []

        valid = True

        for part in parts[2:]:
            try:
                number = float(part)
            except ValueError:
                valid = False
                break

            if not math.isfinite(number):
                valid = False
                break

            values.append(number)

        if (
            not valid
            or len(values) != 33
        ):
            continue

        rows.append(
            {
                "date": parsed.strftime(
                    "%Y-%m-%d"
                ),
                "time": parsed.strftime(
                    "%H:%M"
                ),
                "values": values,
            }
        )

    if not rows:
        raise UpdateError(
            "没有解析到有效历史记录"
        )

    # 去重并按日期升序
    deduplicated = {}

    for row in rows:
        old = deduplicated.get(
            row["date"]
        )

        if (
            old is None
            or row["time"] >= old["time"]
        ):
            deduplicated[
                row["date"]
            ] = row

    return sorted(
        deduplicated.values(),
        key=lambda x: x["date"],
    )


# ============================================================
# 当前 + 历史合并
# ============================================================

def merge_history(
    history_rows,
    current_data,
):

    merged = {
        row["date"]: row
        for row in history_rows
    }

    merged[current_data["date"]] = {
        "date": current_data["date"],
        "time": current_data["time"],
        "values": [
            float(x)
            for x in current_data["values"]
        ],
    }

    records = sorted(
        merged.values(),
        key=lambda x: x["date"],
    )

    if len(records) < MIN_HISTORY_DAYS:
        raise UpdateError(
            f"历史数据不足："
            f"{len(records)} 个交易日"
        )

    keep_days = max(
        MIN_HISTORY_DAYS,
        HISTORY_KEEP_DAYS,
    )

    return records[-keep_days:]


# ============================================================
# 收益率
# ============================================================

def calculate_period_returns(
    records,
    days,
):
    if len(records) <= days:
        return [None] * 33

    latest = records[-1]["values"]

    previous = (
        records[-1 - days]["values"]
    )

    result = []

    for current, base in zip(
        latest,
        previous,
    ):
        if (
            not is_finite_number(current)
            or not is_finite_number(base)
            or base <= 0
        ):
            result.append(None)
            continue

        result.append(
            (
                current / base
                - 1
            ) * 100
        )

    return result


def calculate_daily_returns(records):
    matrix = []

    for i in range(
        1,
        len(records),
    ):
        previous = (
            records[i - 1]["values"]
        )

        current = (
            records[i]["values"]
        )

        daily = []

        for cur, prev in zip(
            current,
            previous,
        ):
            if prev <= 0:
                daily.append(None)
            else:
                daily.append(
                    (
                        cur / prev
                        - 1
                    ) * 100
                )

        matrix.append(daily)

    return matrix


# ============================================================
# 强弱
# ============================================================

def average_if_complete(values):
    if (
        len(values) != 33
        or any(
            value is None
            for value in values
        )
    ):
        return None

    return statistics.fmean(
        values
    )


def relative_strength(values):
    average = (
        average_if_complete(values)
    )

    if average is None:
        return (
            [None] * 33,
            None,
        )

    return (
        [
            value - average
            for value in values
        ],
        average,
    )


# ============================================================
# 排名
# ============================================================

def ranking(values):
    if (
        len(values) != 33
        or any(
            value is None
            for value in values
        )
    ):
        return [None] * 33

    order = sorted(
        range(33),
        key=lambda i: values[i],
        reverse=True,
    )

    ranks = [None] * 33

    for rank, index in enumerate(
        order,
        start=1,
    ):
        ranks[index] = rank

    return ranks


# ============================================================
# 连续强弱
# ============================================================

def calculate_streaks(
    daily_matrix,
):

    if not daily_matrix:
        return (
            [None] * 33,
            [None] * 33,
        )

    relative_history = []

    for day in daily_matrix:
        average = (
            average_if_complete(day)
        )

        if average is None:
            return (
                [None] * 33,
                [None] * 33,
            )

        relative_history.append(
            [
                value - average
                for value in day
            ]
        )

    strong_days = []
    weak_days = []

    for industry_index in range(33):

        latest = (
            relative_history[-1][
                industry_index
            ]
        )

        latest_strong = (
            latest > 0
        )

        count = 0

        for row in reversed(
            relative_history
        ):
            strong = (
                row[industry_index]
                > 0
            )

            if strong != latest_strong:
                break

            count += 1

        if latest_strong:
            strong_days.append(count)
            weak_days.append(None)
        else:
            strong_days.append(None)
            weak_days.append(count)

    return (
        strong_days,
        weak_days,
    )


# ============================================================
# 轮动状态
# ============================================================

def rotation_state(
    daily_rs,
    five_day_rs,
):

    if (
        daily_rs is None
        or five_day_rs is None
    ):
        return None

    if (
        daily_rs > 0
        and five_day_rs > 0
    ):
        return "强势延续"

    if (
        daily_rs > 0
        and five_day_rs <= 0
    ):
        return "转强"

    if (
        daily_rs <= 0
        and five_day_rs > 0
    ):
        return "转弱"

    return "弱势延续"


# ============================================================
# 生成 summary
# ============================================================

def build_summary(
    names,
    records,
):

    if len(records) < 2:
        raise UpdateError(
            "历史不足，无法计算收益率"
        )

    latest = records[-1]

    daily_matrix = (
        calculate_daily_returns(
            records
        )
    )

    daily_returns = (
        daily_matrix[-1]
    )

    five_returns = (
        calculate_period_returns(
            records,
            5,
        )
    )

    twenty_returns = (
        calculate_period_returns(
            records,
            20,
        )
    )

    daily_rs, daily_avg = (
        relative_strength(
            daily_returns
        )
    )

    five_rs, five_avg = (
        relative_strength(
            five_returns
        )
    )

    twenty_rs, twenty_avg = (
        relative_strength(
            twenty_returns
        )
    )

    daily_rank = ranking(
        daily_returns
    )

    five_rank = ranking(
        five_returns
    )

    strong_days, weak_days = (
        calculate_streaks(
            daily_matrix
        )
    )

    summary = []

    for index, name in enumerate(
        names
    ):

        if (
            daily_rank[index]
            is not None
            and five_rank[index]
            is not None
        ):
            rank_change = (
                five_rank[index]
                - daily_rank[index]
            )
        else:
            rank_change = None

        summary.append(
            {
                "日期": latest["date"],
                "时间": latest["time"],
                "市场": "日本",
                "分类体系": "东证33",
                "一级行业": name,
                "行业指数名称": name,

                "行业指数值":
                    clean_float(
                        latest["values"][
                            index
                        ]
                    ),

                "行业指数涨跌幅(%)":
                    clean_percent(
                        daily_returns[
                            index
                        ]
                    ),

                "33行业当日平均涨跌幅(%)":
                    clean_percent(
                        daily_avg
                    ),

                "当日相对强度(%)":
                    clean_percent(
                        daily_rs[index]
                    ),

                "5日收益率(%)":
                    clean_percent(
                        five_returns[
                            index
                        ]
                    ),

                "20日收益率(%)":
                    clean_percent(
                        twenty_returns[
                            index
                        ]
                    ),

                "5日相对强度(%)":
                    clean_percent(
                        five_rs[index]
                    ),

                "20日相对强度(%)":
                    clean_percent(
                        twenty_rs[index]
                    ),

                "当日强弱排名":
                    daily_rank[index],

                "5日排名":
                    five_rank[index],

                "排名变化":
                    rank_change,

                "连续强势天数":
                    strong_days[index],

                "连续弱势天数":
                    weak_days[index],

                "轮动状态":
                    rotation_state(
                        daily_rs[index],
                        five_rs[index],
                    ),

                "数据状态":
                    "来源：nikkei225jp.com",
            }
        )

    if len(summary) != 33:
        raise UpdateError(
            "summary不是33条"
        )

    return summary


# ============================================================
# history 输出
# ============================================================

def build_history(
    names,
    records,
):

    history = []

    for row in records:
        history.append(
            {
                "日期": row["date"],
                "时间": row["time"],
                "行业指数值": {
                    name: clean_float(
                        value
                    )
                    for name, value
                    in zip(
                        names,
                        row["values"],
                    )
                },
            }
        )

    return history


# ============================================================
# embedded-data 替换
# ============================================================

EMBEDDED_RE = re.compile(
    r"""
    (
      <script\b
      (?=[^>]*\bid\s*=\s*
         (?:"embedded-data"|'embedded-data')
      )
      [^>]*>
    )
    (.*?)
    (</script\s*>)
    """,
    re.IGNORECASE
    | re.DOTALL
    | re.VERBOSE,
)


def replace_embedded_data(
    index_html,
    payload,
):

    matches = list(
        EMBEDDED_RE.finditer(
            index_html
        )
    )

    if len(matches) != 1:
        raise UpdateError(
            "embedded-data必须且只能出现一次"
        )

    json_text = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    ).replace(
        "</",
        r"<\/",
    )

    match = matches[0]

    return (
        index_html[
            :match.start(2)
        ]
        + "\n"
        + json_text
        + "\n"
        + index_html[
            match.end(2):
        ]
    )


# ============================================================
# 原子写文件
# ============================================================

def atomic_write(
    path,
    content,
):

    original_mode = (
        path.stat().st_mode
    )

    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=".index.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:

            temp_file.write(
                content
            )

            temp_file.flush()

            os.fsync(
                temp_file.fileno()
            )

            temp_path = Path(
                temp_file.name
            )

        os.chmod(
            temp_path,
            original_mode,
        )

        os.replace(
            temp_path,
            path,
        )

        temp_path = None

    finally:
        if (
            temp_path
            and temp_path.exists()
        ):
            temp_path.unlink()


# ============================================================
# 主程序
# ============================================================

def main():

    names = load_industry_names()

    if len(names) != 33:
        raise UpdateError(
            "行业名称不是33个"
        )

    current_text = fetch_text(
        CURRENT_JS_URL
    )

    current_data = parse_current_js(
        current_text
    )

    past_text = fetch_text(
        PAST_JS_URL
    )

    history_rows = parse_past_js(
        past_text
    )

    records = merge_history(
        history_rows,
        current_data,
    )

    summary = build_summary(
        names,
        records,
    )

    history = build_history(
        names,
        records,
    )

    payload = {
        "summary": summary,
        "rep": [],
        "act": [],
        "detail": [],
        "history": history,

        "updated_at":
            datetime.now(
                JST
            ).isoformat(
                timespec="seconds"
            ),

        "source": {
            "行业页面":
                GYOUSHU_URLS[0],

            "当前数据":
                CURRENT_JS_URL,

            "历史数据":
                PAST_JS_URL,
        },
    }

    # 最终安全检查
    json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
    )

    index_html = (
        INDEX_PATH.read_text(
            encoding="utf-8"
        )
    )

    new_html = (
        replace_embedded_data(
            index_html,
            payload,
        )
    )

    if new_html == index_html:
        log(
            "数据没有变化，无需写入"
        )
        return

    atomic_write(
        INDEX_PATH,
        new_html,
    )

    log(
        f"更新成功："
        f"日本33行业，"
        f"日期={records[-1]['date']}"
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        print(
            "更新失败，旧 index.html "
            f"保持不变：{exc}",
            file=sys.stderr,
        )

        sys.exit(1)
