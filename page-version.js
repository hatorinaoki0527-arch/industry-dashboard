(() => {
'use strict';
function signature(html) {
 return [...html.matchAll(/<(?:script|link)\b[^>]*\b(?:src|href)=["']([^"']+)["'][^>]*>/gi)]
  .map(m=>m[1]).filter(p=>/^[a-zA-Z0-9_.-]+\.(?:js|css)(?:\?|$)/.test(p)).sort().join('|');
}
function changed(current, incoming) { const next=signature(incoming); return Boolean(next && next!==signature(current)); }
function install(env) {
 const {document:doc,location:loc}=env;let busy=false,lastCheck=0,pending=false;
 const initial=doc.documentElement.outerHTML;
 function reload(){const url=new URL(loc.href);url.searchParams.set('ui',String(Date.now()));loc.replace(url.href);}
 async function check(force=false){
  if(busy||(!force&&Date.now()-lastCheck<300000))return;
  busy=true;lastCheck=Date.now();
  try {
   const url=new URL('index.html',loc.href);url.searchParams.set('check',String(lastCheck));
   const response=await env.fetch(url.href,{cache:'no-store',signal:AbortSignal.timeout(8000)});
   if(!response.ok||!changed(initial,await response.text()))return;
   pending=true;
   // An unlocked page keeps its key only in memory; never discard it silently.
   const retriedAt=Number(new URL(loc.href).searchParams.get('ui'))||0;
   if(!doc.getElementById('gate').hidden && Date.now()-retriedAt>60000){reload();return;}
   doc.getElementById('pageUpdate').hidden=false;
  }catch{/* Offline or a failed check must keep the current page usable. */}
  finally{busy=false;}
 }
 doc.getElementById('refreshPage').addEventListener('click',reload);
 doc.addEventListener('visibilitychange',()=>{if(doc.visibilityState==='visible')check();});
 doc.getElementById('unlockForm').addEventListener('submit',e=>{if(pending){e.preventDefault();e.stopImmediatePropagation();reload();}},true);
 env.setInterval(check,600000);check(true);
 return {check};
}
if(typeof module!=='undefined'&&module.exports)module.exports={signature,changed,install};
else if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>install(window),{once:true});
else install(window);
})();
