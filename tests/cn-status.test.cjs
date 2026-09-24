const {test}=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../app.js'),'utf8');
const ctx={valid:x=>typeof x==='number'&&Number.isFinite(x),fmt:x=>String(x),esc:x=>String(x).replaceAll('<','&lt;')};
vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf('function cnUpdateStatus('),source.indexOf('function renderUS(){')),ctx);
test('partial indices and same-date stocks have separate coverage',()=>{const day={date:'2026-09-21',rows:[{level:100,change:1},{level:null,change:null}]};const html=ctx.cnUpdateStatus({status:{state:'partial'}},day,{days:[{date:day.date,rows:[{},{}]}],status:{state:'partial'}});assert.match(html,/1\/31/);assert.match(html,/同日个股 2只/);assert.match(html,/缺失项显示 —/);assert.doesNotMatch(html,/取数异常/)});
test('different stock date is never described as same-day',()=>{const html=ctx.cnUpdateStatus({status:{state:'failed',fetchedAt:'check'}},{date:'2026-09-21',rows:[]},{days:[{date:'2026-09-18',rows:[{}]}],status:{state:'success'}});assert.match(html,/同日个股 暂缺/);assert.match(html,/个股最新归档 2026-09-18/);assert.match(html,/行业接口失败/)});
test('all missing industry values are unavailable, never zero breadth',()=>{
 const c={...ctx,cnCardMetrics:()=>({indexChange:null})};vm.createContext(c);
 vm.runInContext("let cnPeriodKey='indexChange';"+source.slice(source.indexOf('function cnPeriodOverview('),source.indexOf("document.addEventListener('click',e=>{const b=e.target.closest('[data-cn-period]')")),c);
 const day={date:'2026-09-22',rows:[{code:'a',name:'行业',level:null,change:null}]};
 const html=c.cnPeriodOverview(day,[{date:'2026-09-21',rows:[{level:100,change:1}]},day]);
 assert.match(html,/暂无可计算数据/);assert.match(html,/2026-09-21/);assert.doesNotMatch(html,/上涨 0|下跌 0|平盘 0/);
});
test('stock sector coverage is separate from official index coverage and same-date only',()=>{
 const c={...ctx};vm.createContext(c);vm.runInContext(source.slice(source.indexOf('function cnSummaryStats('),source.indexOf('function cnExclusionNotice(')),c);
 const day={date:'2026-09-23',rows:[{code:'a',level:null,change:null},{code:'b',level:100,change:null}]};
 const stocks={date:day.date,rows:[{code:'1',sector:'a',change:1},{code:'2',sector:'b',change:-1},{code:'3',sector:'unknown',change:1}],sectors:[]};
 let m=c.cnSummaryStats(day,stocks);assert.equal(m.industries,0);assert.equal(m.stockIndustries,2);
 m=c.cnSummaryStats(day,{...stocks,date:'2026-09-22'});assert.equal(m.stockIndustries,0);
 m=c.cnSummaryStats(day,{...stocks,rows:[...stocks.rows,stocks.rows[0]]});assert.equal(m.stockIndustries,1);
});
