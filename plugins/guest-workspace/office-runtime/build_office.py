"""Administrator-only builder; no guest-controlled flags or build context."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True, help='Hermes source root')
    parser.add_argument('--scratch', type=Path, required=True, help='Isolated build evidence directory')
    args = parser.parse_args()
    args.scratch.mkdir(parents=True, exist_ok=False)
    assets = Path(__file__).parent
    context = args.scratch / 'context'
    context.mkdir()
    for name in ('Dockerfile', 'requirements.txt', 'office_cli.py'):
        shutil.copyfile(assets / name, context / name)
    shutil.copyfile(args.source / 'tools/guest_office_worker.py', context / 'guest_office_worker.py')
    hashes = {name: hashlib.sha256((context / name).read_bytes()).hexdigest()
              for name in ('guest_office_worker.py', 'office_cli.py')}
    env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT') if k in os.environ}
    command = ['docker', 'build', '--pull=false', '--tag', 'hermes-guest-office:1',
               '--build-arg', 'WORKER_SHA256=' + hashes['guest_office_worker.py'],
               '--build-arg', 'HELPER_SHA256=' + hashes['office_cli.py'], str(context)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    overflow = threading.Event()

    def drain():
        total = 0
        with (args.scratch / 'build.log').open('wb') as log:
            while True:
                data = process.stdout.read(8192)
                if not data:
                    break
                total += len(data)
                if total > 16 * 1024 * 1024:
                    overflow.set()
                    break
                log.write(data)
                log.flush()
        process.stdout.close()

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    deadline = time.monotonic() + 900
    while process.poll() is None:
        if overflow.wait(0.1) or time.monotonic() > deadline:
            process.kill()
            break
    process.wait(timeout=3)
    thread.join(timeout=3)
    evidence = {'returncode': process.returncode, 'hashes': hashes, 'command': command}
    if process.returncode == 0:
        raw = subprocess.run(['docker', 'image', 'inspect', 'hermes-guest-office:1'],
                             capture_output=True, timeout=10, env=env, check=True).stdout
        (args.scratch / 'image-inspect.json').write_bytes(raw)
        evidence['image_id'] = json.loads(raw)[0]['Id']
    (args.scratch / 'build-result.json').write_text(json.dumps(evidence, indent=2))
    print(json.dumps(evidence))
    raise SystemExit(process.returncode or int(overflow.is_set() or thread.is_alive()))


if __name__ == '__main__':
    main()
