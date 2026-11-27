const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../../src/tuomin_gateway/service/static/index.html'), 'utf8');
const workbench = html.slice(html.indexOf('// --- workbench: file redaction & refill ---'), html.indexOf('async function doRefill(confirm)'));
const mime = {
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
};
const encoded = value => Buffer.from(value).toString('base64');
const egress = allowed => ({ egress_allowed: allowed, reasons: allowed ? [] : [{code: 'app_not_bound', message: '未绑定应用'}] });
function file(name, contents = 'source bytes') {
  return { name, text: async () => contents, arrayBuffer: async () => new TextEncoder().encode(contents).buffer };
}
function redacted(format) {
  return { status: 'ok', task_id: 'job-test', file_name: '报告.' + format, file_format: format,
    [format === 'docx' ? 'redacted_docx_b64' : 'redacted_office_b64']: encoded('redacted bytes'),
    egress: egress(true), warnings: [] };
}
function harness() {
  const elements = new Map();
  const requests = [], downloads = [], toasts = [], responses = [], apiRequests = [];
  const blobs = new Map();
  const $ = selector => {
    if (!elements.has(selector)) elements.set(selector, { value: '', files: [], disabled: false, textContent: '', innerHTML: '', style: {} });
    return elements.get(selector);
  };
  const context = vm.createContext({ $, Blob, Uint8Array, warningText: w => (w && (w.message || w.code)) || '',
    atob: value => Buffer.from(value, 'base64').toString('binary'),
    btoa: value => Buffer.from(value, 'binary').toString('base64'),
    URL: { createObjectURL(blob) { const url = 'blob:' + blobs.size; blobs.set(url, blob); return url; }, revokeObjectURL() {} },
    document: { body: { appendChild() {} }, createElement() { return {
      click() { downloads.push({ name: this.download, blob: blobs.get(this.href) }); }, remove() {},
    }; } },
    setTimeout(callback) { callback(); },
    esc: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
    getToken: () => 'test-token', toast: message => toasts.push(message),
    explainError: (data, status) => (data && data.error && data.error.message) || (data && typeof data.detail === 'string' && data.detail) || ((data && Array.isArray(data.error_types) && data.error_types.length) ? data.error_types.join('、') : ('HTTP ' + status)),
    fetch: async (url, options) => {
      requests.push({ url, ...options });
      assert.ok(responses.length, 'unexpected fetch: ' + url);
      const response = responses.shift();
      return { ok: response.status < 400, status: response.status, json: async () => response.body };
    },
    api: async (method, route, body) => { apiRequests.push({ method, route, body }); return { package: { encrypted: true } }; },
  });
  vm.runInContext(workbench, context);
  return { $, requests, downloads, toasts, apiRequests,
    respond(body, status = 200) { responses.push({ body, status }); },
    choose(name, contents) { $('#f_file').files = [file(name, contents)]; $('#f_file').onchange(); },
    async recoveryPackage() { $('#r_pkg').files = [file('report.tuominmap', '{"binding":{"job_id":"job-test"}}')]; await $('#r_pkg').onchange(); $('#r_pass').value = 'restore-pass'; },
  };
}

test('original-format action follows supported file selection, converts legacy DOC/XLS and leaves XLSM on Markdown', () => {
  const h = harness();
  for (const extension of ['docx', 'XLSX', 'pptx', 'doc', 'xls']) {
    h.choose('报告.' + extension);
    assert.equal(h.$('#f_run_docx').style.display, '');
    assert.match(h.$('#f_run_docx').textContent, new RegExp(extension.toUpperCase()));
  }
  assert.match(h.$('#f_run_docx').textContent, /XLS → XLSX/);
  for (const extension of ['xlsm', 'txt']) {
    h.choose('报告.' + extension);
    assert.equal(h.$('#f_run_docx').style.display, 'none');
  }
});

