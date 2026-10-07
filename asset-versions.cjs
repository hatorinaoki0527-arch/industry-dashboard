'use strict';
const crypto=require('node:crypto');
function versionHtml(html,read){return html.replace(/\b(src|href)="([a-zA-Z0-9_.-]+\.(?:js|css))(?:\?[^"\n]*)?"/g,(_,attr,file)=>`${attr}="${file}?v=${crypto.createHash('sha256').update(read(file)).digest('hex').slice(0,16)}"`);}
module.exports={versionHtml};
if(require.main===module){const fs=require('node:fs'),path=require('node:path'),root=process.argv[2];if(!root)throw Error('Missing site directory');const index=path.join(root,'index.html');fs.writeFileSync(index,versionHtml(fs.readFileSync(index,'utf8'),f=>fs.readFileSync(path.join(root,f))));}
