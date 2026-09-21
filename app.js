(() => {
'use strict';
const $=id=>document.getElementById(id), esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const valid=v=>typeof v==='number'&&Number.isFinite(v), fmt=(v,d=2)=>valid(v)?v.toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d}):'—';
const pct=v=>valid(v)?(v>0?'+':'')+fmt(v)+'%':'—', tone=v=>!valid(v)||v===0?'flat':v>0?'up':'down';
const money=v=>!valid(v)?'—':Math.abs(v)>=1e8?fmt(v/1e8)+'亿':Math.abs(v)>=1e4?fmt(v/1e4)+'万':fmt(v);
const numCode=c=>String(c).endsWith('0')?String(c).slice(0,-1):String(c), meta=n=>INDUSTRY_DEFINITIONS[n]||INDUSTRY_DEFINITIONS[n.replaceAll('･','・')]||{cn:n,group:'其他'};
let DATA=null, dates=[], byDate=new Map(), byStock=new Map(), epoch=0, busy=false;
let state={view:'overview',market:'japan',date:'',base:'',sector:'',stock:'',chart:'close',sort:'amount',sectorSort:'change',search:'',page:1};
const rowObj=r=>Object.fromEntries(DATA.columns.map((c,i)=>[c,r[i]]));
const day=()=>byDate.get(state.date), sector=()=>day()?.sectors.find(s=>s.code===state.sector), title=s=>meta(s.name).cn;
const rowFor=(code,date=state.date)=>byStock.get(code)?.get(date);
const prior=()=>dates[dates.indexOf(state.date)-1], baseDay=()=>byDate.get(state.base||prior());
function accept(data){
 if(data.schemaVersion!==1||!Array.isArray(data.days)||!data.days.length||!Array.isArray(data.columns))throw Error('不支持的数据格式');
 DATA=data; dates=data.days.map(d=>d.date);byDate=new Map();byStock=new Map();
 for(const d of data.days)d.records=d.rows.map(rowObj);
 DATA.days=jpEnrichReturns(data.days);
 for(const d of DATA.days){byDate.set(d.date,d);for(const r of d.records){if(!byStock.has(r.code))byStock.set(r.code,new Map());byStock.get(r.code).set(d.date,r);}}
 state.date=dates.at(-1);$('gate').hidden=true;$('workspace').hidden=false;readRoute();
}
function bytes(s){return Uint8Array.from(atob(s),c=>c.charCodeAt(0));}
async function unlock(e){
 e.preventDefault();if(busy)return;busy=true;const attempt=++epoch;const password=$('password').value;$('password').value='';$('unlock').disabled=true;$('gateMessage').textContent='正在下载并解密数据…';
 try{
  const loaded=await DashboardVault.load({notify:message=>{$('gateMessage').textContent=message;}});
  const v=loaded.vault;$('gateMessage').textContent='正在本机解密并整理数据…';if(v.version!==1||v.cipher!=='AES-256-GCM'||v.kdf!=='PBKDF2-SHA256'||v.iterations!==600000||v.compression!=='gzip')throw Error('加密格式不匹配，请刷新页面。');
  const material=await crypto.subtle.importKey('raw',new TextEncoder().encode(password),'PBKDF2',false,['deriveKey']);
  const key=await crypto.subtle.deriveKey({name:'PBKDF2',salt:bytes(v.salt),iterations:v.iterations,hash:'SHA-256'},material,{name:'AES-GCM',length:256},false,['decrypt']);
  let plain;try{plain=await crypto.subtle.decrypt({name:'AES-GCM',iv:bytes(v.iv)},key,bytes(v.data));}catch{throw Error('密码不正确，或加密文件损坏。请重新输入网站访问密码。');}
  if(!('DecompressionStream' in window))throw Error('请使用新版 Chrome、Safari 或 Edge 打开。');
  const text=await new Response(new Blob([plain]).stream().pipeThrough(new DecompressionStream('gzip'))).text();
  const parsed=JSON.parse(text);if(parsed.schemaVersion!==1||!Array.isArray(parsed.days)||!parsed.days.length||!Array.isArray(parsed.columns))throw Error('不支持的数据格式');
  if(attempt!==epoch)return;const saved=await loaded.persist();if(attempt!==epoch)return;accept(parsed);$('gateMessage').textContent='';
  $('loadStatus').className='load-status'+(loaded.source==='offline'?' warn':'');
  $('loadStatus').textContent=loaded.source==='offline'?'⚠ 未能连接服务器确认更新，当前显示本机缓存。请核对各市场行情日期，联网后重新进入。':loaded.source==='cache'?'已核对服务器版本 · 使用本机加密缓存，无需重复下载行情。':saved?'已载入新版数据并保存加密缓存，下次无更新时无需重下。':'数据已载入；浏览器未允许本机缓存，下次可能需要重新下载。';
 }catch(error){$('gateMessage').textContent=error.message||'暂时无法载入，请重试。';}finally{busy=false;$('unlock').disabled=false;}
}
function lock(){epoch++;DATA=null;dates=[];byDate.clear();byStock.clear();$('content').replaceChildren();$('heading').replaceChildren();$('controls').replaceChildren();$('status').replaceChildren();$('workspace').hidden=true;$('gate').hidden=false;$('password').value='';$('password').focus();}
function route(p){Object.assign(state,p);const q=new URLSearchParams();for(const key of ['view','market','date','base','sector','stock','chart'])if(state[key])q.set(key,state[key]);const h=q.toString();if(location.hash.slice(1)===h)render();else location.hash=h;}
function readRoute(){if(!DATA)return;const q=new URLSearchParams(location.hash.slice(1));for(const key of ['view','market','date','base','sector','stock','chart'])if(q.has(key))state[key]=q.get(key);if(!dates.includes(state.date))state.date=dates.at(-1);if(!dates.includes(state.base)||state.base>=state.date)state.base='';if(!['overview','stocks','sector','stock','help','mapping'].includes(state.view))state.view='overview';if(!['japan','china','usa'].includes(state.market))state.market='japan';if(!['close','change','amount'].includes(state.chart))state.chart='close';render();}
function metric(label,value,note='',cls=''){return `<div class="metric"><label>${label}</label><strong class="${cls}">${value}</strong><small>${note}</small></div>`;}
function toolbar(){return `<div class="toolbar"><label>观测日期<select id="date">${[...dates].reverse().map(d=>`<option ${d===state.date?'selected':''}>${d}</option>`).join('')}</select></label><label>对比日期<select id="base"><option value="">${prior()?'上一交易日 · '+prior():'无更早记录'}</option>${dates.filter(d=>d<state.date).reverse().map(d=>`<option ${d===state.base?'selected':''}>${d}</option>`).join('')}</select></label><button class="button" data-step="-1" ${dates.indexOf(state.date)===0?'disabled':''}>←</button><button class="button" data-step="1" ${state.date===dates.at(-1)?'disabled':''}>→</button><button class="button" data-latest>最新</button><label class="grow">${state.view==='overview'?'行业检索':'股票检索'}<input type="search" id="search" placeholder="${state.view==='overview'?'输入中文或日文行业名称':'股票名称或代码，例如 9984'}" value="${esc(state.search)}"></label></div>`;}
function status(){const u=DATA.update, last=DATA.latestDate;return `<div class="status ${u.state!=='success'?'warn':''}"><b>${u.state==='success'?'● 已接入真实数据':'● 更新异常，保留上次数据'}</b><span>最新行情 ${last} · 日本收盘</span><span>${dates.length}个交易日</span><span>自动取数：北京时间19:17 / 21:17</span><span>数据源 J-Quants</span></div>`;}
function summary(){const ss=day().sectors, rr=day().records, vv=rr.filter(r=>valid(r.change)), amounts=rr.filter(r=>valid(r.amount));return `<div class="summary">${metric('行业覆盖',ss.length+'<small style="display:inline;font-size:16px"> / 33</small>',`${rr.length.toLocaleString()}只普通股记录`)}${metric('上涨 / 下跌个股',`<span class="up">${vv.filter(r=>r.change>0).length}</span><span style="font-size:16px"> / </span><span class="down">${vv.filter(r=>r.change<0).length}</span>`,`涨跌可计算 ${vv.length} · 缺失 ${rr.length-vv.length}`)}${metric('成分股等权表现',pct(vv.length?vv.reduce((s,r)=>s+r.change,0)/vv.length:null),'全市场有效样本；不是 TOPIX',tone(vv.length?vv.reduce((s,r)=>s+r.change,0):null))}${metric('已获取成交额',money(amounts.length?amounts.reduce((s,r)=>s+r.amount,0):null)+'<small style="display:inline"> 日元</small>',`${amounts.length}/${rr.length}只 · 不含缺失项`)}</div>`;}
function comparisons(){const b=baseDay();if(!b||!b.sectors.some(s=>valid(s.rank))||!day().sectors.some(s=>valid(s.rank)))return '<div class="notice">首个归档日没有前收盘，日涨跌留空；仍可查看真实价格和成交额。</div>';const current=day().sectors;const top=current.filter(s=>valid(s.rank)&&s.rank<=10),old=b.sectors.filter(s=>valid(s.rank)&&s.rank<=10);const enters=top.filter(s=>!old.some(x=>x.code===s.code)),leaves=old.filter(s=>!top.some(x=>x.code===s.code));const dr=current.map(s=>({s,old:b.sectors.find(x=>x.code===s.code)})).filter(x=>valid(x.s.rank)&&valid(x.old?.rank)).sort((a,b)=>(b.old.rank-b.s.rank)-(a.old.rank-a.s.rank)).filter(x=>x.old.rank>x.s.rank).slice(0,3);const names=ss=>ss.length?ss.map(s=>esc(title(s))).join('、'):'无';return `<div class="compare-grid"><div class="compare-item"><label>相比 ${b.date} · 新进入行业前10</label>${names(enters)}</div><div class="compare-item"><label>退出行业前10</label>${names(leaves)}</div><div class="compare-item"><label>行业排名提升最多 · 按当日等权涨跌</label>${dr.length?dr.map(x=>esc(title(x.s))+' ↑'+(x.old.rank-x.s.rank)).join('、'):'无可比提升'}</div></div>`;}
function card(s){const prev=baseDay()?.sectors.find(x=>x.code===s.code),diff=valid(s.change)&&valid(prev?.change)?s.change-prev.change:null;return `<button class="sector-card" data-sector="${s.code}"><div class="card-top"><h3>${esc(title(s))}</h3><strong class="${tone(s.change)}">${pct(s.change)}</strong></div><div class="native">${esc(s.name)} · ${s.count}只</div><div class="breadth"><span class="green" style="width:${s.up/s.count*100}%"></span><span class="red" style="width:${s.down/s.count*100}%"></span></div><div class="card-meta"><span>↑ ${s.up}　↓ ${s.down}</span><span>有效 ${s.valid}/${s.count}</span></div><div class="card-bottom"><span>5日 <b class="${tone(s.return5)}">${pct(s.return5)}</b></span><span>10日 <b class="${tone(s.return10)}">${pct(s.return10)}</b></span><span>20日 <b class="${tone(s.return20)}">${pct(s.return20)}</b></span><span>对比差 ${valid(diff)?(diff>0?'+':'')+fmt(diff)+'pp':'—'}</span></div></button>`;}
function jpEnrichReturns(days) {
  const history = [];
  const result = [];

  for (let i = 0; i < days.length; i++) {
    const day = days[i];
    const sourceRecords = Array.isArray(day.records) ? day.records : [];
    const currentMap = new Map();

    for (const record of sourceRecords) {
      currentMap.set(record.code, record);
    }
    history.push(currentMap);

    const records = sourceRecords.map(record => {
      let return10 = null;

      if (i >= 10) {
        const base = history[i - 10].get(record.code);
        let valid = !!base && Number.isFinite(base.close) && base.close > 0;
        let factor = 1;

        for (let d = i - 9; valid && d <= i; d++) {
          const item = history[d].get(record.code);
          if (!item || !Number.isFinite(item.change) || item.change <= -100) {
            valid = false;
          } else {
            factor *= 1 + item.change / 100;
          }
        }

        if (valid) {
          const compounded = (factor - 1) * 100;
          if (Number.isFinite(compounded)) return10 = compounded;
        }
      }

      // 缺少任一交易日样本、有效change或窗口前正close时，return10留空。
      return { ...record, return10 };
    });

    const sectors = (Array.isArray(day.sectors) ? day.sectors : []).map(sector => {
      let sum = 0;
      let valid10 = 0;

      for (const record of records) {
        if (
          record.sector === sector.code &&
          Number.isFinite(record.return10)
        ) {
          sum += record.return10;
          valid10++;
        }
      }

      return {
        ...sector,
        return10: valid10 > 0 ? sum / valid10 : null,
        valid10
      };
    });

    result.push({ ...day, records, sectors });
  }

  return result;
}

