"""Admin build: host-download signed Debian indexes/debs and pinned wheels.

No listener, proxy/daemon changes or host mounts. Docker build is network=none;
APT validates Debian signatures and indexes; host checks every package SHA256.
"""
import concurrent.futures
import hashlib
import json
import lzma
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import unquote

import httpx

ROOT = Path(__file__).parent
SOURCE = Path(sys.argv[1]).resolve()
WORK = Path(sys.argv[2]).resolve()
WORK.mkdir(parents=True, exist_ok=True)
BASE = 'sha256:17cbbc787c42807b179b559a5356f87804f01812cbcabd57f2becd3605ce65cd'
APPS = 'bash zip unzip poppler-utils fonts-noto-cjk libreoffice-writer libreoffice-calc libreoffice-impress'
ENV = {k: os.environ[k] for k in ('PATH','SYSTEMROOT','WINDIR','COMSPEC','PATHEXT') if k in os.environ}
ENV['DOCKER_BUILDKIT'] = '0'
REPOS = [('debian','bookworm'),('debian','bookworm-updates'),('debian-security','bookworm-security')]
MIRROR = 'https://mirrors.ustc.edu.cn/'


def download(url, target, digest=None):
    if target.exists() and (digest is None or hashlib.sha256(target.read_bytes()).hexdigest() == digest):
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(6):
        try:
            attempt_url = url
            proxy = None
            if attempt >= 4:
                attempt_url = url.replace(MIRROR, 'https://deb.debian.org/')
                proxy = os.environ.get('HERMES_OFFICE_BUILD_PROXY') or None
            elif attempt >= 2:
                attempt_url = url.replace(MIRROR, 'https://mirrors.tuna.tsinghua.edu.cn/')
            with httpx.Client(trust_env=False, proxy=proxy, timeout=httpx.Timeout(90, connect=10), follow_redirects=True) as client:
                with client.stream('GET', attempt_url) as response:
                    response.raise_for_status()
                    h = hashlib.sha256()
                    with target.with_suffix(target.suffix+'.part').open('wb') as stream:
                        for chunk in response.iter_bytes():
                            stream.write(chunk)
                            h.update(chunk)
            if digest and h.hexdigest() != digest:
                raise ValueError('Download SHA256 mismatch')
            target.with_suffix(target.suffix+'.part').replace(target)
            return
        except (httpx.HTTPError, OSError):
            print('download retry', target.name, attempt + 1, flush=True)
            if attempt == 5:
                raise


def run(command, label, timeout=900):
    with (WORK/(label+'.log')).open('wb') as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, env=ENV, timeout=timeout)
    if result.returncode:
        raise RuntimeError(label+' failed; see '+str(WORK/(label+'.log')))


packages = {}
for repo, suite in REPOS:
    relative = f'dists/{suite}/InRelease'
    release = WORK/'repos'/repo/relative
    download(MIRROR+repo+'/'+relative, release)
    index_rel = 'main/binary-amd64/Packages.xz'
    pattern = re.compile(r'^ ([0-9a-f]{64})\s+\d+\s+'+re.escape(index_rel)+r'$', re.M)
    matches = pattern.findall(release.read_text())
    if len(matches) != 1:
        raise RuntimeError('Missing signed index SHA256')
    index = WORK/'repos'/repo/f'dists/{suite}'/index_rel
    download(MIRROR+repo+f'/dists/{suite}/'+index_rel, index, matches[0])
    for paragraph in lzma.decompress(index.read_bytes()).decode().split('\n\n'):
        fields = dict(line.split(': ',1) for line in paragraph.splitlines() if ': ' in line and not line.startswith(' '))
        if 'Filename' in fields:
            packages[(repo, fields['Filename'])] = fields['SHA256']
print('Verified signed-index package hashes:',len(packages),flush=True)

prep = WORK/'Dockerfile.prepare'
prep.write_text('FROM '+BASE+'\nCOPY repos /repos\nRUN rm -f /etc/apt/sources.list.d/* && printf "%s\\n" '+
    ' '.join("'deb file:/repos/"+repo+' '+suite+" main'" for repo,suite in REPOS)+
    ' > /etc/apt/sources.list && apt-get -o Acquire::Languages=none update && apt-get --print-uris --yes --no-install-recommends install '+APPS+' > /opt/package-uris.txt\n')
