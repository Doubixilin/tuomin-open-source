const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {randomUUID} = require('node:crypto');
const staticDir = path.join(__dirname, '../../src/tuomin_gateway/service/static');
const html = fs.readFileSync(path.join(staticDir, 'index.html'), 'utf8');
const script = fs.readFileSync(path.join(staticDir, 'shell-ui.js'), 'utf8');
const helpers = require(path.join(staticDir, 'shell-ui.js'));

function settings(extra = {}) {
  return {idle_hours:8, hotkeys:{enabled:true,redact:'alt+cmd+d',refill:'alt+cmd+r'}, dictionary_source:'', dictionary_sources:[{app_id:'',label:'通用 PII'},{app_id:'synthetic',label:'合成词典'}], autostart:{available:false,enabled:false}, onboarding_complete:true, session:{}, hotkey_errors:{}, ...extra};
}
async function settle() { for (let n = 0; n < 6; n++) await Promise.resolve(); }
function harness({native = false, demo = false, initial = settings(), request} = {}) {
  const elements = new Map(), windows = {}, documents = {}, intervals = new Map(), timers = new Map();
  const PV = {pkg:null,restored:"",lockTimer:null,lockEpoch:0};
  const requests = [], saves = [], unlocked = [], previews = [], locks = [];
  function element() {
    const classes = new Set(), handlers = {};
    return {value:'',textContent:'',innerHTML:'',hidden:false,disabled:false,checked:false,open:false,options:[],dataset:{},style:{},
      classList:{contains:c=>classes.has(c), toggle(c,value) { if(value) classes.add(c); else classes.delete(c); }},
      appendChild(o) { this.options.push(o); }, addEventListener(type, fn) { (handlers[type] ||= []).push(fn); },
      async fire(type) { for(const fn of handlers[type] || []) await fn(); },
      showModal() { this.open=true; }, close() { this.open=false; }, scrollIntoView() {},
      closest() { return this.group ||= {open:false}; },
      click() { if(this.disabled) return; if(this.dataset.tab) { for(const [key, el] of elements) if(key.startsWith('#tab-')) el.classList.toggle('active', key === '#tab-' + this.dataset.tab); } for(const fn of handlers.click || []) fn(); if(this.onclick) return this.onclick(); },
    };
  }
  const $ = id => { if(!elements.has(id)) elements.set(id,element()); return elements.get(id); };
  for(const match of html.matchAll(/id="([^"]+)"/g)) $('#' + match[1]);
  const buttons = [...html.matchAll(/data-tab="([^"]+)"/g)].map(match => { const b=element(); b.dataset.tab=match[1]; return b; });
  const $$ = selector => selector === '#tabs button' ? buttons : selector === '#shellSettings input, #shellSettings select' ? ['#shellIdle','#shellHotkeys','#shellSource','#shellRedactKey','#shellRefillKey'].map($) : [];
  $('#tab-try').classList.toggle('active',true);
  const nativeApi = {get_shell_settings:async()=>initial, save_shell_settings:async value=>{ saves.push(value); return {...initial,...value,autostart:initial.autostart}; }};
  const window = {__tuominShell:native,__tuominDemo:demo,pywebview:{api:nativeApi},addEventListener(type,fn) { windows[type]=fn; }};
  const document = {hidden:false, body:element(), querySelector(selector) { const tab=selector.match(/data-tab="([^"]+)"/); return tab ? buttons.find(b=>b.dataset.tab===tab[1]) : $(selector); }, addEventListener(type,fn) { documents[type]=fn; }};
  const api = async(method,route,body) => { requests.push({method,route,body}); return request ? request(method,route,body) : {calls:[]}; };
  const context = vm.createContext({window,document,$,$$,api,location:{origin:'http://127.0.0.1:8765'},navigator:{clipboard:{writeText:async()=>{}}},Option:function(label,value){this.textContent=label;this.value=value;},setInterval(fn,ms){intervals.set(ms,fn);},setTimeout(fn,ms){const id=timers.size+1;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);},PV,esc:helpers.escape,confirm:()=>true,toast(){},renderPreview:(...args)=>previews.push(args)});
  // Execute the actual shared lock/render/ordinary unlock path, not a lock stub.
  vm.runInContext(html.slice(html.indexOf('function lockMapping(note)'),html.indexOf('// --- records tab ---')),context);
  const actualLock=context.lockMapping, actualShow=context.showUnlockedMapping;
  context.lockMapping=note=>{locks.push(note);actualLock(note);};
  context.showUnlockedMapping=data=>{unlocked.push(data);actualShow(data);};
  vm.runInContext(script,context);
  return {$,window,document,requests,saves,unlocked,previews,locks,intervals,PV,
    event:async(type,detail)=>{ if(windows[type]) await windows[type]({detail}); await settle(); },
    select:tab=>window.TuominShellUI.showTab(tab), visibility:async hidden=>{document.hidden=hidden;await documents.visibilitychange();await settle();}};
}

