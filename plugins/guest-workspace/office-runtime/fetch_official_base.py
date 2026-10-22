"""Fetch a digest-verified official base via host HTTPS, never daemon proxy changes.

Administrator-only build aid. OCI archive contains only the official selected
platform manifest, config and verified layer blobs; no host files/credentials.
"""
import hashlib
import json
from pathlib import Path
import sys
import tarfile
import urllib.request

ROOT = 'https://registry-1.docker.io/v2/library/python/'
DIGEST = 'sha256:b4409e44a3e971621ee4bc2f3dc7a53859980685d261ab7a3a7bd4f0b84445c7'


def main():
    destination = Path(sys.argv[1])
    destination.mkdir(exist_ok=False, parents=True)
    token = json.load(urllib.request.urlopen(
        'https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull', timeout=30))['token']

    def fetch(kind, digest):
        request = urllib.request.Request(ROOT + kind + '/' + digest, headers={
            'Authorization': 'Bearer ' + token,
            'Accept': 'application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json'})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(256 * 1024 * 1024 + 1)
        if len(data) > 256 * 1024 * 1024 or 'sha256:' + hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Official registry digest/size mismatch')
        return data

    index = json.loads(fetch('manifests', DIGEST))
    selected = next(m for m in index['manifests'] if m.get('platform', {}).get('os') == 'linux'
                    and m['platform'].get('architecture') == 'amd64')
    manifest_raw = fetch('manifests', selected['digest'])
    manifest = json.loads(manifest_raw)
    blobs = destination / 'blobs/sha256'
    blobs.mkdir(parents=True)
    (blobs / selected['digest'].split(':')[1]).write_bytes(manifest_raw)
    for descriptor in [manifest['config'], *manifest['layers']]:
        digest = descriptor['digest']
        (blobs / digest.split(':')[1]).write_bytes(fetch('blobs', digest))
        print('Verified', digest, flush=True)
    selected['annotations'] = {'org.opencontainers.image.ref.name': 'hermes-office-official-base:1'}
    (destination / 'index.json').write_text(json.dumps({'schemaVersion': 2, 'manifests': [selected]}))
    (destination / 'oci-layout').write_text('{"imageLayoutVersion":"1.0.0"}')
    archive = destination.with_suffix('.tar')
    with tarfile.open(archive, 'w') as tar:
        for file in destination.rglob('*'):
            if file.is_file():
                tar.add(file, arcname=str(file.relative_to(destination)).replace('\\', '/'))
    print(json.dumps({'archive': str(archive), 'official_index_digest': DIGEST,
                      'platform_digest': selected['digest'], 'config_id': manifest['config']['digest']}))


if __name__ == '__main__':
    main()
