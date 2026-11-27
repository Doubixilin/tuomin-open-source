const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../../src/tuomin_gateway/service/static/index.html'), 'utf8');
function harness(initial) {
  const elements = new Map();
  function element() {
    return { value: '', options: [], textContent: '',
      set innerHTML(value) { this.options=[]; this.value=''; },
      appendChild(option) { this.options.push(option); if(this.options.length===1) this.value=option.value; },
      prepend(option) { this.options.unshift(option); },
      addEventListener(event, callback) { this[event]=callback; } };
  }
  const $ = id => { if(!elements.has(id)) elements.set(id, element()); return elements.get(id); };
  let apps=initial;
  const requests=[];
  const context=vm.createContext({ $, document: { createElement: element },
    Option: function(text,value) { this.textContent=text; this.value=value; },
    api: async(method,route) => { requests.push([method,route]); return {apps}; },
    renderAppsTable() {}, wbFillApps() {}, loadDict: async()=>{} });
  const fill = html.match(/function fillApps\(sel, apps\) \{[^\n]+/)[0];
  const selection = html.slice(html.indexOf('const DEFAULT_BIND_APP'),html.indexOf('$("#dictApp").onchange = loadDict;'));
  vm.runInContext(fill+'\n'+selection,context);
  return { $, requests, load: ()=>vm.runInContext('loadAppsIntoSelectors()',context),
    setApps(values) { apps=values; }, choose(value) { $('#f_app').value=value; $('#f_app').change(); } };
}
const app = app_id => ({app_id, has_dictionary:true});
test('workbench defaults to existing production only on first initialization', async()=>{
  const h=harness([app('secondary'),app('production')]); await h.load();
  assert.equal(h.$('#f_app').value,'production');
  assert.equal(h.$('#f_app').options[0].value,'');
  h.choose(''); await h.load(); assert.equal(h.$('#f_app').value,'');
  h.choose('secondary'); await h.load(); assert.equal(h.$('#f_app').value,'secondary');
  assert.ok(h.requests.every(([method,route])=>method==='GET' && route==='/admin/apps'));
});
test('workbench never creates or later auto-selects a missing production app', async()=>{
  const h=harness([app('secondary')]); await h.load(); assert.equal(h.$('#f_app').value,'');
  h.setApps([app('secondary'),app('production')]); await h.load();
  assert.equal(h.$('#f_app').value,'');
});
test('workbench falls back to unbound with a notice after a selected app disappears', async()=>{
  const h=harness([app('production')]); await h.load(); h.setApps([]); await h.load();
  assert.equal(h.$('#f_app').value,'');
  assert.match(h.$('#f_app_notice').textContent,/原绑定应用已不存在/);
  assert.equal(h.$('#f_app').options.length,1);
});
test('dictionary selection remains explicit across refreshes and app removal', async()=>{
  const h=harness([app('first'),app('secondary')]); await h.load();
  h.$('#dictApp').value='secondary'; await h.load();
  assert.equal(h.$('#dictApp').value,'secondary');
  assert.equal(h.$('#dictCurrent').textContent,'当前编辑：secondary 的词典');
  h.setApps([app('first')]); await h.load();
  assert.equal(h.$('#dictApp').value,'');
  assert.match(h.$('#dictCurrent').textContent,/尚未选择应用/);
  h.setApps([app('first'),app('secondary')]); await h.load();
  assert.equal(h.$('#dictApp').value,'');
});
test('late dictionary responses cannot replace the explicitly selected app contents', async()=>{
  const elements = new Map();
  const $ = id => { if(!elements.has(id)) elements.set(id,{value:'',innerHTML:'',textContent:''}); return elements.get(id); };
  let resolveOld, reachedOld;
  const oldRequested = new Promise(resolve=>{reachedOld=resolve;});
  const context=vm.createContext({$, $$:()=>[], window:{}, esc:String,
    api:async(method,route)=>{
      if(route==='/admin/apps/first/dictionary') return new Promise(resolve=>{resolveOld=resolve;reachedOld();});
      if(route.endsWith('/dictionary')) return {entries:[]};
      return {entry:{profile:'file_workbench'}};
    }});
  vm.runInContext(html.slice(html.indexOf('async function loadDict()'),html.indexOf('async function addEntry()')),context);
  $('#dictApp').value='first'; const pending=vm.runInContext('loadDict()',context);
  await oldRequested;
  $('#dictApp').value='secondary'; await vm.runInContext('loadDict()',context);
  const current=$('#dictArea').innerHTML;
  resolveOld({entries:[{entry_id:'synthetic',canonical_value:'stale synthetic',label:'ORG'}]}); await pending;
  assert.equal($('#dictCurrent').textContent,'当前编辑：secondary 的词典');
  assert.equal($('#dictArea').innerHTML,current);
});
