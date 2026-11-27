"""All clipboard transitions use fake I/O and synthetic values."""
import secrets
from pathlib import Path
import pytest
from tuomin_gateway.shell.clipboard import ClipboardController
from tuomin_gateway.shell.storage import SessionFile


class Board:
    def __init__(self, text): self.text, self.writes = text, []
    def read_text(self): return self.text
    def write_text(self, text, concealed=False):
        self.text = text
        self.writes.append((text, concealed))


class Client:
    def __init__(self, results): self.results, self.calls = list(results), []
    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path, payload, kwargs))
        assert self.results, 'unexpected request'
        return self.results.pop(0)


def controller(tmp_path, text='合成文本', results=(), session=True):
    board, client, notices = Board(text), Client(results), []
    file = SessionFile(tmp_path)
    if session: file.save(dict(namespace_id=secrets.token_hex(16),started_at=1,updated_at=100,segment_count=0,entity_count=0))
    value = ClipboardController(board,client,{k:secrets.token_urlsafe(32) for k in ('namespace','trusted_refill')},file,lambda t,a:notices.append((t,a)),clock=lambda:101)
    value._restored = True
    value.handles = [secrets.token_urlsafe(24)] if session else []
    return value, board, client, notices


def ok(**values):
    return 200, dict(status='ok',masked_text='<ORG_001>',mapping_handle=secrets.token_urlsafe(24),egress_allowed=True,detectors={'degraded':False},label_counts={'ORG':1},entity_count=1,**values)


@pytest.mark.parametrize('text', [None, '', '  '])
def test_nontext_never_requests_or_writes(tmp_path,text):
    c,b,h,n=controller(tmp_path,text)
    assert not c.redact() and not c.refill()
    assert not h.calls and not b.writes


def test_length_and_direction(tmp_path):
    c,b,h,n=controller(tmp_path,'abc');c.max_chars=2
    assert not c.redact() and not c.refill()
    c.max_chars=100;b.text='<ORG_001>'
    assert not c.redact() and n[-1][1]=='refill'
    b.text='合成文本'
    assert not c.refill() and n[-1][1]=='redact'
    assert not h.calls and not b.writes


@pytest.mark.parametrize('result',[
 (403,{'error':{'code':'license_expired'}}),
 (503,{'error':{'code':'required_detector_unavailable'}}),
 (200,{'status':'ok','masked_text':'safe','detectors':{'degraded':True},'egress_allowed':True}),
 (200,{'status':'ok','masked_text':'safe','egress_allowed':False,'blocked_labels':['CREDENTIAL']}),
 (0,{}),(500,{}),(200,{'status':'ok','egress_allowed':True}),
 (200,{'status':'ok','masked_text':'safe'}),
])
def test_redact_fail_closed(tmp_path,result):
    c,b,h,n=controller(tmp_path,results=[result]);original=b.text
    assert not c.redact() and b.text==original and not b.writes
    assert n and all(original not in message for message,_ in n)


def test_redact_success_and_session_metadata(tmp_path):
    c,b,h,n=controller(tmp_path,results=[ok()])
    assert c.redact() and b.writes==[('<ORG_001>',False)]
    assert c.session['segment_count']==1 and c.session['entity_count']==1
    assert 'mapping_handle' not in c.file.path.read_text()
    assert h.calls[0][3]['token']==c.tokens['namespace']


def test_new_session_then_redact(tmp_path):
    c,b,h,n=controller(tmp_path,results=[(200,{'namespace_id':secrets.token_hex(16)}),ok()],session=False)
    assert c.redact() and len(h.calls)==2


def test_expired_session_rehydrates_handles_only_in_memory(tmp_path):
    handle=secrets.token_urlsafe(24)
    c,b,h,n=controller(tmp_path,results=[(200,{'namespace_status':'active','mapping_handles':[handle]}),(200,{'status':'ok','text':'合成结果','audit':{'restored_count':1}})],text='<ORG_001>')
    c._restored=False;c.handles=[]
    assert c.refill() and b.writes[-1][1] is True
    assert handle not in c.file.path.read_text()