test('three sidebar groups retain all original workbench tabs and shared styling', () => {
  const nav = html.slice(html.indexOf('<nav class="tabs"'), html.indexOf('</nav>', html.indexOf('<nav class="tabs"')));
  assert.deepEqual([...nav.matchAll(/<summary>(.*?)<\/summary>/g)].map(m=>m[1]), ['工作台','接入','管理']);
  assert.deepEqual([...nav.matchAll(/data-tab="([^"]+)"/g)].map(m=>m[1]), ['home','try','files','refill','answer','preview','records','onboard','dict','apps','settings']);
  assert.match(nav, /<details class="tab-group" open><summary>工作台/);
  assert.match(html, /<title>Tuomin 本地脱敏工作台<\/title>/);
  assert.match(html, /href="\.\/tuomin-theme.css"/);
  assert.match(html, /src="\.\/shell-ui.js"/);
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);
  assert.equal(new Set(ids).size,ids.length);
});

test('integration URLs encode application IDs and use the actual masked route', () => {
  const rows = helpers.endpoints('http://127.0.0.1:8765','合成 /?#');
  assert.deepEqual(rows.map(r=>r[0]),['masked','demo','auto']);
  for(const [,url] of rows) { const parsed = new URL(url); assert.equal(parsed.search,''); assert.equal(parsed.hash,''); assert.ok(parsed.pathname.includes(encodeURIComponent('合成 /?#'))); }
  assert.ok(rows[0][1].endsWith('/v1/chat/completions'));
  assert.ok(!rows[0][1].includes('/masked/'));
});

test('native settings accept default, never, custom idle and disabled shortcuts', () => {
  const values = {idle:'8',source:'',enabled:true,redact:' alt+cmd+d ',refill:'alt+cmd+r',autostartAvailable:false};
  assert.deepEqual(helpers.settingsPayload(values),{idle_hours:8,dictionary_source:'',hotkeys:{enabled:true,redact:'alt+cmd+d',refill:'alt+cmd+r'}});
  assert.equal(helpers.settingsPayload({...values,idle:'never'}).idle_hours,null);
  assert.equal(helpers.settingsPayload({...values,idle:'custom',customIdle:'0.5'}).idle_hours,0.5);
  const off = helpers.settingsPayload({...values,enabled:false,redact:'',refill:'',autostartAvailable:true,autostart:true});
  assert.equal(off.hotkeys.enabled,false); assert.equal(off.autostart,true);
  for(const customIdle of ['',0,-1,721,'not-a-number']) assert.throws(()=>helpers.settingsPayload({...values,idle:'custom',customIdle}));
});

test('call ledger renders only aggregate fields and escapes application metadata', () => {
  const unrelated = randomUUID();
  const rendered = helpers.ledger([{ts:123,entry:'clipboard',app_id:'<synthetic>',duration_ms:4,char_count:20,label_counts_json:{PERSON:2},blocked:true,text:unrelated,original_value:unrelated}]);
  assert.match(rendered,/clipboard/); assert.match(rendered,/&lt;synthetic&gt;/); assert.match(rendered,/PERSON:2/); assert.match(rendered,/⛔/);
  assert.ok(!rendered.includes(unrelated));
});

test('browser retains the token field while shell hides it and shows current session', async () => {
  const browser = harness(); await settle(); assert.equal(browser.$('#adminTokenBar').hidden,false); assert.equal(browser.requests.length,0);
  const native = harness({native:true,initial:settings({session:{started_at:123,segment_count:3,entity_count:7}})}); await settle();
  assert.equal(native.$('#adminTokenBar').hidden,true); assert.match(native.$('#clipboardSummary').textContent,/3 段 · 7 个实体/);
  assert.equal(native.$('#shellAutostart').disabled,true);
});

