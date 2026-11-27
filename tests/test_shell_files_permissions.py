import json
import secrets
import pytest
from permission_assertions import assert_owner_private
from tuomin_gateway.shell.registration import ClipboardRegistration
from tuomin_gateway.shell.storage import SessionFile, read_private, write_private


class Admin:
    def __init__(self): self.calls=[]
    def request(self,*args,**kwargs): self.calls.append((args,kwargs));return 200,{}


def test_credentials_and_metadata_are_private(tmp_path):
    client=Admin();registration=ClipboardRegistration(client,secrets.token_urlsafe(32),tmp_path)
    registration.configure()
    file=SessionFile(tmp_path)
    file.save({'namespace_id':secrets.token_hex(16),'started_at':1,'updated_at':2,'segment_count':3,'entity_count':4,'mapping_handle':secrets.token_urlsafe(32),'original':'合成原值','entries':[]})
    for path in [registration.path,file.path]: assert_owner_private(path, is_dir=False)
    assert set(file.load())=={'namespace_id','started_at','updated_at','segment_count','entity_count'}
    entry=client.calls[0][0][2]
    assert entry['profile']=='file_workbench'
    assert all(value.startswith('sha256:') for value in entry['capability_tokens'].values())
    serialized=json.dumps(client.calls)
    assert all(token not in serialized for token in registration.tokens.values())
    assert ClipboardRegistration(client,secrets.token_urlsafe(32),tmp_path).tokens==registration.tokens


def test_private_file_rejects_symlinks(tmp_path):
    destination=tmp_path/'target';destination.write_text('{}');link=tmp_path/'linked'
    try:
        link.symlink_to(destination)
    except OSError:
        pytest.skip('Windows requires Developer Mode or symlink privilege')
    with pytest.raises(ValueError): read_private(link)
    with pytest.raises(ValueError): write_private(link,{})


def test_permission_failure_cannot_write_credentials_or_replace_existing_state(tmp_path, monkeypatch):
    from tuomin_gateway.shell import storage
    target = tmp_path / 'state.json'
    target.write_text('{"old": true}', encoding='utf-8')
    def deny_temporary(path, *, is_dir):
        if not is_dir:
            assert not Path(path).read_bytes()
            raise PermissionError('synthetic ACL failure')
    from pathlib import Path
    monkeypatch.setattr(storage, 'restrict_permissions', deny_temporary)
    with pytest.raises(PermissionError):
        storage.write_private(target, {'credential': secrets.token_urlsafe(32)})
    assert json.loads(target.read_text(encoding='utf-8')) == {'old': True}
    assert list(tmp_path.iterdir()) == [target]
