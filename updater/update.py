import json
import math
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "index.html"

CURRENT_JS_URL = "https://nikkei225jp.com/_data/_nfsDATA/min/country_jp_gyo.js"
# 目前先不强依赖 past.js，先用 index.html 里累积的 history 作为 5日/20日基础
# PAST_JS_URL = "https://nikkei225jp.com/_data/_nfsDATA/min/country_jp_gyo_past.js"

SOURCE_TEXT = "来源: nikkei225jp.com"
CLASSIFICATION = "东证33"
MARKET_NAME = "日本"

MIN_HISTORY_DAYS = 21
HISTORY_LIMIT = 180

INDUSTRY_LABELS = {
    "水産・農林業": "水产与农林",
    "鉱業": "矿业",
    "建設業": "建筑",
    "食料品": "食品",
    "繊維製品": "纺织制品",
    "パルプ・紙": "纸浆与造纸",
    "化学": "化学",
    "医薬品": "医药",
    "石油・石炭製品": "石油与煤炭制品",
    "ゴム製品": "橡胶制品",
    "ガラス・土石製品": "玻璃与土石制品",
    "鉄鋼": "钢铁",
    "非鉄金属": "有色金属",
    "金属製品": "金属制品",
    "機械": "机械",
    "電気機器": "电气设备",
    "輸送用機器": "运输设备",
    "精密機器": "精密设备",
    "その他製品": "其他制品",
    "電気・ガス業": "电力与燃气",
    "陸運業": "陆运",
    "海運業": "海运",
    "空運業": "空运",
    "倉庫・運輸関連業": "仓储与运输配套",
    "情報・通信業": "信息与通信",
    "卸売業": "批发贸易",
    "小売業": "零售",
    "銀行業": "银行",
    "証券、商品先物取引業": "证券与商品期货",
    "保険業": "保险",
    "その他金融業": "其他金融",
    "不動産業": "房地产",
    "サービス業": "服务",
}


class UpdateError(Exception):
    pass


