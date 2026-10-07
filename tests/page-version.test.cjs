const test=require('node:test'),assert=require('node:assert/strict'),V=require('../page-version.js');
const old='<script src="jp-market.js?v=1"></script><script src="app.js?v=1"></script>';
const latest='<script src="cn-market.js?v=2"></script><script src="jp-market.js?v=1"></script><script src="app.js?v=2"></script>';
test('an old Japanese-only page is detected even when its data are current',()=>assert.equal(V.changed(old,latest),true));
test('normal HTML or data-only changes do not reload a page',()=>{assert.equal(V.changed(latest,latest+'<p>new data</p>'),false);assert.equal(V.changed(old,'server error'),false)});
function fake(locked,fetch){let replacements=0, banner={hidden:true},events={};const env={document:{documentElement:{outerHTML:old},getElementById:id=>id==='gate'?{hidden:!locked}:id==='pageUpdate'?banner:{addEventListener:(n,f)=>events[id+n]=f},addEventListener(){}},location:{href:'https://example.com/#market=china',replace:()=>replacements++},fetch,setInterval(){}};const api=V.install(env);return {api,banner,events,count:()=>replacements};}
test('a locked stale page reloads; an unlocked page preserves its session and shows one update control',async()=>{for(const locked of [true,false]){const f=fake(locked,async()=>({ok:true,text:async()=>latest}));await new Promise(setImmediate);assert.equal(f.count(),locked?1:0);assert.equal(f.banner.hidden,locked);}});
test('offline checks preserve the page and do not claim a new version',async()=>{const f=fake(true,async()=>{throw Error('offline')});await new Promise(setImmediate);assert.equal(f.count(),0);assert.equal(f.banner.hidden,true)});
const {versionHtml}=require('../asset-versions.cjs');
test('deployment fingerprints follow file contents, not manually maintained dates',()=>{const a=versionHtml(latest,f=>'unchanged '+f),b=versionHtml(latest,f=>f==='cn-market.js'?'fixed code':'unchanged '+f);assert.equal(V.changed(a,b),true);assert.equal(V.changed(a,versionHtml(latest,f=>'unchanged '+f)),false)});

test('browser extension injection does not trigger a false page update',()=>assert.equal(V.changed('<script src="chrome-extension://example/inject.js"></script>'+latest,latest),false));
