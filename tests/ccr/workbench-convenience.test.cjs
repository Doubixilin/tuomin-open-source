const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../../src/tuomin_gateway/service/static/index.html'), 'utf8');
const H = require('../../src/tuomin_gateway/service/static/shell-ui.js');

test('Windows recorder uses Win and readable Ctrl key names', () => {
  assert.equal(H.shortcutFromEvent({code:'KeyD', ctrlKey:true, metaKey:true}, 'win32'), 'ctrl+win+d');
  assert.equal(H.shortcutFromEvent({code:'F24'}, 'win32'), 'f24');
  assert.equal(H.shortcutGlyphs('ctrl+win+d', 'win32'), 'Ctrl+Win+D');
  assert.equal(H.shortcutProblem('win+d', 'win32'), '');
  assert.equal(H.shortcutProblem('f24', 'win32'), '');
  assert.match(H.shortcutProblem('shift+d', 'win32'), /Ctrl、Alt 或 Win/);
});

// Pull one top-level function out of the inline script by brace matching.
function extract(name) {
  const start = html.indexOf('function ' + name + '(');
  assert.ok(start >= 0, 'missing function ' + name);
  let depth = 0, i = html.indexOf('{', start);
  for (; i < html.length; i++) { if (html[i] === '{') depth++; else if (html[i] === '}' && --depth === 0) break; }
  return html.slice(start, i + 1);
}
function context(extra = {}) {
  const elements = new Map();
  const $ = id => { if (!elements.has(id)) elements.set(id, { textContent: '', innerHTML: '', value: '' }); return elements.get(id); };
  const ctx = vm.createContext({ $, esc: s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])), ...extra });
  return { ctx, $ };
}

test('recorder maps physical keys and flags shortcuts that need a modifier', () => {
  const ev = (code, m = {}) => ({ code, ctrlKey: !!m.c, altKey: !!m.a, shiftKey: !!m.s, metaKey: !!m.m });
  assert.equal(H.shortcutFromEvent(ev('KeyD', { a: 1, m: 1 })), 'alt+cmd+d');
  assert.equal(H.shortcutFromEvent(ev('Digit5', { c: 1, s: 1 })), 'ctrl+shift+5');
  assert.equal(H.shortcutFromEvent(ev('F13')), 'f13');
  assert.equal(H.shortcutFromEvent(ev('ShiftLeft', { s: 1 })), null);
  assert.equal(H.shortcutGlyphs('cmd+option+r'), '⌥⌘R');
  assert.equal(H.shortcutProblem('f13'), '');
  assert.match(H.shortcutProblem('shift+d'), /⌘、⌥ 或 ⌃/);
  assert.deepEqual(H.settingsPayload({ idle: '8', source: '', enabled: true, redact: 'alt+cmd+d', refill: '', refillPaste: 'f14' }).hotkeys,
    { enabled: true, redact: 'alt+cmd+d', refill: '', refill_paste: 'f14' });
});

test('agent sessions use their own refill and mapping routes', () => {
  const { ctx } = context();
  vm.runInContext(extract('taskRoute') + '\n' + extract('agentSessionLabel'), ctx);
  assert.equal(vm.runInContext('taskRoute("agent:ns_ab/c", "/refill-answer")', ctx), '/api/v1/documents/agent-sessions/ns_ab%2Fc/refill-answer');
  assert.equal(vm.runInContext('taskRoute("t_123", "/mapping")', ctx), '/api/v1/documents/t_123/mapping');
  assert.match(vm.runInContext('agentSessionLabel({last_ts: 1, calls: 3, placeholder_count: 7})', ctx), /^智能体会话 · .* · 3 次脱敏 · 7 个占位符$/);
});

test('agent client status distinguishes current, stale and missing installs', () => {
  const { ctx } = context();
  vm.runInContext(extract('agentStatusText') + '\n' + extract('agentCommandLine'), ctx);
  assert.match(vm.runInContext('agentStatusText({installed:true,current:true})', ctx), /已接入/);
  assert.match(vm.runInContext('agentStatusText({installed:true,current:false})', ctx), /旧位置/);
  assert.match(vm.runInContext('agentStatusText({installed:false,detected:false})', ctx), /未检测到/);
  assert.equal(vm.runInContext('agentCommandLine({command:"/Applications/My App/x", args:["--mcp-stdio"]})', ctx), '"/Applications/My App/x" --mcp-stdio');
});

test('import preview renders statuses and escapes dictionary values', () => {
  const { ctx, $ } = context();
  vm.runInContext('const DI_STATUS = { added: "新增", updated: "合并别名", unchanged: "已存在", error: "错误", blocked: "未通过质量检查" };\n' + extract('diStatusLabel') + '\n' + extract('diRender'), ctx);
  ctx.result = { dry_run: true, counts: { added: 1, blocked: 1 }, rows: [
    { line: 1, canonical_value: '<b>甲</b>公司', label: 'ORG', aliases: ['甲'], status: 'added', message: '' },
    { line: 2, canonical_value: '市', label: 'ORG', aliases: [], status: 'blocked', message: '单个字太短' }] };
  vm.runInContext('diRender(result)', ctx);
  assert.equal($('#di_summary').textContent, '预览：新增 1，未通过质量检查 1');
  assert.ok($('#di_rows').innerHTML.includes('&lt;b&gt;甲&lt;/b&gt;公司'));
  assert.ok($('#di_rows').innerHTML.includes('class="di-blocked"') && $('#di_rows').innerHTML.includes('单个字太短'));
});

test('parser notices are shown in Chinese and noise is hidden', () => {
  const { ctx } = context();
  const table = html.slice(html.indexOf('const WARNING_TEXT = {'), html.indexOf('function warningText('));
  vm.runInContext(table + '\n' + extract('warningText'), ctx);
  assert.equal(vm.runInContext('warningText({code:"DOCX_RENDERED_PAGES_UNAVAILABLE", message:"DOCX OOXML has no reliable rendered page map"})', ctx), null);
  assert.match(vm.runInContext('warningText({code:"DOCX_MEDIA_NOT_OCR", message:"3 embedded media file(s) were indexed but not OCRed"})', ctx), /图片/);
  assert.equal(vm.runInContext('warningText({code:"pivot_cache_rewritten", message:"透视表缓存已一并脱敏"})', ctx), '透视表缓存已一并脱敏');
});
