import html
import json
import math
import os
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "index.html"

GYOUSHU_URLS = ("https://nikkei225jp.com/chart/gyoushu.php",)
# 根据已观察到的文件名/同目录推定；
# 如果 GitHub Actions 返回 404，再回浏览器 Headers 确认这个地址。
CURRENT_JS_URL = (
    "https://nikkei225jp.com/_data/_hsDATA/min/country_jp_gyo.js"
)

PAST_JS_URL = (
    "https://nikkei225jp.com/_data/_hsDATA/min/country_jp_gyo_past.js"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
    ),
    "Accept": "text/html,application/javascript,text/javascript,*/*;q=0.8",
    "Referer": "https://nikkei225jp.com/",
    "Cache-Control": "no-cache",
}


class UpdateError(RuntimeError):
    pass


def decode_body(raw, declared_charset=None):
    encodings = []

    if declared_charset and declared_charset.lower() not in {
        "iso-8859-1",
        "latin-1",
    }:
        encodings.append(declared_charset)

    encodings.extend(("utf-8", "cp932", "shift_jis", "euc_jp"))

    tried = set()

    for encoding in encodings:
        key = encoding.lower()
        if key in tried:
            continue

        tried.add(key)

        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            pass

    raise UpdateError("无法识别服务器响应的字符编码")


def fetch_text(url, retries=3):
    last_error = None

    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=HEADERS)

            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status != 200:
                    raise UpdateError(
                        f"{url} 返回 HTTP {response.status}"
                    )

                raw = response.read()

                if not raw:
                    raise UpdateError(f"{url} 返回空内容")

                charset = response.headers.get_content_charset()
                return decode_body(raw, charset)

        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            OSError,
            UpdateError,
        ) as exc:
            last_error = exc

            if attempt + 1 < retries:
                time.sleep(attempt + 1)

    raise UpdateError(f"抓取失败：{url}；{last_error}")


def js_unescape(value):
    def replace_unicode(match):
        return chr(int(match.group(1), 16))

    value = re.sub(
        r"\\u([0-9a-fA-F]{4})",
        replace_unicode,
        value,
    )

    return (
        value.replace(r"\/", "/")
        .replace(r"\"", '"')
        .replace(r"\'", "'")
        .replace(r"\\", "\\")
    )


def parse_industry_names(text):
    pattern = re.compile(
        r"""
        \bGyo\s*\[\s*(\d{1,2})\s*\]\s*=\s*
        (?P<quote>["'])
        (?P<value>.*?)
        (?P=quote)\s*;
        """,
        re.VERBOSE | re.DOTALL,
    )

    matches = list(pattern.finditer(text))

    if len(matches) != 33:
        raise UpdateError(
            f"gyoushu.php 中应有 33 条 Gyo 定义，实际为 {len(matches)} 条"
        )

    names_by_index = {}

    for match in matches:
        index = int(match.group(1))
        name = html.unescape(
            js_unescape(match.group("value"))
        ).strip()

        if not name:
            raise UpdateError(f"Gyo[{index}] 的行业名称为空")

        if index in names_by_index:
            raise UpdateError(f"Gyo[{index}] 重复定义")

        names_by_index[index] = name

    if set(names_by_index) != set(range(33)):
        raise UpdateError("Gyo 索引必须连续为 0-32")

    return [names_by_index[i] for i in range(33)]


def load_industry_names():
    errors = []

    for url in GYOUSHU_URLS:
        try:
            return parse_industry_names(fetch_text(url))
        except Exception as exc:
            errors.append(f"{url}: {exc}")

    raise UpdateError(
        "无法从 gyoushu.php 页面取得行业名称：\n"
        + "\n".join(errors)
    )


def extract_js_assignment(text, variable):
    pattern = re.compile(
        rf"""
        \b{re.escape(variable)}\s*=\s*
        (?P<quote>["'])
        (?P<value>.*?)
        (?P=quote)\s*;
        """,
        re.VERBOSE | re.DOTALL,
    )

    matches = list(pattern.finditer(text))

    if len(matches) != 1:
        raise UpdateError(
            f"{variable} 应出现一次，实际为 {len(matches)} 次"
        )

    return js_unescape(
        matches[0].group("value")
    ).strip()


def parse_number_list(value, label):
    parts = [x.strip() for x in value.split(",")]

    if len(parts) != 33:
        raise UpdateError(
            f"{label} 应包含 33 个数值，实际为 {len(parts)} 个"
        )

    numbers = []

    for i, part in enumerate(parts):
        if not part:
            raise UpdateError(
                f"{label} 第 {i + 1} 个数值为空"
            )

        try:
            number = float(part)
        except ValueError as exc:
            raise UpdateError(
                f"{label} 第 {i + 1} 个值不是数字：{part}"
            ) from exc

        if not math.isfinite(number):
            raise UpdateError(
                f"{label} 第 {i + 1} 个值不是有限数字"
            )

        numbers.append(number)

    return numbers


