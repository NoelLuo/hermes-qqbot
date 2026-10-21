#!/usr/bin/env python3
"""Run the pinned host's canonical, real-home-guarded targeted regression suite.

Prepare the disposable host first. This does not install dependencies, pull images,
change configuration or restart services. Live tests need documented Docker images.
"""
import argparse
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys

from prepare_host import PreparationError, prepare, relative


def bash_path(explicit):
    candidates = [explicit] if explicit else []
    if not explicit:
        git = shutil.which('git')
        if git:
            candidates.append(str(Path(git).parent.parent / 'usr/bin/bash.exe'))
        candidates.append(shutil.which('bash'))
    for value in candidates:
        if value and Path(value).is_file():
            path = Path(value).resolve()
            if os.name == 'nt' and 'system32' in str(path).casefold():
                continue
            return str(path)
    raise PreparationError('Supply --bash with Git Bash on Windows, not the WSL System32 launcher')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', type=Path, required=True)
    parser.add_argument('--test-python', type=Path, required=True)
    parser.add_argument('--bash')
    parser.add_argument('--jobs', type=int, default=1, help='Default serializes resource-heavy Docker/browser files')
    parser.add_argument('--log', type=Path, help='Optional local log; do not publish raw host logs')
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    try:
        state = prepare(project, args.host)
        if state['status'] != 'already_matches':
            raise PreparationError('Apply the reviewed patch first; refusing to test the unmodified host')
        if not args.test_python.is_file() or not 1 <= args.jobs <= 8:
            raise PreparationError('Invalid prepared test interpreter or jobs')
        manifest = json.loads((project / 'source-manifest.json').read_text(encoding='utf-8'))
        files = sorted({relative(value) for value in manifest['tests']})
        if not files or any(not (args.host / name).is_file() for name in files):
            raise PreparationError('Missing manifest tests')
        env = os.environ.copy()
        env.pop('__HERMES_ACTIVATED', None)
        env['HERMES_PYTHON'] = str(args.test_python.resolve())
        env['PYTHONIOENCODING'] = 'utf-8'
        command = [bash_path(args.bash), 'scripts/run_tests.sh', *files,
                   '-p', 'tests.qq_role_runtime_bootstrap', '-j', str(args.jobs),
                   '--file-retries', '0', '--tb=short']
        image = env.get('QQBOT_TEST_PYTHON_IMAGE', '')
        if image:
            if re.fullmatch(r'sha256:[0-9a-f]{64}', image) is None:
                raise PreparationError('QQBOT_TEST_PYTHON_IMAGE must be a full immutable sha256 image ID')
            command.extend(['--', '--qqbot-test-python-image=' + image])
        if args.log:
            args.log.parent.mkdir(parents=True, exist_ok=True)
        log = args.log.open('w', encoding='utf-8') if args.log else None
        try:
            with subprocess.Popen(command, cwd=args.host, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace') as process:
                for line in process.stdout:
                    print(line, end='', flush=True)
                    if log:
                        log.write(line)
                        log.flush()
                return process.wait()
        finally:
            if log:
                log.close()
    except (PreparationError, OSError, ValueError, KeyError) as exc:
        print('Refused: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
