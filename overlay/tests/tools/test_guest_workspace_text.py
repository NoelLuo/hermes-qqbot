"""Real bounded workspace import/read/search/CSV/export with strict text decoding."""
import codecs
from pathlib import Path

import pytest

from agent.request_security import RequestSecurityPolicy, policy_scope
from tools.guest_workspace_store import MAX_OUTPUT_BYTES, execute_workspace
from tools.guest_workspace_text import TextDecodingError, decode_text


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    import hermes_constants
    home = tmp_path / 'home'
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: home)
    policy = RequestSecurityPolicy('visitor', 'qqbot', 'room')
    base = tmp_path / 'storage'

    def call(action, **kwargs):
        with policy_scope(policy):
            return execute_workspace({'action': action, **kwargs}, policy, base_dir=base)

    def put(data, name='input.txt'):
        source = tmp_path / ('transport-' + name)
        source.write_bytes(data)
        assert policy.grant_artifact(source)
        assert call('import_attachment', source=str(source), path=name)['success']

    return call, put, policy, base


@pytest.mark.parametrize('codec,expected,mode', [
    ('utf-8', 'utf-8', 'utf8'), ('utf-8-sig', 'utf-8-sig', 'bom'),
    ('utf-16', 'utf-16-le', 'bom'), ('utf-32', 'utf-32-le', 'bom'),
    ('gbk', 'gb18030', 'fallback'), ('gb18030', 'gb18030', 'fallback'),
])
def test_real_import_read_export_keeps_original_bytes(workspace, codec, expected, mode):
    call, put, _, _ = workspace
    text = '中文，首尔夜晚\r\n第二行\n'
    raw = text.encode(codec)
    put(raw)
    result = call('read', path='input.txt')
    assert result['success'], result
    assert (result['text'], result['encoding'], result['encoding_detection']) == (text, expected, mode)
    exported = call('export', path='input.txt')
    assert exported['success']
    assert Path(exported['file_path']).read_bytes() == raw


@pytest.mark.parametrize('bom,codec', [(codecs.BOM_UTF16_BE, 'utf-16-be'), (codecs.BOM_UTF32_BE, 'utf-32-be')])
def test_big_endian_boms_are_detected_before_compatibility_fallback(bom, codec):
    text = '中文🌊\n'
    assert decode_text(bom + text.encode(codec)) == {
        'text': text, 'encoding': codec, 'encoding_detection': 'bom'}


@pytest.mark.parametrize('raw', [b'\x81', b'\xff\xfeA', b'x\x00y\x00', b'\x00\x01\x02', b'abc\x7f'])
def test_bad_text_is_explicit_not_authorization_failure_and_can_still_export(workspace, raw):
    call, put, _, _ = workspace
    put(raw)
    result = call('read', path='input.txt')
    assert not result['success'] and result['error_code'] == 'text_decode_error'
    assert 'decoding failed' in result['error']
    assert 'refused or unavailable' not in result['error']
    exported = call('export', path='input.txt')
    assert Path(exported['file_path']).read_bytes() == raw


def test_search_and_csv_use_same_decoder_without_rewriting_source(workspace):
    call, put, _, _ = workspace
    raw = '姓名,数值\n乙,2\n甲,1\n'.encode('gb18030')
    put(raw, 'input.csv')
    result = call('search', query='姓名')
    assert result['success'] and result['matches'][0]['encoding'] == 'gb18030'
    organized = call('organize_csv', path='input.csv', destination='sorted.csv', sort_by=['姓名'])
    assert organized['success'], organized
    assert call('read', path='sorted.csv')['encoding'] == 'utf-8'
    assert Path(call('export', path='input.csv')['file_path']).read_bytes() == raw


def test_limits_and_other_identity_remain_enforced(workspace):
    call, put, _, base = workspace
    put(('中' * (MAX_OUTPUT_BYTES // 3)).encode('utf-16'))
    result = call('read', path='input.txt')
    assert not result['success'] and 'output limit' in result['error']
    other = RequestSecurityPolicy('other', 'qqbot', 'room')
    with policy_scope(other):
        denied = execute_workspace({'action': 'read', 'path': 'input.txt'}, other, base_dir=base)
    assert not denied['success']
    assert not call('read', path='../outside.txt')['success']


def test_empty_text_and_gb18030_supplementary_characters():
    assert decode_text(b'')['text'] == ''
    assert decode_text('𠀀'.encode('gb18030'))['text'] == '𠀀'
    with pytest.raises(TextDecodingError):
        decode_text(codecs.BOM_UTF32_LE + b'x')
