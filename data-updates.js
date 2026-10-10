(() => {
'use strict';
function followDate(selected,previous,next){return !selected||selected===previous?next:selected;}
function install({initialSha,refresh,notify,env=window}){
 let sha=initialSha,stopped=false,busy=false,lastCheck=0,timer;
 const schedule=delay=>{env.clearTimeout(timer);if(!stopped)timer=env.setTimeout(()=>check(true),delay);};
 async function check(force=false){
  if(stopped||busy||(!force&&Date.now()-lastCheck<60000))return;
  busy=true;lastCheck=Date.now();let failed=false;
  try{
   const url=new URL('release.json',env.location.href);url.searchParams.set('check',String(lastCheck));
   const r=await env.fetch(url.href,{cache:'no-store',signal:AbortSignal.timeout(15000)});
   if(!r.ok)throw Error('更新确认失败');const receipt=await r.json();
   if(receipt.version!==1||!/^[a-f0-9]{64}$/.test(receipt.vaultSha256))throw Error('版本清单无效');
   if(stopped)return;
   if(receipt.vaultSha256!==sha){notify('loading');const current=await refresh(receipt.vaultSha256);if(stopped)return;if(current!==receipt.vaultSha256)throw Error('新版尚未完整到达');sha=current;}
   if(!stopped)notify('current');
  }catch{failed=true;if(!stopped)notify('offline');}
  finally{busy=false;schedule(failed?60000:300000);}
 }
 const online=()=>check(true),visible=()=>{if(env.document.visibilityState==='visible')check();};
 env.addEventListener('online',online);env.document.addEventListener('visibilitychange',visible);
 check(true);
 return {check,stop(){stopped=true;env.clearTimeout(timer);env.removeEventListener('online',online);env.document.removeEventListener('visibilitychange',visible);}};
}
if(typeof module!=='undefined'&&module.exports)module.exports={install,followDate};else window.DashboardUpdates={install,followDate};
})();
