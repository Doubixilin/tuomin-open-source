"""Admin-only setup, kept separate from daily clipboard capability operations."""
from __future__ import annotations
import hashlib
from pathlib import Path
from urllib.parse import quote
from tuomin_gateway.capability_tokens import generate_capability_token
from .storage import read_private, write_private

CAPABILITIES = ('detect', 'redact', 'namespace', 'trusted_refill')


class ClipboardRegistration:
    def __init__(self, client, admin_token, directory):
        self.client, self.admin_token = client, admin_token
        self.path = Path(directory) / 'clipboard-tokens.json'
        self.tokens = read_private(self.path)
        if self.tokens and (set(self.tokens) != set(CAPABILITIES) or not all(isinstance(v, str) and len(v) >= 32 for v in self.tokens.values())):
            raise ValueError('invalid clipboard credentials; open settings to reset')
        if not self.tokens:
            self.tokens = {name: generate_capability_token()[0] for name in CAPABILITIES}
            write_private(self.path, self.tokens)

    def _admin(self, method, path, payload=None):
        status, value = self.client.request(method, path, payload, token=self.admin_token, admin=True)
        if status != 200:
            raise RuntimeError('clipboard configuration unavailable')
        return value

    def sources(self):
        apps = self._admin('GET', '/admin/apps').get('apps', [])
        return [{'app_id': '', 'label': '不使用词典（仅规则识别）'}] + [
            {'app_id': app['app_id'], 'label': app['name'] if isinstance(app.get('name'), str) and app['name'] else app['app_id']}
            for app in apps if app.get('app_id') != 'clipboard' and app.get('has_dictionary')]

    def configure(self, source=''):
        entry = {
            'profile': 'file_workbench', 'name': '剪贴板',
            'capabilities': list(CAPABILITIES),
            'capability_tokens': {k: 'sha256:' + hashlib.sha256(v.encode()).hexdigest() for k, v in self.tokens.items()},
            'refill_contracts': ['trusted_display'], 'mapping_scopes': ['namespace'],
        }
        if source:
            original = self._admin('GET', '/admin/apps/' + quote(source, safe='')).get('entry', {})
            if not original.get('dictionary'):
                raise ValueError('selected dictionary unavailable')
            entry['dictionary'] = original['dictionary']
        self._admin('PUT', '/admin/apps/clipboard', entry)
        self._admin('POST', '/dict/reload', {})
