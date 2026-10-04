const test=require('node:test'),assert=require('node:assert/strict');
const M=require('../cn-market.js');
const days=[{date:'2026-09-28',rows:[{code:'1',name:'电子',level:100,change:1}]},{date:'2026-09-29',rows:[{code:'1',name:'电子',level:null,change:null}]},{date:'2026-09-30',rows:[{code:'1',name:'电子',level:102,change:2}]}];
test('industry values use reported changes and never carry a prior day forward',()=>{const b=M.adapt({},days);assert.equal(M.sectorRows(b.history,'2026-09-29')[0].change,null);assert.equal(M.sectorRows(b.history,'2026-09-30')[0].change,2);assert.deepEqual(M.sectorRows(b.history,'2026-10-01'),[]);assert.equal(M.streak(b.history,'2026-09-30',0).count,1)});
test('Chinese heat colors and exact-date metadata',()=>{assert.match(M.color(1),/216,90,99/);assert.match(M.color(-1),/40,163,102/);const html=M.render(M.adapt({},days),'2026-09-29');assert.match(html,/最新归档 2026-09-30/);assert.match(html,/回到最新/);assert.match(html,/红涨 · 绿跌/)});