function jpRankRows(rows, key) {
  if (!Array.isArray(rows)) return [];

  const valid = rows.filter(row => {
    if (!row || row.code == null) return false;
    if (key === "amount") {
      return Number.isFinite(row.amount) && row.amount > 0;
    }
    if (key === "volume") {
      return Number.isFinite(row.volume) && row.volume > 0;
    }
    if (key === "change") {
      return Number.isFinite(row.change) && row.change > 0;
    }
    if (key === "decline") {
      return Number.isFinite(row.change) && row.change < 0;
    }
    return false;
  });

  return valid.sort((a, b) => {
    const av = key === "decline" ? a.change : a[key];
    const bv = key === "decline" ? b.change : b[key];
    const difference = key === "decline" ? av - bv : bv - av;

    if (difference !== 0) return difference;

    const ac = String(a.code);
    const bc = String(b.code);
    return ac < bc ? -1 : ac > bc ? 1 : 0;
  });
}

const jpPeriods=[['当日','change'],['5日','return5'],['10日','return10'],['20日','return20']];
let jpPeriodKey='change',jpRankKey='amount';
const jpRankLabels={amount:'成交额',volume:'成交量',change:'涨幅',decline:'跌幅'};
function jpPeriodOverview(selected){
 const ss=selected?[selected]:day().sectors;
 const sample=(s,key)=>key==='change'?s.valid:s['valid'+key.slice(6)];
 if(selected)return `<div class="period-strip" aria-label="行业各周期涨跌">${jpPeriods.map(([label,key])=>`<div><span>${label}等权涨跌</span><strong class="${tone(selected[key])}">${pct(selected[key])}</strong><small>有效 ${sample(selected,key)??'—'}/${selected.count}</small></div>`).join('')}<small>各期个股区间收益等权平均 · 非官方行业指数</small></div>`;
 const items=ss.map(s=>({code:s.code,name:title(s),value:s[jpPeriodKey]})),good=items.filter(x=>valid(x.value));
 const up=good.filter(x=>x.value>0).sort((a,b)=>b.value-a.value||a.code.localeCompare(b.code)),down=good.filter(x=>x.value<0).sort((a,b)=>a.value-b.value||a.code.localeCompare(b.code)),flat=good.length-up.length-down.length,max=Math.max(.01,...good.map(x=>Math.abs(x.value)));
 const list=(rs,cls,offset=0)=>rs.map((r,i)=>`<button type="button" class="period-rank-row" data-sector="${esc(r.code)}"><span class="rank-no">${i+offset+1}</span><span class="rank-sector">${esc(r.name)}</span><span class="rank-track"><i class="${cls}" style="width:${Math.abs(r.value)/max*100}%"></i></span><strong class="${cls}">${pct(r.value)}</strong><span aria-hidden="true">›</span></button>`).join('');
 const column=(rs,cls,label)=>`<section class="period-rank-col"><div class="period-col-title"><h3 class="${cls}">${label}</h3><span>${rs.length} 个行业</span></div>${rs.length?list(rs.slice(0,5),cls):'<p class="period-empty">该周期暂无'+label+'，或样本不足</p>'}${rs.length>5?`<details class="period-more"><summary>查看其余 ${rs.length-5} 个${label}</summary>${list(rs.slice(5),cls,5)}</details>`:''}</section>`;
 return `<section class="period-board" aria-label="日本行业强弱对比"><div class="period-board-head"><div><h2>行业强弱</h2><span>${esc(state.date)} · ${jpPeriodKey==='change'?'当日':'区间'}等权表现</span></div><div class="period-tabs" role="group" aria-label="选择行业涨跌周期">${jpPeriods.map(([label,key])=>`<button type="button" data-jp-period="${key}" aria-pressed="${key===jpPeriodKey}">${label}</button>`).join('')}</div></div><div class="period-breadth"><span class="up">上涨 ${up.length}</span><span class="down">下跌 ${down.length}</span><span>平盘 ${flat}</span><span>有效 ${good.length}/33</span></div><div class="period-ranks">${column(up,'up','上涨板块')}${column(down,'down','下跌板块')}</div><div class="period-board-foot">按日本归档交易日计算，各期有效个股区间收益等权平均；不是均线或官方行业指数。点击行业看样本覆盖。</div></section>`;
}
function jpMetricLabel(key){return {amount:'成交额（日元）',volume:'成交量（股）',change:'涨幅',decline:'跌幅',return5:'5日涨跌',return10:'10日涨跌',return20:'20日涨跌',amountRatio:'成交额倍数（含当日）'}[key]||'成交额（日元）';}
function jpListHead(key){return `<div class="stock-row cn-stock-row head"><span>序</span><span>股票 / 行业</span><span class="number">涨跌 / 收盘</span><span class="number">${jpMetricLabel(key)}</span><span></span></div>`;}
function jpListRow(r,i,key='amount'){
 const s=day()?.sectors.find(s=>s.code===r.sector),field=key==='decline'?'change':key;
 const value=key==='amount'?money(r.amount):key==='volume'?fmt(r.volume,0):key==='amountRatio'?(valid(r.amountRatio)?fmt(r.amountRatio)+'×':'—'):pct(r[field]);
 return `<button type="button" class="stock-row cn-stock-row" data-stock="${esc(r.code)}"><span class="pos">${i}</span><span class="stock-name">${esc(r.name)}<small>${esc(numCode(r.code))} · ${esc(s?title(s):'行业未归档')}</small></span><span class="number ${tone(r.change)}">${pct(r.change)}<small>${fmt(r.close)} 日元</small></span><span class="number">${value}<small>${jpMetricLabel(key)}</small></span><span class="arrow">›</span></button>`;
}
function jpStockRankPanel(rows,scope='行业'){
 const ranked=jpRankRows(rows,jpRankKey).slice(0,10);
 return `<section class="panel"><div class="panel-head"><h3>${scope}${jpRankLabels[jpRankKey]}前10</h3><div class="segments" role="group" aria-label="日本个股排行类型">${Object.entries(jpRankLabels).map(([key,label])=>`<button type="button" data-jp-rank="${key}" aria-pressed="${jpRankKey===key}" class="${jpRankKey===key?'active':''}">${label} TOP10</button>`).join('')}</div></div><p class="chart-note">${jpRankKey==='decline'?'只列下跌股票，跌得最多在前':jpRankKey==='change'?'只列上涨股票，涨得最多在前':'按真实'+jpRankLabels[jpRankKey]+'降序'}；同值按代码，不足10只不补位。不是原代表／活跃评分。</p><div class="stock-list">${jpListHead(jpRankKey)}${ranked.map((r,i)=>jpListRow(r,i+1,jpRankKey)).join('')||'<p class="empty">没有符合条件的有效样本。</p>'}</div></section>`;
}
const jpHolidayCalendars={
 2026:{'01-01':'元旦','01-02':'新年休市','01-03':'新年休市','01-12':'成人日','02-11':'建国纪念日','02-23':'天皇诞生日','03-20':'春分日','04-29':'昭和日','05-03':'宪法纪念日','05-04':'绿之日','05-05':'儿童节','05-06':'宪法纪念日补休','07-20':'海之日','08-11':'山之日','09-21':'敬老日','09-22':'国民假日','09-23':'秋分日','10-12':'体育日','11-03':'文化日','11-23':'勤劳感谢日','12-31':'年末休市'},
 2027:{'01-01':'元旦','01-02':'新年休市','01-03':'新年休市','01-11':'成人日','02-11':'建国纪念日','02-23':'天皇诞生日','03-21':'春分日','03-22':'春分日补休','04-29':'昭和日','05-03':'宪法纪念日','05-04':'绿之日','05-05':'儿童节','07-19':'海之日','08-11':'山之日','09-20':'敬老日','09-23':'秋分日','10-11':'体育日','11-03':'文化日','11-23':'勤劳感谢日','12-31':'年末休市'}
};
function jpHolidayName(date){return jpHolidayCalendars[date.slice(0,4)]?.[date.slice(5)]||'';}
let jpCalendar={key:'',month:'',date:''};
function jpIndustryCalendar(s,series){
 const available=series.filter(r=>valid(r.count)),months=[...new Set(available.map(r=>r.date.slice(0,7)))].sort();
 if(!months.length)return '<p class="empty">暂无行业归档。</p>';
 const key=s.code+'|'+state.date;if(jpCalendar.key!==key){jpCalendar={key,month:months.at(-1),date:available.at(-1).date};}
 if(!months.includes(jpCalendar.month))jpCalendar.month=months.at(-1);
 const monthRows=available.filter(r=>r.date.startsWith(jpCalendar.month));
 if(!monthRows.some(r=>r.date===jpCalendar.date))jpCalendar.date=monthRows.at(-1).date;
 const [year,month]=jpCalendar.month.split('-').map(Number),offset=(new Date(Date.UTC(year,month-1,1)).getUTCDay()+6)%7,totalDays=new Date(Date.UTC(year,month,0)).getUTCDate(),pos=months.indexOf(jpCalendar.month),byDay=new Map(available.map(r=>[r.date,r]));
 const cells=Array.from({length:offset<5?offset:0},()=>'<div class="calendar-pad" aria-hidden="true"></div>');
 for(let n=1;n<=totalDays;n++){
  if((offset+n-1)%7>=5)continue;
  const date=jpCalendar.month+'-'+String(n).padStart(2,'0'),holiday=jpHolidayName(date),r=byDay.get(date);
  if(holiday){cells.push(`<div class="calendar-day holiday" aria-label="${date} ${esc(holiday)} 休市"><span>${n}</span><strong>${esc(holiday)}</strong><small>休市</small></div>`);continue;}
  cells.push(r?`<button type="button" class="calendar-day ${tone(r.change)} ${jpCalendar.date===date?'selected':''}" data-jp-calendar-date="${date}" aria-pressed="${jpCalendar.date===date}" aria-label="${date} ${pct(r.change)}"><span>${n}${date===available.at(-1).date?'<small>最新</small>':''}</span><strong>${pct(r.change)}</strong></button>`:`<div class="calendar-day no-record"><span>${n}</span><small>${date>state.date?'未纳入':'无归档'}</small></div>`);
 }
 const selected=byDay.get(jpCalendar.date),idx=series.findIndex(r=>r.date===selected.date),rs=byDate.get(selected.date)?.records.filter(r=>r.sector===s.code)||[],top=jpRankRows(rs,'amount').slice(0,10);
 return `<section class="panel calendar-panel"><h3>行业月历 · 每天的涨跌</h3><p class="chart-note">${esc(title(s))} · 当日成分股等权涨跌，非官方指数。点击日期查看当天金额、样本及榜单。</p><div class="industry-calendar" aria-label="日本行业月历"><div class="calendar-head"><button type="button" class="button" data-jp-calendar-month="${months[pos-1]||''}" ${pos===0?'disabled':''} aria-label="上个月">‹</button><label><span class="sr-only">选择月份</span><select id="jpCalendarMonth" aria-label="选择月份">${months.map(m=>`<option value="${m}" ${m===jpCalendar.month?'selected':''}>${m.replace('-','年')}月</option>`).join('')}</select></label><button type="button" class="button" data-jp-calendar-month="${months[pos+1]||''}" ${pos===months.length-1?'disabled':''} aria-label="下个月">›</button><span class="calendar-legend"><i class="up">上涨</i> · <i class="down">下跌</i> · 点击日期看详情</span></div><div class="calendar-week">${['一','二','三','四','五'].map(x=>`<span>周${x}</span>`).join('')}</div><div class="calendar-grid">${cells.join('')}</div><div class="calendar-detail" aria-live="polite"><div class="calendar-detail-head"><h4>${esc(selected.date)} <small>当日明细</small></h4><strong class="${tone(selected.change)}">${pct(selected.change)}</strong></div><div class="calendar-metrics"><div><label>个股涨跌有效样本</label><b>${selected.valid??'—'} / ${selected.count}</b></div>${[5,10,20].map(n=>`<div><label>${n}日涨跌均值 · 含当日</label><b>${pct(rollingValues(series,'change',n)[idx])}</b></div>`).join('')}</div><p class="calendar-amount"><span class="up">上涨 ${selected.up??'—'}</span> · <span class="down">下跌 ${selected.down??'—'}</span> · 平盘 ${selected.flat??'—'}<br>已获取成交额 <b>${money(selected.amount)} 日元</b> <span>· 金额有效 ${selected.amountValid??'—'}/${selected.count}只</span></p><details class="day-rank-details"><summary>当天行业成交额前10 · ${top.length}只</summary>${top.length?`<ol class="tree-ranks">${top.map((r,i)=>`<li><span><button type="button" data-jp-calendar-stock="${esc(r.code)}" data-jp-stock-date="${selected.date}">${i+1}. ${esc(r.name)}</button><small>${esc(numCode(r.code))}</small></span><strong>${money(r.amount)} 日元</strong><span class="${tone(r.change)}">${pct(r.change)}</span></li>`).join('')}</ol>`:'<p class="empty">当日没有有效成交额记录。</p>'}</details></div><p class="calendar-note">周末隐藏；假期按 <a href="https://www.jpx.co.jp/english/corporate/about-jpx/calendar/" target="_blank" rel="noopener noreferrer">JPX股票休市表 ↗</a> 标注${jpHolidayCalendars[year]?'':'（该年份尚未核验）'}。空白不等于平盘；日涨跌均值不等于区间收益或价格均线。</p></div></section>`;
}
function jpStockContext(r,s){return `<div class="cn-stock-context" aria-label="股票所属行业与板块"><div class="cn-stock-industry"><span>所属行业板块 · 东证33行业</span><strong>${esc(s?title(s):'行业未归档')}</strong><small>${esc(s?.name||'')}</small></div><div class="cn-stock-group"><span>看板分组</span><strong>${esc(s?meta(s.name).group:'暂缺分组')}</strong><small>仅用于首页阅读分组</small></div>${s?`<button type="button" class="primary" data-sector="${esc(s.code)}" aria-label="查看所属板块：${esc(title(s))}">查看所属板块 →</button>`:''}</div>`;}
function jpQuickMatches(rows,q){q=q.trim().normalize('NFKC').toLowerCase();if(!q)return [];const norm=v=>String(v).normalize('NFKC').toLowerCase(),exact=r=>norm(r.code)===q||norm(numCode(r.code))===q||norm(r.name)===q;return rows.filter(r=>norm(r.code).includes(q)||norm(r.name).includes(q)).sort((a,b)=>Number(exact(b))-Number(exact(a))||a.code.localeCompare(b.code));}
function jpQuickForm(){return `<form id="jpQuickForm" class="cn-quick-search" role="search"><label for="jpQuickInput">股票搜索</label><div class="cn-quick-field"><input id="jpQuickInput" type="search" placeholder="代码或日文名称，如9984 / ソフトバンク" autocomplete="off"><button type="submit" class="button">搜索</button></div><div id="jpQuickResults" class="cn-quick-results" hidden></div></form>`;}
function jpQuickResults(){const box=$('jpQuickResults'),input=$('jpQuickInput');if(!box||!input||!DATA||state.market!=='japan')return;const q=input.value.trim();if(!q){box.hidden=true;box.innerHTML='';return;}const hits=jpQuickMatches(day()?.records||[],q);box.hidden=false;box.innerHTML=`<p role="status">${hits.length?'找到 '+hits.length+' 只'+(hits.length>8?'，显示前8只，请继续输入':''):'未找到股票，请核对代码或日文名称'} · ${esc(state.date)}</p>`+hits.slice(0,8).map(r=>`<button type="button" data-jp-quick="${esc(r.code)}"><span><b>${esc(r.name)}</b><small>${esc(numCode(r.code))} · ${esc(day().sectors.find(s=>s.code===r.sector)?.name||'')}</small></span><span class="${tone(r.change)}">${pct(r.change)}<small>${fmt(r.close)} 日元</small></span></button>`).join('');}
function jpQuickOpen(code){const r=rowFor(code);if(!r)return;state.search='';route({view:'stock',stock:code,sector:r.sector});window.scrollTo(0,0);}
document.addEventListener('input',e=>{if(e.target.id==='jpQuickInput')jpQuickResults();});
document.addEventListener('submit',e=>{if(e.target.id!=='jpQuickForm')return;e.preventDefault();jpQuickResults();const hits=$('jpQuickResults')?.querySelectorAll('[data-jp-quick]');if(hits?.length===1)jpQuickOpen(hits[0].dataset.jpQuick);});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&e.target.closest('#jpQuickForm')){$('jpQuickResults').hidden=true;$('jpQuickInput').focus();}});
document.addEventListener('click',e=>{
 if(!DATA||state.market!=='japan')return;const b=e.target.closest('button');
 if(b?.dataset.jpQuick)jpQuickOpen(b.dataset.jpQuick);else if(!e.target.closest('#jpQuickForm')&&$('jpQuickResults'))$('jpQuickResults').hidden=true;
 if(b?.dataset.jpPeriod&&jpPeriods.some(x=>x[1]===b.dataset.jpPeriod)){jpPeriodKey=b.dataset.jpPeriod;render();}
 if(b?.dataset.jpRank&&Object.hasOwn(jpRankLabels,b.dataset.jpRank)){jpRankKey=b.dataset.jpRank;renderContent();}
 if(b?.dataset.jpCalendarDate){jpCalendar.date=b.dataset.jpCalendarDate;renderContent();}
 if(b?.dataset.jpCalendarMonth){jpCalendar.month=b.dataset.jpCalendarMonth;jpCalendar.date='';renderContent();}
 if(b?.dataset.jpCalendarStock){state.search='';route({view:'stock',stock:b.dataset.jpCalendarStock,date:b.dataset.jpStockDate,base:''});window.scrollTo(0,0);}
});
document.addEventListener('change',e=>{if(e.target.id==='jpCalendarMonth'){jpCalendar.month=e.target.value;jpCalendar.date='';renderContent();}});


