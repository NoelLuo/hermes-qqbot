#!/opt/office/bin/python3
"""Bounded office CLI: ZIP inspection/extraction and per-page PDF text."""
import argparse
import json
import os
from pathlib import Path
import stat
import zipfile

from guest_office_worker import MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES, validate_name


def zip_members(archive):
    result = []
    seen = set()
    total = 0
    if len(archive.infolist()) > MAX_FILES:
        raise ValueError('ZIP member count limit')
    for info in archive.infolist():
        name = info.filename.rstrip('/')
        validate_name(name)
        folded = name.casefold()
        mode = info.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if (folded in seen or info.flag_bits & 1
                or kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                or (kind == stat.S_IFDIR and not info.is_dir())):
            raise ValueError('ZIP duplicate, encrypted, link or special member')
        seen.add(folded)
        total += info.file_size
        if (info.file_size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES
                or info.file_size > max(info.compress_size, 1) * 200):
            raise ValueError('ZIP expansion limit')
        result.append((info, name))
    regular = {n.casefold() for i, n in result if not i.is_dir()}
    for _, name in result:
        parts = name.casefold().split('/')
        if any('/'.join(parts[:i]) in regular for i in range(1, len(parts))):
            raise ValueError('ZIP file/directory collision')
    return result


def zip_action(path, destination=None):
    with zipfile.ZipFile(path) as archive:
        members = zip_members(archive)
        if destination is None:
            return [{'name': n, 'bytes': i.file_size, 'directory': i.is_dir()} for i, n in members]
        # Require a fresh destination: no preexisting links, overwrites or races
        # with existing paths. The outer runtime still independently validates.
        root = Path(destination)
        root.mkdir(exist_ok=False, parents=False)
        total = 0
        for info, name in members:
            target = root / name
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            count = 0
            with archive.open(info) as src, os.fdopen(fd, 'wb') as dst:
                while True:
                    chunk = src.read(65536)
                    if not chunk:
                        break
                    count += len(chunk)
                    total += len(chunk)
                    if count > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                        raise ValueError('ZIP actual expansion limit')
                    dst.write(chunk)
        return {'extracted': len(members), 'bytes': total}


def pdf_text(path, start, end):
    from pypdf import PdfReader
    reader = PdfReader(path)
    if not 1 <= start <= end <= len(reader.pages) or end - start >= 100:
        raise ValueError('Invalid page range (max 100)')
    pages = []
    budget = 48000
    truncated = False
    for number in range(start, end + 1):
        text = reader.pages[number - 1].extract_text() or ''
        scan_suspected = not bool(text.strip())
        page_truncated = len(text.encode()) > budget
        truncated = truncated or page_truncated
        data = text.encode()[:budget]
        text = data.decode('utf-8', 'ignore')
        budget -= len(text.encode())
        pages.append({'page': number, 'text': text, 'scan_suspected': scan_suspected,
                      'truncated': page_truncated})
    return {'page_count': len(reader.pages), 'pages': pages, 'truncated': truncated}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('zip-list', 'zip-extract'):
        item = sub.add_parser(action)
        item.add_argument('path')
        if action == 'zip-extract':
            item.add_argument('destination')
    item = sub.add_parser('pdf-text')
    item.add_argument('path')
    item.add_argument('--start', type=int, default=1)
    item.add_argument('--end', type=int, default=1)
    args = parser.parse_args()
    if args.action == 'pdf-text':
        result = pdf_text(args.path, args.start, args.end)
    else:
        result = zip_action(args.path, getattr(args, 'destination', None))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
