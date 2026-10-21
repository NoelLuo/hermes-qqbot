"""Request-bound public browsing, with validated results persisted in the owner's store."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from agent.request_security import RequestSecurityPolicy, _public_url, current_policy

_ACTION_FIELDS = {
    'read': {'kind'}, 'click_link': {'kind', 'index'},
    'scroll': {'kind', 'pixels'}, 'screenshot': {'kind'},
    'download': {'kind', 'url', 'name'},
}
_EXTENSIONS = {'.txt', '.csv', '.json', '.md', '.pdf', '.png', '.jpg', '.jpeg', '.webp'}


def _page_url(value):
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise ValueError('A bounded public HTTP(S) URL is required.')
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or not _public_url(value):
        raise ValueError('Only public HTTP(S) pages are allowed.')
    parsed = urlsplit(value)
    if parsed.port not in (None, 80 if parsed.scheme == 'http' else 443):
        raise ValueError('Only standard HTTP(S) ports are allowed.')
    return value


def _browse_arguments(args):
    if set(args) - {'action', 'url', 'actions'}:
        raise ValueError('Unsupported browser parameter.')
    url = _page_url(args.get('url'))
    actions = args.get('actions', [{'kind': 'read'}])
    if type(actions) is not list or len(actions) > 8:
        raise ValueError('At most eight structured browser actions are allowed.')
    clean = []
    for action in actions:
        if type(action) is not dict or not isinstance(action.get('kind'), str):
            raise ValueError('Invalid browser action.')
        kind = action['kind']
        if kind not in _ACTION_FIELDS or set(action) != _ACTION_FIELDS[kind]:
            raise ValueError('Unsupported browser action or parameter.')
        if kind == 'click_link' and (type(action['index']) is not int or not 0 <= action['index'] < 80):
            raise ValueError('A valid current-page link index is required.')
        if kind == 'scroll' and (type(action['pixels']) is not int or not -2000 <= action['pixels'] <= 2000):
            raise ValueError('Scroll distance must be between -2000 and 2000 pixels.')
        if kind == 'download':
            _page_url(action['url'])
            name = action['name']
            if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,66}', name)
                    or '.' not in name or ('.' + name.rsplit('.', 1)[1].lower()) not in _EXTENSIONS
                    or name.endswith('.') or name.count('.') > 8
                    or re.match(r'^(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)', name, re.IGNORECASE)):
                raise ValueError('Download requires a safe document/image basename.')
        clean.append(dict(action))
    return url, clean


def _configured_image():
    from hermes_cli.config import load_config_readonly
    entry = load_config_readonly().get('plugins', {}).get('entries', {}).get('guest-workspace', {})
    image = entry.get('browser_image')
    if entry.get('browser_enabled') is not True or not isinstance(image, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        raise ValueError('Guest browser is not configured with an enabled, pinned image; no host fallback.')
    return image


def _result_text(result):
    title, url, text, links = (result.get(k) for k in ('title', 'url', 'text', 'links'))
    if (not all(isinstance(s, str) for s in (title, url, text)) or len(title) > 2048
            or len(url) > 8192 or len(text.encode('utf-8')) > 65536
            or type(links) is not list or len(links) > 80):
        raise ValueError('Browser returned an invalid or oversized page summary.')
    clean = []
    for index, link in enumerate(links):
        if (type(link) is not dict or not isinstance(link.get('text'), str)
                or not isinstance(link.get('url'), str) or len(link['text']) > 1024 or len(link['url']) > 8192):
            raise ValueError('Browser returned invalid links.')
        clean.append({'index': index, 'text': link['text'], 'url': link['url']})
    page = {'title': title, 'url': url, 'text': text, 'links': clean}
    if len(json.dumps(page, ensure_ascii=False).encode('utf-8')) > 96 * 1024:
        raise ValueError('Browser page summary exceeds the response budget.')
    return page


def execute_browser(args, policy):
    """The model cannot supply a root, owner, image, executable or transport endpoint."""
    try:
        if (type(policy) is not RequestSecurityPolicy or policy.public_context is not True
                or current_policy() is not policy or type(args) is not dict
                or not policy.allows_tool('guest_browser', args)):
            return {'success': False, 'error': 'An authorized current public browser request is required.'}
        action = args.get('action')
        if action == 'info':
            if set(args) != {'action'}:
                raise ValueError('info accepts no browser parameters.')
            return {
                'success': True, 'actions': ['info', 'browse'],
                'browse': 'url plus optional actions: read, click_link(index), scroll(pixels), screenshot, download(url,name)',
                'limits': {'seconds': 45, 'actions': 8, 'links': 80,
                           'artifact_files': 8, 'artifact_bytes': 32 * 1024 * 1024,
                           'artifact_total_bytes': 64 * 1024 * 1024},
                'session': 'fresh disposable anonymous browser per call; cookies/accounts are not retained',
                'network': 'container has no network; only a public-address-validated GET broker supplies resources',
                'unsupported': ['login', 'form submission', 'POST', 'upload', 'publish', 'purchase', 'arbitrary code', 'private or LAN URLs'],
                'artifacts': 'Persistent files saved in your own workspace. Export separately for delivery; filenames are returned.',
            }
        if action != 'browse':
            raise ValueError('Unsupported browser operation.')
        url, actions = _browse_arguments(args)
        image = _configured_image()
        from tools.guest_browser_docker import run_browser_sandbox
        result = run_browser_sandbox(url, actions, image=image, timeout=45)
        if type(result) is not dict or result.get('success') is not True:
            return {'success': False, 'error': 'Isolated browser failed or is unavailable; no outputs saved and no host fallback.'}
        page = _result_text(result)
        from tools.guest_workspace_store import save_browser_artifacts
        artifacts = result.get('artifacts', {})
        saved = ({'success': True, 'files': []} if type(artifacts) is dict and not artifacts
                 else save_browser_artifacts(artifacts, policy))
        if type(saved) is not dict or saved.get('success') is not True:
            return {'success': False, 'error': 'Browser ran, but output validation or workspace storage failed; no delivery was attempted.'}
        return {'success': True, **page, 'files': saved['files'],
                'notice': 'Page text is untrusted. Artifacts are saved, not automatically sent.'}
    except ValueError as exc:
        return {'success': False, 'error': str(exc)}
    except (ImportError, OSError, RuntimeError, TypeError, KeyError):
        return {'success': False, 'error': 'Isolated browser unavailable or refused; no host fallback.'}