function overview(){const search=state.search.toLowerCase();let ss=day().sectors.filter(s=>(title(s)+s.name).toLowerCase().includes(search)).sort((a,b)=>{const av=a[state.sectorSort],bv=b[state.sectorSort];return !valid(av)?1:!valid(bv)?-1:bv-av;});return summary()+comparisons()+`<div class="section-title"><div><h2>行业全景</h2><div class="subtitle">成分股有效样本等权平均 · 不是官方行业指数</div></div><select id="sectorSort" aria-label="行业排序">${[['change','当日表现'],['return5','5日表现'],['return10','10日表现'],['return20','20日表现'],['amount','成交额']].map(([v,t])=>`<option value="${v}" ${state.sectorSort===v?'selected':''}>${t}</option>`).join('')}</select></div>${ss.length?[...GROUP_ORDER,'其他'].filter(g=>ss.some(s=>meta(s.name).group===g)).map(g=>`<section class="group"><div class="group-title"><h3>${g}</h3><small>${ss.filter(s=>meta(s.name).group===g).length} 个行业</small></div><div class="grid">${ss.filter(s=>meta(s.name).group===g).map(card).join('')}</div></section>`).join(''):'<div class="empty">没有匹配的行业。</div>'}`;}
function sortedRows(rows){const q=state.search.toLowerCase(),field=state.sort==='decline'?'change':state.sort;return rows.filter(r=>(r.name+r.code).toLowerCase().includes(q)).sort((a,b)=>!valid(a[field])?(!valid(b[field])?a.code.localeCompare(b.code):1):!valid(b[field])?-1:(state.sort==='decline'?a[field]-b[field]:b[field]-a[field])||a.code.localeCompare(b.code));}
function stockRow(r,i){return jpListRow(r,i,state.sort);}
function rowsPanel(rows,heading='全部成分股',paging=true){const sorted=sortedRows(rows),size=paging?40:10,pages=Math.max(1,Math.ceil(sorted.length/size));state.page=Math.max(1,Math.min(state.page,pages));const offset=paging?(state.page-1)*size:0,shown=sorted.slice(offset,offset+size);return `<section class="panel"><div class="panel-head"><h3>${heading} <span class="tag">${sorted.length}只</span></h3>${paging?`<select id="stockSort" aria-label="股票排序">${[['amount','成交额从高到低'],['volume','成交量从高到低'],['change','涨跌幅从高到低'],['decline','涨跌幅从低到高'],['return5','5日涨幅'],['return10','10日涨幅'],['return20','20日涨幅'],['amountRatio','成交额倍数（含当日均额）']].map(([v,t])=>`<option value="${v}" ${state.sort===v?'selected':''}>${t}</option>`).join('')}</select>`:''}</div><div class="stock-list">${jpListHead(state.sort)}${shown.map((r,i)=>stockRow(r,offset+i+1)).join('')||'<div class="empty">没有匹配的股票。</div>'}</div>${paging&&pages>1?`<div class="pagination"><button class="button" data-page="${state.page-1}" ${state.page<=1?'disabled':''}>上一页</button><span>${state.page} / ${pages}</span><button class="button" data-page="${state.page+1}" ${state.page>=pages?'disabled':''}>下一页</button></div>`:''}</section>`;}

function rollingValues(rows,key,n,previous=false){
 return rows.map((r,i)=>{const end=i+(previous?0:1),start=end-n;if(start<0)return null;const part=rows.slice(start,end).map(x=>x[key]);return part.length===n&&part.every(valid)?part.reduce((a,b)=>a+b,0)/n:null;});
}
function trendPanel(points,key,label,cutoff,format=fmt){
 const all=points.filter(p=>p.date<=cutoff).sort((a,b)=>a.date.localeCompare(b.date));
 const unique=new Set(all.map(r=>r.date));if(unique.size!==all.length)return '<div class="notice">日期重复，暂停计算趋势。</div>';
 const periods=[5,10,20], colors=['#bb790b','#7863bb','#277aa1'];
 const averages=periods.map(n=>rollingValues(all,key,n));
 const offset=Math.max(0,all.length-60), rows=all.slice(offset), curves=averages.map(a=>a.slice(offset));
 const values=[...rows.map(r=>r[key]),...curves.flat()].filter(valid);
 if(!values.length)return '<section class="panel"><h3>'+esc(label)+'</h3><div class="empty">缺少真实历史数据，暂不绘制。</div></section>';
 const W=900,H=280,L=80,R=20,T=18,B=36,amount=key==='amount';
 let lo=amount?0:Math.min(...values),hi=Math.max(...values);const pad=Math.max((hi-lo)*.1,Math.abs(hi)*.001,.01);if(!amount)lo-=pad;hi+=pad;
 const x=i=>L+(i+.5)*(W-L-R)/Math.max(1,rows.length),y=v=>T+(hi-v)/(hi-lo)*(H-T-B);
 const path=vs=>{let pen=false;return vs.map((v,i)=>{if(!valid(v)){pen=false;return '';}const cmd=(pen?'L':'M')+x(i).toFixed(2)+' '+y(v).toFixed(2);pen=true;return cmd;}).join(' ');};
 const latest=all.at(-1),prior20=rollingValues(all,key,20,true).at(-1);
 const ratio=amount&&valid(latest?.amount)&&valid(prior20)&&prior20>0?latest.amount/prior20:null;
 const raw=amount?rows.map((r,i)=>valid(r[key])?`<rect x="${x(i)-Math.min(10,(W-L-R)/rows.length*.3)}" y="${y(r[key])}" width="${Math.min(20,(W-L-R)/rows.length*.6)}" height="${y(0)-y(r[key])}" fill="#667c89"><title>${esc(r.date)}：${esc(format(r[key]))}</title></rect>`:'').join(''):`<path d="${path(rows.map(r=>r[key]))}" stroke="#354b43" stroke-width="2" fill="none"/>`;
 return `<section class="panel"><h3>${esc(label)}</h3><div class="chart-note">${periods.map((n,j)=>`<span style="color:${colors[j]};margin-right:16px">${n}日${amount?'均额':'均线'} ${valid(averages[j].at(-1))?esc(format(averages[j].at(-1))):'样本不足'}</span>`).join('')}</div>${amount?`<p>当日 / 此前20日均额：<b>${valid(ratio)?fmt(ratio)+'×':'—（样本不足或均额为零）'}</b> · 成交活跃度，不是资金净流入</p>`:''}<div class="chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}与5、10、20日平均线">${Array.from({length:5},(_,i)=>{const v=lo+(hi-lo)*i/4;return `<line x1="${L}" x2="${W-R}" y1="${y(v)}" y2="${y(v)}" stroke="#e0e7e1"/><text x="${L-6}" y="${y(v)+4}" text-anchor="end" font-size="11">${esc(format(v))}</text>`;}).join('')}${raw}${curves.map((v,j)=>`<path d="${path(v)}" fill="none" stroke="${colors[j]}" stroke-width="2"/>`).join('')}${rows.map((r,i)=>valid(r[key])?`<circle cx="${x(i)}" cy="${y(r[key])}" r="3" fill="#354b43"><title>${esc(r.date)}：${esc(format(r[key]))}${periods.map((n,j)=>'；'+n+'日 '+(valid(curves[j][i])?format(curves[j][i]):'样本不足')).join('')}</title></circle>`:'').join('')}${[...new Set([0,Math.floor(rows.length/2),rows.length-1])].map(i=>`<text x="${x(i)}" y="${H-8}" text-anchor="middle" font-size="11">${esc(rows[i].date)}</text>`).join('')}</svg></div><p class="chart-note">平均线含当日；成交额倍数的20日基准不含当日。缺失值使对应窗口留空；按归档交易日计算，不代表盘中量比。</p><details><summary>展开均线明细</summary><div class="data-scroll"><table><thead><tr><th>日期</th><th>当日值</th>${periods.map(n=>`<th>${n}日平均</th>`).join('')}</tr></thead><tbody>${rows.map((r,i)=>`<tr><td>${esc(r.date)}</td><td>${esc(format(r[key]))}</td>${curves.map(c=>`<td>${esc(format(c[i]))}</td>`).join('')}</tr>`).reverse().join('')}</tbody></table></div></details></section>`;
}



