const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('a failed preview replaces previous success and never keeps stale statistics', async () => {
  const html = fs.readFileSync(path.join(__dirname, '../../src/tuomin_gateway/service/static/index.html'), 'utf8');
  const script = html.slice(html.indexOf('async function runRedact()'), html.indexOf('$("#t_run").onclick = runRedact;'));
  const elements = new Map();
  const $ = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', checked: false, disabled: false, style: {}, content: '',
      get innerHTML() { return this.content; }, set innerHTML(value) { this.content = value; },
      get textContent() { return this.content; }, set textContent(value) { this.content = value; },
    });
    return elements.get(id);
  };
  let rejectPending;
  let requestCount = 0;
  const context = vm.createContext({ $, SAMPLE_APP: null,
    api: () => ++requestCount === 1
      ? Promise.resolve({ masked: '<CREDENTIAL_001>', risk_summary: { detection_count: 1, placeholder_count: 1 } })
      : new Promise((resolve, reject) => { rejectPending = reject; }),
    renderDiff(original, masked) { $('#t_before').textContent = original; $('#t_after').textContent = masked; },
    esc: text => String(text).replaceAll('<', '&lt;').replaceAll('>', '&gt;'), toast() {},
  });
  vm.runInContext(script, context);
  $('#t_text').value = 'synthetic complete key';
  await vm.runInContext('runRedact()', context);
  assert.equal($('#t_after').textContent, '<CREDENTIAL_001>');
  assert.equal($('#t_stats').style.display, '');

  $('#t_text').value = 'synthetic incomplete key';
  const pending = vm.runInContext('runRedact()', context);
  assert.equal($('#t_before').textContent, '');
  assert.equal($('#t_stats').style.display, 'none');
  assert.ok(!$('#t_after').textContent.includes('<CREDENTIAL_001>'));
  rejectPending(new Error('incomplete private key <synthetic>'));
  await pending;
  assert.match($('#t_after').textContent, /本次脱敏失败/);
  assert.match($('#t_extra').innerHTML, /&lt;synthetic&gt;/);
  assert.equal($('#t_run').disabled, false);
  assert.equal($('#t_stats').style.display, 'none');
});

for (const mode of ['markdown', 'docx', 'xlsx', 'pptx']) {
  test(`${mode} failure clears the previous file result, export buttons and egress approval`, async () => {
    const html = fs.readFileSync(path.join(__dirname, '../../src/tuomin_gateway/service/static/index.html'), 'utf8');
    const formats = html.slice(html.indexOf('const OFFICE_MIMES ='), html.indexOf('const WB ='));
    const state = html.slice(html.indexOf('const WB ='), html.indexOf('function renderEgress('));
    const handler = mode === 'markdown'
      ? html.slice(html.indexOf('$("#f_run").onclick = async'), html.indexOf('$("#f_dl_md").onclick'))
      : html.slice(html.indexOf('function updateOfficeButton()'), html.indexOf('$("#f_run_docx").onclick ='));
    const elements = new Map();
    const $ = id => {
      if (!elements.has(id)) elements.set(id, {
        value: '', disabled: false, textContent: '', innerHTML: '', style: {},
        files: [{ name: 'synthetic.' + (mode === 'markdown' ? 'txt' : mode), arrayBuffer: async () => new ArrayBuffer(0) }],
      });
      return elements.get(id);
    };
    const context = vm.createContext({ $, getToken: () => 'synthetic-admin',
      fetch: async () => ({ ok: false, status: 400, json: async () => ({ error: { code: 'invalid_private_key_block', message: 'incomplete private key' } }) }),
      esc: String, toast() {},
      explainError: (data, status) => (data && data.error && data.error.message) || (data && typeof data.detail === 'string' && data.detail) || ((data && Array.isArray(data.error_types) && data.error_types.length) ? data.error_types.join('、') : ('HTTP ' + status)),
    });
    vm.runInContext(formats + state + handler, context);
    vm.runInContext('WB.taskId="old-task"; WB.redacted="old-result"; WB.manifest={};', context);
    for (const id of ['#f_egress', '#f_meta', '#f_warn']) $(id).innerHTML = 'previous success';
    await vm.runInContext(mode === 'markdown' ? '$("#f_run").onclick()' : 'runOfficeRedact(false)', context);
    assert.equal(vm.runInContext('WB.taskId', context), null);
    assert.equal(vm.runInContext('WB.redacted', context), '');
    assert.equal(vm.runInContext('WB.manifest', context), null);
    assert.equal($('#f_egress').innerHTML, '');
    assert.equal($('#f_meta').innerHTML, '');
    assert.match($('#f_out').textContent, /本次脱敏失败/);
    assert.match($('#f_warn').innerHTML, /incomplete private key/);
    for (const id of ['#f_dl_md', '#f_dl_manifest', '#f_pkg']) assert.equal($(id).disabled, true);
  });
}
