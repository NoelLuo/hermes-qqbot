"""Resolve opt-in requester policy from the bot-owning config, never message labels."""
from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urlsplit

from agent.request_security import PUBLIC_COMMANDS, PUBLIC_TOOLS, RequestSecurityPolicy


def grant_inbound_media(policy: RequestSecurityPolicy | None, media_urls: Any) -> None:
    """Authorize only trusted transport attachment fields, never paths from chat text."""
    # QQ media_urls can include someone else's quoted/background attachments.
    # Only the typed transport receipt seam may grant QQ imports after restore.
    if policy is None or policy.platform == 'qqbot' or not isinstance(media_urls, (list, tuple)):
        return
    for value in media_urls:
        if not isinstance(value, str) or not value:
            continue
        try:
            parsed = urlsplit(value)
        except ValueError:
            continue
        if parsed.scheme in {"http", "https", "data"}:
            continue
        if parsed.scheme == "file":
            if parsed.netloc:
                continue
            value = unquote(parsed.path)
            if len(value) > 3 and value[0] == "/" and value[2] == ":":
                value = value[1:]
        policy.grant_artifact(value)


def _ids(value: Any) -> frozenset[str]:
    if isinstance(value, str):
        value = value.split(',')
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(x.strip() for x in value if isinstance(x, str) and x.strip())


def grant_verified_event_attachments(runner: Any, event: Any) -> list[str]:
    """Grant only transport receipts revalidated against FINAL execution paths."""
    if _platform(event.source) != 'qqbot':
        return []
    from gateway.inbound_attachment_proofs import eligible_paths
    policy = policy_for_source(runner, event.source)
    paths = eligible_paths(event) if policy is not None else []
    return [path for path in paths if policy.grant_artifact(path)]


def _platform(source: Any) -> str:
    value = getattr(source, 'platform', '')
    value = getattr(value, 'value', value)
    return value if isinstance(value, str) else ''


def attachment_workspace_note(runner: Any, event: Any, eligible: list[str]) -> str:
    """Explain final import grants without promoting downloaded quote/background media."""
    if eligible:
        return ('[本请求已核验可导入附件；import_attachment 的 source 使用以下最终路径，'
                'path 另填本人工作区相对目标；列表外的引用或背景附件不因此获得导入授权]\n'
                + '\n'.join(eligible))
    policy = policy_for_source(runner, event.source)
    if (_platform(event.source) == 'qqbot' and policy is not None
            and policy.public_context and getattr(event, 'media_urls', None)):
        return ('[附件导入授权说明：本请求没有已核验可供 import_attachment 使用的附件路径。'
                '缓存中的引用或背景附件仅是资料，不代表本请求获得导入权限。'
                '若引用此前已导入的文件，先 list 本人工作区并复用已保存文件的相对路径，'
                '不要未经检查就要求重传，也不要按文件名或哈希绕过授权。]')
    return ''


def policy_from_extra(extra: dict, source: Any, *, scope_id: str = 'default') -> RequestSecurityPolicy | None:
    if _platform(source) != 'qqbot':
        return None
    options = extra.get('role_policy') if isinstance(extra, dict) else None
    if not isinstance(options, dict) or options.get('enabled') is not True:
        return None
    subject = getattr(source, 'user_id', '')
    chat = getattr(source, 'chat_id', '')
    subject = subject if isinstance(subject, str) else ''
    chat = chat if isinstance(chat, str) else ''
    chat_type = getattr(source, 'chat_type', '')
    private = chat_type in {'dm', 'c2c', 'direct', 'private'}
    admins = _ids(extra.get('allow_admin_from' if private else 'group_allow_admin_from'))
    admin = bool(subject and chat and subject in admins)
    if getattr(source, 'profile_route_rejected', False) is True:
        admin = False
        subject = ''
    requested = options.get('public_tools')
    tools = PUBLIC_TOOLS if requested is None else (_ids(requested) & PUBLIC_TOOLS)
    policy = RequestSecurityPolicy(subject_id=subject, platform='qqbot', chat_id=chat, is_admin=admin,
        public_context=not (private and admin), allowed_tools=tools,
        allowed_commands=PUBLIC_COMMANDS, scope_id=scope_id)
    # The source is trusted runtime state, not a wire/prompt field. Keep the same
    # request's verified artifact ledger across model execution and final delivery.
    previous = getattr(source, '_request_security_policy', None)
    if isinstance(previous, RequestSecurityPolicy) and previous.cache_key() == policy.cache_key():
        return previous
    if hasattr(source, '__dict__'):
        source._request_security_policy = policy
    return policy


def policy_for_adapter(adapter: Any, source: Any) -> RequestSecurityPolicy | None:
    config = getattr(adapter, 'config', None)
    extra = getattr(config, 'extra', None)
    if not isinstance(extra, dict):
        extra = {}
    owner = getattr(adapter, '_owner_profile', None)
    owner = owner if isinstance(owner, str) and owner else 'default'
    return policy_from_extra(extra, source, scope_id=owner)


def policy_for_source(runner: Any, source: Any) -> RequestSecurityPolicy | None:
    if _platform(source) != 'qqbot':
        return None
    config = getattr(runner, 'config', None)
    owner = getattr(runner, '_primary_profile_name', None) or 'default'
    if not isinstance(owner, str):
        owner = 'default'
    if getattr(config, 'multiplex_profiles', False) is True:
        from gateway.session_identity import identity_of
        identity = identity_of(source)
        transport = getattr(identity, 'transport_profile', None) if identity is not None else getattr(source, 'profile', None)
        if isinstance(transport, str) and transport and transport != owner:
            owner = transport
            config = (getattr(runner, '_profile_configs', None) or {}).get(owner)
            if config is None:
                # An unresolved owner must never inherit the launch bot's grants.
                return RequestSecurityPolicy('', 'qqbot', '', allowed_tools=frozenset(), scope_id=owner)
    platforms = getattr(config, 'platforms', None)
    platform_config = platforms.get(getattr(source, 'platform', None)) if isinstance(platforms, dict) else None
    if platform_config is None and isinstance(platforms, dict):
        platform_config = platforms.get('qqbot')
    extra = getattr(platform_config, 'extra', None)
    if not isinstance(extra, dict):
        extra = platform_config if isinstance(platform_config, dict) else {}
    return policy_from_extra(extra, source, scope_id=owner)