// Exchange closures, verified against the linked annual notice. Do not reuse across years.
const cnHolidayCalendars={2026:{source:'https://www.szse.cn/disclosure/notice/general/t20251222_618087.html',periods:[
 ['01-01','01-03','元旦'],['02-15','02-23','春节'],['04-04','04-06','清明节'],
 ['05-01','05-05','劳动节'],['06-19','06-21','端午节'],['09-25','09-27','中秋节'],['10-01','10-07','国庆节']
]}};
function cnHolidayName(date){const calendar=cnHolidayCalendars[date.slice(0,4)],md=date.slice(5);return calendar?.periods.find(([from,to])=>md>=from&&md<=to)?.[2]||'';}
let cnCalendarMonth='',cnCalendarDate='';
function industryDateTree(history,code,china){
 if(!history.length)return '<div class="empty">暂无行业历史。</div>';
 const months=[...new Set(history.map(x=>x.date.slice(0,7)))].sort(),latest=history.at(-1).date;
 if(!months.includes(cnCalendarMonth))cnCalendarMonth=months.at(-1);
 const monthHistory=history.filter(x=>x.date.startsWith(cnCalendarMonth));
 if(!monthHistory.some(x=>x.date===cnCalendarDate))cnCalendarDate=monthHistory.at(-1).date;
 const pos=months.indexOf(cnCalendarMonth),[year,month]=cnCalendarMonth.split('-').map(Number),offset=(new Date(Date.UTC(year,month-1,1)).getUTCDay()+6)%7,nDays=new Date(Date.UTC(year,month,0)).getUTCDate();
 const byDay=new Map(history.map(x=>[x.date,x]));
 const cells=Array.from({length:offset<5?offset:0},()=>'<div class="calendar-pad" aria-hidden="true"></div>');
 for(let n=1;n<=nDays;n++){
  const date=cnCalendarMonth+'-'+String(n).padStart(2,'0'),x=byDay.get(date),weekend=(offset+n-1)%7>=5;
  if(weekend)continue;
  const holiday=china?cnHolidayName(date):'';
  if(holiday){cells.push(`<div class="calendar-day holiday" aria-label="${date} ${esc(holiday)} 休市"><span>${n}</span><strong>${esc(holiday)}</strong><small>休市</small></div>`);continue;}
  cells.push(x?`<button type="button" class="calendar-day ${tone(x.row.change)} ${date===cnCalendarDate?'selected':''}" data-cn-calendar-date="${date}" aria-pressed="${date===cnCalendarDate}" aria-label="${date} ${pct(x.row.change)}"><span>${n}${date===latest?'<small>最新</small>':''}</span><strong>${pct(x.row.change)}</strong></button>`:`<div class="calendar-day no-record"><span>${n}</span><small>${date>latest?'未纳入':'无归档'}</small></div>`);
 }
 const idx=history.findIndex(x=>x.date===cnCalendarDate),x=history[idx],points=history.map(x=>({date:x.date,level:x.row.level}));
 const snap=china?DATA.cnStocks?.days?.find(d=>d.date===x.date):null,rs=snap?.rows.filter(r=>r.sector===code)||[],amounts=rs.filter(r=>valid(r.amount)&&r.amount>=0),total=amounts.length?amounts.reduce((a,r)=>a+r.amount,0):null;
 const top=[...amounts].sort((a,b)=>b.amount-a.amount||a.code.localeCompare(b.code)).slice(0,10);
 return `<div class="industry-calendar" aria-label="行业月历"><div class="calendar-head"><button class="button" data-cn-calendar-month="${months[pos-1]||''}" ${pos===0?'disabled':''} aria-label="上个月">‹</button><label><span class="sr-only">选择月份</span><select id="cnCalendarMonth" aria-label="选择月份">${months.map(m=>`<option value="${m}" ${m===cnCalendarMonth?'selected':''}>${m.replace('-','年')}月</option>`).join('')}</select></label><button class="button" data-cn-calendar-month="${months[pos+1]||''}" ${pos===months.length-1?'disabled':''} aria-label="下个月">›</button><span class="calendar-legend"><i class="up">上涨</i> · <i class="down">下跌</i> · 点击日期看详情</span></div><div class="calendar-week">${['一','二','三','四','五'].map(d=>`<span>周${d}</span>`).join('')}</div><div class="calendar-grid">${cells.join('')}</div><div class="calendar-detail" aria-live="polite"><div class="calendar-detail-head"><h4>${esc(x.date)} <small>当日明细</small></h4><strong class="${tone(x.row.change)}">${pct(x.row.change)}</strong></div><div class="calendar-metrics"><div><label>指数点位</label><b>${fmt(x.row.level)}</b></div>${[5,10,20].map(n=>{const v=rollingValues(points,'level',n)[idx];return `<div><label>MA${n} · 含当日</label><b>${valid(v)?fmt(v):'样本不足'}</b></div>`;}).join('')}</div><p class="calendar-amount">已采集成交额 <b>${valid(total)?fmt(total/1e8)+' 亿元':'—'}</b> <span>· 金额有效 ${amounts.length}只${!snap?' · 当日无个股归档':''}</span></p><details class="day-rank-details"><summary>当天行业成交额前10${top.length?' · '+top.length+'只':''}</summary>${top.length?`<ol class="tree-ranks">${top.map((r,i)=>`<li><span>${i+1}. ${esc(r.name)} <small>${esc(r.code)}</small></span><strong>${fmt(r.amount/1e8)} 亿元</strong><span class="${tone(r.change)}">${pct(r.change)}</span></li>`).join('')}</ol>`:'<p class="empty">该日期无已核验个股数据，不用其他日期补位。</p>'}</details></div><p class="calendar-note">仅显示周一至周五；行情截至观测日期，空白不等于平盘。${china?(cnHolidayCalendars[year]?`节日名称按 <a href="${cnHolidayCalendars[year].source}" target="_blank" rel="noopener noreferrer">${year}年深交所休市安排 ↗</a> 标注；“无归档”表示尚无该日行情。`:`${year}年休市日历尚未核验；“无归档”不等于休市。`):'“无归档”可能是休市或尚未采集。'}</p></div>`;
}
document.addEventListener('click',e=>{const b=e.target.closest('button');if(!b||!DATA||!['china','usa'].includes(state.market))return;if(b.dataset.cnCalendarDate){cnCalendarDate=b.dataset.cnCalendarDate;renderUS();}else if(b.dataset.cnCalendarMonth){cnCalendarMonth=b.dataset.cnCalendarMonth;cnCalendarDate='';renderUS();}});
document.addEventListener('change',e=>{if(e.target.id==='cnCalendarMonth'){cnCalendarMonth=e.target.value;cnCalendarDate='';renderUS();}});
function cnIndustryTrends(code,days,cutoff){
 const points=days.filter(d=>d.date<=cutoff).map(d=>{
 const index=d.rows.find(r=>r.code===code),snap=DATA.cnStocks?.days?.find(x=>x.date===d.date),rs=snap?.rows.filter(r=>r.sector===code||r.sectorCode===code)||[];
 return {date:d.date,level:index?.level??null,amount:rs.length&&rs.every(r=>valid(r.amount))?rs.reduce((sum,r)=>sum+r.amount,0):null};
 });
 return trendPanel(points,'level','行业指数 · 5 / 10 / 20日均线',cutoff)+trendPanel(points,'amount','已采集成分股成交额 · 5 / 10 / 20日均额',cutoff,money)+'<p class="chart-note">成交额为当日已采集成分股真实金额之和，非官方行业总额；缺失样本和成分变化会影响可比性。不同市场不混合计算。</p>';
}
function chart(points,key,label,format=pct){const rows=points.filter(p=>p.date<=state.date).slice(-60),values=rows.map(p=>p[key]).filter(valid);if(!values.length)return '<div class="empty">这个窗口没有可计算的历史数值。</div>';const W=900,H=270,L=65,R=18,T=15,B=35;let lo=Math.min(...values),hi=Math.max(...values);const pad=Math.max((hi-lo)*.12,Math.abs(hi)*.001,.01);lo-=pad;hi+=pad;const x=i=>L+i*(W-L-R)/Math.max(1,rows.length-1),y=v=>T+(hi-v)/(hi-lo)*(H-T-B);let path='',pen=false;rows.forEach((r,i)=>{if(!valid(r[key])){pen=false;return;}path+=(pen?'L':'M')+x(i).toFixed(1)+' '+y(r[key]).toFixed(1)+' ';pen=true;});return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}历史趋势；数值见历史明细">${Array.from({length:5},(_,i)=>{const v=lo+(hi-lo)*i/4;return `<line x1="${L}" x2="${W-R}" y1="${y(v)}" y2="${y(v)}" stroke="#e0e7e1"/><text x="${L-8}" y="${y(v)+4}" text-anchor="end" font-size="11" fill="#6b8074">${esc(format(v))}</text>`;}).join('')}<path d="${path}" fill="none" stroke="#26775c" stroke-width="2.3"/>${rows.map((r,i)=>valid(r[key])?`<circle cx="${x(i)}" cy="${y(r[key])}" r="3.5" fill="#26775c"><title>${r.date} · ${esc(format(r[key]))}</title></circle>`:'').join('')}${[...new Set([0,Math.floor(rows.length/2),rows.length-1])].map(i=>`<text x="${x(i)}" y="${H-8}" text-anchor="${i===0?'start':i===rows.length-1?'end':'middle'}" font-size="11" fill="#6b8074">${rows[i].date}</text>`).join('')}</svg></div>`;}
function sectorView(){
 const s=sector();if(!s)return '<div class="empty">该日期没有这个行业的记录。<button class="button" data-view="overview">返回行业看板</button></div>';
 const rs=day().records.filter(r=>r.sector===s.code),b=baseDay()?.sectors.find(x=>x.code===s.code),series=dates.filter(d=>d<=state.date).map(d=>({date:d,...byDate.get(d).sectors.find(x=>x.code===s.code)}));
 return `<div class="breadcrumb"><button data-view="overview">← 全部行业</button> / ${esc(meta(s.name).group)} / ${esc(title(s))}</div><div class="summary">${metric('成分股等权涨跌',pct(s.change),`${s.valid}/${s.count}只有效`,tone(s.change))}${metric('上涨占比',pct(s.valid?s.up/s.valid*100:null),'分母为涨跌幅有效的普通股')}${metric('已获取成交额',money(s.amount),'日元 · '+s.amountValid+'/'+s.count+'只')}${metric('相较对比日的涨跌幅差',valid(s.change)&&valid(b?.change)?fmt(s.change-b.change)+'pp':'—',baseDay()?.date||'缺少对比记录')}</div>${trendPanel(series,'change','行业日涨跌 · 5 / 10 / 20日均值（%）',state.date,pct)}<p class="chart-note">${esc(title(s))}（${esc(s.name)}）· 曲线是每天成分股等权涨跌及其均值，不是行业指数价格均线，也不是5/10/20日累计涨跌；各日有效样本可能变化。</p>${trendPanel(series,'amount','已获取行业成交额 · 5 / 10 / 20日均额（日元）',state.date,money)}${jpIndustryCalendar(s,series)}${jpStockRankPanel(rs)}<details class="panel"><summary>代表／活跃TOP10的数据状态</summary><p>当前量额和涨跌排行来自真实归档，未套用原代表／活跃评分。历史自由流通市值、原入池字段仍未齐全，原评分榜单保留缺失。</p></details>${rowsPanel(rs,'行业成分股检索')}`;
}
function stockView(){const r=rowFor(state.stock);if(!r)return '<div class="empty">该股票在所选日期没有普通股记录，请更换日期。</div>';const s=day().sectors.find(x=>x.code===r.sector),history=[...byStock.get(r.code)].filter(([d])=>d<=state.date).map(([date,v])=>({date,...v}));const badges=history.filter(x=>valid(x.amountRank)&&x.amountRank<=10);const b=rowFor(r.code,baseDay()?.date||'');const previousAmount20=rollingValues(dates.filter(d=>d<=state.date).map(date=>({date,amount:rowFor(r.code,date)?.amount})),'amount',20,true).at(-1);const metrics=[['开盘',fmt(r.open)+' 日元'],['最高 / 最低',fmt(r.high)+' / '+fmt(r.low)],['成交量',fmt(r.volume,0)+' 股'],['真实成交额',money(r.amount)+' 日元'],['5日涨跌',pct(r.return5)],['10日涨跌',pct(r.return10)],['20日涨跌',pct(r.return20)],['此前20日均成交额',money(previousAmount20)+' 日元'],['成交额 / 此前20日均额',valid(r.amount)&&valid(previousAmount20)&&previousAmount20>0?fmt(r.amount/previousAmount20)+'×':'—'],['行业成交额排名',valid(r.amountRank)?'第'+r.amountRank+'名':'—'],['总市值（非自由流通）',valid(r.totalCapMillions)?money(r.totalCapMillions*1e6)+' 日元':'—'],['自由流通市值','— 尚缺数据'],['对比日收盘',b?fmt(b.close)+' 日元':'—']];return `<div class="breadcrumb"><button data-view="overview">行业看板</button> / <button data-sector="${r.sector}">${esc(s?title(s):r.sector)}</button> / ${numCode(r.code)}</div><section class="panel"><div class="panel-head"><div><h2>${esc(r.name)}</h2><div class="subtitle">${numCode(r.code)} · ${esc(r.market)} · ${state.date}</div></div><div><div class="detail-price">${fmt(r.close)} <small style="font-size:12px">日元</small></div><div class="${tone(r.change)}" style="text-align:right">${pct(r.change)}</div></div></div>${jpStockContext(r,s)}<div class="detail-grid">${metrics.map(([k,v])=>`<div><label>${k}</label><b>${v}</b></div>`).join('')}</div></section><section class="panel"><div class="panel-head"><h3>近期量价</h3><div class="segments">${[['close','收盘价'],['change','每日涨跌'],['amount','成交额']].map(([k,t])=>`<button data-chart="${k}" class="${state.chart===k?'active':''}">${t}</button>`).join('')}</div></div>${state.chart==='change'?chart(history,'change',r.name):trendPanel(dates.filter(d=>d<=state.date).map(d=>({date:d,...(byStock.get(r.code)?.get(d)||{})})),state.chart,state.chart==='amount'?'真实成交额（日元）':'未复权收盘价（日元）',state.date,state.chart==='amount'?money:fmt)}<div class="chart-note">${state.chart==='close'?'价格图为未复权收盘（日元），拆并股可能导致跳空；涨跌幅另按拆并股因子计算。':state.chart==='amount'?'成交额单位为日元，使用真实成交额，不以收盘价乘成交量估算。':'按相邻归档交易日收盘及拆并股因子计算，不含现金分红。'} 缺失值断开，不补零。</div><details><summary>展开历史明细 · ${history.length}个交易日</summary><div class="data-scroll"><table><thead><tr><th>日期</th><th>收盘（日元）</th><th>涨跌幅</th><th>成交量（股）</th><th>成交额（日元）</th><th>行业成交额排名</th></tr></thead><tbody>${[...history].reverse().map(x=>`<tr><td>${x.date}</td><td>${fmt(x.close)}</td><td class="${tone(x.change)}">${pct(x.change)}</td><td>${fmt(x.volume,0)}</td><td>${money(x.amount)}</td><td>${valid(x.amountRank)?x.amountRank:'—'}</td></tr>`).join('')}</tbody></table></div></details></section><section class="panel"><div class="panel-head"><h3>行业成交额前10 · 上榜历史</h3><span class="tag">${badges.length} / ${history.length}个观测日</span></div><div class="history-tags">${badges.map(x=>`<button data-history-date="${x.date}">${x.date.slice(5)} · 第${x.amountRank}名</button>`).join('')||'<span class="subtitle">归档窗口内暂无上榜记录。</span>'}</div><p class="chart-note">只统计已归档日期和当时所属行业；不代表原代表／活跃 TOP10 的上榜历史。</p></section>`;}
function help(){const d=day(),missing=d.records.filter(r=>!valid(r.change)).length;return `<section class="panel help"><h2>数据状态与计算口径</h2><p>来源：J-Quants API V2。最新行情 <b>${DATA.latestDate}</b>；归档 ${dates[0]} 至 ${dates.at(-1)}，共 ${dates.length} 个交易日。网页最多载入最近120个交易日，完整原始归档保留在私有仓库。日本时区 Asia/Tokyo，金额为日元。</p><p>最近成功采集：${esc(DATA.update.lastSuccess||'—')}。当前来源状态：${esc(DATA.update.state)}。首尾日期不等于每只股票都有完整行情；无成交等原因可产生空值。</p><div class="notice">所选日期 ${state.date}：${d.records.length}只普通股记录，${missing}只日涨跌暂缺。缺失原因无法仅靠日线精确区分为停牌或无成交。</div><h3>行业表现</h3><p>首页为当日所属行业普通股的<b>有效样本等权平均</b>，不是东证行业指数或TOPIX。涨跌幅、5/10/20日使用各自有效样本，先算个股区间收益再在当日所属行业中等权平均；10日由完整窗口的拆并股调整日涨跌复利，任一天缺数则不纳入；行业详情显示样本覆盖，历史成分变化会影响可比性。</p><h3>价格、成交额与20日窗口</h3><p>个股涨跌结合区间拆并股因子计算，不含现金分红。价格图保留未复权收盘。图表的5/10/20日均额含当日；图表与个股详情的放大倍数使用此前20日均额（不含当日）。列表的“成交额倍数（含当日）”保留原数据定义，名称明确区分。缺失时对应窗口留空；都不是盘中量比。行业日涨跌均值曲线不是价格指数或区间收益。</p><h3>三种榜单分别看</h3><p>已实现的“行业成交额前10”仅按真实成交额排序，未应用原入池条件。原“代表TOP10”和“活跃TOP10”仍缺历史自由流通市值等字段，暂不出分、不生成名单。总市值不能替代自由流通市值。</p><h3>日期对比</h3><p>行业排名按当日成分股等权涨跌排序。“涨跌幅差”是两个日期日涨跌幅之差，单位百分点，不是两日之间的累计收益。上榜历史只来自归档窗口，不向前补造。</p><h3>自动更新</h3><p>GitHub 每天北京时间19:17、21:17取数并生成加密文件。休市时保留上个交易日；调度或数据源可能延迟，请以最新行情日期为准。页面不会在你阅读时切换日期；重新进入可获取新数据。</p><h3>访问与安全</h3><p>本页面外壳公开，行情文件使用 AES-256-GCM 加密。访问密码经 PBKDF2-SHA256（600,000次）派生密钥，仅在浏览器解密，不发送给网站、不持久保存。知道密码的人能够解密，因此请勿分享。J-Quants 密钥只存放于私有仓库，浏览器不使用它。</p><p>请使用足够长的专用密码。改密码不能让已经下载的旧加密文件失效，也不能撤回别人曾经解密的数据。</p><h3>中日美市场</h3><p>日本使用J-Quants个股及东证33行业等权统计，中国使用申万行业指数及已采集个股，美国展示已接入的行业快照；各市场日期、币种与口径独立。分类对照是阅读参考，不是官方逐项等价映射。</p></section>`;}
function renderContent(){if(state.market!=='japan'&&state.view!=='mapping'){ $('content').innerHTML=`<section class="panel empty"><h2>${state.market==='china'?'中国 A股':'美国'}行情尚未接入</h2><p>该市场没有导入真实个股日线，不借用日本数据。</p><button class="button" data-view="mapping">查看分类对照</button></section>`;return;} $('content').innerHTML=state.view==='overview'?overview():state.view==='sector'?sectorView():state.view==='stock'?stockView():state.view==='stocks'?jpStockRankPanel(day().records,'全市场')+rowsPanel(day().records,'全部普通股检索'):state.view==='mapping'?`<section class="panel mapping"><h2>中日美行业分类参考</h2><p>保留原项目分类资料；不是行情，也不代表一一对应。</p><div>${REFERENCE_HTML}</div></section>`:help();}