def test_session_expired_redact_creates_new(tmp_path):
    c,b,h,n=controller(tmp_path,results=[(404,{}),(200,{'namespace_id':secrets.token_hex(16)}),ok()]);c._restored=False
    assert c.redact()


def test_restore_gateway_failure_does_not_discard_session(tmp_path):
    c,b,h,n=controller(tmp_path,results=[(0,{})]);c._restored=False;previous=dict(c.session)
    assert not c.redact() and c.session==previous and not b.writes


def test_idle_archives_before_new_and_never_mixes_handles(tmp_path):
    c,b,h,n=controller(tmp_path,results=[(200,{'namespace_id':secrets.token_hex(16)}),(200,{}),ok()]);c.clock=lambda:40000
    old=list(c.handles)
    assert c.redact() and '/archive' in h.calls[1][1]
    assert not set(old)&set(c.handles)


def test_idle_can_be_disabled(tmp_path):
    c,b,h,n=controller(tmp_path,results=[ok()]);c.clock=lambda:40000;c.idle_hours=None
    assert c.redact() and len(h.calls)==1


def test_clear_archives_then_deletes(tmp_path):
    c,b,h,n=controller(tmp_path,results=[(200,{}),(200,{'deleted':True})])
    c.new_session(clear=True)
    assert [r[0] for r in h.calls]==['POST','DELETE']
    assert not c.session and not c.handles and c.file.load()=={}


@pytest.mark.parametrize('result',[(200,{'status':'blocked','text':None,'error_types':['unknown_placeholder']}),(400,{'error_types':['altered_placeholder']}),(500,{}),(200,{'status':'ok','text':None})])
def test_refill_blocked_or_bad_response_keeps_clipboard(tmp_path,result):
    c,b,h,n=controller(tmp_path,'<ORG_999>',[result]);assert not c.refill() and not b.writes
    assert h.calls[0][2]['contract']=='trusted_display'
    assert h.calls[0][3]['token']==c.tokens['trusted_refill']


def test_altered_placeholder_still_reaches_guard(tmp_path):
    c,b,h,n=controller(tmp_path,'<org_001>',[(200,{'status':'blocked','error_types':['altered_placeholder']})])
    assert not c.refill() and h.calls and not b.writes


def test_handle_fallback_same_session_and_exhaustion(tmp_path):
    expired=(404,{'error':{'code':'mapping_handle_not_found'}})
    c,b,h,n=controller(tmp_path,'<ORG_001>',[expired,(200,{'status':'ok','text':'合成单位','audit':{'restored_count':1}})])
    c.handles.append(secrets.token_urlsafe(24));latest=c.handles[-1]
    assert c.refill() and h.calls[0][2]['mapping_handle']==latest
    assert b.writes==[('合成单位',True)]
    c,b,h,n=controller(tmp_path,'<ORG_001>',[expired]);assert not c.refill() and not b.writes


def test_clipboard_changed_during_request_is_not_overwritten(tmp_path):
    c,b,h,n=controller(tmp_path,results=[ok()]);request=h.request
    def concurrent(*args,**kwargs):
        result=request(*args,**kwargs);b.text='新复制的内容';return result
    h.request=concurrent
    assert not c.redact() and not b.writes and b.text=='新复制的内容'


@pytest.mark.parametrize('status,code',[(404,'namespace_not_found'),(409,'namespace_inactive')])
def test_expired_live_session_can_recover_on_next_explicit_action(tmp_path,status,code):
    c,b,h,n=controller(tmp_path,results=[(status,{'error':{'code':code}}),(200,{'namespace_id':secrets.token_hex(16)}),ok()])
    c.idle_hours=None
    assert not c.redact() and not c.session and not c.handles and not b.writes
    assert c.redact() and len(h.calls)==3