test('demo opens file workbench with persistent amber notice and read-only dictionary', async () => {
  const h = harness({native:true,demo:true}); await settle();
  assert.equal(h.$('#tab-files').classList.contains('active'),true);
  assert.equal(h.$('#demoBanner').hidden,false); assert.equal(h.$('#addBtn').disabled,true); assert.equal(h.$('#dictReadonly').hidden,false);
  assert.equal(h.$('#w_apply').disabled,true); assert.equal(h.$('#w_verify').disabled,true); assert.equal(h.$('#workbuddyDemoHint').hidden,false);
  h.select('dict'); await h.intervals.get(5000)(); assert.equal(h.$('#tab-dict').classList.contains('active'),true);
});

test('onboarding defaults to general PII without any license step', async () => {
  const h = harness({native:true,initial:settings({onboarding_complete:false})}); await settle();
  assert.equal(h.$('#shellOnboarding').open,true); assert.equal(h.$('#onboardSource').value,''); assert.equal(html.includes('onboardLicense'), false);
  await h.$('#finishShellOnboarding').onclick();
  assert.equal(h.saves[0].dictionary_source,''); assert.equal(h.saves[0].onboarding_complete,true); assert.equal(h.$('#shellOnboarding').open,false);
});

test('native preference polling preserves edits until explicit save', async () => {
  const h=harness({native:true}); await settle(); h.$('#shellRedactKey').value='ctrl+cmd+d'; await h.$('#shellRedactKey').fire('input');
  await h.intervals.get(5000)(); assert.equal(h.$('#shellRedactKey').value,'ctrl+cmd+d');
  h.$('#shellIdle').value='never'; await h.$('#saveShellSettings').onclick();
  assert.equal(h.saves[0].idle_hours,null); assert.equal(h.saves[0].hotkeys.redact,'ctrl+cmd+d');
});

test('safe notification details navigate to preview and render residual placeholders without answer text', async () => {
  const h=harness({native:true}); await settle();
  await h.event('tuomin-shell-open',{tab:'preview',redacted_text:'<ORG_001>',blocked_labels:['CREDENTIAL']});
  assert.equal(h.previews[0][0],'<ORG_001>'); assert.match(h.$('#pv_msg').textContent,/CREDENTIAL/);
  const answer=randomUUID();
  await h.event('tuomin-shell-open',{tab:'answer',error_types:['unknown_placeholder'],error_count:1,unknown_placeholders:['<ORG_999>'],text:answer});
  assert.match(h.$('#an_diff').textContent,/<ORG_999>/); assert.ok(!h.$('#an_diff').textContent.includes(answer)); assert.equal(h.$('#an_text').value,'');
  assert.ok(!h.requests.some(r=>r.route.includes('refill')));
});

test('clipboard mapping reveals through the authenticated mapping route without any passphrase', async () => {
  const h=harness({native:true,request:async()=>({entries:[]})}); await settle();
  await h.event('tuomin-shell-open',{tab:'original',clipboard_mapping:true,namespace_id:'synthetic /namespace'});
  await h.$('#clipboardMapUnlock').onclick();
  assert.equal(h.requests.length,1);
  assert.equal(h.requests[0].route,'/admin/clipboard/namespaces/synthetic%20%2Fnamespace/mapping');
  assert.equal(h.unlocked.length,1); assert.equal(h.saves.length,0);
  assert.equal(html.includes('clipboardMapPass'), false);
});

test('mapping responses cannot reopen original values after leaving the page', async () => {
  let resolve;
  const h=harness({native:true,request:async(method,route)=>route.endsWith('/package') ? {package:{ciphertext:randomUUID()}} : new Promise(r=>{resolve=r;})}); await settle();
  await h.event('tuomin-shell-open',{tab:'original',clipboard_mapping:true,namespace_id:randomUUID()});
  const pending=h.$('#clipboardMapUnlock').onclick(); await settle();
  h.select('files'); resolve({entries:[]}); await pending;
  assert.equal(h.unlocked.length,0); assert.equal(h.$('#clipboardMapping').hidden,true);
});

test('call ledger polls only while integration is visible and lock clears on window blur', async () => {
  const h=harness(); await settle(); await h.intervals.get(3000)(); assert.equal(h.requests.length,0);
  h.select('onboard'); await settle(); assert.equal(h.requests.length,1);
  await h.visibility(true); await h.intervals.get(3000)(); assert.equal(h.requests.length,1);
  await h.event('blur'); assert.match(h.locks.at(-1),/窗口离开锁定/);
});