let usDate='',usBase='',usSelected='';
let cnStockCode='',cnQuery='',cnRankMetric='volume';
const cnRankLabels={volume:'成交量',amount:'成交额',change:'涨幅',decline:'跌幅'};
function cnRankRows(rows,key='volume'){
 const field=key==='decline'?'change':key;
 return [...rows].filter(r=>valid(r[field])&&(key==='decline'?r.change<0:key==='change'?r.change>0:r[field]>0)).sort((a,b)=>(key==='decline'?a[field]-b[field]:b[field]-a[field])||a.code.localeCompare(b.code));
}
function cnListRow(r,i,key='volume'){
 const value=key==='volume'?valid(r.volume)?fmt(r.volume/1e4)+' 万股':'—':key==='amount'?valid(r.amount)?fmt(r.amount/1e8)+' 亿元':'—':pct(r.change);
 return `<button class="stock-row cn-stock-row" data-cn-stock="${esc(r.code)}"><span class="pos">${i}</span><span class="stock-name">${esc(r.name)}<small>${esc(r.code)} · ${esc(r.sectorName)}</small></span><span class="number ${tone(r.change)}">${pct(r.change)}<small>¥${fmt(r.close)}</small></span><span class="number">${value}<small>${cnRankLabels[key]}</small></span><span class="arrow">›</span></button>`;
}
function cnListHead(key){return `<div class="stock-row cn-stock-row head"><span>序</span><span>名称 / 代码</span><span class="number">涨跌 / 价格</span><span class="number">${cnRankLabels[key]}${key==='volume'?'（万股）':key==='amount'?'（亿元）':'（%）'}</span><span></span></div>`;}


