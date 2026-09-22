// UI draft by Sakana Fugu; data semantics, safety and responsive layout reviewed.
(function (global) {
  "use strict";

  const isNum = v => typeof v === "number" && Number.isFinite(v);
  const esc = v => String(v == null ? "" : v).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
  const isDate = v => {
    if (typeof v !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(v)) return false;
    const d = new Date(v + "T00:00:00Z");
    return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === v;
  };
  const isMonth = v => typeof v === "string" && /^\d{4}-(0[1-9]|1[0-2])$/.test(v);
  const trend = v => isNum(v) && v > 0 ? "up" : isNum(v) && v < 0 ? "down" : "";
  const number = (v, digits = 4) => isNum(v) ? v.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "—";
  const percent = v => {
    if (!isNum(v)) return "—";
    const n = Object.is(v, -0) ? 0 : v;
    return (n > 0 ? "+" : "") + n.toLocaleString("zh-CN", { maximumFractionDigits: 2 }) + "%";
  };
  const price = (v, unit) => isNum(v) ? number(v) + (unit === "CNY" ? " 元" : unit === "JPY" ? " 日元" : unit ? " " + unit : "") : "—";
  const money = (v, unit) => {
    if (!isNum(v)) return "—";
    const a = Math.abs(v), suffix = unit === "CNY" ? ["元", "万元", "亿元"] : unit === "JPY" ? ["日元", "万日元", "亿日元"] : [unit || "", "万" + (unit || ""), "亿" + (unit || "")];
    const level = a >= 1e8 ? 2 : a >= 1e4 ? 1 : 0;
    return number(v / (level === 2 ? 1e8 : level === 1 ? 1e4 : 1), 2) + " " + suffix[level];
  };
  const volume = v => !isNum(v) ? "—" : Math.abs(v) >= 1e8 ? number(v / 1e8, 2) + " 亿股" : Math.abs(v) >= 1e4 ? number(v / 1e4, 2) + " 万股" : number(v, 0) + " 股";
  const metric = (label, value, cls) => `<div class="report-metric"><span class="report-metric-label">${esc(label)}</span><strong class="report-metric-value ${esc(cls || "")}">${esc(value)}</strong></div>`;
  const mondayOf = iso => {
    const d = new Date(iso + "T00:00:00Z"), offset = (d.getUTCDay() + 6) % 7;
    d.setUTCDate(d.getUTCDate() - offset);
    return d.toISOString().slice(0, 10);
  };
  const errorHTML = message => `<div class="report-shell"><section class="report-panel report-empty report-error"><strong>报告暂时不可用</strong><p>${esc(message)}</p></section></div>`;

  function render(input) {
    const d = input && typeof input === "object" ? input : {};
    if (!isDate(d.asOf)) return errorHTML("数据截止日期无效，请刷新重试。");

    const raw = Array.isArray(d.rows) ? d.rows : [];
    const seen = new Set();
    for (const row of raw) {
      if (!row || !isDate(row.date)) return errorHTML("发现无效记录日期：" + (row && row.date != null ? row.date : "空值"));
      if (seen.has(row.date)) return errorHTML("发现重复日期记录：" + row.date);
      seen.add(row.date);
    }

    const rows = raw.filter(r => r.date <= d.asOf).slice().sort((a, b) => a.date.localeCompare(b.date));
    const breadth = d.kind === "breadth";
    const unit = d.unit == null ? "" : String(d.unit);
    const latest = rows.length ? rows[rows.length - 1] : null;
    const requestedMonth = isMonth(d.month) && d.month <= d.asOf.slice(0, 7) ? d.month :
      isDate(d.selectedDate) && d.selectedDate <= d.asOf ? d.selectedDate.slice(0, 7) :
      latest ? latest.date.slice(0, 7) : d.asOf.slice(0, 7);
    const months = Array.from(new Set(rows.map(r => r.date.slice(0, 7)).concat(requestedMonth))).sort().reverse();
    const monthRows = rows.filter(r => r.date.slice(0, 7) === requestedMonth);
    const selected = isDate(d.selectedDate) && d.selectedDate.slice(0, 7) === requestedMonth ?
      monthRows.find(r => r.date === d.selectedDate) || monthRows[monthRows.length - 1] :
      monthRows[monthRows.length - 1];

    const heroRow = selected || latest;
    const heroValue = breadth ? percent(heroRow && heroRow.change) : price(heroRow && heroRow.close, unit);
    const heroChange = breadth ? "" : percent(heroRow && heroRow.change);
    const heroClass = trend(heroRow && heroRow.change);
    const identity = [d.code, d.market].filter(v => v != null && String(v) !== "").map(esc).join(" · ") || "—";
    const latestText = latest ? latest.date : "暂无数据";

    const options = months.map(m => `<option value="${esc(m)}"${m === requestedMonth ? " selected" : ""}>${esc(m)}</option>`).join("");
    const dayButtons = monthRows.length ? monthRows.map(r =>
      `<button type="button" class="report-day-button ${trend(r.change)}${selected && selected.date === r.date ? " report-active" : ""}" data-report-date="${esc(r.date)}" aria-pressed="${selected && selected.date === r.date ? "true" : "false"}"><span>${esc(r.date.slice(8, 10))}日</span><small>${esc(percent(r.change))}</small></button>`
    ).join("") : `<div class="report-empty report-nav-empty">本月暂无已归档记录</div>`;

    let detailHTML;
    if (!selected) {
      detailHTML = `<article class="report-panel report-detail"><div class="report-section-head"><div><span class="report-eyebrow">当日明细</span><h2>暂无可显示记录</h2></div></div><p class="report-subtle">仅在存在归档数据时显示字段。</p></article>`;
    } else {
      let fields = [], missing = [];
      if (breadth) {
        fields = [
          metric("行业等权涨跌", percent(selected.change), trend(selected.change)),
          metric("已采集成交额", money(selected.amount, d.currency || unit))
        ];
        if(isNum(selected.up))fields.push(metric("上涨家数",number(selected.up,0),"up"));
        if(isNum(selected.down))fields.push(metric("下跌家数",number(selected.down,0),"down"));
        if(isNum(selected.valid)&&isNum(selected.count))fields.push(metric("有效样本",selected.valid+" / "+selected.count));
        if (!isNum(selected.change)) missing.push("行业等权涨跌");
        if (!isNum(selected.amount)) missing.push("成交额");
      } else {
        const amplitude = isNum(selected.high) && isNum(selected.low) && isNum(selected.previousClose) && selected.previousClose !== 0 ? (selected.high - selected.low) / selected.previousClose * 100 : null;
        fields = [
          metric("开盘", price(selected.open, unit)), metric("最高", price(selected.high, unit)),
          metric("最低", price(selected.low, unit)), metric("收盘", price(selected.close, unit)),
          metric("涨跌", percent(selected.change), trend(selected.change)), metric("涨跌额", isNum(selected.close)&&isNum(selected.previousClose)?price(selected.close-selected.previousClose,unit):"—",trend(selected.change)), metric("成交量", volume(selected.volume)),
          metric("已采集成交额", money(selected.amount, d.currency || unit)), metric("换手率", isNum(selected.turnover)?number(selected.turnover,2)+"%":"—"),
          metric("振幅", isNum(amplitude)?number(amplitude,2)+"%":"—")
        ];
        [["开盘", selected.open], ["最高", selected.high], ["最低", selected.low], ["收盘", selected.close], ["涨跌", selected.change], ["成交量", selected.volume], ["成交额", selected.amount], ["换手率", selected.turnover]].forEach(x => { if (!isNum(x[1])) missing.push(x[0]); });
        if (!isNum(amplitude)) missing.push("振幅（需最高、最低及昨收）");
      }
      detailHTML = `<article class="report-panel report-detail"><div class="report-section-head"><div><span class="report-eyebrow">当日明细</span><h2>${esc(selected.date)}</h2></div><span class="report-date-chip">周${["日","一","二","三","四","五","六"][new Date(selected.date+"T00:00:00Z").getUTCDay()]}</span></div><div class="report-metrics">${fields.join("")}</div><p class="report-missing">${missing.length ? "缺失字段：" + esc(missing.join("、")) + "；空缺不估算。" : "本日所列字段均有值。"}</p></article>`;
    }

    const summaryRows = selected ? monthRows.filter(r=>r.date<=selected.date) : monthRows;
    const upDays = summaryRows.filter(r => isNum(r.change) && r.change > 0).length;
    const downDays = summaryRows.filter(r => isNum(r.change) && r.change < 0).length;
    const validAmounts = summaryRows.filter(r => isNum(r.amount));
    const totalAmount = validAmounts.reduce((s, r) => s + r.amount, 0);
    const amountSummary = !summaryRows.length ? "—" : validAmounts.length === summaryRows.length ? money(totalAmount, d.currency || unit) : `部分金额${validAmounts.length}/${summaryRows.length}日 · ${validAmounts.length ? money(totalAmount, d.currency || unit) : "—"}`;
    const highs = summaryRows.filter(r => isNum(r.high)).map(r => r.high), lows = summaryRows.filter(r => isNum(r.low)).map(r => r.low);
    const summaryMetrics = [
      metric("观察天数", summaryRows.length + " 日"), metric("上涨天数", upDays + " 日", "up"),
      metric("下跌天数", downDays + " 日", "down")
    ];
    if (!breadth) {
      summaryMetrics.push(metric("已知最高", highs.length ? price(Math.max(...highs), unit) : "—"));
      summaryMetrics.push(metric("已知最低", lows.length ? price(Math.min(...lows), unit) : "—"));
    }
    summaryMetrics.push(metric("累计已采集成交额", amountSummary));

    const weekMap = new Map();
    rows.filter(r=>!selected||r.date<=selected.date).forEach(r => {
      const key = mondayOf(r.date);
      if (!weekMap.has(key)) weekMap.set(key, []);
      weekMap.get(key).push(r);
    });
    const weeks = Array.from(weekMap.entries()).filter(x => x[0].slice(0, 7) === requestedMonth);
    const weekHTML = weeks.length ? weeks.map(([key, wr]) => {
      const first = wr[0], last = wr[wr.length - 1], va = wr.filter(r => isNum(r.amount));
      const u = wr.filter(r => isNum(r.change) && r.change > 0).length, dn = wr.filter(r => isNum(r.change) && r.change < 0).length;
      const canPriceChange = !breadth && isNum(first.open) && first.open !== 0 && isNum(last.close);
      const intervalChange = canPriceChange ? (last.close / first.open - 1) * 100 : null;
      return `<details class="report-week"><summary class="report-week-summary"><span><strong>${esc(key)} 当周</strong><small>${esc(first.date)} 至 ${esc(last.date)} · ${wr.length}日</small></span><span class="${trend(intervalChange)}">${breadth ? `${u}涨 / ${dn}跌` : "开收 " + esc(percent(intervalChange))}</span></summary><div class="report-week-body">${metric("实际覆盖日期", `${first.date} 至 ${last.date}`)}${metric("成交额有效天数", `${va.length}/${wr.length} 日`)}${metric("已采集成交额", va.length ? money(va.reduce((s, r) => s + r.amount, 0), d.currency || unit) : "—")}${metric("上涨 / 下跌天数", `${u} / ${dn} 日`)}${breadth ? "" : metric("区间开收变化（未复权）", percent(intervalChange), trend(intervalChange))}<p class="report-missing">${breadth ? "行业等权涨跌不计算价格变化。" : "按本周已归档区间的首日开盘至末日收盘计算；不是相对上周收盘的周收益。"}</p></div></details>`;
    }).join("") : `<div class="report-panel report-empty">本月暂无归属于周一键的周记录。</div>`;

    const currencyUnit = d.currency || unit;
    const currency = currencyUnit === "CNY" ? "CNY（元）" : currencyUnit === "JPY" ? "JPY（日元）" : currencyUnit || "—";
    const range = rows.length ? `${rows[0].date} 至 ${rows[rows.length - 1].date}` : "—";

    return `<div class="report-shell">
      <section class="report-hero">
        <div class="report-hero-left"><span class="report-eyebrow">${breadth ? "行业观察" : d.kind === "index" ? "指数观察" : "股票观察"}</span><h2>${esc(d.name || "—")}</h2><p>${identity}</p><p>${esc(d.kind === "stock" ? "所属行业（按最新归档）" : "分类")}：${esc(d.sectorName || "—")}</p><p>观测日：${esc(heroRow ? heroRow.date : d.asOf)} · 最新归档：${esc(latestText)}</p></div>
        <div class="report-hero-right"><span>${breadth ? "所选日行业等权涨跌" : "所选日收盘"}</span><strong class="report-hero-number ${heroClass}">${esc(heroValue)}</strong>${breadth ? "" : `<b class="${heroClass}">${esc(heroChange)}</b>`}</div>
      </section>
      <div class="report-layout">
        <div class="report-panel report-nav"><label class="report-month-label" for="reportMonth">归档月份</label><select class="report-month-select" id="reportMonth">${options}</select><div class="report-days">${dayButtons}</div></div>
        <div class="report-main">${detailHTML}
          <section class="report-panel report-summary"><div class="report-section-head"><div><span class="report-eyebrow">当月已归档摘要</span><h2>${esc(requestedMonth)}</h2></div></div><div class="report-metrics">${summaryMetrics.join("")}</div><p class="report-subtle">截至 ${esc(selected ? selected.date : d.asOf)} 的已归档记录，不代表完整自然月。</p></section>
          <section class="report-weeks"><div class="report-section-head"><div><span class="report-eyebrow">周回顾</span><h2>按周一日期归属</h2></div></div>${weekHTML}<p class="report-subtle">跨月周统一归入周一所在月份。</p></section>
        </div>
      </div>
      <div class="report-note"><span>来源：${esc(d.source || "—")}</span><span>币种：${esc(currency)}</span><span>日期范围：${esc(range)}</span><span>仅显示截至 ${esc(d.asOf)} 已有数据；不补齐不存在日期，空缺成交量（含不提供成交量的指数）不估算。</span></div>
    </div>`;
  }

  global.DailyReport = { render };
})(window);