def parse_current_js(text):
    mod_date = extract_js_assignment(text, "ModDate")
    mod_time = extract_js_assignment(text, "ModTime")

    current_values = parse_number_list(
        extract_js_assignment(text, "G1"),
        "G1",
    )

    changes = parse_number_list(
        extract_js_assignment(text, "G2"),
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

    return (
        parsed_date.strftime("%Y-%m-%d"),
        mod_time,
        current_values,
        changes,
    )


def parse_past_js(text):
    pattern = re.compile(
        r"""
        \bGY\s*\[\s*[^\]]+\s*\]\s*=\s*
        (?P<quote>["'])
        (?P<value>.*?)
        (?P=quote)\s*;
        """,
        re.VERBOSE | re.DOTALL,
    )

    matches = list(pattern.finditer(text))

    if not matches:
        raise UpdateError(
            "past.js 中没有找到 GY[...] 历史记录"
        )

    rows = []

    for row_number, match in enumerate(matches, start=1):
        raw = js_unescape(match.group("value"))
        parts = [x.strip() for x in raw.split(",")]

        if len(parts) != 35:
            raise UpdateError(
                f"past.js 第 {row_number} 条应为日期、时间和33个数值"
            )

        date_text = parts[0]
        time_text = parts[1]

        datetime.strptime(
            f"{date_text} {time_text}",
            "%Y/%m/%d %H:%M",
        )

        values = parse_number_list(
            ",".join(parts[2:]),
            f"past.js 第 {row_number} 条",
        )

        rows.append(
            {
                "日期": date_text.replace("/", "-"),
                "时间": time_text,
                "values": values,
            }
        )

    if len(rows) < 5:
        raise UpdateError("历史记录少于 5 条")

    return rows


def build_data(names, current_data, history_rows):
    date_text, time_text, current_values, changes = current_data

    summary = []

    for index, name in enumerate(names):
        current = current_values[index]
        change = changes[index]

        previous = current - change

        if previous <= 0:
            raise UpdateError(
                f"{name} 无法计算涨跌幅"
            )

        pct = change / previous * 100

        if not math.isfinite(pct):
            raise UpdateError(
                f"{name} 涨跌幅计算异常"
            )

        summary.append(
            {
                "日期": date_text,
                "时间": time_text,
                "市场": "日本",
                "分类体系": "東証33業種",
                "一级行业": name,
                "行业指数名称": name,
                "行业指数值": current,
                "行业指数涨跌幅(%)": round(pct, 4),
                "数据状态": "来源：nikkei225jp.com",
            }
        )

    history = []

    for row in history_rows[-65:]:
        history.append(
            {
                "日期": row["日期"],
                "时间": row["时间"],
                "行业指数值": {
                    name: value
                    for name, value in zip(
                        names,
                        row["values"],
                    )
                },
            }
        )

    return {
        "summary": summary,
        "rep": [],
        "act": [],
        "detail": [],
        "history": history,
    }


def replace_embedded_data_atomically(index_path, data):
    old_text = index_path.read_text(encoding="utf-8")

    pattern = re.compile(
        r"""
        (
          <script\b
          (?=[^>]*\bid\s*=\s*
             (?:"embedded-data"|'embedded-data'|embedded-data\b)
          )
          [^>]*>
        )
        (.*?)
        (</script\s*>)
        """,
        re.IGNORECASE | re.DOTALL | re.VERBOSE,
    )

    matches = list(pattern.finditer(old_text))

    if len(matches) != 1:
        raise UpdateError(
            f"embedded-data 应出现一次，实际为 {len(matches)} 次"
        )

    payload = json.dumps(
        data,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
    ).replace("</", r"<\/")

    match = matches[0]

    new_text = (
        old_text[:match.start(2)]
        + "\n"
        + payload
        + "\n"
        + old_text[match.end(2):]
    )

    mode = stat.S_IMODE(
        index_path.stat().st_mode
    )

    temp_name = None

    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=".index.",
            suffix=".tmp",
            dir=str(index_path.parent),
        )

        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
            f.flush()
            os.fsync(f.fileno())

        os.chmod(temp_name, mode)
        os.replace(temp_name, index_path)
        temp_name = None

    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def main():
    names = load_industry_names()

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

    data = build_data(
        names,
        current_data,
        history_rows,
    )

    if len(data["summary"]) != 33:
        raise UpdateError(
            "最终 summary 不是 33 条"
        )

    replace_embedded_data_atomically(
        INDEX_PATH,
        data,
    )

    print(
        f"更新成功：33 个日本行业，日期={current_data[0]}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            f"更新失败，旧 index.html 未被替换：{exc}",
            file=sys.stderr,
        )
        sys.exit(1)