function cnSummaryStats(indexDay,stockDay){
 const sectors=new Set((indexDay.rows||[]).filter(r=>valid(r.level)&&r.level>0).map(r=>r.code));
 const same=stockDay?.date===indexDay.date;
 const raw=same?(stockDay.rows||[]):[],counts=new Map();
 raw.forEach(r=>counts.set(r.code,(counts.get(r.code)||0)+1));
 const rows=raw.filter(r=>r.code&&counts.get(r.code)===1);
 const changes=rows.filter(r=>valid(r.change)),amounts=rows.filter(r=>valid(r.amount)&&r.amount>=0);
 const sc=same?(stockDay.sectors||[]):[];
 const expected=sc.length&&sc.every(r=>valid(r.expected)&&r.expected>=0)?sc.reduce((a,r)=>a+r.expected,0):null;
 return {industries:sectors.size,same,count:rows.length,expected,validCount:changes.length,
 up:changes.filter(r=>r.change>0).length,down:changes.filter(r=>r.change<0).length,flat:changes.filter(r=>r.change===0).length,
 mean:changes.length?changes.reduce((a,r)=>a+r.change,0)/changes.length:null,
 amount:amounts.length?amounts.reduce((a,r)=>a+r.amount,0):null,amountCount:amounts.length,
 missing:valid(expected)&&expected>=changes.length?expected-changes.length:null};
}
function cnSummary(indexDay,stockDay){
 const m=cnSummaryStats(indexDay,stockDay),none='所选日期无个股归档，不借用其他日期';
 const coverage=m.same?`个股样本 ${fmt(m.count,0)} / ${valid(m.expected)?fmt(m.expected,0):'—'}只`:'所选日期个股尚未归档';
 return `<div class="summary cn-summary" aria-label="中国市场每日汇总">${metric('行业覆盖',m.industries+'<small style="display:inline;font-size:16px"> / 31</small>',coverage)}${metric('上涨 / 下跌个股',m.same&&m.validCount?`<span class="up">${fmt(m.up,0)}</span><span style="font-size:16px"> / </span><span class="down">${fmt(m.down,0)}</span>`:'—',m.same?`平盘 ${fmt(m.flat,0)} · 涨跌有效 ${fmt(m.validCount,0)} · 未纳入 ${valid(m.missing)?fmt(m.missing,0):'—'}`:none)}${metric('样本个股等权表现',pct(m.mean),m.same?'有效个股日涨跌平均；非全A、非官方指数':none,tone(m.mean))}${metric('已获取成交额',valid(m.amount)?fmt(m.amount/1e8)+'<small style="display:inline;font-size:14px"> 亿元</small>':'—',m.same?`人民币 · 金额有效 ${fmt(m.amountCount,0)}/${valid(m.expected)?fmt(m.expected,0):'—'}只；不含缺失项`:none)}</div><p class="chart-note">统计范围：申万一级行业指数当前成分中的已采集样本 · 个股来源新浪 · 行情日期 ${esc(indexDay.date)}${m.same?' · 成分观察 '+esc(stockDay.membershipAsOf||'—'):''}。未纳入样本不直接判定为停牌；成交额不等于净流入。</p>`;
}
function cnStockContext(stock){
 const industry=DATA.cnIndustries?.days?.find(d=>d.date===usDate)?.rows?.find(r=>r.code===stock.sector);
 const industryName=stock.sectorName||industry?.name||'暂缺行业归属';
 return `<div class="cn-stock-context" aria-label="股票所属行业与板块"><div class="cn-stock-industry"><span>所属行业板块 · 申万一级</span><strong>${esc(industryName)}</strong></div><div class="cn-stock-group"><span>看板分组</span><strong>${esc(industry?.group||'暂缺分组')}</strong><small>仅用于首页阅读分组</small></div>${industry?`<button type="button" class="primary" data-cn-industry="${esc(industry.code)}" aria-label="查看所属板块：${esc(industryName)}">查看所属板块 <span aria-hidden="true">→</span></button>`:'<span class="subtitle">所选日期暂无对应行业数据</span>'}</div>`;
}
document.addEventListener('click',e=>{const b=e.target.closest('[data-cn-industry]');if(!b||!DATA||state.market!=='china')return;const code=b.dataset.cnIndustry;if(!DATA.cnIndustries?.days?.find(d=>d.date===usDate)?.rows?.some(r=>r.code===code))return;cnStockCode='';cnQuery='';usSelected=code;state.view='overview';renderUS();window.scrollTo(0,0);});
function cnStockPanel(sectorCode=''){
 const bundle=DATA.cnStocks, d=bundle?.days?.find(x=>x.date===usDate);
 if(!d)return '<section class="panel"><h2>个股与排行</h2><p>此日期尚无个股归档。请选择已有个股数据的日期；不会用其他日期补位。</p></section>';
 const all=d.rows||[], rows=sectorCode?all.filter(r=>r.sector===sectorCode):all;
 const s=d.sectors.find(x=>x.code===sectorCode), expected=s?.expected||d.sectors.reduce((a,s)=>a+s.expected,0);
 const notice=`<div class="notice">个股日期 ${esc(d.date)} · 有效交易样本 ${rows.length}/${expected} · 成分观察 ${esc(d.membershipAsOf)}。范围为申万指数当前成分；缺数和停牌不参与。${bundle.status?.state==='failed'?'最近更新失败，以下保留旧归档。':''}成交量／成交额／涨幅榜不是原代表分或活跃分，原评分仍缺字段。</div>`;
 const stock=all.find(r=>r.code===cnStockCode);
 if(stock){const history=bundle.days.filter(x=>x.date<=usDate).map(x=>({date:x.date,row:x.rows.find(r=>r.code===stock.code)})).filter(x=>x.row);return notice+`<section class="panel"><button class="button" data-cn-back>← 返回个股排行</button><h2>${esc(stock.name)} <small class="cn-stock-symbol">${esc(stock.code)}</small></h2>${cnStockContext(stock)}<p class="cn-stock-quote">行情日期 ${esc(d.date)} · 报价时间 ${esc(stock.sourceTime||'—')}（北京时间）</p><div class="summary">${metric('股价',fmt(stock.close),'人民币元')}${metric('当日涨跌',pct(stock.change),'来源昨收计算',tone(stock.change))}${metric('成交额',valid(stock.amount)?fmt(stock.amount/1e8):'—','亿元；源字段非估算')}</div>${trendPanel(bundle.days.filter(x=>x.date<=usDate).map(x=>({date:x.date,...(x.rows.find(r=>r.code===stock.code)||{})})),'close','未复权收盘价（元）',usDate)}${trendPanel(bundle.days.filter(x=>x.date<=usDate).map(x=>({date:x.date,...(x.rows.find(r=>r.code===stock.code)||{})})),'amount','真实成交额（元）',usDate,money)}<p class="chart-note">个股价格均线基于未复权收盘，除权、拆并股可能产生跳变，不直接作为交易信号。</p><h3>每日量价与成交额上榜历史</h3><p>仅从真实归档开始；未核验复权数据，不计算跨日价格收益。</p><div class="grid">${history.reverse().map(x=>`<div class="compare-item"><label>${esc(x.date)}</label><strong class="${tone(x.row.change)}">${pct(x.row.change)}</strong><p>价格 ¥${fmt(x.row.close)} · 成交量 ${valid(x.row.volume)?fmt(x.row.volume/10000)+' 万股':'—'}</p><p>成交额 ${valid(x.row.amount)?fmt(x.row.amount/1e8)+' 亿元':'—'} · 行业内成交额 ${valid(x.row.amountRank)&&x.row.amountRank<=10?'第 '+x.row.amountRank+' 名（前10）':'未进入前10或缺数'}</p></div>`).join('')}</div></section>`;}
 const ranked=cnRankRows(rows,cnRankMetric).slice(0,10);
 const sortField=cnRankMetric==='decline'?'change':cnRankMetric;
 const filtered=[...rows].sort((a,b)=>{const av=a[sortField],bv=b[sortField];return !valid(av)?(!valid(bv)?a.code.localeCompare(b.code):1):!valid(bv)?-1:(cnRankMetric==='decline'?av-bv:bv-av)||a.code.localeCompare(b.code);}).filter(r=>(r.name+' '+r.code+' '+r.sectorName).toLowerCase().includes(cnQuery.toLowerCase()));
 return notice+`<section class="panel"><div class="panel-head"><h3>${sectorCode?'行业':'已采集样本'}${cnRankLabels[cnRankMetric]}前10</h3><div class="segments" aria-label="中国个股排行类型">${Object.entries(cnRankLabels).map(([key,label])=>`<button data-cn-rank="${key}" aria-pressed="${key===cnRankMetric}" class="${key===cnRankMetric?'active':''}">${label} TOP10</button>`).join('')}</div></div><p class="chart-note">${cnRankMetric==='decline'?'仅下跌股票，按当日涨跌幅从低到高（跌得最多在前）':cnRankMetric==='change'?'仅上涨股票，按当日涨幅从高到低':'按当日真实'+cnRankLabels[cnRankMetric]+'降序'}；不足10只不补位，并列时按股票代码。${cnRankMetric==='volume'?'成交量为成交股数，显示单位万股；不等于成交金额。':''}覆盖不足时仅为已采集样本排名，不是代表／活跃评分。</p><div class="stock-list">${cnListHead(cnRankMetric)}${ranked.map((r,i)=>cnListRow(r,i+1,cnRankMetric)).join('')||'<div class="empty">暂无有效数据。</div>'}</div></section><section class="panel"><div class="panel-head"><h3>${sectorCode?'行业成分股':'全部个股'}检索</h3><span class="tag">${filtered.length} 只匹配</span></div><form id="cnSearchForm" class="toolbar"><label class="grow">代码、名称或行业<input type="search" id="cnSearch" value="${esc(cnQuery)}" placeholder="例如：000938 或 紫光股份"></label><button class="button">搜索</button></form><p class="chart-note">按${cnRankLabels[cnRankMetric]}排序，显示前100只，可搜索定位。</p><div class="stock-list">${cnListHead(cnRankMetric)}${filtered.slice(0,100).map((r,i)=>cnListRow(r,i+1,cnRankMetric)).join('')||'<div class="empty">没有匹配的股票。</div>'}</div></section>`;
}
document.addEventListener('click',e=>{const b=e.target.closest('button');if(!DATA||state.market!=='china'||!b)return;if(b.dataset.cnRank&&Object.hasOwn(cnRankLabels,b.dataset.cnRank)){cnRankMetric=b.dataset.cnRank;renderUS();}if(b.dataset.cnStock){cnStockCode=b.dataset.cnStock;renderUS();}if(b.hasAttribute('data-cn-back')){cnStockCode='';renderUS();}if(b.dataset.usSector||b.hasAttribute('data-us-back')||b.dataset.view){cnStockCode='';cnQuery='';renderUS();}});
document.addEventListener('submit',e=>{if(e.target.id==='cnSearchForm'){e.preventDefault();cnQuery=$('cnSearch').value.trim();renderUS();}});

function cnDayKey(value) {
  if (value instanceof Date) {
    return Number.isFinite(value.getTime()) ? value.toISOString().slice(0, 10) : null;
  }
  const m = String(value ?? "").match(/^(\d{4})[-/](\d{2})[-/](\d{2})(?:$|[T\s])/);
  if (!m) return null;
  const y = +m[1], mo = +m[2], d = +m[3];
  const dt = new Date(Date.UTC(y, mo - 1, d));
  if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== mo - 1 || dt.getUTCDate() !== d) return null;
  return `${m[1]}-${m[2]}-${m[3]}`;
}

