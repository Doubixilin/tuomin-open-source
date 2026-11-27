const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../../src/tuomin_gateway/service/static/index.html'), 'utf8');
const code = html.slice(html.indexOf('// --- answer refill tab ---'), html.indexOf('// --- trusted preview tab ---'));

function harness() {
  const nodes = new Map(), requests = [], downloads = [];
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, {value:'', files:[], style:{}});
    return nodes.get(id);
  };
  const context = vm.createContext({
    $, toast() {}, esc: x => x, getToken: () => '', selectedTask: id => $(id).value,
    taskRoute: (task, suffix) => task + suffix, explainError: () => 'blocked', renderDiffLists: () => '',
    fetch: () => new Promise(resolve => requests.push(body => resolve({ok:true, status:200, json:async () => body}))),
    download: (_filename, value) => downloads.push(value),
  });
  vm.runInContext(code, context);
  $('#an_task').value = 'test-job';
  $('#an_text').value = '合成回答';
  return {$, requests, downloads, run: () => vm.runInContext('doAnswerRefill(false)', context)};
}

test('editing an answer removes the prior output and ignores a late successful refill', async () => {
  const { $, requests, downloads, run } = harness();
  const original = run();
  requests.shift()({status:'ok', restored_markdown:'合成结果一'});
  await original;
  $('#an_dl').onclick();
  assert.deepEqual(downloads, ['合成结果一']);

  const stale = run();
  assert.equal($('#an_dl').disabled, true);
  $('#an_text').value = '新版回答';
  $('#an_text').oninput();
  const latest = run();
  requests[1]({status:'blocked'});
  await latest;
  requests[0]({status:'ok', restored_markdown:'过期原值'});
  await stale;
  assert.equal($('#an_out').textContent, '');
  assert.equal($('#an_dl').disabled, true);
  $('#an_dl').onclick();
  assert.deepEqual(downloads, ['合成结果一']);
});

test('source and credential changes clear a restored answer; a pending import cannot replace a newer edit', async () => {
  const { $, requests, run } = harness();
  for (const [id, event] of [['#an_task','onchange'], ['#an_pass','oninput'], ['#an_pkg','onchange']]) {
    const current = run();
    requests.shift()({status:'ok', restored_markdown:'本次结果'});
    await current;
    $(id)[event]();
    assert.equal($('#an_out').textContent, '');
    assert.equal($('#an_dl').disabled, true);
  }
  let finish;
  $('#an_file').files = [{text: () => new Promise(resolve => { finish = resolve; })}];
  const importing = $('#an_file').onchange();
  $('#an_text').value = '手工更新';
  $('#an_text').oninput();
  finish('旧文件内容');
  await importing;
  assert.equal($('#an_text').value, '手工更新');
});
