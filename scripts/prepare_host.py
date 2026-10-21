#!/usr/bin/env python3
"""Check/apply the reviewed overlay to an exact disposable Hermes baseline.

No network, dependencies, credentials, configuration edits or service operations.
Default is read-only; plugin copying additionally needs an absent explicit target.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
import subprocess
import sys
import uuid

BASE_COMMIT = 'ab44d8acf7bca230aea8574f2f2ab4666ca293ce'


class PreparationError(Exception):
    pass


def git(host: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(['git', '-C', str(host), *args], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=120)
    if check and result.returncode:
        raise PreparationError(result.stderr.strip() or result.stdout.strip() or 'git failed')
    return result


def relative(value: str) -> str:
    if (not isinstance(value, str) or not value or '\\' in value
            or any(ord(c) < 32 for c in value) or PureWindowsPath(value).drive):
        raise PreparationError('Invalid manifest path')
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {'', '.', '..', '.git'} for part in value.split('/')):
        raise PreparationError('Invalid manifest path')
    return str(path)


def content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def linked(path: Path) -> bool:
    """Reject links and Windows reparse points, including on Python 3.11."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))


def regular(path: Path, root: Path) -> bool:
    if not path.is_file() or linked(path):
        return False
    return path.resolve().is_relative_to(root.resolve()) and all(
        not linked(parent)
        for parent in (path, *path.parents) if parent != root.parent)


def manifest_files(project: Path) -> dict[str, str]:
    manifest = json.loads((project / 'source-manifest.json').read_text(encoding='utf-8'))
    if manifest['upstream']['commit'] != BASE_COMMIT:
        raise PreparationError('Manifest is not for this pinned baseline')
    result = {}
    folded = set()
    for row in manifest['files']:
        name = relative(row['path'])
        if name.casefold() in folded:
            raise PreparationError('Duplicate/case-alias manifest path')
        folded.add(name.casefold())
        source = project / 'overlay' / name
        if not regular(source, project / 'overlay') or content_hash(source) != row['sha256']:
            raise PreparationError('Overlay content does not match manifest: ' + name)
        result[name] = row['sha256']
    if not result:
        raise PreparationError('Empty manifest')
    patch = project / 'patches' / 'qqbot-suite.patch'
    if (not regular(patch, project)
            or hashlib.sha256(patch.read_bytes()).hexdigest() != manifest.get('patch_sha256')):
        raise PreparationError('Patch hash does not match manifest')
    return result


def install_plugin(project: Path, target: Path) -> None:
    if target.exists() or linked(target):
        raise PreparationError('Plugin target already exists; refusing to overwrite')
    source = project / 'plugins' / 'guest-workspace'
    files = [path for path in source.rglob('*') if path.is_file()]
    if not files or not all(regular(path, source) for path in files):
        raise PreparationError('Missing/unsafe plugin source')
    if any(linked(path) for path in source.rglob('*')):
        raise PreparationError('Plugin links are forbidden')
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / ('.guest-workspace-install-' + uuid.uuid4().hex)
    try:
        shutil.copytree(source, staging)
        staging.rename(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def prepare(project: Path, host: Path, *, apply: bool = False, plugin_target: Path | None = None) -> dict:
    if plugin_target is not None and not apply:
        raise PreparationError('Plugin installation requires --apply')
    project, host = project.resolve(), host.resolve()
    files = manifest_files(project)
    if git(host, 'rev-parse', 'HEAD').stdout.strip() != BASE_COMMIT:
        raise PreparationError('Host HEAD must be the exact pinned upstream commit')
    if Path(git(host, 'rev-parse', '--show-toplevel').stdout.strip()).resolve() != host:
        raise PreparationError('--host must be the checkout root')
    if plugin_target is not None and (plugin_target.exists() or linked(plugin_target)):
        raise PreparationError('Plugin target already exists; refusing to overwrite')
    matches = all(regular(host / name, host) and content_hash(host / name) == digest
                  for name, digest in files.items())
    if not matches:
        if git(host, 'status', '--porcelain', '--untracked-files=all').stdout.strip():
            raise PreparationError('Host is dirty; use a clean disposable checkout, not a running installation')
        patch = project / 'patches' / 'qqbot-suite.patch'
        if not regular(patch, project):
            raise PreparationError('Missing/unsafe reviewed patch')
        changes = git(host, 'apply', '--numstat', '-z', str(patch)).stdout
        patched_paths = {relative(row.split('\t', 2)[2]) for row in changes.split('\0') if row}
        if not patched_paths or not patched_paths <= files.keys():
            raise PreparationError('Patch changes paths outside the reviewed manifest')
        git(host, 'apply', '--check', str(patch))
        if apply:
            git(host, 'apply', str(patch))
            if not all(regular(host / name, host) and content_hash(host / name) == digest
                       for name, digest in files.items()):
                raise PreparationError('Post-apply manifest mismatch; checkout retained for inspection')
    if plugin_target is not None:
        install_plugin(project, plugin_target)
    return {'baseline': BASE_COMMIT, 'files': len(files),
            'status': 'already_matches' if matches else ('applied' if apply else 'check_passed'),
            'plugin_installed': plugin_target is not None,
            'config_modified': False, 'services_restarted': False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, type=Path)
    parser.add_argument('--apply', action='store_true', help='Explicitly apply to the exact clean baseline')
    parser.add_argument('--install-plugin', action='store_true')
    parser.add_argument('--plugin-dir', type=Path, help='Explicit new destination directory; never overwritten')
    args = parser.parse_args()
    if args.install_plugin != (args.plugin_dir is not None):
        parser.error('--install-plugin and --plugin-dir must be supplied together')
    try:
        result = prepare(Path(__file__).resolve().parents[1], args.host,
                         apply=args.apply, plugin_target=args.plugin_dir)
    except (PreparationError, OSError, ValueError, KeyError) as exc:
        print('Refused: ' + str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
