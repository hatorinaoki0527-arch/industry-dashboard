/* Daily candles: real OHLC only. Range is calendar years, never candle aggregation. */
(function(root){
'use strict';
const valid=n=>typeof n==='number'&&Number.isFinite(n),esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=n=>valid(n)?n.toLocaleString('zh-CN',{maximumFractionDigits:2}):'—';
function startDate(cutoff,years){const [y,m,d]=cutoff.split('-').map(Number),year=y-years,last=new Date(Date.UTC(year,m,0)).getUTCDate();return `${year}-${String(m).padStart(2,'0')}-${String(Math.min(d,last)).padStart(2,'0')}`;}
function isBar(r){return ['open','high','low','close'].every(k=>valid(r[k])&&r[k]>0)&&r.low<=Math.min(r.open,r.close)&&Math.max(r.open,r.close)<=r.high;}
function windowData(points,cutoff,years){
 const start=startDate(cutoff,years),seen=new Set();
 const all=[...points].filter(r=>/^\d{4}-\d{2}-\d{2}$/.test(r.date)&&r.date<=cutoff).sort((a,b)=>a.date.localeCompare(b.date));
 for(const r of all){if(seen.has(r.date))throw Error('重复日K日期');seen.add(r.date);}
 return {start,all,rows:all.filter(r=>r.date>=start)};
}
function render(points,{cutoff,years=1,label='日K线',selected=new Set([5,20,60])}={}){
 years=[1,2,3,4,5].includes(years)?years:1;
 const {start,all,rows}=windowData(points,cutoff,years),good=rows.filter(isBar),periods=[5,10,20,60,90],colors=['#b17816','#765ab4','#228aaa','#c5753c','#a0497c'];
 const controls=`<div class="panel-head"><div><h3>${esc(label)}</h3><p class="subtitle">日K · 每根蜡烛为一个交易日</p></div><div class="segments" aria-label="日K历史范围">${[1,2,3,4,5].map(n=>`<button data-k-years="${n}" aria-pressed="${years===n}" class="${years===n?'active':''}">近${n}年</button>`).join('')}</div></div>`;
 const note=`<p class="chart-note">请求区间 ${start} — ${cutoff}。${good.length?`实际四价覆盖 ${good[0].date} — ${good.at(-1).date}，${good.length}根日K。`:''}${rows.length-good.length?` ${rows.length-good.length}个归档日缺少完整四价，留空。`:''}${!all.some(r=>r.date<=start&&isBar(r))?' 历史覆盖可能不足所选年数，补齐后自动显示。':''} 不将日线合并成年K；阴阳按收盘与开盘比较。</p>`;
 if(!good.length)return `<section class="panel candle-panel">${controls}<div class="empty">该范围暂无完整开、高、低、收数据，暂不能绘制日K线。</div>${note}</section>`;
 const averages=periods.map(n=>all.map((r,i)=>i<n-1||!all.slice(i-n+1,i+1).every(isBar)?null:all.slice(i-n+1,i+1).reduce((s,p)=>s+p.close,0)/n));
 const offset=all.length-rows.length,curves=averages.map(a=>a.slice(offset));
 const values=[...good.flatMap(r=>[r.high,r.low]),...curves.flat().filter(valid)];
 const W=Math.max(900,rows.length*6+95),H=330,L=72,R=20,T=15,B=38;
 let lo=Math.min(...values),hi=Math.max(...values);const pad=Math.max((hi-lo)*.06,hi*.001,.01);lo-=pad;hi+=pad;
 const step=(W-L-R)/Math.max(1,rows.length),x=i=>L+step*(i+.5),y=v=>T+(hi-v)/(hi-lo)*(H-T-B),width=Math.min(8,step*.65);
 const bars=rows.map((r,i)=>{if(!isBar(r))return '';const cls=r.close>r.open?'up':r.close<r.open?'down':'flat';return `<g class="${cls}" data-candle="${r.date}"><title>${r.date} 开 ${fmt(r.open)} 高 ${fmt(r.high)} 低 ${fmt(r.low)} 收 ${fmt(r.close)}</title><line x1="${x(i)}" x2="${x(i)}" y1="${y(r.high)}" y2="${y(r.low)}" stroke="currentColor"/><rect x="${x(i)-width/2}" y="${Math.min(y(r.open),y(r.close))}" width="${width}" height="${Math.max(1,Math.abs(y(r.open)-y(r.close)))}" fill="currentColor"/></g>`;}).join('');
 const path=arr=>{let pen=false;return arr.map((v,i)=>{if(!valid(v)){pen=false;return '';}const s=(pen?'L':'M')+x(i).toFixed(1)+' '+y(v).toFixed(1);pen=true;return s;}).join(' ');};
 return `<section class="panel candle-panel" data-ma-kind="price">${controls}<div class="ma-toggles">${periods.map((n,i)=>`<label style="color:${colors[i]}"><input type="checkbox" data-ma-period="${n}" ${selected.has(n)?'checked':''}><span>MA${n}<b>${valid(averages[i].at(-1))?fmt(averages[i].at(-1)):'样本不足'}</b></span></label>`).join('')}</div><div class="candle-scroll" tabindex="0" aria-label="日K图；左右滚动查看完整区间"><svg style="width:${W}px" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}，近${years}年日K线">${Array.from({length:5},(_,i)=>{const v=lo+(hi-lo)*i/4;return `<line x1="${L}" x2="${W-R}" y1="${y(v)}" y2="${y(v)}" stroke="#e0e7e1"/><text x="${L-8}" y="${y(v)+4}" text-anchor="end">${fmt(v)}</text>`;}).join('')}${bars}${curves.map((a,i)=>`<path data-ma-line="${periods[i]}" style="display:${selected.has(periods[i])?'':'none'}" d="${path(a)}" fill="none" stroke="${colors[i]}" stroke-width="1.4"/>`).join('')}${rows.map((r,i)=>i===0||i===rows.length-1||i%Math.max(40,Math.round(rows.length/10))===0?`<text x="${x(i)}" y="${H-10}" text-anchor="${i===0?'start':i===rows.length-1?'end':'middle'}">${r.date}</text>`:'').join('')}</svg></div><p class="chart-note">左右滚动查看日期；鼠标停在蜡烛上查看四价，手机可展开下方明细。均线含当日，使用区间之前的已归档数据计算；缺失四价的窗口不画均线。</p>${note}<details><summary>展开日K四价明细</summary><div class="data-scroll"><table><thead><tr><th>日期</th><th>开盘</th><th>最高</th><th>最低</th><th>收盘</th></tr></thead><tbody>${[...rows].reverse().map(r=>`<tr><td>${r.date}</td>${['open','high','low','close'].map(k=>`<td>${fmt(r[k])}</td>`).join('')}</tr>`).join('')}</tbody></table></div></details></section>`;
}
const api={startDate,isBar,windowData,render};if(typeof module==='object'&&module.exports)module.exports=api;else root.CandleCharts=api;
})(globalThis);
