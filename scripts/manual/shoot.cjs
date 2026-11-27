// Drive headless Edge over CDP to capture the real Tuomin workbench UI.
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const [,, N, ONLY] = process.argv;
const data = path.join(N, 'manual-data');
const base = fs.readFileSync(path.join(data, 'instance.url'), 'utf8').trim().replace(/\/ui\/$/, '');
const token = fs.readFileSync(path.join(data, 'admin-token'), 'utf8').trim();
const seed = JSON.parse(fs.readFileSync(path.join(N, 'manual-seed.json'), 'utf8'));
const OUT = path.join(N, 'shots');
const HOME = path.join(N, 'manual-home');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const port = 9339;
const edge = spawn('/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge', [
  '--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${path.join(N, 'edge-profile')}`,
  '--no-first-run', '--no-default-browser-check', '--hide-scrollbars', '--lang=zh-CN', '--window-size=1280,860', 'about:blank'], { stdio: 'ignore' });

let ws, nextId = 0; const pending = new Map();
function send(method, params = {}) {
  return new Promise((resolve, reject) => { const id = ++nextId; pending.set(id, { resolve, reject }); ws.send(JSON.stringify({ id, method, params })); });
}
async function evaluate(expr) {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error('eval failed: ' + (r.exceptionDetails.exception && r.exceptionDetails.exception.description || expr.slice(0, 80)));
  return r.result.value;
}
async function waitFor(expr, timeout = 30000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { try { if (await evaluate(expr)) return true; } catch {} await sleep(150); }
  throw new Error('timeout waiting for ' + expr);
}
const now = Math.floor(Date.now() / 1000);
function snapshot(extra = {}) {
  return { idle_hours: 8, hotkeys: { enabled: true, redact: 'alt+cmd+d', refill: 'alt+cmd+r', redact_selection: 'ctrl+alt+d', redact_paste: 'f13', refill_paste: 'f14' },
    dictionary_source: 'workbench', onboarding_complete: true,
    session: { namespace_id: 'ns_demo', started_at: now - 3700, updated_at: now - 60, segment_count: 4, entity_count: 9 },
    dictionary_sources: [{ app_id: '', label: '不使用词典（仅规则识别）' }, { app_id: 'workbench', label: '本机工作台' }, { app_id: 'contract_review', label: '合同评审助手' }],
    hotkey_actions: { redact: '脱敏剪贴板', refill: '回填剪贴板', redact_selection: '脱敏选中文字', redact_paste: '选中脱敏并替换', refill_paste: '选中回填并替换' },
    hotkey_display: { redact: '⌥⌘D', refill: '⌥⌘R', redact_selection: '⌃⌥D', redact_paste: 'F13', refill_paste: 'F14' },
    accessibility: { available: true, trusted: false }, autostart: { available: true, enabled: true },
    hotkey_errors: {}, demo: false, dictionary_error: null,
    mcp_command: '/Applications/Tuomin.app/Contents/MacOS/tuomin-workbench --mcp-stdio', ...extra };
}
function bootstrap(native, extra) {
  // Same state the native window's bootstrap creates; the bridge is a stand-in.
  return `localStorage.setItem('tuomin_admin_token', ${JSON.stringify(token)});
  ${native ? `window.__tuominShell = true; const __snap = ${JSON.stringify(snapshot(extra))};
  window.pywebview = { api: { get_shell_settings: async () => __snap, save_shell_settings: async () => __snap,
    clipboard_action: async () => true, save_file: async () => ({cancelled: true}), reveal_file: async () => true,
    accessibility_action: async () => __snap.accessibility } };` : ''}`;
}
let scriptId = null;
async function open(native = true, extra = {}) {
  if (scriptId) await send('Page.removeScriptToEvaluateOnNewDocument', { identifier: scriptId });
  scriptId = (await send('Page.addScriptToEvaluateOnNewDocument', { source: bootstrap(native, extra) })).identifier;
  await setSize(1280, 860);
  await send('Page.navigate', { url: base + '/ui/' });
  await waitFor(`document.readyState === 'complete' && !!document.querySelector('#tabs')`);
  await sleep(900);
}
async function setSize(width, height) {
  await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 2, mobile: false });
}
async function tab(name) { await evaluate(`document.querySelector('#tabs button[data-tab="${name}"]').click(), true`); await sleep(700); }
const HOME_RE = HOME.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
async function tidy() {
  // Show the paths an installed copy shows (temp home → ~, dev command → app bundle).
  await evaluate(`(() => {
    const fixes = [[new RegExp(${JSON.stringify(HOME_RE)}, 'g'), '~'],
      [/\\/Users\\/[^\\s"]*\\/\\.venv\\/bin\\/python \\/Users\\/[^\\s"]*\\/packaging\\/launcher\\.py/g, '/Applications/Tuomin.app/Contents/MacOS/tuomin-workbench'],
      [/127\\.0\\.0\\.1:\\d{4,5}/g, '127.0.0.1:8765']];
    const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n; (n = walk.nextNode());) { let t = n.nodeValue; for (const [re, to] of fixes) t = t.replace(re, to); if (t !== n.nodeValue) n.nodeValue = t; }
    return true; })()`);
}
async function shot(name, selector, { pad = 16, full = false, height } = {}) {
  if (ONLY && !name.startsWith(ONLY)) return;
  await tidy();
  if (full) {
    await setSize(1280, height || 860); await sleep(300);
    const { data } = await send('Page.captureScreenshot', { format: 'webp', quality: 92 });
    fs.writeFileSync(path.join(OUT, name + '.webp'), Buffer.from(data, 'base64'));
  } else {
    await setSize(1280, 3200); await sleep(350);
    const r = await evaluate(`(() => { const el = document.querySelector(${JSON.stringify(selector)}); if (!el) return null; const b = el.getBoundingClientRect(); return { x: b.left, y: b.top, w: b.width, h: b.height }; })()`);
    if (!r) throw new Error('missing ' + selector);
    const clip = { x: Math.max(0, r.x - pad), y: Math.max(0, r.y - pad), width: r.w + pad * 2, height: Math.min(r.h + pad * 2, 3150), scale: 1 };
    const { data } = await send('Page.captureScreenshot', { format: 'webp', quality: 92, clip });
    fs.writeFileSync(path.join(OUT, name + '.webp'), Buffer.from(data, 'base64'));
    await setSize(1280, 860);
  }
  console.log('shot', name);
}
async function setFile(selector, file) {
  const { root } = await send('DOM.getDocument', { depth: -1 });
  const { nodeId } = await send('DOM.querySelector', { nodeId: root.nodeId, selector });
  await send('DOM.setFileInputFiles', { nodeId, files: [file] });
  await evaluate(`document.querySelector(${JSON.stringify(selector)}).dispatchEvent(new Event('change')), true`);
}
async function main() {
  let target;
  for (let i = 0; i < 100; i++) { try { target = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(t => t.type === 'page'); if (target) break; } catch {} await sleep(150); }
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise(r => ws.addEventListener('open', r));
  ws.addEventListener('message', e => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result); } });
  await send('Page.enable'); await send('Runtime.enable'); await send('DOM.enable');
  await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: 'light' }] });
  await send('Page.setDownloadBehavior', { behavior: 'deny' }).catch(() => {});

  // 1 home + 2 onboarding
  await open(true); await shot('01-home', null, { full: true });
  await open(true, { onboarding_complete: false }); await waitFor(`document.querySelector('#shellOnboarding').open`); await sleep(400);
  await shot('02-onboarding', '#shellOnboarding', { pad: 0 });

  await open(true);
  // 3 text redaction
  await tab('try');
  await evaluate(`document.querySelector('[data-sample="contract"]').click(), true`); await sleep(300);
  await evaluate(`document.querySelector('#t_run').click(), true`);
  await waitFor(`/<[A-Z_]+_\\d{3}>/.test(document.querySelector('#t_after').textContent)`);
  await sleep(500); await shot('03-try', '#tab-try');

  // 4 file redaction (parse + egress panel)
  await tab('files');
  await setFile('#f_file', path.resolve('demo/samples/synthetic-contract.docx'));
  await evaluate(`(document.querySelector('#f_app').value = 'workbench', document.querySelector('#f_app').dispatchEvent(new Event('change')), true)`);
  await evaluate(`document.querySelector('#f_run').click(), true`);
  await waitFor(`/<[A-Z_]+_\\d{3}>/.test(document.querySelector('#f_out').textContent)`, 60000);
  await sleep(600); await shot('04-files', '#tab-files .card');

  // 5 file refill
  await tab('refill'); await sleep(500); await shot('05-refill', '#tab-refill .card');

  // 6 answer refill (document task) and 6b (agent session)
  await tab('answer');
  const contract = seed.tasks['synthetic-contract.docx'];
  await waitFor(`Array.from(document.querySelector('#an_task').options).some(o => o.value === ${JSON.stringify(contract)})`);
  const answer = '审阅结论：<ORG_001> 与 <ORG_002> 签订的合同 <CONTRACT_ID_001>（金额 <AMOUNT_001>）付款条款基本完整。建议由 <PERSON_001> 在签署前补充验收标准，并与 <PERSON_002> 确认违约责任。';
  await evaluate(`(document.querySelector('#an_task').value = ${JSON.stringify(contract)}, document.querySelector('#an_text').value = ${JSON.stringify(answer)}, document.querySelector('#an_text').dispatchEvent(new Event('input')), true)`);
  await evaluate(`document.querySelector('#an_run').click(), true`);
  await waitFor(`document.querySelector('#an_out').textContent.includes('虚构星橙工程有限公司')`);
  await sleep(400); await shot('06-answer', '#tab-answer .card');
  const agentAnswer = '要点：' + seed.agent_masked[0].replace('请审阅：', '') + ' 需要在 30 日内由 <PERSON_002> 书面确认付款节点。';
  await evaluate(`(document.querySelector('#an_task').value = 'agent:' + ${JSON.stringify(seed.session)}, document.querySelector('#an_text').value = ${JSON.stringify(agentAnswer)}, true)`);
  await evaluate(`document.querySelector('#an_run').click(), true`);
  await waitFor(`document.querySelector('#an_out').textContent.includes('许星舟')`);
  await sleep(400); await shot('06b-answer-agent', '#tab-answer .card');

  // 7 preview with unlocked mapping
  await tab('preview');
  await waitFor(`Array.from(document.querySelector('#pv_task').options).some(o => o.value === ${JSON.stringify(contract)})`);
  await evaluate(`(document.querySelector('#pv_task').value = ${JSON.stringify(contract)}, document.querySelector('#pv_task_unlock').click(), true)`);
  await sleep(1500); await shot('07-preview', '#tab-preview');

  // 8 records
  await tab('records'); await sleep(900); await shot('08-records', '#tab-records .card');

  // 9 tools: agent card + self-test, endpoints, WorkBuddy
  await tab('onboard');
  await waitFor(`document.querySelectorAll('#agentClients .agent-client').length >= 5`);
  await evaluate(`document.querySelector('#agentSelftest').click(), true`);
  await waitFor(`document.querySelector('#agentSelftestOut').textContent.includes('自检通过') || document.querySelector('#agentSelftestOut').textContent.includes('未通过')`, 90000);
  await sleep(500); await shot('09-agents', '#agentCard');
  await shot('09b-endpoints', '#integrationEndpoints');
  await evaluate(`(document.querySelector('#w_app').closest('.card').id = 'wbCard', true)`);
  await evaluate(`document.querySelector('#w_status').click(), true`);
  await waitFor(`document.querySelector('#w_result table') !== null || document.querySelector('#w_gateway').textContent.includes('未找到')`);
  await sleep(400);
  await shot('09c-workbuddy', '#wbCard');

  // 10 dictionary: entries, import preview, learn
  await tab('dict');
  await evaluate(`(document.querySelector('#dictApp').value = 'workbench', document.querySelector('#dictApp').dispatchEvent(new Event('change')), true)`);
  await waitFor(`document.querySelector('#dictArea table') !== null`);
  await evaluate(`(document.querySelector('#di_text').value = ${JSON.stringify('虚构北辰物流有限公司,供应商,北辰物流\n周若川,人名\n滨江示范园区改造项目,项目,滨江一期\n市,单位\n海岳,ORG')}, true)`);
  await evaluate(`document.querySelector('#di_preview').click(), true`);
  await waitFor(`document.querySelector('#di_rows table') !== null`);
  await sleep(300); await shot('10-dict-import', '#dictImportCard');
  const memo = seed.tasks['采购备忘.txt'];
  await waitFor(`Array.from(document.querySelector('#dl_task').options).some(o => o.value === ${JSON.stringify(memo)})`);
  await evaluate(`(document.querySelector('#dl_task').value = ${JSON.stringify(memo)}, document.querySelector('#dl_find').click(), true)`);
  await waitFor(`document.querySelector('#dl_rows table') !== null || document.querySelector('#dl_msg').textContent.includes('没有')`);
  await sleep(300); await shot('10b-dict-learn', '#dictLearnCard');
  await evaluate(`(document.querySelector('#dictArea').closest('.card').id = 'dictEntriesCard', true)`);
  await shot('10c-dict-entries', '#dictEntriesCard');

  // 11 apps, 12 settings
  await tab('apps'); await sleep(800); await shot('11-apps', '#tab-apps');
  await tab('settings'); await sleep(900); await shot('12-settings', '#shellSettings');
  console.log('done');
}
main().catch(e => { console.error(e); process.exitCode = 1; }).finally(() => { try { ws && ws.close(); } catch {} edge.kill(); });