test('explicit lock also cancels a pending clipboard mapping reveal', async () => {
  let resolve;
  const h=harness({native:true,request:async(method,route)=>route.endsWith('/package') ? {package:{ciphertext:randomUUID()}} : new Promise(r=>{resolve=r;})}); await settle();
  await h.event('tuomin-shell-open',{tab:'original',clipboard_mapping:true,namespace_id:randomUUID()});
  const pending=h.$('#clipboardMapUnlock').onclick(); await settle();
  h.$('#pv_lock').click(); resolve({entries:[]}); await pending;
  assert.equal(h.unlocked.length,0);
});

for (const cancel of ['lock','tab','hidden','blur']) {
  test('ordinary recovery-package mapping does not reveal after ' + cancel, async () => {
    let resolve;
    const h=harness({request:async()=>new Promise(r=>{resolve=r;})}); await settle();
    h.select('preview'); h.PV.pkg={ciphertext:randomUUID()}; h.$('#pv_pass').value=randomUUID();
    const pending=h.$('#pv_unlock').onclick(); await settle();
    assert.equal(h.$('#pv_lock').disabled,false);
    if (cancel === 'lock') h.$('#pv_lock').click();
    if (cancel === 'tab') h.select('files');
    if (cancel === 'hidden') await h.visibility(true);
    if (cancel === 'blur') await h.event('blur');
    resolve({entries:[{placeholder:'<ORG_001>',label:'ORG',original_value:randomUUID()}]}); await pending;
    assert.equal(h.unlocked.length,0); assert.equal(h.$('#pv_map').innerHTML,'');
  });
}

for (const cancel of ['hidden','blur']) {
  test('clipboard mapping does not reveal after ' + cancel, async () => {
    let resolve;
    const h=harness({native:true,request:async(method,route)=>route.endsWith('/package') ? {package:{ciphertext:randomUUID()}} : new Promise(r=>{resolve=r;})}); await settle();
    await h.event('tuomin-shell-open',{tab:'original',clipboard_mapping:true,namespace_id:randomUUID()});
    const pending=h.$('#clipboardMapUnlock').onclick(); await settle();
    if (cancel === 'hidden') await h.visibility(true); else await h.event('blur');
    resolve({entries:[{placeholder:'<ORG_001>',label:'ORG',original_value:randomUUID()}]}); await pending;
    assert.equal(h.unlocked.length,0); assert.equal(h.$('#pv_map').innerHTML,'');
  });
}

test('ordinary package mapping displays only in the current visible unlocked epoch', async () => {
  const h=harness({request:async()=>({entries:[]})}); await settle();
  h.select('preview'); h.PV.pkg={ciphertext:randomUUID()}; h.$('#pv_pass').value=randomUUID();
  await h.$('#pv_unlock').onclick();
  assert.equal(h.unlocked.length,1); assert.match(h.$('#pv_lock_state').textContent,/90 秒/);
  h.$('#pv_lock').click(); assert.equal(h.$('#pv_map').innerHTML,'');
});

test('a new clipboard unlock invalidates an older ordinary package unlock', async () => {
  const pending=[];
  const h=harness({native:true,request:async(method,route)=>route.endsWith('/package') ? {package:{ciphertext:randomUUID()}} : new Promise(resolve=>pending.push(resolve))}); await settle();
  h.select('preview'); h.PV.pkg={ciphertext:randomUUID()}; h.$('#pv_pass').value=randomUUID();
  const oldRequest=h.$('#pv_unlock').onclick(); await settle();
  await h.event('tuomin-shell-open',{tab:'original',clipboard_mapping:true,namespace_id:randomUUID()});
  const newRequest=h.$('#clipboardMapUnlock').onclick(); await settle();
  pending[1]({entries:[]}); await newRequest;
  pending[0]({entries:[{placeholder:'<ORG_001>',label:'ORG',original_value:randomUUID()}]}); await oldRequest;
  assert.equal(h.unlocked.length,1); assert.equal(h.unlocked[0].entries.length,0);
});

test('missing dictionary source is displayed without silently selecting general PII', async () => {
  const h=harness({native:true,initial:settings({dictionary_source:'removed-synthetic',dictionary_error:'词典来源不可用，请重新选择'})}); await settle();
  assert.equal(h.$('#shellSource').value,'removed-synthetic');
  assert.equal(h.$('#shellSource').options.find(o=>o.value==='removed-synthetic').disabled,true);
  assert.match(h.$('#shellSettingsMessage').textContent,/词典来源不可用/);
  assert.equal(h.saves.length,0);
  h.$('#shellSource').value=''; await h.$('#saveShellSettings').onclick();
  assert.equal(h.saves.length,1); assert.equal(h.saves[0].dictionary_source,'');
});
