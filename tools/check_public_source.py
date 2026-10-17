"""Fail before publishing if staged Git files contain runtime data or secrets.

This is a conservative local check, not a replacement for reviewing the staged
diff. It only inspects files already selected for this repository's commit and
never opens the App's data directory or a browser profile.
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_PARTS = {'data', 'downloads', 'browser-profile', 'browser-profiles', '.venv',
                 'node_modules', 'dist', 'build', 'build-config', 'qa', '__pycache__'}
PRIVATE_SUFFIXES = {'.db', '.sqlite', '.sqlite3', '.log', '.har', '.pem', '.p12', '.pfx'}
SECRETS = [
    re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(rb'gh[pousr]_[A-Za-z0-9]{36,}'),
    re.compile(rb'github_pat_[A-Za-z0-9_]{60,}'),
    re.compile(rb'GOCSPX-[A-Za-z0-9_-]{20,}'),
]


def main():
    result = subprocess.run(['git', 'ls-files', '--stage', '-z'], cwd=ROOT,
                            check=True, stdout=subprocess.PIPE)
    entries = [entry for entry in result.stdout.split(b'\0') if entry]
    failures = []
    for entry in entries:
        metadata, name = entry.split(b'\t', 1)
        mode, object_id, stage = metadata.decode('ascii').split()
        relative = Path(name.decode('utf-8'))
        if any(part.lower() in PRIVATE_PARTS or part.lower().startswith(('generated-browser-', '.qa-')) for part in relative.parts):
            failures.append(f'Runtime/private path: {relative.as_posix()}')
            continue
        if relative.suffix.lower() in PRIVATE_SUFFIXES:
            failures.append(f'Private/generated file type: {relative.as_posix()}')
            continue
        if mode not in {'100644', '100755'} or stage != '0':
            failures.append(f'Unexpected file/link: {relative.as_posix()}')
            continue
        # Inspect the exact staged blob: a later working-tree edit must not hide
        # a credential that is still selected for the next commit.
        blob_id = object_id
        size = subprocess.run(['git', 'cat-file', '-s', blob_id], cwd=ROOT,
                              check=True, stdout=subprocess.PIPE)
        if int(size.stdout) > 2 * 1024 * 1024:
            failures.append(f'Unexpected large source file: {relative.as_posix()}')
            continue
        content = subprocess.run(['git', 'cat-file', 'blob', blob_id], cwd=ROOT,
                                 check=True, stdout=subprocess.PIPE).stdout
        if any(pattern.search(content) for pattern in SECRETS):
            # Report only the path. Never print a matching credential.
            failures.append(f'Potential credential: {relative.as_posix()}')
    if failures:
        print('\n'.join(failures), file=sys.stderr)
        return 1
    print(f'Public source check passed: {len(entries)} staged files; no runtime data or known credential patterns.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
