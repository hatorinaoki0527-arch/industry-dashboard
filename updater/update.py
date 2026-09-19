import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

# TODO: 确认 nikkei225jp.com 的真实公开 JSON/XHR 地址后再填写。
PUBLIC_JSON_URL = ""

ROOT = Path(__file__).resolve().parents[1]
INDEX_FILE = ROOT / "index.html"

EMBEDDED_RE = re.compile(
    r'<script\b(?=[^>]*\bid\s*=\s*["\']embedded-data["\'])[^>]*>'
    r'(.*?)</script\s*>',
    re.IGNORECASE | re.DOTALL,
)

def validate_data(data):
    if not isinstance(data, (dict, list)) or not data:
        raise ValueError("数据必须是非空的 JSON 对象或数组")

def fetch_json(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("PUBLIC_JSON_URL 必须是有效的 HTTPS 地址")

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 GitHub-Actions-Updater"},
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read(10 * 1024 * 1024 + 1)

    if len(raw) > 10 * 1024 * 1024:
        raise ValueError("返回数据超过 10 MB")

    data = json.loads(raw.decode("utf-8"))
    validate_data(data)
    return data

def replace_embedded_data(html, data):
    matches = list(EMBEDDED_RE.finditer(html))
    if len(matches) != 1:
        raise ValueError(
            'index.html 必须且只能包含一个 id="embedded-data" 区块'
        )

    payload = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")

    block = (
        '<script id="embedded-data" type="application/json">'
        f"{payload}</script>"
    )

    return EMBEDDED_RE.sub(lambda _: block, html, count=1)

def validate_html(html):
    matches = list(EMBEDDED_RE.finditer(html))
    if len(matches) != 1:
        raise ValueError("生成结果中的 embedded-data 区块无效")

    json.loads(matches[0].group(1))

def main():
    if not PUBLIC_JSON_URL.strip():
        raise RuntimeError(
            "PUBLIC_JSON_URL 尚未配置；"
            "为保护 index.html，本次不会写入任何文件"
        )

    if not INDEX_FILE.is_file():
        raise FileNotFoundError(f"找不到 {INDEX_FILE}")

    data = fetch_json(PUBLIC_JSON_URL)

    old_html = INDEX_FILE.read_text(encoding="utf-8")
    new_html = replace_embedded_data(old_html, data)
    validate_html(new_html)

    if new_html == old_html:
        print("数据没有变化")
        return

    temp_file = INDEX_FILE.with_suffix(".html.tmp")

    try:
        temp_file.write_text(new_html, encoding="utf-8")
        os.replace(temp_file, INDEX_FILE)
    finally:
        temp_file.unlink(missing_ok=True)

    print("index.html 更新成功")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