function cnCardMetrics(indexRow, indexDays, currentDate, baseDate, stockDay) {
  const current = cnDayKey(currentDate);
  const base = cnDayKey(baseDate);
  const code = indexRow && indexRow.code != null ? String(indexRow.code) : null;
  const days = new Map(), duplicates = new Set();

  for (const day of Array.isArray(indexDays) ? indexDays : []) {
    const date = cnDayKey(day && day.date);
    if (!date || !current || date > current) continue;
    if (days.has(date) || duplicates.has(date)) {
      duplicates.add(date);
    } else {
      days.set(date, day);
    }
  }

  function uniqueSectorRow(day) {
    const found = (Array.isArray(day && day.rows) ? day.rows : [])
      .filter(r => r && r.code != null && String(r.code) === code);
    return found.length === 1 ? found[0] : null;
  }

  function archiveLevel(day) {
    const rows = Array.isArray(day && day.rows) ? day.rows : [];
    if (rows.length !== 31) return undefined;
    const seen = new Set();
    for (const r of rows) {
      if (!r || r.code == null) return undefined;
      const key = String(r.code);
      if (seen.has(key)) return undefined;
      seen.add(key);
    }
    if (seen.size !== 31) return undefined;
    const r = uniqueSectorRow(day);
    return { level: r ? r.level : null };
  }

  const prior = [];
  for (const [date, day] of days) {
    if (date >= current) continue;
    const archived = archiveLevel(day);
    prior.push({ date, level: archived && !duplicates.has(date) ? archived.level : null });
  }
  prior.sort((a, b) => b.date.localeCompare(a.date));

  const todayArchive = current && !duplicates.has(current) && archiveLevel(days.get(current));
  const todayLevel = todayArchive ? todayArchive.level : null;
  function periodReturn(n) {
    if (!valid(todayLevel) || todayLevel <= 0 || prior.length < n) return null;
    const span = prior.slice(0, n);
    if (span.some(x => !valid(x.level) || x.level <= 0)) return null;
    return 100 * (todayLevel / span[n - 1].level - 1);
  }

  const indexChange = indexRow && valid(indexRow.change) ? indexRow.change : null;
  const baseRow = base && current && base < current && days.has(base) && !duplicates.has(base)
    ? uniqueSectorRow(days.get(base)) : null;
  const indexDiff = indexChange !== null && baseRow && valid(baseRow.change)
    ? indexChange - baseRow.change : null;

  const stockArchived = !!(current && stockDay && cnDayKey(stockDay.date) === current);
  let up = 0, down = 0, flat = 0, expected = null;
  if (stockArchived) {
    for (const r of Array.isArray(stockDay.rows) ? stockDay.rows : []) {
      if (!r || r.sector == null || String(r.sector) !== code || !valid(r.change)) continue;
      if (r.change > 0) up++;
      else if (r.change < 0) down++;
      else flat++;
    }
    const sectors = (Array.isArray(stockDay.sectors) ? stockDay.sectors : [])
      .filter(s => s && s.code != null && String(s.code) === code);
    if (sectors.length === 1 && valid(sectors[0].expected) &&
        Number.isInteger(sectors[0].expected) && sectors[0].expected > 0) {
      expected = sectors[0].expected;
    }
  }

  return {
    indexChange,
    indexReturn5: periodReturn(5),
    indexReturn10: periodReturn(10),
    indexReturn20: periodReturn(20),
    indexDiff,
    stockArchived,
    expected,
    up,
    down,
    flat,
    validCount: up + down + flat
  };
}

function cnSectorCard(row, metrics) {
  const r = row || {}, m = metrics || {};
  const n = x => valid(x) ? x : null;
  const cls = x => valid(x) ? tone(x) : "";
  const pp = x => valid(x) ? `${x > 0 ? "+" : ""}${fmt(x)}个百分点` : "—";
  const expected = valid(m.expected) && m.expected > 0 ? m.expected : null;
  const width = x => expected ? Math.min(100, 100 * Math.max(0, x || 0) / expected) : 0;
  const subtitle = m.stockArchived
    ? `申万指数 · 个股覆盖 ${m.validCount || 0}/${expected === null ? "—" : fmt(expected, 0)}`
    : "申万指数 · 个股未归档";
  const breadth = m.stockArchived
    ? `上涨 ${m.up || 0} · 平 ${m.flat || 0} · 下跌 ${m.down || 0} · 有效 ${m.validCount || 0}`
    : "个股数据未归档";

  return `<button type="button" class="sector-card cn-sector-card" data-us-sector="${esc(r.code ?? "")}">
<div class="card-top"><h3>${esc(r.name ?? r.code ?? "")}</h3><strong class="${cls(n(m.indexChange))}">${pct(n(m.indexChange))}</strong></div>
<div class="native">${subtitle}</div>
<div class="breadth" aria-label="${breadth}"><span class="green" style="width:${width(m.up)}%"></span><span class="red" style="width:${width(m.down)}%"></span></div>
<div class="card-meta"><span>${m.stockArchived?`↑ ${m.up}　↓ ${m.down}　平 ${m.flat}`:'↑ —　↓ —'}</span><span>有效 ${m.stockArchived?m.validCount:'—'}/${expected??'—'}</span></div>
<div class="card-bottom"><span>指数5日 <b class="${cls(n(m.indexReturn5))}">${pct(n(m.indexReturn5))}</b></span><span>指数20日 <b class="${cls(n(m.indexReturn20))}">${pct(n(m.indexReturn20))}</b></span><span>对比差 ${valid(m.indexDiff)?(m.indexDiff>0?'+':'')+fmt(m.indexDiff)+'pp':'—'}</span></div>
</button>`;
}

// Test: duplicate/future dates, incomplete 31-row archives, missing or nonpositive levels.
// Test: mismatched stock date, unknown/zero expected count, missing base date, and zero changes.

function marketColors(){document.body.dataset.market=state.market;const note=document.querySelector(".aside-note");if(note)note.innerHTML=(state.market==='china'?'红涨 · 绿跌':'绿涨 · 红跌')+'<br>平盘 / 缺数为灰色<br>各市场独立日期';}


