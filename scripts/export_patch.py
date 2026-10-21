#!/usr/bin/env python3
"""Regenerate the reviewed patch from manifest-listed sources and an exact base.

Writes release artifacts only, never changes/applies to the host or its Git state.
"""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import subprocess

from prepare_host import BASE_COMMIT, PreparationError, content_hash, relative


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', type=Path, required=True, help='Git checkout containing the pinned base objects')
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    manifest_path = project / 'source-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest['upstream']['commit'] != BASE_COMMIT:
        raise PreparationError('Unexpected baseline')
    patches = []
    changed = 0
    for row in manifest['files']:
        name = relative(row['path'])
        public = project / 'overlay' / name
        if content_hash(public) != row['sha256']:
            raise PreparationError('Changed public overlay: ' + name)
        result = subprocess.run(['git', '-C', str(args.host), 'show', BASE_COMMIT + ':' + name],
                                capture_output=True, timeout=30)
        if result.returncode and row['base_sha256'] is not None:
            raise PreparationError('Missing base file: ' + name)
        before = result.stdout if result.returncode == 0 else b''
        if row['base_sha256'] is not None and hashlib.sha256(before).hexdigest() != row['base_sha256']:
            raise PreparationError('Base file hash mismatch: ' + name)
        if result.returncode == 0 and row['base_sha256'] is None:
            raise PreparationError('Unexpected existing base file: ' + name)
        after = public.read_bytes().replace(b'\r\n', b'\n')
        if before != after:
            changed += 1
            lines = difflib.unified_diff(before.decode('utf-8').splitlines(keepends=True),
                                        after.decode('utf-8').splitlines(keepends=True),
                                        fromfile='a/' + name if result.returncode == 0 else '/dev/null',
                                        tofile='b/' + name)
            for line in lines:
                patches.append(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n')
    data = ''.join(patches).encode('utf-8')
    path = project / 'patches/qqbot-suite.patch'
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    manifest['patch_sha256'] = hashlib.sha256(data).hexdigest()
    manifest_path.write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
    print(json.dumps({'overlay_files': len(manifest['files']), 'patch_files': changed,
                      'patch_sha256': manifest['patch_sha256']}))


if __name__ == '__main__':
    main()
