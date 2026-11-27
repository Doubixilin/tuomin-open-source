/* Native preferences and navigation only; business requests retain HTTP auth. */
const TuominShellHelpers = (() => {
  const escape = value => String(value == null ? '' : value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const count = value => Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0;
  function endpoints(origin, appId) {
    const base = origin + '/apps/' + encodeURIComponent(appId);
    return [['masked', base + '/v1/chat/completions'], ['demo', base + '/demo/v1/chat/completions'], ['auto', base + '/auto/v1/chat/completions']];
  }
  function sessionSummary(session = {}) {
    if (!session.started_at) return '剪贴板会话：未开始';
    return '剪贴板会话：' + new Date(session.started_at * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) + ' 开始 · ' + count(session.segment_count) + ' 段 · ' + count(session.entity_count) + ' 个实体';
  }
  function settingsPayload(values) {
    const idle = values.idle === 'never' ? null : Number(values.idle === 'custom' ? values.customIdle : values.idle);
    if (idle !== null && (!Number.isFinite(idle) || idle <= 0 || idle > 720)) throw new Error('空闲时长须大于 0 且不超过 720 小时，或选择永不');
    const hotkeys = {enabled:!!values.enabled, redact:values.redact.trim(), refill:values.refill.trim()};
    for (const [field, name] of [['redactSelection','redact_selection'],['redactPaste','redact_paste'],['refillPaste','refill_paste']])
      if (typeof values[field] === 'string') hotkeys[name] = values[field].trim();
    return {idle_hours:idle, dictionary_source:values.source, hotkeys, ...(values.autostartAvailable ? {autostart:!!values.autostart} : {})};
  }
  const CODE_KEYS = {Minus:'-',Equal:'=',BracketLeft:'[',BracketRight:']',Backslash:'\\',Semicolon:';',Quote:"'",Comma:',',Period:'.',Slash:'/',Backquote:'`',Space:'space'};
  // Physical key (event.code) like Carbon's virtual key codes, so the
  // recorded shortcut is the same whatever input method is active.
  function shortcutFromEvent(e, platform = 'darwin') {
    const code = e.code || '';
    let key = null;
    if (/^Key[A-Z]$/.test(code)) key = code.slice(3).toLowerCase();
    else if (/^Digit[0-9]$/.test(code)) key = code.slice(5);
    else if ((platform === 'win32' ? /^F([1-9]|1[0-9]|2[0-4])$/ : /^F([1-9]|1[0-9]|20)$/).test(code)) key = code.toLowerCase();
    else key = CODE_KEYS[code] || null;
    if (!key) return null;
    const mods = [];
    if (e.ctrlKey) mods.push('ctrl'); if (e.altKey) mods.push('alt'); if (e.shiftKey) mods.push('shift'); if (e.metaKey) mods.push(platform === 'win32' ? 'win' : 'cmd');
    return [...mods, key].join('+');
  }
  function shortcutProblem(value, platform = 'darwin') {
    if (!value) return '';
    const parts = value.split('+'), key = parts[parts.length - 1];
    if (platform === 'win32') {
      if (/^f(1[3-9]|2[0-4])$/.test(key)) return '';
      return parts.some(p => ['win','windows','alt','ctrl','control'].includes(p)) ? '' : '需要搭配 Ctrl、Alt 或 Win';
    }
    if (/^f(1[3-9]|20)$/.test(key)) return '';
    return parts.some(p => ['cmd','alt','ctrl'].includes(p)) ? '' : (/^f\d+$/.test(key) ? 'F1–F12 需要搭配 ⌘、⌥ 或 ⌃' : '需要搭配 ⌘、⌥ 或 ⌃');
  }
  function shortcutGlyphs(value, platform = 'darwin') {
    if (!value) return '';
    const parts = value.toLowerCase().split('+'), key = parts.pop();
    if (platform === 'win32') return [...parts.map(p => ({ctrl:'Ctrl',control:'Ctrl',alt:'Alt',shift:'Shift',win:'Win',windows:'Win'}[p] || p)), key === 'space' ? '空格' : key.toUpperCase()].join('+');
    const glyph = [['ctrl','⌃'],['control','⌃'],['alt','⌥'],['option','⌥'],['shift','⇧'],['cmd','⌘'],['command','⌘']];
    const order = ['⌃','⌥','⇧','⌘'];
    const mods = [...new Set(parts.map(p => (glyph.find(([n]) => n === p) || [null, ''])[1]).filter(Boolean))].sort((a, b) => order.indexOf(a) - order.indexOf(b));
    return mods.join('') + (key === 'space' ? '空格' : key.toUpperCase());
  }
  function ledger(calls) {
    if (!calls.length) return '<div class="empty">暂无调用记录。</div>';
    const rows = calls.map(c => '<tr><td>' + escape(new Date(count(c.ts) * 1000).toLocaleTimeString()) + '</td><td>' + escape(c.entry) + '</td><td>' + escape(c.app_id) + '</td><td>' + count(c.duration_ms) + 'ms</td><td>' + count(c.char_count) + '</td><td>' + Object.entries(c.label_counts_json || {}).map(([k,v]) => escape(k) + ':' + count(v)).join(' ') + '</td><td>' + (c.blocked ? '⛔' : '—') + '</td></tr>').join('');
    return '<table><thead><tr><th>时间</th><th>入口</th><th>应用</th><th>耗时</th><th>字符数</th><th>标签计数</th><th>阻断</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  return {escape, endpoints, sessionSummary, settingsPayload, ledger, shortcutFromEvent, shortcutProblem, shortcutGlyphs};
})();

const HOTKEY_FIELDS = [['#shellRedactKey','redact'],['#shellRefillKey','refill'],['#shellRedactSelectionKey','redact_selection'],['#shellRedactPasteKey','redact_paste'],['#shellRefillPasteKey','refill_paste']];
function initTuominShellUI() {
  const H = TuominShellHelpers;
  let snapshot = null, settingsDirty = false, onboardingShown = false, shellLoading = false, lastDictionaryError = '';
  let callsLoading = false, mappingNamespace = null, demoInitialized = false, demoAppInitialized = false;
  const bridge = () => window.__tuominShell && window.pywebview && window.pywebview.api;
  function showTab(tab) {
    const button = document.querySelector('#tabs button[data-tab="' + tab + '"]');
    if (!button) return;
    button.closest('details').open = true;
    button.click();
  }
  function selectDemoApp() {
    const select = $('#f_app');
    if (!window.__tuominDemo || demoAppInitialized || !select || !select.options) return;
    if (Array.from(select.options).some(option => option.value === 'demo_contract')) {
      select.value = 'demo_contract';
      demoAppInitialized = true;
    }
  }
  function renderApps(apps) {
    selectDemoApp();
    $('#integrationEndpoints').innerHTML = apps.length ? apps.map(a => '<div class="endpoint-app"><h3>' + H.escape(a.app_id) + '</h3>' + H.endpoints(location.origin, a.app_id).map(([mode, url]) => '<div class="endpoint-row"><span class="meta">' + mode + '</span><code>' + H.escape(url) + '</code><button class="btn ghost sm" data-copy-endpoint="' + H.escape(url) + '">复制</button></div>').join('') + '</div>').join('') : '<div class="empty">尚未注册应用，请先在管理中添加。</div>';
    $$('#integrationEndpoints [data-copy-endpoint]').forEach(b => b.onclick = () => copyText(b.dataset.copyEndpoint));
  }
  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); toast('已复制'); }
    catch { toast('复制失败，请选中内容手动复制'); }
  }
  async function refreshCalls() {
    if (callsLoading || document.hidden || !$('#tab-onboard').classList.contains('active')) return;
    callsLoading = true;
    try {
      const data = await api('GET', '/api/v1/documents/calls?limit=100');
      $('#integrationCalls').innerHTML = H.ledger(data.calls || []);
      $('#integrationCallStatus').textContent = '已更新 · ' + new Date().toLocaleTimeString();
    } catch { $('#integrationCallStatus').textContent = '账本读取失败，请检查网关状态与管理凭证。'; }
    finally { callsLoading = false; }
  }
  function fillSources(selector, selected) {
    const select = $(selector);
    select.innerHTML = '';
    const sources = snapshot.dictionary_sources || [];
    for (const item of sources) select.appendChild(new Option(item.label, item.app_id));
    if (selected && !sources.some(item => item.app_id === selected)) {
      const missing = new Option('原词典来源不可用，请重新选择', selected);
      missing.disabled = true;
      select.appendChild(missing);
    }
    select.value = selected || '';
  }
  function fillNativeForm() {
    fillSources('#shellSource', snapshot.dictionary_source);
    const idle = snapshot.idle_hours === null ? 'never' : String(snapshot.idle_hours);
    $('#shellIdle').value = ['8','24','never'].includes(idle) ? idle : 'custom';
    $('#shellIdleCustom').value = snapshot.idle_hours || 8;
    $('#shellIdleCustom').hidden = $('#shellIdle').value !== 'custom';
    $('#shellHotkeys').checked = !!snapshot.hotkeys.enabled;
    for (const [id, name] of HOTKEY_FIELDS) { const input = $(id); if (input) input.value = snapshot.hotkeys[name] || ''; }
    refreshHotkeyGlyphs();
    $('#shellAutostart').checked = !!snapshot.autostart.enabled;
    $('#shellAutostart').disabled = !snapshot.autostart.available;
    $('#shellAutostartHint').textContent = snapshot.autostart.available ? '' : '仅已安装的正式 App 可启用';
  }
  function refreshHotkeyGlyphs() {
    for (const [id, name] of HOTKEY_FIELDS) {
      const input = $(id), badge = document.querySelector && document.querySelector('[data-hotkey-display="' + name + '"]');
      if (input && badge) { const problem = H.shortcutProblem(input.value.trim(), snapshot.platform); badge.textContent = problem || H.shortcutGlyphs(input.value.trim(), snapshot.platform); badge.classList && badge.classList.toggle('bad', !!problem); }
    }
  }
  function renderKeys() {
    const display = snapshot.hotkey_display || {}, on = snapshot.hotkeys && snapshot.hotkeys.enabled;
    for (const [id, name] of [['#homeRedactKey','redact'],['#homeRefillKey','refill'],['#onboardRedactKey','redact'],['#onboardRefillKey','refill']]) {
      const el = $(id); if (el) el.textContent = on && display[name] ? display[name] : '未设置';
    }
    const access = snapshot.accessibility || {}, row = $('#shellAccessRow');
    if (row) {
      row.hidden = !access.available || snapshot.platform === 'win32';
      $('#shellAccessState').textContent = access.trusted ? '辅助功能：已授权，「选中文字」三项可用。' : '辅助功能：未授权。「选中文字」三项需要它，其他快捷键不受影响。';
      $('#shellAccessGrant').hidden = !!access.trusted;
    }
  }
  function renderSnapshot(data) {
    snapshot = data;
    const redactKey = $('#introRedactKey'), refillKey = $('#introRefillKey'), keyHint = $('#shellHotkeyHint');
    if (redactKey) redactKey.textContent = snapshot.hotkeys.redact || '已关闭快捷键';
    if (refillKey) refillKey.textContent = snapshot.hotkeys.refill || '已关闭快捷键';
    if (keyHint) keyHint.textContent = '快捷键写法示例：' + (snapshot.platform === 'win32' ? 'ctrl+alt+d' : 'alt+cmd+d（⌥⌘D）') + '。留空即关闭该项；托盘／菜单栏动作始终可用。';
    renderKeys();
    if (typeof snapshot.mcp_command === 'string') $('#mcpCommand').textContent = snapshot.mcp_command;
    $('#clipboardSummary').hidden = false;
    $('#clipboardSummary').textContent = H.sessionSummary(snapshot.session);
    $('#shellSettings').hidden = false;
    const dictionaryError = snapshot.dictionary_error || '';
    if (dictionaryError) {
      $('#shellSettingsMessage').textContent = dictionaryError;
      if (dictionaryError !== lastDictionaryError) toast(dictionaryError);
    } else if (lastDictionaryError && $('#shellSettingsMessage').textContent === lastDictionaryError) $('#shellSettingsMessage').textContent = '';
    lastDictionaryError = dictionaryError;
    if (!settingsDirty) fillNativeForm();
    $('#shellHotkeyErrors').textContent = Object.entries(snapshot.hotkey_errors || {}).map(([name, message]) => (name === 'redact' ? '脱敏：' : '回填：') + message).join('；');
    if (!snapshot.onboarding_complete && !onboardingShown) openOnboarding();
  }
  function applyShellMode() {
    const native = !!window.__tuominShell, demo = !!window.__tuominDemo;
    $('#adminTokenBar').hidden = native;
    $('#modeBadge').textContent = demo ? '演示模式' : '正式模式';
    $('#demoBanner').hidden = !demo;
    document.body.classList.toggle('tuomin-demo', demo);
    $('#dictReadonly').hidden = !demo;
    $('#workbuddyDemoHint').hidden = !demo;
    for (const id of ['#w_apply','#w_verify']) $(id).disabled = demo;
    for (const id of ['#f_canonical','#f_label','#f_aliases','#f_risk','#addBtn','#reloadBtn','#di_commit','#dl_add']) $(id).disabled = demo;
    $$('#dictArea [data-del]').forEach(button => { button.disabled = demo; });
    if (demo && !demoInitialized) { demoInitialized = true; showTab('files'); }
    selectDemoApp();
  }
  async function refreshNative() {
    applyShellMode();
    const native = bridge();
    if (!native || !native.get_shell_settings || shellLoading || document.hidden) return;
    shellLoading = true;
    try { renderSnapshot(await native.get_shell_settings()); }
    catch { $('#shellSettingsMessage').textContent = '无法读取原生设置，请重新打开工作台。'; }
    finally { shellLoading = false; }
  }
  async function openOnboarding() {
    if (!snapshot) return;
    onboardingShown = true;
    fillSources('#onboardSource', snapshot.dictionary_source);
    $('#onboardAutostart').checked = !!snapshot.autostart.enabled;
    $('#onboardAutostart').disabled = !snapshot.autostart.available;
    $('#onboardAutostartHint').textContent = snapshot.autostart.available ? '' : '仅已安装的正式 App 可启用';
    if (!$('#shellOnboarding').open) $('#shellOnboarding').showModal();
  }
  async function saveNative(onboarding) {
    const native = bridge();
    if (!native || !snapshot) return;
    const message = $(onboarding ? '#onboardMessage' : '#shellSettingsMessage');
    const button = $(onboarding ? '#finishShellOnboarding' : '#saveShellSettings');
    let payload;
    try {
      const optional = id => ($(id) && typeof $(id).value === 'string') ? $(id).value : undefined;
      payload = onboarding ? {dictionary_source:$('#onboardSource').value, onboarding_complete:true, ...(snapshot.autostart.available ? {autostart:$('#onboardAutostart').checked} : {})} : H.settingsPayload({idle:$('#shellIdle').value,customIdle:$('#shellIdleCustom').value,source:$('#shellSource').value,enabled:$('#shellHotkeys').checked,redact:$('#shellRedactKey').value,refill:$('#shellRefillKey').value,redactSelection:optional('#shellRedactSelectionKey'),redactPaste:optional('#shellRedactPasteKey'),refillPaste:optional('#shellRefillPasteKey'),autostartAvailable:snapshot.autostart.available,autostart:$('#shellAutostart').checked});
    } catch (e) { message.textContent = e.message; return; }
    if (!onboarding) {
      const bad = HOTKEY_FIELDS.map(([id, name]) => [name, H.shortcutProblem(($(id) || {value:''}).value.trim(), snapshot.platform)]).find(([, problem]) => problem);
      if (bad) { message.textContent = '快捷键「' + (snapshot.hotkey_actions || {})[bad[0]] + '」' + bad[1]; return; }
    }
    if (payload.dictionary_source !== snapshot.dictionary_source && !confirm('切换剪贴板词典来源？新词典从下一段脱敏开始生效，已建立的占位符保持不变。')) return;
    button.disabled = true;
    message.textContent = '保存中…';
    try {
      const updated = await native.save_shell_settings(payload);
      settingsDirty = false;
      renderSnapshot(updated);
      message.textContent = Object.keys(updated.hotkey_errors || {}).length ? '设置已保存，快捷键冲突请按提示调整。' : '设置已保存';
      if (onboarding) $('#shellOnboarding').close();
    } catch { message.textContent = '设置未能保存，请检查词典来源、快捷键格式与登录项状态。'; }
    finally { button.disabled = false; }
  }
  function receiveShellOpen(event) {
    if (!window.__tuominShell) return;
    const detail = event.detail || {}, tab = detail.tab === 'original' ? 'preview' : detail.tab;
    if (!['preview','answer','records'].includes(tab)) return;
    showTab(tab);
    if (detail.clipboard_mapping) {
      mappingNamespace = typeof detail.namespace_id === 'string' ? detail.namespace_id : null;
      $('#clipboardMapping').hidden = false;
      $('#clipboardMapMessage').textContent = mappingNamespace ? '点「显示剪贴板对照表」查看当前会话的占位符与原值。' : '尚无剪贴板会话，请先执行一次脱敏。';
      $('#clipboardMapUnlock').disabled = !mappingNamespace;
      $('#clipboardMapping').scrollIntoView({block:'center'});
    }
    if (tab === 'preview' && typeof detail.redacted_text === 'string') {
      renderPreview(detail.redacted_text, '#pv_red_out', '#pv_red_counts');
      $('#pv_msg').textContent = (detail.blocked_labels || []).length ? '含不可外发内容：' + detail.blocked_labels.join('、') + '。剪贴板保持原样。' : '剪贴板脱敏结果，仅保留在本页。';
    }
    if (tab === 'answer') {
      // Error notifications deliberately carry no answer text or original values.
      const residuals = [...new Set([...(detail.unknown_placeholders || []), ...(detail.altered_placeholders || [])])];
      $('#an_diff').textContent = '剪贴板回答未还原：' + (detail.error_types || []).join('、') + (detail.error_count ? '（' + Number(detail.error_count) + ' 处）' : '') + (residuals.length ? '；未匹配或被改写的占位符：' + residuals.join('、') : '') + '。请在上方粘贴回答并选择对应恢复包，按现有流程检查；也可更正占位符后从菜单栏重试。';
    }
  }
  async function unlockClipboardMapping() {
    // The mapping already lives in the protected local store; the trusted
    // window reads it over the authenticated admin route and auto-locks.
    const namespace = mappingNamespace;
    if (!namespace) { $('#clipboardMapMessage').textContent = '尚无剪贴板会话，请先执行一次脱敏。'; return; }
    $('#clipboardMapUnlock').disabled = true;
    $('#clipboardMapMessage').textContent = '正在读取…';
    lockMapping('正在读取剪贴板会话的原值');
    const epoch = PV.lockEpoch;
    $('#pv_lock').disabled = false;
    try {
      const data = await api('POST', '/admin/clipboard/namespaces/' + encodeURIComponent(namespace) + '/mapping', {});
      if (epoch !== PV.lockEpoch || document.hidden || !$('#tab-preview').classList.contains('active')) return;
      showUnlockedMapping(data);
      $('#clipboardMapMessage').textContent = '当前剪贴板对照表已显示，90 秒后自动锁定。';
    } catch { if (epoch === PV.lockEpoch) { lockMapping('读取失败'); $('#clipboardMapMessage').textContent = '无法读取，会话可能已过期或被清除。'; } }
    finally { $('#clipboardMapUnlock').disabled = !mappingNamespace; }
  }
  window.TuominShellUI = {renderApps, showTab};
  function recordHotkey(event) {
    const input = event.currentTarget || event.target;
    if (event.key === 'Tab') return;
    event.preventDefault();
    if (event.key === 'Escape') { input.blur(); return; }
    if ((event.key === 'Backspace' || event.key === 'Delete') && !event.metaKey && !event.ctrlKey && !event.altKey) input.value = '';
    else { const value = H.shortcutFromEvent(event, snapshot && snapshot.platform); if (!value) return; input.value = value; }
    settingsDirty = true; refreshHotkeyGlyphs();
  }
  for (const [id] of HOTKEY_FIELDS) {
    const input = $(id); if (!input || !input.addEventListener) continue;
    input.addEventListener('keydown', recordHotkey);
    input.addEventListener('input', refreshHotkeyGlyphs);
  }
  $$('[data-hotkey-clear]').forEach(button => button.onclick = () => { const input = $('#' + button.dataset.hotkeyClear); input.value = ''; settingsDirty = true; refreshHotkeyGlyphs(); });
  async function accessibility(action) {
    const native = bridge(); if (!native || !native.accessibility_action) return;
    try { snapshot.accessibility = await native.accessibility_action(action); renderKeys(); } catch { toast('无法读取辅助功能权限状态'); }
  }
  if ($('#shellAccessGrant')) $('#shellAccessGrant').onclick = () => accessibility('prompt');
  if ($('#shellAccessOpen')) $('#shellAccessOpen').onclick = () => accessibility('settings');
  $$('[data-onboard-go]').forEach(button => button.onclick = async () => { const tab = button.dataset.onboardGo; await saveNative(true); if ($('#shellOnboarding').open) $('#shellOnboarding').close(); showTab(tab); });
  renderApps(window.__tuominRegisteredApps || []);
  $('#copyMcp').onclick = () => copyText($('#mcpCommand').textContent);
  $$('#tabs button').forEach(button => button.addEventListener('click', () => {
    button.closest('details').open = true;
    if (button.dataset.tab !== 'preview') { mappingNamespace = null; $('#clipboardMapping').hidden = true; }
    if (button.dataset.tab === 'onboard') refreshCalls();
    if (button.dataset.tab === 'settings') refreshNative();
  }));
  $$('#shellSettings input, #shellSettings select').forEach(input => input.addEventListener('input', () => { settingsDirty = true; }));
  $('#shellIdle').onchange = () => { $('#shellIdleCustom').hidden = $('#shellIdle').value !== 'custom'; settingsDirty = true; };
  $('#saveShellSettings').onclick = () => saveNative(false);
  $('#finishShellOnboarding').onclick = () => saveNative(true);
  $('#showShellOnboarding').onclick = openOnboarding;
  $('#laterShellOnboarding').onclick = () => $('#shellOnboarding').close();
  $('#clipboardMapUnlock').onclick = unlockClipboardMapping;
  window.addEventListener('tuomin-shell-ready', refreshNative);
  window.addEventListener('pywebviewready', refreshNative);
  window.addEventListener('tuomin-shell-open', receiveShellOpen);
  window.addEventListener('blur', () => { lockMapping('已随窗口离开锁定'); });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { lockMapping('已随窗口隐藏锁定'); }
    else { refreshNative(); refreshCalls(); }
  });
  setInterval(refreshCalls, 3000);
  setInterval(refreshNative, 5000);
  refreshNative();
  // External browsers and --headless have no native bootstrap.
  if (typeof fetch === 'function') fetch('/api/v1/runtime-mode').then(r => r.json()).then(mode => {
    if (mode.demo) { window.__tuominDemo = true; applyShellMode(); }
  }).catch(() => {});
}

if (typeof module !== 'undefined' && module.exports) module.exports = TuominShellHelpers;
else initTuominShellUI();