for (const format of ['docx', 'xlsx', 'pptx']) {
  test(format + ' redaction routes, downloads, and exports the matching recovery package', async () => {
    const h = harness();
    h.choose('报告.' + format.toUpperCase());
    h.$('#f_app').value = '生产词典';
    h.respond(redacted(format));
    await h.$('#f_run_docx').onclick();
    const request = h.requests[0];
    const url = new URL(request.url, 'http://localhost');
    assert.equal(url.pathname, '/api/v1/documents/redact-' + (format === 'docx' ? 'docx' : 'office'));
    assert.equal(url.searchParams.get('filename'), '报告.' + format.toUpperCase());
    assert.equal(url.searchParams.get('app_id'), '生产词典');
    assert.equal(request.headers['x-tuomin-admin-token'], 'test-token');
    assert.equal(request.headers['X-Requested-With'], 'tuomin-ui');
    assert.equal(h.downloads[0].name, '报告.redacted.' + format);
    assert.equal(h.downloads[0].blob.type, mime[format]);
    assert.equal(await h.downloads[0].blob.text(), 'redacted bytes');
    assert.equal(h.$('#f_pkg').disabled, false);
    assert.equal(h.$('#f_dl_md').disabled, true);
    assert.equal(h.$('#f_run_docx').disabled, false);
    h.$('#f_pass').value = 'export-pass';
    await h.$('#f_pkg').onclick();
    assert.equal(h.apiRequests[0].route, '/api/v1/documents/job-test/package');
    assert.equal(h.apiRequests[0].body.passphrase, 'export-pass');
    assert.equal(h.downloads[1].name, '报告.tuominmap');
    assert.equal(h.$('#f_pass').value, '');
  });

  test(format + ' refill sends the corresponding payload and downloads its original format', async () => {
    const h = harness();
    await h.recoveryPackage();
    h.$('#r_docx').files = [file('报告.redacted.' + format, 'redacted source')];
    h.respond({ status: 'ok', mode: 'edited', restored_count: 2,
      [format === 'docx' ? 'restored_docx_b64' : 'restored_office_b64']: encoded('restored bytes') });
    await h.$('#r_docx_run').onclick();
    const request = h.requests[0];
    assert.equal(request.url, '/api/v1/documents/refill-' + (format === 'docx' ? 'docx' : 'office'));
    const body = JSON.parse(request.body);
    assert.equal(body.passphrase, 'restore-pass');
    assert.equal(body.package.binding.job_id, 'job-test');
    assert.equal(body[format === 'docx' ? 'redacted_docx_b64' : 'redacted_office_b64'], encoded('redacted source'));
    assert.equal(body.file_format, format === 'docx' ? undefined : format);
    assert.equal(body.confirm_edited, format === 'docx' ? undefined : false);
    assert.equal(h.downloads[0].name, '报告.restored.' + format);
    assert.equal(h.downloads[0].blob.type, mime[format]);
    assert.equal(await h.downloads[0].blob.text(), 'restored bytes');
    assert.equal(h.$('#r_docx_run').disabled, false);
  });
}

test('blocked original-format attempt clears the previous task and cannot download a file or package', async () => {
  const h = harness();
  h.choose('first.xlsx'); h.respond(redacted('xlsx'));
  await h.$('#f_run_docx').onclick();
  h.choose('second.pptx');
  h.respond({ error: { code: 'egress_blocked', message: '出站闸门未通过' }, egress: egress(false) }, 422);
  await h.$('#f_run_docx').onclick();
  assert.equal(h.downloads.length, 1);
  assert.equal(h.$('#f_pkg').disabled, true);
  assert.match(h.$('#f_egress').innerHTML, /app_not_bound/);
  assert.match(h.$('#f_warn').innerHTML, /出站闸门未通过/);
  await h.$('#f_pkg').onclick();
  assert.equal(h.apiRequests.length, 0);
  assert.match(h.toasts.at(-1), /请先完成一次文件脱敏/);
});

test('HTTP success cannot bypass the file download egress decision', async () => {
  for (const gate of [undefined, egress(false)]) {
    const h = harness(); h.choose('report.xlsx');
    h.respond({ ...redacted('xlsx'), egress: gate });
    await h.$('#f_run_docx').onclick();
    assert.equal(h.downloads.length, 0);
    assert.equal(h.$('#f_pkg').disabled, true);
    assert.match(h.$('#f_warn').innerHTML, /出站闸门未通过/);
  }
});

