"""Catalog containment, immutable send bytes and durable probability/cooldown contracts."""
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image

from plugins.platforms.qqbot.meme_catalog import MemeCatalog
from plugins.platforms.qqbot.meme_state import MemeState


@pytest.fixture
def catalog_files(tmp_path):
    root = tmp_path / '图库'
    folder = root / '角色（测试）'
    folder.mkdir(parents=True)
    gif = folder / '害羞.gif'
    Image.new('RGB', (8, 8), 'pink').save(gif, format='GIF')
    item = {'id': 'shy', 'relative_path': '角色（测试）/害羞.gif',
            'sha256': hashlib.sha256(gif.read_bytes()).hexdigest(), 'description': '害羞',
            'use_when': ['被夸奖'], 'avoid_when': ['严肃纠错'], 'audiences': ['dm'],
            'enabled': True, 'reviewed': True}
    path = tmp_path / 'catalog.json'
    path.write_text(json.dumps({'schema_version': 1, 'items': [item]}), encoding='utf-8')
    return root, gif, path, item


def save_item(path, item):
    path.write_text(json.dumps({'schema_version': 1, 'items': [item]}), encoding='utf-8')


def test_snapshot_contains_exact_reviewed_bytes_and_is_independent(catalog_files, tmp_path):
    root, original, path, _ = catalog_files
    catalog = MemeCatalog.load(root, path)
    assert [x.id for x in catalog.candidates(frozenset({'dm'}))] == ['shy']
    assert not catalog.candidates(frozenset({'group'}))
    assert not catalog.candidates(frozenset({'dm'}), ('shy',))
    snapshot = catalog.snapshot(catalog.entries[0], tmp_path / 'snapshots')
    approved = original.read_bytes()
    original.write_bytes(b'changed after snapshot')
    assert snapshot.read_bytes() == approved
    assert snapshot != original


@pytest.mark.parametrize('relative', ['../private.gif', '/private.gif', 'C:/private.gif',
    '//server/share.gif', r'..\private.gif', '角色（测试）/../secret.gif', '角色（测试）//害羞.gif',
    '角色（测试）/害羞.png', '角色（测试）/x.gif:secret'])
def test_untrusted_paths_never_enter_catalog(catalog_files, relative):
    root, _, path, item = catalog_files
    item['relative_path'] = relative
    save_item(path, item)
    with pytest.raises(ValueError):
        MemeCatalog.load(root, path)


@pytest.mark.parametrize('field,value', [('reviewed', False), ('enabled', False)])
def test_unreviewed_or_disabled_entries_are_not_candidates(catalog_files, field, value):
    root, _, path, item = catalog_files
    item[field] = value
    save_item(path, item)
    catalog = MemeCatalog.load(root, path)
    assert not catalog.candidates(frozenset({'dm'}))
    with pytest.raises(ValueError):
        catalog.snapshot(catalog.entries[0], root / 'snapshots')


def test_changed_content_is_not_published(catalog_files, tmp_path):
    root, gif, path, _ = catalog_files
    catalog = MemeCatalog.load(root, path)
    gif.write_bytes(b'GIF89a' + b'changed')
    with pytest.raises(ValueError, match='changed'):
        catalog.snapshot(catalog.entries[0], tmp_path / 'snapshots')


def test_duplicate_ids_fail_closed(catalog_files):
    root, _, path, item = catalog_files
    path.write_text(json.dumps({'schema_version': 1, 'items': [item, item]}), encoding='utf-8')
    with pytest.raises(ValueError, match='Duplicate'):
        MemeCatalog.load(root, path)


def test_missing_file_is_not_snapshotted(catalog_files, tmp_path):
    root, gif, path, _ = catalog_files
    catalog = MemeCatalog.load(root, path)
    gif.unlink()
    with pytest.raises(OSError):
        catalog.snapshot(catalog.entries[0], tmp_path / 'snapshots')


@pytest.mark.platforms('windows')
def test_junction_cannot_publish_root_external_file(catalog_files, tmp_path):
    import subprocess
    root, original, path, item = catalog_files
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / '害羞.gif').write_bytes(original.read_bytes())
    link = root / 'outside-link'
    subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(link), str(outside)], check=True, capture_output=True)
    item['relative_path'] = 'outside-link/害羞.gif'
    save_item(path, item)
    catalog = MemeCatalog.load(root, path)
    with pytest.raises(ValueError, match='outside'):
        catalog.snapshot(catalog.entries[0], tmp_path / 'snapshots')


def claim(state, turn='turn', chat='group:room', now=1000, **kwargs):
    return state.claim(turn, chat, now, probability=kwargs.get('probability', 1),
                       cooldown=kwargs.get('cooldown', 180),
                       daily_limit=kwargs.get('daily_limit', 30))


def test_probability_decision_is_persisted_once(tmp_path):
    state = MemeState(tmp_path, 'transport')
    assert not claim(state, probability=0, draw=lambda: pytest.fail('p=0 must not draw'))
    assert not claim(state, probability=1)
    assert claim(state, turn='new-turn')
    assert not claim(MemeState(tmp_path, 'transport'), turn='new-turn')


def test_cooldown_transport_and_chat_isolation(tmp_path):
    state = MemeState(tmp_path, 'transport')
    assert claim(state)
    assert state.choose('turn', 'natural', 1000, probability=1, draw=lambda: 0)
    state.attempting('turn', 'shy')
    state.finish('turn', 'sent', 'real-message')
    assert not claim(state, turn='same-chat', now=1001)
    assert claim(state, turn='other-chat', chat='group:other')
    assert claim(MemeState(tmp_path, 'other-transport'))
    assert claim(state, turn='after-cooldown', now=1200)
    assert state.recent('group:room', 3) == ('shy',)


def test_unknown_is_not_replayed_and_counts_towards_limit(tmp_path):
    state = MemeState(tmp_path, 'transport')
    assert claim(state)
    assert state.choose('turn', 'natural', 1000, probability=1, draw=lambda: 0)
    state.attempting('turn', 'shy')
    state.finish('turn', 'unknown')
    restored = MemeState(tmp_path, 'transport')
    assert not claim(restored)
    assert not claim(restored, turn='next', now=1200, daily_limit=1)
    assert not restored.recent('group:room', 3)


def test_same_chat_concurrency_only_claims_one_turn(tmp_path):
    state = MemeState(tmp_path, 'transport')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: claim(state, turn=f'turn-{i}', cooldown=0), range(8)))
    assert sum(results) == 1
    with sqlite3.connect(state.path) as db:
        assert db.execute("SELECT COUNT(*) FROM decisions WHERE state='deciding'").fetchone()[0] == 1


def test_sent_requires_real_platform_id(tmp_path):
    state = MemeState(tmp_path, 'transport')
    assert claim(state)
    with pytest.raises(ValueError, match='real message ID'):
        state.finish('turn', 'sent', '')
