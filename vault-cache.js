/* Stores ciphertext only. Authentication and decoded data stay in memory. */
globalThis.DashboardVault = (() => {
 'use strict';
 const CACHE='industry-dashboard-ciphertext-v1';
 function validate(v){
  if(!v||v.version!==1||v.cipher!=='AES-256-GCM'||v.kdf!=='PBKDF2-SHA256'||v.iterations!==600000||v.compression!=='gzip'||!['salt','iv','data'].every(k=>typeof v[k]==='string'&&/^[A-Za-z0-9+/]+={0,2}$/.test(v[k])))throw Error('加密文件格式异常，请稍后重试。');
  if(atob(v.salt).length!==32||atob(v.iv).length!==12||v.data.length<24)throw Error('加密文件不完整，请稍后重试。');
  return v;
 }
 async function storage(){try{return await caches.open(CACHE);}catch{return null;}}
 async function clear(){try{return await caches.delete(CACHE);}catch{return false;}}
 async function load({notify=()=>{},timeoutMs=15000,url=new URL('vault.json',location.href).href}={}){
  const cache=await storage();let cached=null,old=null;
  if(cache){try{cached=await cache.match(url);if(cached)old=validate(await cached.clone().json());}catch{cached=null;}}
  const headers={};if(old&&cached.headers.get('etag'))headers['If-None-Match']=cached.headers.get('etag');
  else if(old&&cached.headers.get('last-modified'))headers['If-Modified-Since']=cached.headers.get('last-modified');
  notify(old?'正在检查数据版本…':'首次载入，正在下载加密数据…');
  const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),timeoutMs);
  let response,raw;
  try{
   response=await fetch(url,{cache:cache?'no-store':'no-cache',headers,signal:controller.signal});
   if(response.status!==304&&response.ok){notify('正在下载新版加密数据…');raw=await response.text();}
  }catch(error){
   if(old)return {vault:old,source:'offline',savedAt:cached.headers.get('x-dashboard-cached-at'),persist:async()=>true};
   throw Error('网络连接失败，且本机没有可用缓存。请联网后重试。');
  }finally{clearTimeout(timer);}
  if(response.status===304){
   if(!old)throw Error('数据版本校验异常，请重试。');
   return {vault:old,source:'cache',persist:async()=>true};
  }
  if((response.status>=500||response.status===429)&&old)return {vault:old,source:'offline',savedAt:cached.headers.get('x-dashboard-cached-at'),persist:async()=>true};
  if(!response.ok)throw Error(`无法取得加密数据（HTTP ${response.status}），请稍后重试。`);
  let vault;try{vault=validate(JSON.parse(raw));}catch{throw Error('新版加密数据不完整，原有缓存已保留。请稍后重试。');}
  // Only the caller that successfully decrypts and parses this candidate may save it.
  return {vault,source:'network',persist:async()=>{
   if(!cache)return false;
   const savedHeaders={'content-type':'application/json','x-dashboard-cached-at':new Date().toISOString()};
   for(const name of ['etag','last-modified'])if(response.headers.get(name))savedHeaders[name]=response.headers.get(name);
   try{await cache.put(url,new Response(raw,{headers:savedHeaders}));return true;}catch{return false;}
  }};
 }
 return {load,clear};
})();
