#!/usr/bin/env python3
"""Bounded release hygiene checks, not a proof that source contains no secrets.

Optional private deny strings stay in a local file outside the published project.
Only finding category/path/line is reported; matched secret values are never echoed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

SKIP = {'.git', '__pycache__', '.pytest_cache', '.ruff_cache'}
FORBIDDEN = {'.env', 'auth.json', 'config.yaml', 'soul.md', 'user.md', 'memory.md',
             'state.db', 'config.private.yaml'}
SUFFIXES = {'.py', '.md', '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.txt',
            '.patch', '.sh', '.ps1', '.gitignore', '.gitattributes', '.dockerignore', '.dockerfile'}
NAMES = {'LICENSE', 'NOTICE', 'Dockerfile', '.gitignore', '.gitattributes', '.dockerignore'}
PATTERNS = {
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'github-token': re.compile(r'\bgh[pousr]_[A-Za-z0-9]{32,}\b|github_pat_[A-Za-z0-9_]{50,}'),
    'provider-token': re.compile(r'\bsk-[A-Za-z0-9_-]{30,}\b'),
    'personal-home': re.compile(r'(?i)[A-Z]:[\\/]+Users[\\/]+(?!test\b|demo\b|example\b|user\b|runner\b|<user>)[A-Za-z0-9_.-]+'),
}


def candidates(root: Path, staged: bool):
    if staged:
        result = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'], capture_output=True, check=True)
        return [root / name.decode('utf-8') for name in result.stdout.split(b'\0') if name]
    return [path for path in root.rglob('*') if path.is_file() and not SKIP.intersection(path.relative_to(root).parts)]


def audit(root: Path, *, staged: bool = False, deny=()) -> dict:
    root = root.resolve()
    files = candidates(root, staged)
    findings = []
    for path in sorted(files):
        relative = path.relative_to(root)
        name = relative.as_posix()
        if (path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(root)
                or any(parent.is_symlink() or parent.is_junction() for parent in path.parents if parent != root.parent)):
            findings.append({'path': name, 'kind': 'link-or-escape'})
            continue
        if (path.name.lower() in FORBIDDEN or path.name.startswith('.env')
                or path.suffix.lower() not in SUFFIXES and path.name not in NAMES):
            findings.append({'path': name, 'kind': 'unexpected-file-type'})
            continue
        if path.stat().st_size > 5 * 1024 * 1024:
            findings.append({'path': name, 'kind': 'oversized-source'})
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except UnicodeError:
            findings.append({'path': name, 'kind': 'binary-or-non-utf8'})
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for kind, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append({'path': name, 'line': number, 'kind': kind})
            if any(value and value.casefold() in line.casefold() for value in deny):
                findings.append({'path': name, 'line': number, 'kind': 'private-deny-match'})
    if not files:
        findings.append({'path': '.', 'kind': 'empty-release'})
    return {'files_checked': len(files), 'findings': findings, 'passed': not findings,
            'scope': 'bounded source hygiene only; not comprehensive secret/security certification'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--staged', action='store_true')
    parser.add_argument('--deny-file', type=Path)
    args = parser.parse_args()
    deny = json.loads(args.deny_file.read_text(encoding='utf-8')) if args.deny_file else []
    if not isinstance(deny, list) or any(not isinstance(value, str) for value in deny):
        parser.error('deny file must be a JSON string array')
    result = audit(args.root, staged=args.staged, deny=deny)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