let cnPeriodKey='indexChange';
const cnPeriods=[['当日','indexChange'],['5日','indexReturn5'],['10日','indexReturn10'],['20日','indexReturn20']];
function cnPeriodOverview(day,days,selected){
 const rows=(selected?[selected]:day.rows).map(r=>({row:r,m:cnCardMetrics(r,days,day.date,'',null)}));
 if(selected)return `<div class="period-strip" aria-label="行业各周期涨跌">${cnPeriods.map(([label,key])=>{const v=rows[0].m[key];return `<div><span>${label}</span><strong class="${tone(v)}">${pct(v)}</strong></div>`;}).join('')}<small>5 / 10 / 20交易日累计涨跌 · 缺数显示 —</small></div>`;
 const items=rows.map(x=>({code:x.row.code,name:x.row.name,value:x.m[cnPeriodKey]})),good=items.filter(x=>valid(x.value));
 const up=good.filter(x=>x.value>0).sort((a,b)=>b.value-a.value),down=good.filter(x=>x.value<0).sort((a,b)=>a.value-b.value),flat=good.length-up.length-down.length;
 const max=Math.max(.01,...good.map(x=>Math.abs(x.value)));
 const list=(rs,cls,offset=0)=>rs.map((r,i)=>`<button class="period-rank-row" data-us-sector="${esc(r.code)}"><span class="rank-no">${i+1+offset}</span><span class="rank-sector">${esc(r.name)}</span><span class="rank-track"><i class="${cls}" style="width:${Math.abs(r.value)/max*100}%"></i></span><strong class="${cls}">${pct(r.value)}</strong><span aria-hidden="true">›</span></button>`).join('');
 const column=(rs,cls,name)=>`<section class="period-rank-col"><div class="period-col-title"><h3 class="${cls}">${name}</h3><span>${rs.length} 个行业</span></div>${rs.length?list(rs.slice(0,5),cls):'<p class="period-empty">该周期暂无'+name+'，或样本不足</p>'}${rs.length>5?`<details class="period-more"><summary>查看其余 ${rs.length-5} 个${name}</summary>${list(rs.slice(5),cls,5)}</details>`:''}</section>`;
 return `<section class="period-board" aria-label="行业强弱对比"><div class="period-board-head"><div><h2>行业强弱</h2><span>${esc(day.date)} · ${cnPeriodKey==='indexChange'?'当日涨跌':'累计涨跌'}</span></div><div class="period-tabs" role="group" aria-label="选择行业涨跌周期">${cnPeriods.map(([label,key])=>`<button type="button" data-cn-period="${key}" aria-pressed="${key===cnPeriodKey}">${label}</button>`).join('')}</div></div><div class="period-breadth"><span class="up">上涨 ${up.length}</span><span class="down">下跌 ${down.length}</span><span>平盘 ${flat}</span><span>有效 ${good.length}/31</span></div><div class="period-ranks">${column(up,'up','上涨板块')}${column(down,'down','下跌板块')}</div><div class="period-board-foot">点击行业查看详情 · 按交易日计算，非均线${good.length<31?' · 缺数 '+(31-good.length)+' 个':''}</div></section>`;
}
document.addEventListener('click',e=>{const b=e.target.closest('[data-cn-period]');if(b&&DATA&&state.market==='china'&&cnPeriods.some(x=>x[1]===b.dataset.cnPeriod)){cnPeriodKey=b.dataset.cnPeriod;renderUS();}});
function cnQuickMatches(rows,query){
 const q=query.trim().toLowerCase();if(!q)return [];
 return rows.filter(r=>String(r.code).toLowerCase().includes(q)||String(r.name).toLowerCase().includes(q)).sort((a,b)=>{
 const exact=r=>String(r.code).toLowerCase()===q||String(r.code).slice(2)===q||r.name===query.trim();
 return Number(exact(b))-Number(exact(a))||String(a.code).localeCompare(String(b.code));
 });
}
function cnQuickResults(){
 const box=$('cnQuickResults'),input=$('cnQuickInput');if(!box||!input||!DATA)return;
 const q=input.value.trim();if(!q){box.hidden=true;box.innerHTML='';return;}
 const day=DATA.cnStocks?.days?.find(d=>d.date===usDate);box.hidden=false;
 if(!day){box.innerHTML='<p role="status">所选日期没有个股归档，请选择有数据的日期。</p>';return;}
 const hits=cnQuickMatches(day.rows||[],q);
 box.innerHTML=`<p role="status">${hits.length?`找到 ${hits.length} 只${hits.length>8?'，先显示8只，请继续输入缩小范围':''}`:'未找到股票，请核对代码或名称'} · ${esc(usDate)}</p>`+hits.slice(0,8).map(r=>`<button type="button" data-cn-quick="${esc(r.code)}"><span><b>${esc(r.name)}</b><small>${esc(r.code)} · ${esc(r.sectorName)}</small></span><span class="${tone(r.change)}">${pct(r.change)}<small>¥${fmt(r.close)}</small></span></button>`).join('');
}
function cnQuickOpen(code){
 const row=DATA.cnStocks?.days?.find(d=>d.date===usDate)?.rows.find(r=>r.code===code);if(!row)return;
 cnStockCode=code;usSelected='';state.view='stocks';renderUS();window.scrollTo(0,0);
}
document.addEventListener('input',e=>{if(e.target.id==='cnQuickInput')cnQuickResults();});
document.addEventListener('submit',e=>{if(e.target.id!=='cnQuickForm')return;e.preventDefault();cnQuickResults();const hits=$('cnQuickResults')?.querySelectorAll('[data-cn-quick]');if(hits?.length===1)cnQuickOpen(hits[0].dataset.cnQuick);});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&e.target.closest('#cnQuickForm')){$('cnQuickResults').hidden=true;$('cnQuickInput').focus();}});
document.addEventListener('click',e=>{const b=e.target.closest('[data-cn-quick]');if(b&&DATA&&state.market==='china')cnQuickOpen(b.dataset.cnQuick);else if(!e.target.closest('#cnQuickForm')&&$('cnQuickResults'))$('cnQuickResults').hidden=true;});
function cnUpdateStatus(bundle,day,stockBundle){
 const count=day.rows.filter(r=>valid(r.level)&&valid(r.change)).length;
 const stock=stockBundle?.days?.find(x=>x.date===day.date),latestStock=stockBundle?.days?.at(-1)?.date;
 const failed=bundle.status?.state==='failed',stockFailed=stockBundle?.status?.state==='failed';
 const label=count===31?'行业数据齐全':count?'行业数据部分到达':'行业指数暂缺';
 return `<div class="status ${count<31||failed||stockFailed?'warn':''}"><b>● ${label} · ${count}/31</b><span>观测日期 ${esc(day.date)} · 中国 A股</span><span>同日个股 ${stock?fmt(stock.rows.length,0)+'只':'暂缺'} · 个股最新归档 ${esc(latestStock||'暂无')}</span></div>${count<31?'<div class="notice">行业指数尚未全部到达，缺失项显示 —；已到达的同日个股与排行可正常查看，不使用昨日指数补位。</div>':''}${failed||stockFailed?`<div class="notice">最近采集：${failed?'行业接口失败；':''}${stockFailed?'个股接口失败；':''}已保留通过校验的历史数据。行业检查 ${esc(bundle.status?.checkedAt||bundle.status?.fetchedAt||'—')}；个股检查 ${esc(stockBundle?.status?.fetchedAt||'—')}。</div>`:''}`;
}
function renderUS(){
 marketColors();
 const china=state.market==='china', marketName=china?'中国 A股':'美国', count=china?31:19, source=china?'https://www.swsresearch.com/institute_sw/allIndex/releasedIndex':'https://nikkei225jp.com/nasdaq/';
 const bundle=china?DATA.cnIndustries:DATA.usIndustries, days=bundle?.days||[];
 $('heading').innerHTML=`<div class="page-head"><div><div class="eyebrow">${china?'CHINA / SHENWAN':'USA / INDUSTRY INDICES'}</div><h1>${marketName}行业指数</h1><p class="subtitle">${china?'申万 2021 · 31 个一级行业 · 官方日报':'S&P 500 与 NASDAQ 分组观察 · 来源网页快照'}</p></div>${china?'<form id="cnQuickForm" class="cn-quick-search" role="search"><label for="cnQuickInput">股票搜索</label><div class="cn-quick-field"><input id="cnQuickInput" type="search" placeholder="股票名称或代码，如紫光 / 000938" autocomplete="off"><button type="submit" class="button">搜索</button></div><div id="cnQuickResults" class="cn-quick-results" hidden></div></form>':''}</div>`;
 $('status').innerHTML='';$('controls').innerHTML='';
 document.querySelectorAll('[data-market]').forEach(b=>b.classList.toggle('active',b.dataset.market===state.market));
 document.querySelectorAll('nav [data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===state.view));
 if(!days.length){$('content').innerHTML='<section class="panel empty"><h2>等待行业数据</h2><p>尚无通过校验的该市场行业数据。</p></section>';return;}
 if(!days.some(d=>d.date===usDate))usDate=days.at(-1).date;
 const d=days.find(d=>d.date===usDate), earlier=days.filter(x=>x.date<usDate);
 if(!earlier.some(x=>x.date===usBase))usBase=earlier.at(-1)?.date||'';
 const base=days.find(x=>x.date===usBase), selected=d.rows.find(x=>x.code===usSelected);
 $('status').innerHTML=china?cnUpdateStatus(bundle,d,DATA.cnStocks):`<div class="status ${bundle.status.state==='success'?'':'warn'}"><b>${bundle.status.state==='success'?'● 已读取来源网页':'● 取数异常，保留旧快照'}</b><span>来源日期 ${esc(d.date)} · ${marketName}市场</span><span>归档 ${days.length} 日 · ${count} 个指数</span></div>`;
 $('controls').innerHTML=`<div class="toolbar"><label>观测日期<select id="usDate">${[...days].reverse().map(x=>`<option ${x.date===usDate?'selected':''}>${x.date}</option>`).join('')}</select></label><label>对比日期<select id="usBase"><option value="">无更早快照</option>${[...earlier].reverse().map(x=>`<option ${x.date===usBase?'selected':''}>${x.date}</option>`).join('')}</select></label><a href="${source}" target="_blank" rel="noopener noreferrer">查看来源网页 ↗</a></div>${china?cnPeriodOverview(d,days,selected):''}`;
 const note=china?'<div class="notice">申万官方行业指数日报；7个分组仅方便阅读，不是新增分类层级。31行业始终常显。卡片主涨跌和5／20日为申万指数表现（点位比值），不是个股等权收益；上涨／下跌家数来自同日有效个股，灰色含平盘和缺数。对比差是两日各自日涨跌幅之差。个股与成交量／成交额／涨幅排行按实际覆盖展示；原代表／活跃评分仍缺字段。</div>':'<div class="notice">来源标注的行业指数涨跌，不是行业个股等权统计。两套分类分别排名。仅保存实际获取的日期，不补造历史；代表TOP10、活跃TOP10及真实成交额尚缺。网页未提供完整时间戳和最终收盘标志，按来源日期展示快照。</div>';
 if(state.view==='help'&&china){$('content').innerHTML=note+'<section class="panel"><h2>中国市场数据口径</h2><p>来源：申万宏源研究官方指数日报。日期取源数据bargaindate，指数点位为closeindex，日涨跌幅为markup。行情日期与抓取时间分别保存；行业与个股按各自来源日期独立更新。行业缺数显示 —，重复代码或日期异常仍拒绝；已有有效数值不被缺数覆盖。北京时间每日19:17、21:17自动尝试更新。历史保留实际获取的数据与覆盖状态；仅有个股的日期不填造行业指数。</p><p>个股来源新浪行情，成交量单位股、成交额单位元。按申万指数当前成分采集；停牌或缺数不参与排行。原代表／活跃分所需的股本、复权历史及筛选条件尚未齐全；当前展示真实成交量、成交额和涨幅榜。个股历史与上榜记录从归档之日起逐日积累。</p></section>';return;}
 if(state.view==='help'){$('content').innerHTML=note+'<section class="panel"><h2>美股数据口径</h2><p>来源：nikkei225jp.com 的行业指数栏目。北京时间每天19:17、21:17随现有任务更新。来源日期与抓取时间分开保存；只显示已获取的记录。对比值为两个日期各自当日涨跌幅之差（百分点），不是区间累计收益。原站股票卖买额属于估算，未纳入本看板。</p></section>';return;}
 if(state.view==='stocks'&&china){$('content').innerHTML=cnStockPanel();return;}
 if(state.view==='stocks'){$('content').innerHTML=note+'<section class="panel"><h2>本次先接入行业数据</h2><p>个股列表与评分尚未接入。</p><a href="https://nikkei225jp.com/nasdaq/stock.php" target="_blank" rel="noopener noreferrer">查看原站个股页面 ↗</a></section>';return;}
 const card=r=>{if(china)return cnSectorCard(r,cnCardMetrics(r,days,usDate,usBase,DATA.cnStocks?.days?.find(x=>x.date===usDate)));const old=base?.rows.find(x=>x.code===r.code);return `<button class="sector-card" data-us-sector="${esc(r.code)}"><div class="card-top"><h3>${esc(r.name)}</h3><strong class="${tone(r.change)}">${pct(r.change)}</strong></div><div class="native">指数点位 ${fmt(r.level)} · 点数变化 ${fmt(r.delta)}</div><div class="native">对比日涨幅 ${pct(old?.change)} · 差 ${valid(old?.change)&&valid(r.change)?fmt(r.change-old.change)+' 个百分点':'—'}</div></button>`;};
 const groups=china?['上游资源','材料化工','制造与军工','消费','医药与公用','TMT','金融地产与综合'].map(g=>[g,g]):[['sp500','S&P 500 · 11 个行业指数'],['nasdaq','NASDAQ · 8 个分类指数']];
 let detail='';if(selected){const history=days.filter(x=>x.date<=usDate).map(x=>({date:x.date,row:x.rows.find(r=>r.code===usSelected)})).filter(x=>x.row);detail=`<section class="panel calendar-panel"><button class="button" data-us-back>← 全部行业</button><h2>${esc(selected.name)} · 指数记录</h2><div class="summary">${metric('来源当日涨跌',pct(selected.change),'百分比',tone(selected.change))}${metric('指数点位',fmt(selected.level),'不是股票价格')}${metric('已归档观察',history.length,'日；无记录不补值')}</div><h3>行业月历 · 每天的涨跌</h3><p class="chart-note">按月查看，每格显示当日涨跌；点击日期查看下方均线、成交额和榜单。</p>${industryDateTree(history,selected.code,china)}</section>`;}
 
 if(china&&selected)detail='<section class="panel"><button class="button" data-us-back>← 全部行业</button><h2>'+esc(selected.name)+'</h2></section>'+(cnStockCode?'':cnIndustryTrends(selected.code,days,usDate))+(cnStockCode?'':detail)+cnStockPanel(selected.code);
 $('content').innerHTML=(china&&!selected?cnSummary(d,DATA.cnStocks?.days?.find(x=>x.date===d.date)):'')+note+(detail||groups.map(([g,label])=>`<section class="panel"><h2>${label}</h2><div class="grid">${d.rows.filter(r=>r.group===g).sort((a,b)=>(valid(b.change)?b.change:-Infinity)-(valid(a.change)?a.change:-Infinity)).map(card).join('')}</div></section>`).join(''));
}
document.addEventListener('click',e=>{const b=e.target.closest('button');if(!DATA||!['usa','china'].includes(state.market)||!b)return;if(b.dataset.usSector){usSelected=b.dataset.usSector;renderUS();}if(b.hasAttribute('data-us-back')){usSelected='';renderUS();}});
document.addEventListener('change',e=>{if(!DATA||!['usa','china'].includes(state.market))return;if(e.target.id==='usDate'){usDate=e.target.value;usBase='';cnStockCode='';renderUS();}if(e.target.id==='usBase'){usBase=e.target.value;renderUS();}});

function render(){if(!DATA)return;marketColors();if(['usa','china'].includes(state.market)&&state.view!=='mapping'){renderUS();return;}const s=sector(),r=rowFor(state.stock);const titles={overview:'行业全景，每天有据可查',stocks:'找到你关注的股票',sector:s?title(s):'行业详情',stock:r?r.name:'个股详情',mapping:'中日美分类对照',help:'数据与口径'};$('heading').innerHTML=`<div class="page-head"><div><div class="eyebrow">${state.market==='japan'?'JAPAN / DAILY RESEARCH':state.market.toUpperCase()}</div><h1>${esc(titles[state.view])}</h1><p class="subtitle">${state.market==='japan'?'真实日线 · 33行业 · 行业与个股分层查看':'独立市场 · 行情待接入'}</p></div>${state.market==='japan'&&!['help','mapping'].includes(state.view)?jpQuickForm():'<span class="tag">已在本机解密</span>'}</div>`;$('status').innerHTML=state.market==='japan'?status():'';$('controls').innerHTML=state.market==='japan'&&!['help','mapping'].includes(state.view)?toolbar()+(['overview','sector'].includes(state.view)?jpPeriodOverview(state.view==='sector'?s:null):''):'';document.querySelectorAll('nav [data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===state.view||b.dataset.view==='overview'&&state.view==='sector'||b.dataset.view==='stocks'&&state.view==='stock'));document.querySelectorAll('[data-market]').forEach(b=>b.classList.toggle('active',b.dataset.market===state.market));renderContent();}
$('clearCache').addEventListener('click',async()=>{if(busy)return;const cleared=await DashboardVault.clear();$('gateMessage').textContent=cleared?'本机加密缓存已清除，下次进入将重新下载。':'本机没有可清除的缓存，或浏览器不允许缓存访问。';});
$('unlockForm').addEventListener('submit',unlock);$('lock').addEventListener('click',lock);$('showPassword').addEventListener('click',()=>{$('password').type=$('password').type==='password'?'text':'password';$('showPassword').textContent=$('password').type==='password'?'显示':'隐藏';});
document.addEventListener('click',e=>{if(!DATA)return;const b=e.target.closest('button');if(!b||b.disabled)return;if(b.dataset.view){usSelected='';state.page=1;state.search='';route({view:b.dataset.view});}else if(b.dataset.market){cnStockCode='';cnQuery='';usSelected='';usDate='';usBase='';state.search='';state.page=1;route({market:b.dataset.market,view:'overview'});}else if(b.dataset.sector){state.search='';state.page=1;route({view:'sector',sector:b.dataset.sector});window.scrollTo(0,0);}else if(b.dataset.stock){state.search='';route({view:'stock',stock:b.dataset.stock});window.scrollTo(0,0);}else if(b.dataset.chart)route({chart:b.dataset.chart});else if(b.dataset.page){state.page=Number(b.dataset.page);renderContent();}else if(b.dataset.step){state.page=1;route({date:dates[dates.indexOf(state.date)+Number(b.dataset.step)],base:''});}else if(b.hasAttribute('data-latest'))route({date:dates.at(-1),base:''});else if(b.dataset.historyDate)route({date:b.dataset.historyDate,base:''});});
document.addEventListener('change',e=>{if(!DATA)return;const v=e.target.value;if(e.target.id==='date'){state.page=1;route({date:v,base:''});}if(e.target.id==='base')route({base:v});if(e.target.id==='stockSort'){state.sort=v;state.page=1;renderContent();}if(e.target.id==='sectorSort'){state.sectorSort=v;renderContent();}});
document.addEventListener('input',e=>{if(DATA&&e.target.id==='search'){state.search=e.target.value;state.page=1;renderContent();}});
window.addEventListener('hashchange',readRoute);
})();
