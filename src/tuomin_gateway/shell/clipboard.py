"""Explicit clipboard state machine. No Cocoa, admin credential or raw-value persistence."""
from __future__ import annotations
import threading
import time
from urllib.parse import quote
from tuomin_gateway.placeholders import PLACEHOLDER_RE, find_altered_placeholders


def error_code(result):
    error = result.get('error', {})
    return error.get('code', '') if isinstance(error, dict) else ''


def summary(counts):
    # Only label names and integer counts can enter notification strings.
    return ' · '.join(f'{label} {count}' for label, count in sorted(counts.items())
                      if isinstance(label, str) and label.replace('_', '').isalpha() and label.isupper()
                      and isinstance(count, int) and count >= 0)


class ClipboardController:
    def __init__(self, board, client, tokens, session_file, notify, *, clock=time.time,
                 max_chars=200000, idle_hours=8, on_change=None):
        self.board, self.client, self.tokens = board, client, dict(tokens)
        self.file, self.notify, self.clock = session_file, notify, clock
        self.max_chars, self.idle_hours = max_chars, idle_hours
        self.on_change = on_change or (lambda: None)
        self.session = self.file.load()
        self.handles = []
        self._restored = False
        self._lock = threading.RLock()
        self.configuration_error = None
        self._namespace_status = "active"
        self.last_view = None  # Masked result/report only; never restored text.

    def _request(self, method, path, payload=None, *, capability='namespace'):
        return self.client.request(method, path, payload, token=self.tokens[capability])

    def _path(self, suffix=''):
        return '/api/v1/namespaces/' + quote(str(self.session['namespace_id']), safe='') + suffix

    def _save(self):
        self.file.save(self.session)
        self.on_change()

    def _notice(self, text, action=None):
        self.notify(text, action)
        return False

    def _restore(self):
        if self._restored or not self.session:
            return bool(self.session)
        status, data = self._request('GET', self._path('?app_id=clipboard'))
        if status == 404 or (status == 200 and data.get('namespace_status') not in {'active', 'archived'}):
            self.session, self.handles = {}, []
            self._save()
        elif status != 200:
            return False
        else:
            self.handles = list(data.get('mapping_handles', []))
            self._namespace_status = data['namespace_status']
        self._restored = True
        return bool(self.session)

    def _new(self, clear=False):
        # Reserve the replacement before archiving the old session. A license
        # denial or transient failure must never destroy the recovery context.
        replacement = None
        if not clear:
            # Surface identity: refill gives back the spelling the user copied
            # (an alias stays an alias), not the dictionary's canonical value.
            status, data = self._request('POST', '/api/v1/namespaces', {'app_id': 'clipboard', 'identity_contract': 'surface'})
            if status == 403 and error_code(data).startswith('license_'):
                return self._notice('授权已过期，无法新建脱敏；回填不受影响')
            if status != 200 or not data.get('namespace_id'):
                return self._notice('无法创建剪贴板会话，当前会话已保留')
            replacement = data['namespace_id']
        if self.session:
            status, _ = self._request('POST', self._path('/archive'), {'app_id': 'clipboard'})
            if status not in (200, 404):
                if replacement:
                    self._request('DELETE', '/api/v1/namespaces/' + quote(replacement, safe='') + '?app_id=clipboard')
                return self._notice('无法结束当前会话，请稍后重试')
            self._namespace_status = 'archived'
            if clear:
                status, _ = self._request('DELETE', self._path('?app_id=clipboard'))
                if status not in (200, 404):
                    return self._notice('会话尚未清除，请稍后重试')
        self.session, self.handles, self.last_view = {}, [], None
        self._restored = True
        if clear:
            self._save()
            return self._notice('会话已结束并清除')
        now = self.clock()
        self._namespace_status = 'active'
        self.session = dict(namespace_id=replacement, started_at=now, updated_at=now, segment_count=0, entity_count=0)
        self._save()
        return True

    def new_session(self, clear=False):
        with self._lock:
            return self._new(clear)

    def _ensure_session(self):
        if self.session and not self._restore():
            if self.session:  # Transient gateway failure must not overwrite recovery metadata.
                return self._notice('网关暂不可用，请稍后重试')
        if self.session and self._namespace_status == 'archived':
            return self._new()
        if self.session and self.idle_hours is not None and self.clock() - self.session.get('updated_at', 0) > self.idle_hours * 3600:
            if not self._new():
                return False
            self._notice('空闲会话已归档，已开始新会话')
        return bool(self.session) or self._new()

    def _write(self, source, result, *, concealed):
        if self.board.read_text() != source:
            return self._notice('剪贴板已变化，未覆盖新内容，请重新操作')
        self.board.write_text(result, concealed=concealed)
        return True

    def redact(self):
        with self._lock:
            if self.configuration_error:
                return self._notice(self.configuration_error, "records")
            text = self.board.read_text()
            if not isinstance(text, str) or not text.strip():
                return self._notice('剪贴板不是文本')
            if self.max_chars > 0 and len(text) > self.max_chars:
                return self._notice('文本过长，请用工作台的文件脱敏')
            if PLACEHOLDER_RE.search(text):
                return self._notice('看起来是 AI 回答，要回填吗？', 'refill')
            if not self._ensure_session():
                return False
            status, data = self._request('POST', self._path('/redact'), {'app_id': 'clipboard', 'text': text})
            code = error_code(data)
            if (status == 404 and code == 'namespace_not_found') or (status == 409 and code == 'namespace_inactive'):
                self.session, self.handles, self.last_view = {}, [], None
                self._save()
                return self._notice('会话已过期或结束，请再次脱敏以开始新会话')
            if status == 403 and code.startswith('license_'):
                return self._notice('授权已过期，无法新建脱敏；回填不受影响')
            if status == 503 or data.get('detectors', {}).get('degraded'):
                return self._notice('检测器未就绪，剪贴板未改动')
            if status != 200 or data.get('status') != 'ok' or not isinstance(data.get('masked_text'), str):
                return self._notice('脱敏未完成，请在工作台检查服务状态', 'records')
            if data.get('mapping_handle'):
                self.handles.append(data['mapping_handle'])
            self.session.update(updated_at=self.clock(), segment_count=self.session.get('segment_count', 0) + 1,
                                entity_count=data.get('entity_count', self.session.get('entity_count', 0)))
            self._save()
            self.last_view = {'tab': 'preview', 'redacted_text': data['masked_text'], 'blocked_labels': data.get('blocked_labels', [])}
            if data.get('egress_allowed') is not True:
                return self._notice('含不可外发内容：' + summary({k: 1 for k in data.get('blocked_labels', [])}), 'view')
            if not self._write(text, data['masked_text'], concealed=False):
                return False
            counts = data.get('label_counts', {})
            self._notice(f'已脱敏 {sum(v for v in counts.values() if isinstance(v, int))} 处' + (' · ' + summary(counts) if counts else ''), 'view')
            return True

    def refill(self):
        with self._lock:
            text = self.board.read_text()
            if not isinstance(text, str) or not text.strip():
                return self._notice('剪贴板不是文本')
            if self.max_chars > 0 and len(text) > self.max_chars:
                return self._notice('文本过长，请用工作台的回答回填')
            if not PLACEHOLDER_RE.search(text) and not find_altered_placeholders(text):
                return self._notice('没有找到占位符；要脱敏吗？', 'redact')
            if not self._restore() or not self.handles:
                return self._notice('会话映射已过期或被清除', 'records')
            for handle in reversed(self.handles):
                status, data = self._request('POST', '/api/v1/refill', {'app_id': 'clipboard', 'contract': 'trusted_display', 'mapping_handle': handle, 'text': text}, capability='trusted_refill')
                if status == 404 and error_code(data) == 'mapping_handle_not_found':
                    continue
                if data.get('status') == 'blocked' or (status == 400 and data.get('error_types')):
                    # Do not pass the answer (which may contain user-added originals) to notifications or store it.
                    errors = data.get('error_types', [])
                    unknown = data.get('unknown_placeholders', [])
                    altered = data.get('altered_placeholders', [])
                    count = len(set(unknown + altered))
                    self.last_view = {'tab': 'answer', 'error_types': errors, 'error_count': count,
                                      'unknown_placeholders': unknown, 'altered_placeholders': altered}
                    return self._notice(f'有 {count} 个占位符无法还原（可能被 AI 改写）', 'view')
                if status != 200 or data.get('status') != 'ok' or not isinstance(data.get('text'), str):
                    return self._notice('回填未完成，请在工作台查看', 'records')
                if not self._write(text, data['text'], concealed=True):
                    return False
                self.session['updated_at'] = self.clock()
                self._save()
                self._notice(f'已还原 {int(data.get("audit", {}).get("restored_count", 0))} 处')
                return True
            return self._notice('会话映射已过期或被清除', 'records')
