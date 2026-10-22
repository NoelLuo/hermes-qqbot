"""Typed failures, PDF download-only routing and read resource policy contracts."""
import base64
import json
import logging
import subprocess
import time

import pytest

from tools import guest_browser_docker as browser
from tools import guest_browser_network as network
from tools.guest_workspace_docker import _docker_env


def image():
    return subprocess.check_output(['docker', 'image', 'inspect', 'hermes-guest-browser:1.63.0',
                                    '--format', '{{.Id}}'], env=_docker_env(), text=True, timeout=10).strip()


def test_broker_preserves_safe_transport_code_without_exception_text():
    def fetch(*args, **kwargs):
        raise network.PublicNetworkError('secret query=cookie host/path', 'network_connect_timeout')
    event = {'event': 'fetch', 'id': 1, 'url': 'https://example.com/', 'method': 'GET', 'headers': {}}
    reply = browser._fetch_response(event, fetch, time.monotonic() + 5)
    assert reply == {'id': 1, 'success': False, 'error_code': 'network_connect_timeout'}


def test_cleanup_failure_keeps_original_cause_and_discards_artifacts(monkeypatch, caplog):
    immutable = 'sha256:' + 'a' * 64
    monkeypatch.setattr(browser, '_exchange', lambda *a: (0, json.dumps({'Id': immutable, 'Os': 'linux'}).encode(), b''))
    monkeypatch.setattr(browser, '_browser_exchange', lambda *a: browser._failure(error_code='network_tls_error'))

    def failed_cleanup(*args):
        raise RuntimeError('C:/secret/path?token=secret')
    monkeypatch.setattr(browser, '_remove', failed_cleanup)
    with caplog.at_level(logging.INFO, logger=browser.__name__):
        result = browser._run('https://example.com/?secret=1', [], image=immutable, timeout=5, fetcher=None)
    assert result['error_code'] == 'cleanup_failed'
    assert result['cause_error_code'] == 'network_tls_error'
    assert result['artifacts'] == {}
    assert result['correlation_id'] in caplog.text
    assert 'secret' not in json.dumps(result) + caplog.text


def test_real_pdf_direct_download_skips_html_navigation_and_rejects_pdf_read():
    calls = []
    pdf = b'%PDF-1.7\nfixture document bytes\n%%EOF'

    def fetch(url, **kwargs):
        calls.append(url)
        assert url == 'https://example.com/report.pdf'
        return {'status': 200, 'headers': {'content-type': 'application/octet-stream'}, 'body': pdf,
                'url': 'https://example.com/report.pdf'}
    result = browser._run('https://example.com/must-not-navigate',
                          [{'kind': 'download', 'url': 'https://example.com/report.pdf', 'name': 'report.pdf'}],
                          image=image(), timeout=30, fetcher=fetch)
    assert result['success'], result
    assert calls == ['https://example.com/report.pdf']
    assert result['result_type'] == 'download' and result['text'] == ''
    assert result['downloads'][0]['content_type'] == 'application/pdf'
    assert result['artifacts']['report.pdf'] == pdf
    read = browser._run('https://example.com/report.pdf', [{'kind': 'read'}], image=image(), timeout=30, fetcher=fetch)
    assert read['error_code'] == 'unsupported_document' and read['artifacts'] == {}


def test_real_read_skips_visual_resources_but_executes_javascript_and_preserves_soft404():
    seen = []

    def fetch(url, **kwargs):
        seen.append(url)
        if url.endswith('/code.js'):
            body, content_type = b'document.title="Soft 404"', 'application/javascript'
        else:
            body = b'<title>Before JS</title><link rel="stylesheet" href="/slow.css"><body>Page missing<img src="/slow.png"><script src="/code.js"></script></body>'
            content_type = 'text/html'
        return {'status': 200, 'headers': {'content-type': content_type}, 'body': body}
    result = browser._run('https://example.com/', [{'kind': 'read'}], image=image(), timeout=30, fetcher=fetch)
    assert result['success'], result
    assert result['http_status'] == 200 and result['title'] == 'Soft 404'
    assert 'Page missing' in result['text']
    assert seen == ['https://example.com/', 'https://example.com/code.js']


@pytest.mark.parametrize('status', [403, 404, 500])
def test_real_http_failure_is_not_transport_or_captcha(status):
    def fetch(url, **kwargs):
        return {'status': status, 'headers': {'content-type': 'text/html'}, 'body': b'<body>error</body>'}
    result = browser._run('https://example.com/', [{'kind': 'read'}], image=image(), timeout=30, fetcher=fetch)
    assert result['error_code'] == 'http_error' and not result['artifacts']


def test_download_metadata_size_must_match_real_bytes():
    value = {'success': True, 'title': '', 'url': 'https://example.com/', 'text': '', 'links': [],
             'artifacts': {'report.pdf': base64.b64encode(b'%PDF-').decode()}, 'result_type': 'download',
             'downloads': [{'name': 'report.pdf', 'url': 'https://example.com/', 'content_type': 'application/pdf',
                            'bytes': 999, 'http_status': 200}]}
    with pytest.raises(ValueError, match='size mismatch'):
        browser._decode_result(value)


def test_static_fallback_bounds_real_text_and_does_not_expose_script_content():
    from tools.guest_browser_worker import static_snapshot
    result = static_snapshot(b'<title>Fixture</title><script>not-visible</script><style>hidden</style>'
                             b'<body>Real server text <a href="next">Next</a>'
                             b'<a href="file:///etc/passwd">Unsafe</a></body>', 'https://example.com/base/')
    assert result['title'] == 'Fixture'
    assert 'Real server text' in result['text'] and 'not-visible' not in result['text']
    assert result['links'] == [{'text': 'Next', 'url': 'https://example.com/base/next'}]


def test_real_read_timeout_returns_explicit_static_html_not_fake_rendering():
    def fetch(url, **kwargs):
        return {'status': 200, 'headers': {'content-type': 'text/html'},
                'body': b'<title>Fallback fixture</title><script>while(true){}</script><body>Real server body</body>'}
    result = browser._run('https://example.com/', [{'kind': 'read'}], image=image(), timeout=40, fetcher=fetch)
    assert result['success'], result
    assert result['rendering'] == 'static_html' and 'not completed JavaScript' in result['warning']
    assert result['title'] == 'Fallback fixture' and 'Real server body' in result['text']
    assert result['artifacts'] == {}
