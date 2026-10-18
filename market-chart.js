(function(root){
'use strict';
const valid=n=>typeof n==='number'&&Number.isFinite(n),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=(n,d=2)=>valid(n)?n.toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d}):'—';
const pct=n=>valid(n)?(n>0?'+':'')+fmt(n)+'%':'—';
function modeFor(choice,selected){return choice==='auto'?(selected.size===1?'value':'percent'):choice;}
function label(code,p,o){const stamp=new Date(p[0]+o.offset*3600000).toISOString().replace('T',' ').slice(0,16);return `${o.names[code]} ${fmt(p[2],o.decimals(code))} ${o.unit(code)} · ${pct(p[1])} · ${stamp} ${o.timezone}`;}
function render(input,o){
 const actual=o.mode==='value',model=(actual?input.slice(0,1):input).filter(s=>s.points.filter(p=>valid(p[0])&&valid(p[2])&&valid(p[actual?2:1])).length>=2);
 if(!model.length)return '<div class="jm-chart-empty">该日期／范围暂无可核验曲线，请选择其他范围。</div>';
 const column=actual?2:1,all=model.flatMap(s=>s.points).filter(p=>valid(p[0])&&valid(p[2])&&valid(p[column]));
 const minX=Math.min(...all.map(p=>p[0])),maxX=Math.max(...all.map(p=>p[0])),values=all.map(p=>p[column]);
 let minY=Math.min(...values),maxY=Math.max(...values);
 if(actual){const pad=maxY>minY?(maxY-minY)*.08:Math.max(Math.abs(minY)*.0001,2*Math.pow(10,-o.decimals(model[0].code)));minY-=pad;maxY+=pad;}else{minY=Math.min(0,minY);maxY=Math.max(0,maxY);}
 const w=1040,h=300,L=actual?88:54,R=24,T=30,B=34,span=maxY-minY||1,x=t=>L+(t-minX)/(maxX-minX||1)*(w-L-R),y=v=>T+(maxY-v)/span*(h-T-B);
 const axis=actual?o.unit(model[0].code):'涨跌幅',axisValue=v=>actual?fmt(v,o.decimals(model[0].code)):pct(v);
 const grids=Array.from({length:5},(_,i)=>{const v=minY+span*i/4;return `<line x1="${L}" x2="${w-R}" y1="${y(v)}" y2="${y(v)}" stroke="#e5eeea"/><text x="${L-8}" y="${y(v)+4}" text-anchor="end">${axisValue(v)}</text>`;}).join('');
 const paths=model.map(s=>{let drawing=false;const d=s.points.map(p=>{if(!valid(p[0])||!valid(p[2])||!valid(p[column])){drawing=false;return '';}const start=drawing?'L':'M';drawing=true;return `${start}${x(p[0]).toFixed(2)},${y(p[column]).toFixed(2)}`;}).join(' ');return `<path d="${d}" fill="none" stroke="${o.colors[s.code]}" stroke-width="2"/>`;}).join('');
 const labels=Array.from({length:5},(_,i)=>{const t=minX+(maxX-minX)*i/4,stamp=new Date(t+o.offset*3600000).toISOString();return `<text x="${x(t)}" y="${h-10}" text-anchor="middle">${o.range==='1D'?stamp.slice(11,16):stamp.slice(5,10)}</text>`;}).join('');
 const lastValues=model.map(s=>{const p=s.points.filter(p=>valid(p[2])&&valid(p[1])).at(-1);return p?`<div class="jm-value"><span><i style="background:${o.colors[s.code]}"></i>${esc(o.names[s.code])}</span><strong>${fmt(p[2],o.decimals(s.code))}<small>${esc(o.unit(s.code))}</small></strong><span class="jm-value-time">${esc(label(s.code,p,o).split(' · ').slice(1).join(' · '))}</span></div>`:'';}).join('');
 return `<div class="jm-chart-scroll"><svg id="jmChart" viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(o.market)}${actual?'实际数值走势图':'涨跌百分比对比图'}" data-mode="${o.mode}" data-min="${minX}" data-max="${maxX}" data-left="${L}" data-right="${w-R}"><text x="${L}" y="15">${esc(axis)}</text>${grids}${actual?'':`<line x1="${L}" x2="${w-R}" y1="${y(0)}" y2="${y(0)}" stroke="#acbfb7" stroke-dasharray="4 4"/>`}${paths}${labels}<line id="jmCross" y1="${T}" y2="${h-B}" stroke="#8b9e94" hidden/></svg></div><div class="jm-values" aria-label="所选范围末值">${lastValues}</div><div id="jmTooltip" class="jm-readout jm-chart-readout" aria-live="polite">移动鼠标或触碰图表，可查看对应时刻的具体数值和涨跌幅。</div>`;
}
root.MarketChart={render,modeFor,label};if(typeof module!=='undefined')module.exports=root.MarketChart;
})(typeof window==='undefined'?globalThis:window);