def fetch_text(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/127.0 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def extract_embedded_data(index_text: str) -> dict:
    m = re.search(
        r'<script[^>]+id="embedded-data"[^>]*>(.*?)</script>',
        index_text,
        re.S | re.I,
    )
    if not m:
        return {}
    raw = m.group(1).strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def replace_embedded_data(index_text: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    pattern = re.compile(
        r'(<script[^>]+id="embedded-data"[^>]*>)(.*?)(</script>)',
        re.S | re.I,
    )
    if not pattern.search(index_text):
        raise UpdateError("index.html 中未找到 id='embedded-data' 的 script 标签")
    return pattern.sub(r"\1\n" + payload + r"\n  \3", index_text, count=1)


def parse_js_indexed_arrays(js_text: str) -> dict:
    """
    解析形如：
      Gyo[0]="水産・農林業";
      G1[0]="730.09";
      G2[0]="-13.11";
    的 indexed array。
    """
    arrays = {}
    pattern = re.compile(
        r'([A-Za-z_]\w*)\[(\d+)\]\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([-+]?\d+(?:\.\d+)?))\s*;',
        re.S,
    )

    for m in pattern.finditer(js_text):
        name = m.group(1)
        idx = int(m.group(2))
        val = m.group(3)
        if val is None:
            val = m.group(4)
        if val is None:
            val = m.group(5)
        arrays.setdefault(name, {})
        arrays[name][idx] = val

    return {
        k: [v[i] for i in sorted(v.keys())]
        for k, v in arrays.items()
    }


def find_first_date(js_text: str) -> str:
    m = re.search(r'(20\d{2})[/-](\d{1,2})[/-](\d{1,2})', js_text)
    if not m:
        return datetime.now().strftime("%Y-%m-%d")
    y, mm, dd = m.groups()
    return f"{int(y):04d}-{int(mm):02d}-{int(dd):02d}"


def find_first_time(js_text: str) -> str:
    m = re.search(r'(\d{1,2}:\d{2})', js_text)
    if not m:
        return datetime.now().strftime("%H:%M")
    hh, mm = m.group(1).split(":")
    return f"{int(hh):02d}:{mm}"


def to_float_list(values):
    out = []
    for v in values:
        try:
            out.append(float(str(v).replace(",", "").strip()))
        except Exception:
            out.append(float("nan"))
    return out


def choose_array(arrays: dict, candidates: list[str], expect_min_len: int = 30):
    for key in candidates:
        if key in arrays and len(arrays[key]) >= expect_min_len:
            return arrays[key]
    return None


def load_current_rows():
    js_text = fetch_text(CURRENT_JS_URL)

    arrays = parse_js_indexed_arrays(js_text)

    names = choose_array(arrays, ["Gyo", "gyo", "GYO"])
    current_values = choose_array(arrays, ["G1", "g1", "G_1"])
    change_points = choose_array(arrays, ["G2", "g2", "G_2"])

    if not names:
        raise UpdateError("抓取失败：未找到行业名称数组 Gyo")
    if not current_values:
        raise UpdateError("抓取失败：未找到行业当前值数组 G1")
    if not change_points:
        raise UpdateError("抓取失败：未找到行业点数涨跌数组 G2")

    if len(names) < 33 or len(current_values) < 33 or len(change_points) < 33:
        raise UpdateError(
            f"抓取失败：数据长度不足，names={len(names)} current={len(current_values)} change={len(change_points)}"
        )

    names = names[:33]
    current_values = to_float_list(current_values[:33])
    change_points = to_float_list(change_points[:33])

    date_text = find_first_date(js_text)
    time_text = find_first_time(js_text)

    rows = []
    for index, ja_name in enumerate(names):
        current = current_values[index]
        change = change_points[index]

        if not math.isfinite(current):
            raise UpdateError(f"{ja_name} 当前指数值读取异常")
        if not math.isfinite(change):
            raise UpdateError(f"{ja_name} 点数涨跌读取异常")

        # 关键修正：
        # G2 是“点数涨跌额”，不是涨跌百分比
        previous = current - change
        if previous <= 0 or not math.isfinite(previous):
            pct = None
        else:
            pct = change / previous * 100

        rows.append(
            {
                "日期": date_text,
                "时间": time_text,
                "市场": MARKET_NAME,
                "分类体系": CLASSIFICATION,
                "一级行业": ja_name,
                "行业中文名": INDUSTRY_LABELS.get(ja_name, ja_name),
                "行业指数名称": ja_name,
                "行业指数值": round(current, 2),
                "行业指数点数涨跌": round(change, 2),
                "行业指数涨跌幅(%)": round(pct, 2) if pct is not None and math.isfinite(pct) else None,
                "数据状态": SOURCE_TEXT,
            }
        )

    return rows


def normalize_history(existing_history: dict) -> dict:
    if not isinstance(existing_history, dict):
        return {}
    normalized = {}
    for key, value in existing_history.items():
        if isinstance(value, list):
            cleaned = []
            for item in value:
                if not isinstance(item, dict):
                    continue
                d = item.get("日期") or item.get("date")
                v = item.get("行业指数值") if "行业指数值" in item else item.get("value")
                p = item.get("行业指数涨跌幅(%)") if "行业指数涨跌幅(%)" in item else item.get("pct")
                pts = item.get("行业指数点数涨跌") if "行业指数点数涨跌" in item else item.get("points")
                cleaned.append({
                    "日期": d,
                    "行业指数值": v,
                    "行业指数涨跌幅(%)": p,
                    "行业指数点数涨跌": pts,
                })
            normalized[key] = cleaned
    return normalized


def update_history(existing_history: dict, latest_rows: list[dict]) -> dict:
    history = normalize_history(existing_history)
    for row in latest_rows:
        key = row["一级行业"]
        arr = history.get(key, [])

        # 同一天先删再加，避免重复
        arr = [x for x in arr if x.get("日期") != row["日期"]]

        arr.append(
            {
                "日期": row["日期"],
                "行业指数值": row["行业指数值"],
                "行业指数涨跌幅(%)": row["行业指数涨跌幅(%)"],
                "行业指数点数涨跌": row["行业指数点数涨跌"],
            }
        )

        # 日期升序
        arr.sort(key=lambda x: str(x.get("日期") or ""))

        # 只保留最近 HISTORY_LIMIT 天
        history[key] = arr[-HISTORY_LIMIT:]

    return history


def calc_return(history_list: list[dict], lookback_days: int):
    """
    history_list 已包含“当前日”且按日期升序。
    比如 lookback=5，需要当前值和 5 个交易日前的值 => 索引倒数第6个
    """
    if len(history_list) < lookback_days + 1:
        return None

    current_val = history_list[-1].get("行业指数值")
    base_val = history_list[-(lookback_days + 1)].get("行业指数值")

    try:
        current_val = float(current_val)
        base_val = float(base_val)
    except Exception:
        return None

    if base_val <= 0:
        return None

    ret = (current_val / base_val - 1) * 100
    return round(ret, 2)


def classify_rotation(rs, ret5, ret20):
    r5 = 0 if ret5 is None else ret5
    r20 = 0 if ret20 is None else ret20

    if rs is None:
        return "—", "观望"

    if rs >= 0 and r5 >= 0 and r20 >= 0:
        return "领涨扩散", "资金流入"
    if rs >= 0 and (r5 >= 0 or r20 >= 0):
        return "转强修复", "资金回流"
    if rs < 0 and r5 < 0 and r20 < 0:
        return "弱势下行", "资金流出"
    if rs < 0 and (r5 < 0 or r20 < 0):
        return "轮动整理", "观望分化"
    return "中性震荡", "观望"


def enrich_rows(rows: list[dict], history: dict) -> list[dict]:
    pct_values = [
        row["行业指数涨跌幅(%)"]
        for row in rows
        if row["行业指数涨跌幅(%)"] is not None and math.isfinite(row["行业指数涨跌幅(%)"])
    ]

    market_avg = round(sum(pct_values) / len(pct_values), 2) if pct_values else None

    # 排名
    valid_rows = [r for r in rows if r["行业指数涨跌幅(%)"] is not None]
    desc_sorted = sorted(valid_rows, key=lambda x: x["行业指数涨跌幅(%)"], reverse=True)
    asc_sorted = sorted(valid_rows, key=lambda x: x["行业指数涨跌幅(%)"])

    rank_up_map = {r["一级行业"]: i + 1 for i, r in enumerate(desc_sorted)}
    rank_down_map = {r["一级行业"]: i + 1 for i, r in enumerate(asc_sorted)}

    enriched = []
    for row in rows:
        ja_name = row["一级行业"]
        hist_list = history.get(ja_name, [])

        ret5 = calc_return(hist_list, 5)
        ret20 = calc_return(hist_list, 20)

        pct = row["行业指数涨跌幅(%)"]
        rs = None if (pct is None or market_avg is None) else round(pct - market_avg, 2)

        rotation, flow = classify_rotation(rs, ret5, ret20)

        enriched_row = dict(row)
        enriched_row.update(
            {
                "33行业当日平均涨跌幅(%)": market_avg,
                "当日相对强度(%)": rs,
                "5日收益率(%)": ret5,
                "20日收益率(%)": ret20,
                "轮动状态": rotation,
                "资金偏向": flow,
                "上涨排名": rank_up_map.get(ja_name),
                "下跌排名": rank_down_map.get(ja_name),
            }
        )
        enriched.append(enriched_row)

    return enriched


def main():
    index_text = INDEX_PATH.read_text(encoding="utf-8")
    existing_data = extract_embedded_data(index_text)

    latest_rows = load_current_rows()
    history = update_history(existing_data.get("history", {}), latest_rows)
    summary = enrich_rows(latest_rows, history)

    data = {
        "summary": summary,
        "rep": existing_data.get("rep", []),      # 预留代表股
        "act": existing_data.get("act", []),      # 预留活跃股
        "detail": existing_data.get("detail", []),# 预留行业个股明细
        "history": history,
        "meta": {
            "market": MARKET_NAME,
            "classification": CLASSIFICATION,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "source": CURRENT_JS_URL,
            "notes": [
                "G2 为点数涨跌额，已换算为涨跌幅百分比",
                "5日/20日收益率基于本地 history 累积",
                "个股明细/代表股/活跃股暂未接入真实数据源"
            ],
        },
    }

    new_index_text = replace_embedded_data(index_text, data)
    INDEX_PATH.write_text(new_index_text, encoding="utf-8")
    print(f"更新成功：写入 {len(summary)} 条日本行业数据到 index.html")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        print(f"更新失败，旧 index.html 保持不变：抓取失败：{e.url}; HTTP Error {e.code}: {e.reason}")
        raise
    except Exception as e:
        print(f"更新失败，旧 index.html 保持不变：{e}")
        raise
