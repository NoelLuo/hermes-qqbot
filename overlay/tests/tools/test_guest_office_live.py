"""Real offline office commands, binary persistence, export and namespace cleanup."""
import json
import os
import shlex
import subprocess
import zipfile

import pytest
import hermes_yaml as yaml

from agent.request_security import RequestSecurityPolicy, policy_scope
from hermes_constants import get_hermes_home
from tools.guest_office_docker import run_office_sandbox, _docker_env
from tools.guest_workspace_store import execute_workspace


@pytest.fixture(scope='module')
def image():
    p = subprocess.run(['docker', 'image', 'inspect', 'hermes-guest-office:1', '--format', '{{.Id}}'],
                       capture_output=True, text=True, timeout=15, env=_docker_env(), check=False)
    if p.returncode:
        pytest.skip('Trusted office image not built')
    return p.stdout.strip()


@pytest.fixture
def office(image, tmp_path):
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'entries': {
        'guest-workspace': {'office_image': image}}}}), encoding='utf-8')
    policy = RequestSecurityPolicy('live-office-visitor', 'qqbot', 'test-room')
    base = tmp_path / 'workspaces'

    def call(**args):
        with policy_scope(policy):
            return execute_workspace(args, policy, base_dir=base)
    return call, policy, base


def test_real_binary_zip_pdf_docx_xlsx_pptx_and_headless_pdf_persist_export(office, tmp_path):
    call, policy, base = office
    source = tmp_path / 'input.zip'
    # Stored random data exercises a genuine near-30MB binary input, not a tiny
    # highly compressed fixture that would never cross the old text budget.
    with zipfile.ZipFile(source, 'w', compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('payload.bin', os.urandom(24 * 1024 * 1024))
        archive.writestr('notes.txt', 'office fixture')
    policy.grant_artifact(source)
    assert call(action='import_attachment', source=str(source), path='input.zip')['success']
    code = '''from pathlib import Path
from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from reportlab.pdfgen import canvas
from pypdf import PdfReader
import json, os, socket
assert os.getuid() == 65534
assert 'OFFICE_HOST_CANARY' not in os.environ
assert not Path('/var/run/docker.sock').exists()
try: socket.create_connection(('1.1.1.1', 80), timeout=0.2)
except OSError: pass
else: raise RuntimeError('network unexpectedly available')
Path('out').mkdir()
doc = Document(); doc.add_paragraph('Verified office fixture'); doc.save('out/report.docx')
book = Workbook(); book.active.append(['name', 'value']); book.active.append(['fixture', 42]); book.save('out/table.xlsx')
slides = Presentation(); slide = slides.slides.add_slide(slides.slide_layouts[0]); slide.shapes.title.text='Verified slide'; slides.save('out/slides.pptx')
pdf = canvas.Canvas('out/source.pdf'); pdf.drawString(60, 750, 'PDF fixture text'); pdf.save()
assert 'PDF fixture text' in PdfReader('out/source.pdf').pages[0].extract_text()
Path('out/check.txt').write_text('BINARY_OFFICE_OK', encoding='utf-8')
'''
    command = ('office-cli zip-list input.zip >/tmp/members.json && office-cli zip-extract input.zip unpacked '
               '&& python3 -c ' + shlex.quote(code) +
               ' && office-cli pdf-text out/source.pdf >out/pdf-text.json'
               ' && libreoffice -env:UserInstallation=file:///tmp/lo-profile --headless --convert-to pdf --outdir out out/report.docx')
    result = call(action='run_office', command=command, timeout=90)
    assert result['success'], result
    expected = {'unpacked/payload.bin', 'unpacked/notes.txt', 'out/report.docx', 'out/table.xlsx',
                'out/slides.pptx', 'out/source.pdf', 'out/report.pdf', 'out/check.txt', 'out/pdf-text.json'}
    assert expected <= set(result['output_files']), result
    assert call(action='read', path='out/check.txt')['text'] == 'BINARY_OFFICE_OK'
    text = json.loads(call(action='read', path='out/pdf-text.json')['text'])
    assert 'PDF fixture text' in text['pages'][0]['text'] and not text['truncated']
    # A subsequent fresh container receives persisted binary results.
    again = call(action='run_office', command="python3 -c \"from docx import Document; print(Document('out/report.docx').paragraphs[0].text)\"", inputs=['out/report.docx'])
    assert again['success'] and 'Verified office fixture' in again['stdout'], again
    for relative in ('out/report.pdf', 'out/report.docx', 'out/table.xlsx', 'out/slides.pptx'):
        exported = call(action='export', path=relative)
        assert exported['success'] and policy.allows_media(exported['file_path'])
        assert not RequestSecurityPolicy('other', 'qqbot', 'test-room').allows_media(exported['file_path'])
    root = next(p for p in base.iterdir() if p.is_dir())
    assert (root / 'out/report.pdf').read_bytes().startswith(b'%PDF-')
    for name in ('report.docx', 'table.xlsx', 'slides.pptx'):
        with zipfile.ZipFile(root / 'out' / name) as archive:
            assert archive.testzip() is None


def test_real_short_command_failure_overwrite_and_escaped_descendant(image):
    result = run_office_sandbox('true', {}, image=image)
    assert result['success'], result
    failed = run_office_sandbox('printf partial >partial.txt; exit 7', {}, image=image)
    assert not failed['success'] and failed['error_code'] == 'command_failed' and not failed['files']
    command = "python3 -c 'import os,time; p=os.fork(); (os.setsid(),time.sleep(60)) if p==0 else None'"
    timed = run_office_sandbox(command, {}, image=image, timeout=1)
    assert not timed['success'] and timed['error_code'] == 'timeout' and not timed['files'], timed
    escaped = run_office_sandbox("python3 -c 'import subprocess; subprocess.Popen([\"sleep\",\"60\"],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)'", {}, image=image)
    assert escaped['success'], escaped


def test_real_unsafe_outputs_and_zip_paths_fail_without_persistence(office, tmp_path):
    call, policy, _ = office
    bad = call(action='run_office', command='ln -s /etc/passwd leak.txt', inputs=[])
    assert not bad['success'] and call(action='list')['entries'] == []
    archive = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('../escape.txt', 'must not be saved')
    policy.grant_artifact(archive)
    assert call(action='import_attachment', source=str(archive), path='bad.zip')['success']
    bad = call(action='run_office', command='office-cli zip-extract bad.zip output')
    assert not bad['success']
    assert [x['path'] for x in call(action='list')['entries']] == ['bad.zip']