test('DOCX retains the explicit OLE-removal retry while Office formats do not offer it', async () => {
  const h = harness(); h.choose('report.docx');
  h.respond({ error: { code: 'ole_object_present', message: 'contains OLE' } }, 422);
  await h.$('#f_run_docx').onclick();
  assert.equal(h.downloads.length, 0);
  assert.match(h.$('#f_warn').innerHTML, /f_ole_retry/);
  h.respond(redacted('docx'));
  await h.$('#f_ole_retry').onclick();
  assert.equal(new URL(h.requests[1].url, 'http://localhost').searchParams.get('ole_strip'), 'true');
  for (const format of ['xlsx', 'pptx']) {
    const office = harness(); office.choose('report.' + format);
    office.respond({ error: { code: 'ole_object_present', message: 'contains OLE' } }, 422);
    await office.$('#f_run_docx').onclick();
    assert.doesNotMatch(office.$('#f_warn').innerHTML, /f_ole_retry/);
    assert.equal(office.downloads.length, 0);
  }
});

test('PPTX is available through Markdown parsing as well as original-format processing', async () => {
  const h = harness(); h.choose('slides.pptx');
  h.respond({ task_id: 'job-preview', file_name: 'slides.pptx', file_format: 'pptx', redacted_markdown: 'Slides <ORG_001>', label_counts: {}, warnings: [], egress: egress(false) });
  await h.$('#f_run').onclick();
  assert.equal(new URL(h.requests[0].url, 'http://localhost').pathname, '/api/v1/documents/parse');
  assert.equal(h.$('#f_out').textContent, 'Slides <ORG_001>');
  assert.equal(h.$('#f_dl_md').disabled, true);
  assert.equal(h.$('#f_pkg').disabled, false);
});

test('unsafe refill remains visible and never triggers a file download', async () => {
  const h = harness(); await h.recoveryPackage();
  h.$('#r_docx').files = [file('report.pptx')];
  h.respond({ status: 'blocked', error_types: ['unknown_placeholder'] }, 422);
  await h.$('#r_docx_run').onclick();
  assert.equal(h.downloads.length, 0);
  assert.match(h.$('#r_diff').innerHTML, /unknown_placeholder/);
  assert.equal(h.$('#r_docx_run').disabled, false);
});

for (const format of ['xlsx', 'pptx']) {
  test(format + ' changed-file refill downloads only after explicit task confirmation', async () => {
    const h = harness(); await h.recoveryPackage();
    h.$('#r_docx').files = [file('report.redacted.' + format)];
    h.respond({ status: 'needs_confirmation', mode: 'edited', diff: { missing: ['<ORG_001>'] } });
    await h.$('#r_docx_run').onclick();
    assert.equal(h.requests.length, 1);
    assert.equal(JSON.parse(h.requests[0].body).confirm_edited, false);
    assert.equal(h.downloads.length, 0);
    assert.match(h.$('#r_diff').innerHTML, /文件已编辑，或与所选任务（恢复包）不对应/);
    assert.match(h.$('#r_diff').innerHTML, /选错会还原成错误的原值/);
    assert.match(h.$('#r_diff').innerHTML, /&lt;ORG_001&gt;/);
    h.respond({ status: 'ok', mode: 'edited', restored_count: 1, restored_office_b64: encoded('confirmed restore') });
    await h.$('#r_office_confirm').onclick();
    assert.equal(h.requests.length, 2);
    assert.equal(JSON.parse(h.requests[1].body).confirm_edited, true);
    assert.equal(h.downloads.length, 1);
    assert.equal(h.downloads[0].name, 'report.restored.' + format);
    assert.equal(await h.downloads[0].blob.text(), 'confirmed restore');
    // Starting a new attempt must not inherit the previous confirmation.
    h.respond({ status: 'needs_confirmation', mode: 'edited', diff: {} });
    await h.$('#r_docx_run').onclick();
    assert.equal(JSON.parse(h.requests[2].body).confirm_edited, false);
    assert.equal(h.downloads.length, 1);
  });
}

test('pending Office confirmation cannot authorize a different file or recovery package', async () => {
  for (const change of ['file', 'package']) {
    const h = harness(); await h.recoveryPackage();
    h.$('#r_docx').files = [file('report.xlsx')];
    h.respond({ status: 'needs_confirmation', mode: 'edited', diff: {} });
    await h.$('#r_docx_run').onclick();
    if (change === 'file') h.$('#r_docx').files = [file('another.xlsx')];
    else await h.recoveryPackage();
    await h.$('#r_office_confirm').onclick();
    assert.equal(h.requests.length, 1);
    assert.equal(h.downloads.length, 0);
    assert.match(h.$('#r_diff').textContent, /文件或任务已更换/);
  }
});