if not (WORK/'package-uris.txt').exists():
    run(['docker','build','--pull=false','--network=none','--iidfile',str(WORK/'prepare.id'),'-f',str(prep),str(WORK)],'prepare')
    image = (WORK/'prepare.id').read_text().strip()
    uris = subprocess.check_output(['docker','run','--rm','--pull=never','--network=none','--read-only','--entrypoint=cat',image,'/opt/package-uris.txt'],env=ENV,timeout=20).decode()
    (WORK/'package-uris.txt').write_text(uris)
uris = (WORK/'package-uris.txt').read_text()
items=[]
for uri in re.findall(r"^'([^']+\.deb)'",uris,re.M):
    match=re.fullmatch(r'file:/repos/([^/]+)/(pool/.+)',uri)
    if not match: raise RuntimeError('Unexpected package URI')
    repo,path=match.groups()
    path=unquote(path)
    digest=packages[(repo,path)]
    items.append((MIRROR+repo+'/'+path,WORK/'debs'/Path(path).name,digest))
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
    list(pool.map(lambda item:download(*item),items))
print('Verified Debian packages:',len(items),flush=True)

wheels=WORK/'wheels'
wheels.mkdir(exist_ok=True)
pip_env=dict(ENV,PIP_DISABLE_PIP_VERSION_CHECK='1')
if os.environ.get('HERMES_OFFICE_BUILD_PROXY'):
    pip_env.update(HTTP_PROXY=os.environ['HERMES_OFFICE_BUILD_PROXY'],HTTPS_PROXY=os.environ['HERMES_OFFICE_BUILD_PROXY'])
with (WORK/'wheels.log').open('wb') as log:
    result=subprocess.run([shutil.which('pip') or 'pip','download','--dest',str(wheels),'--platform','manylinux_2_28_x86_64','--platform','manylinux2014_x86_64','--python-version','3.13','--implementation','cp','--abi','cp313','--only-binary=:all:','--index-url','https://pypi.org/simple','-r',str(ROOT/'requirements.txt')],env=pip_env,stdout=log,stderr=subprocess.STDOUT,timeout=240)
if result.returncode:raise RuntimeError('Wheel download failed; see wheels.log')
for name in ('requirements.txt','office_cli.py'):
    shutil.copyfile(ROOT/name,WORK/name)
shutil.copyfile(SOURCE/'tools/guest_office_worker.py',WORK/'guest_office_worker.py')
hashes={n:hashlib.sha256((WORK/n).read_bytes()).hexdigest() for n in ('guest_office_worker.py','office_cli.py')}
text=(ROOT/'Dockerfile').read_text()
text=re.sub(r'^FROM .+$','FROM '+BASE,text,flags=re.M)
start=text.index('RUN apt-get update')
end=text.index('COPY guest_office_worker.py')
text=text[:start]+('COPY debs /opt/debs\nRUN DPKG_DEB_THREADS_MAX=1 DEBIAN_FRONTEND=noninteractive dpkg -i /opt/debs/*.deb && rm -rf /opt/debs /repos\nCOPY wheels /opt/wheels\nCOPY requirements.txt /opt/requirements.txt\nRUN python3 -m venv /opt/office && /opt/office/bin/pip install --no-index --find-links=/opt/wheels -r /opt/requirements.txt && rm -rf /opt/wheels\n')+text[end:]
(WORK/'Dockerfile').write_text(text)
run(['docker','build','--pull=false','--network=none','--memory=2g','--memory-swap=2g','--cpu-quota=100000','--cpu-period=100000','--tag','hermes-guest-office:1','--iidfile',str(WORK/'office.id'),'--build-arg','WORKER_SHA256='+hashes['guest_office_worker.py'],'--build-arg','HELPER_SHA256='+hashes['office_cli.py'],str(WORK)],'build')
result={'image_id':(WORK/'office.id').read_text().strip(),'hashes':hashes,'deb_packages':len(items),'wheel_files':len(list(wheels.glob('*.whl'))),'network_during_build':'none'}
(WORK/'build-result.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result),flush=True)
