// Focused form interaction and output checks without a browser or live data.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../tools/helpers/address-helper.html'), 'utf8');
const elements = new Map();
function element(id = '') {
  return {id, value:'', disabled:false, children:[], listeners:{}, textContent:'',
    classList:{add(){},remove(){}}, setAttribute(){}, removeAttribute(){},
    replaceChildren(...children){this.children=children;},
    add(child){this.children.push(child);}, append(...children){this.children.push(...children);},
    addEventListener(event, callback){this.listeners[event]=callback;},
    showModal(){},close(){}};
}
for (const [, id] of html.matchAll(/id="([^"]+)"/g)) elements.set(id, element(id));
elements.get('port').value = '8765';
let stored = null;
const context = vm.createContext({
  document:{getElementById:id=>{assert.ok(elements.has(id),id);return elements.get(id);},
    createElement:()=>element()},
  Option:function(name,id){this.name=name;this.value=id;},
  location:{pathname:'/setup/',hostname:'bridge.local',port:'8765'},
  localStorage:{setItem:(key,value)=>stored=value,getItem:()=>stored,removeItem:()=>stored=null},
  URL,URLSearchParams,AbortSignal,Uint8Array,console,
  fetch:async ()=>{throw Error('No interval request should be made by the setup page');},
});
let script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
script = script.replace('restore();render();loadSnapshot();', `
  globalThis.form = {render,save,restore,pickupUrl,loxoneTemplate,resetAfter,
    setAddress(){selected={district:{id:'2',name:'Bezirk Ä & _'},city:{id:'10',name:'Gemeinde St. Ö'},street:{id:'20',name:'Straße_1 / Süd? #+'},houseNumberId:'30',houseNumberName:'1'};},
    setKey(value){$('apiKey').value=value;}};
  restore();render();`);
vm.runInContext(script, context);
const form=context.form;
(()=>{
  form.setAddress();form.setKey('secret🔑');form.render();
  assert.equal(elements.get('downloadTemplate').disabled,false);
  const url=new URL(form.pickupUrl());
  assert.equal(url.searchParams.has('intervals'),false);
  assert.equal(url.searchParams.get('district_name'),'Bezirk Ä & _');
  assert.equal(url.searchParams.get('street_name'),'Straße_1 / Süd? #+');
  assert.equal(url.searchParams.has('street_id'),false);
  const xml=form.loxoneTemplate();
  assert.equal((xml.match(/<VirtualInHttpCmd\s/g)||[]).length,4);
  assert.equal((xml.match(/intervals=/g)||[]).length,0);
  assert.ok(xml.includes('&amp;'));
  assert.ok(!/Address="[^"]*&(?!(?:amp|lt|gt|quot|apos);)/.test(xml));
  assert.equal(new URL(xml.match(/Address="([^"]+)"/)[1].replaceAll('&amp;','&')).href,url.href);
  assert.ok(!stored.includes('secret')&&!stored.includes('apiKey'));
  form.resetAfter(3);form.render();assert.equal(form.ready(),false);
  assert.ok(!html.includes('intervalHeading'));
  console.log('Setup address, URL and XML checks passed');
})();