def test_refill_report_counts_actual_residual_tokens(tmp_path):
    c,b,h,n=controller(tmp_path,'<ORG_999> <org_001>',[(200,{'status':'blocked','error_types':['unknown_placeholder','altered_placeholder'],'unknown_placeholders':['<ORG_999>'],'altered_placeholders':['<org_001>']})])
    assert not c.refill()
    assert c.last_view['error_count']==2
    assert c.last_view['unknown_placeholders']==['<ORG_999>']
    assert not b.writes


@pytest.mark.parametrize('failure', [
    (403, {'error': {'code': 'license_expired'}}),
    (0, {'error': {'code': 'gateway_unavailable'}}),
], ids=['license-expired', 'network-unavailable'])
@pytest.mark.parametrize('action', ['explicit-new', 'idle-redact'])
def test_failed_replacement_preserves_old_session_and_refill(tmp_path, failure, action):
    c,b,h,n=controller(tmp_path, results=[failure, (200, {
        'status': 'ok', 'text': '合成回填结果', 'audit': {'restored_count': 1},
    })])
    before_session, before_handles = dict(c.session), list(c.handles)
    before_file = c.file.path.read_bytes()
    if action == 'idle-redact':
        c.clock = lambda: 40000
        assert not c.redact()
    else:
        assert not c.new_session()
    assert bool(c.session == before_session)
    assert bool(c.handles == before_handles)
    assert c.file.path.read_bytes() == before_file
    assert [(call[0], call[1]) for call in h.calls] == [('POST', '/api/v1/namespaces')]
    assert not b.writes

    b.text = '<ORG_001>'
    assert c.refill()
    assert b.writes == [('合成回填结果', True)]
    assert h.calls[-1][1] == '/api/v1/refill'
    assert h.calls[-1][2]['contract'] == 'trusted_display'
    assert bool(h.calls[-1][2]['mapping_handle'] == before_handles[0])
    assert bool(c.file.load()['namespace_id'] == before_session['namespace_id'])
    assert all(handle not in c.file.path.read_text() for handle in before_handles)


def test_missing_dictionary_configuration_blocks_redact_but_keeps_refill(tmp_path):
    c,b,h,n=controller(tmp_path, results=[(200, {
        'status': 'ok', 'text': '合成回填结果', 'audit': {'restored_count': 1},
    })])
    c.configuration_error = '词典来源已删除，请重新选择'
    before_file = c.file.path.read_bytes()
    assert not c.redact()
    assert not h.calls and not b.writes
    assert n[-1] == (c.configuration_error, 'records')
    assert c.file.path.read_bytes() == before_file
    b.text = '<ORG_001>'
    assert c.refill()
    assert b.writes == [('合成回填结果', True)]
    assert h.calls[0][1] == '/api/v1/refill'


def test_archived_session_can_restore_handles_for_refill(tmp_path):
    handle = secrets.token_urlsafe(24)
    c,b,h,n=controller(tmp_path, text='<ORG_001>', results=[
        (200, {'namespace_status': 'archived', 'mapping_handles': [handle]}),
        (200, {'status': 'ok', 'text': '合成回填结果', 'audit': {'restored_count': 1}}),
    ])
    c._restored, c.handles = False, []
    previous = dict(c.session)
    assert c.refill()
    assert b.writes == [('合成回填结果', True)]
    assert bool(c.session['namespace_id'] == previous['namespace_id'])
    assert bool(h.calls[-1][2]['mapping_handle'] == handle)
    assert handle not in c.file.path.read_text()


def test_new_session_requests_surface_identity_so_refill_returns_user_spelling(tmp_path):
    c,b,h,n=controller(tmp_path, session=False, results=[(200, {'namespace_id': 'ns_synthetic'}), ok()])
    assert c.redact()
    method, path, payload, _ = h.calls[0]
    assert (method, path) == ('POST', '/api/v1/namespaces')
    assert payload == {'app_id': 'clipboard', 'identity_contract': 'surface'}
