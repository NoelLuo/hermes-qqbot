"""Bounded, disposable Linux Docker execution. No host execution fallback.

The caller must supply a trusted, preinstalled immutable image ID containing
python3 and its standard library. No persistence is performed by this module.
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import subprocess
import threading
import time

MAX_FILES = 32
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_CODE_BYTES = 64 * 1024
MAX_STREAM_BYTES = 64 * 1024
MAX_WIRE_BYTES = 2 * 1024 * 1024
_EXTENSIONS = {".txt", ".csv", ".json", ".md"}
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(?:\.|$)", re.IGNORECASE)


def _validate_name(name):
    if not isinstance(name, str) or not 1 <= len(name) <= 160:
        raise ValueError("Invalid document name")
    parts = name.split("/")
    if len(parts) > 5:
        raise ValueError("Document path too deep")
    for part in parts:
        if (not part or part in {".", ".."} or part[-1] in " ."
                or _RESERVED.match(part)
                or any(ord(c) < 32 or c in '\\:<>\"|?*' for c in part)):
            raise ValueError("Unsafe document path")
    if os.path.splitext(name)[1].lower() not in _EXTENSIONS:
        raise ValueError("Only txt/csv/json/md documents are allowed")


def _validate_files(files):
    if not isinstance(files, dict) or len(files) > MAX_FILES:
        raise ValueError("Document count limit exceeded")
    total = 0
    seen = set()
    for name, data in files.items():
        _validate_name(name)
        if name.casefold() in seen:
            raise ValueError("Case-insensitive document collision")
        seen.add(name.casefold())
        if not isinstance(data, bytes) or len(data) > MAX_FILE_BYTES:
            raise ValueError("Document byte limit exceeded")
        data.decode("utf-8")
        total += len(data)
    if total > MAX_TOTAL_BYTES:
        raise ValueError("Total document byte limit exceeded")


# This trusted supervisor is passed on argv, never stored in writable /work.
# The guest gets its own isolated interpreter, not exec() in this interpreter.
_RUNNER = r'''
import base64, ctypes, json, os, re, resource, selectors, stat, subprocess, sys, time

def safe_path(name):
    if len(name)>160 or len(name.split('/'))>5:
        raise ValueError('Output path limit exceeded')
    for part in name.split('/'):
        if (not part or part in ('.','..') or part[-1] in ' .'
            or re.match(r'^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(?:\.|$)',part,re.I)
            or any(ord(c)<32 or c in '\\:<>"|?*' for c in part)):
            raise ValueError('Unsafe output path')
MAX_FILE=262144
MAX_TOTAL=1048576
MAX_STREAM=65536

def collect():
    result={}
    total=0
    entries=0
    for root, dirs, names in os.walk('/work', followlinks=False):
        for name in dirs + names:
            entries+=1
            if entries > 160:
                raise ValueError('Workspace entry limit exceeded')
            path=os.path.join(root,name)
            safe_path(os.path.relpath(path,'/work'))
            s=os.lstat(path)
            if stat.S_ISLNK(s.st_mode):
                raise ValueError('Links forbidden')
            if stat.S_ISDIR(s.st_mode):
                continue
            if not stat.S_ISREG(s.st_mode):
                raise ValueError('Only regular documents allowed')
            if s.st_nlink != 1 or s.st_size > MAX_FILE:
                raise ValueError('File link/size limit exceeded')
            rel=os.path.relpath(path,'/work')
            if len(result)>=32:
                raise ValueError('Document count limit exceeded')
            fd=os.open(path,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd,'rb') as f:
                data=f.read(MAX_FILE+1)
            if len(data)>MAX_FILE:
                raise ValueError('File byte limit exceeded')
            data.decode('utf-8')
            total+=len(data)
            if total>MAX_TOTAL:
                raise ValueError('Total byte limit exceeded')
            result[rel]=base64.b64encode(data).decode('ascii')
    return result

def child_limits():
    # One sentinel byte lets inspection detect short writes even when Python
    # ignores SIGXFSZ and reports a flush error only during finalization.
    resource.setrlimit(resource.RLIMIT_FSIZE,(MAX_FILE+1,MAX_FILE+1))
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    resource.setrlimit(resource.RLIMIT_CPU,(budget,budget))
    # No grandchildren/threads: inspection happens only after the guest exits.
    resource.setrlimit(resource.RLIMIT_NPROC,(1,1))
    resource.setrlimit(resource.RLIMIT_NOFILE,(64,64))

out={'success':False,'stdout':'','stderr':'','files':{},'errors':[]}
try:
    # Block same-uid guest access to supervisor /proc/<pid>/{fd,mem}.
    if ctypes.CDLL(None).prctl(4,0,0,0,0)!=0:
        raise RuntimeError('Cannot protect supervisor')
    req=json.loads(sys.stdin.buffer.read(2097153))
    budget=req['timeout']
    for name,encoded in req['files'].items():
        path='/work/'+name
        os.makedirs(os.path.dirname(path),exist_ok=True)
        with open(path,'xb') as f:
            f.write(base64.b64decode(encoded,validate=True))
    p=subprocess.Popen([sys.executable,'-I','-B','-c',req['code']],cwd='/work',
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':'/tmp','LANG':'C.UTF-8'},
        start_new_session=True,preexec_fn=child_limits)
    sel=selectors.DefaultSelector()
    buffers={'stdout':bytearray(),'stderr':bytearray()}
    sel.register(p.stdout,selectors.EVENT_READ,'stdout')
    sel.register(p.stderr,selectors.EVENT_READ,'stderr')
    deadline=time.monotonic()+budget
    failure=None
    while sel.get_map():
        if time.monotonic()>=deadline:
            failure='Guest timeout'
            break
        for key,_ in sel.select(0.05):
            chunk=os.read(key.fileobj.fileno(),8192)
            if not chunk:
                sel.unregister(key.fileobj)
                continue
            buf=buffers[key.data]
            if len(buf)+len(chunk)>MAX_STREAM:
                failure='Guest output limit exceeded'
                break
            buf.extend(chunk)
        if failure:
            break
    if failure:
        p.kill()
    p.wait(timeout=2)
    for key,buf in buffers.items():
        out[key]=buf.decode('utf-8',errors='replace')
    if failure or p.returncode:
        raise RuntimeError(failure or 'Guest exited with status '+str(p.returncode))
    out['files']=collect()
    out['success']=True
except BaseException as e:
    out['success']=False
    out['files']={}
    out['errors']=[str(e)[:512]]
sys.stdout.write(json.dumps(out,ensure_ascii=True))
'''


def _docker_env():
    """CLI OS essentials only; never forward secrets or Docker remote settings."""
    keys = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT")
    return {key: os.environ[key] for key in keys if key in os.environ}


def _command(image, name):
    return ["docker", "run", "--rm", "--pull=never", "--name", name,
            "--network", "none", "--read-only", "--user", "65534:65534",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "16", "--cpus", "0.5", "--memory", "128m",
            "--memory-swap", "128m", "--ulimit", "core=0:0",
            "--tmpfs", "/work:rw,noexec,nosuid,nodev,size=32m,mode=0700,uid=65534,gid=65534",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=0700,uid=65534,gid=65534",
            "--workdir", "/work", "--log-driver", "none", "--entrypoint", "python3",
            "-i", image, "-I", "-B", "-c", _RUNNER]


def _exchange(command, payload, timeout, env):
    """Drain bounded pipes concurrently; communicate() would allocate without a cap."""
    p = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, shell=False, env=env)
    buffers = [bytearray(), bytearray()]
    overrun = threading.Event()

    def reader(pipe, buf):
        try:
            while True:
                chunk = pipe.read(8192)
                if not chunk:
                    return
                if len(buf) + len(chunk) > MAX_WIRE_BYTES:
                    overrun.set()
                    return
                buf.extend(chunk)
        finally:
            pipe.close()

    def writer():
        try:
            p.stdin.write(payload)
            p.stdin.close()
        except (BrokenPipeError, OSError):
            p.stdin.close()

    threads = [threading.Thread(target=reader, args=(p.stdout, buffers[0]), daemon=True),
               threading.Thread(target=reader, args=(p.stderr, buffers[1]), daemon=True),
               threading.Thread(target=writer, daemon=True)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    failure = None
    try:
        while p.poll() is None:
            if overrun.wait(0.05):
                failure = "Docker output limit exceeded"
                break
            if time.monotonic() >= deadline:
                failure = "Docker execution timeout"
                break
        if failure:
            p.kill()
        p.wait(timeout=3)
    finally:
        if p.poll() is None:
            p.kill()
        for thread in threads:
            thread.join(timeout=3)
    if failure or overrun.is_set() or any(t.is_alive() for t in threads):
        raise RuntimeError(failure or "Docker output limit/pipe failure")
    return p.returncode, bytes(buffers[0]), bytes(buffers[1])


def _decode_result(raw):
    result = json.loads(raw)
    if not isinstance(result, dict) or type(result.get("success")) is not bool:
        raise ValueError("Invalid sandbox response")
    for key in ("stdout", "stderr"):
        if not isinstance(result.get(key), str) or len(result[key].encode()) > MAX_STREAM_BYTES * 3:
            raise ValueError("Invalid sandbox stream")
    errors = result.get("errors")
    if not isinstance(errors, list) or len(errors) > 8 or any(
            not isinstance(e, str) or len(e) > 512 for e in errors):
        raise ValueError("Invalid sandbox errors")
    encoded = result.get("files")
    if not isinstance(encoded, dict) or len(encoded) > MAX_FILES:
        raise ValueError("Invalid sandbox files")
    files = {}
    for name, value in encoded.items():
        _validate_name(name)
        if not isinstance(value, str) or len(value) > MAX_FILE_BYTES * 2:
            raise ValueError("Invalid encoded document")
        files[name] = base64.b64decode(value, validate=True)
    _validate_files(files)
    if not result["success"] and files:
        raise ValueError("Failed sandbox returned documents")
    return {"success": result["success"], "stdout": result["stdout"],
            "stderr": result["stderr"], "files": files, "errors": errors}


def run_python_sandbox(code: str, files: dict[str, bytes], *, image: str,
                       timeout: int = 20) -> dict:
    """Run Python in a fresh container, returning success/stdout/stderr/files/errors.

    Documents are relative UTF-8 .txt/.csv/.json/.md files, at most 32 files,
    256 KiB each and 1 MiB total. Timeout must be 1..30 seconds; outer startup
    overhead is capped at ten additional seconds. No outputs survive failure.
    Docker must be locally available; unavailable Docker fails closed.
    """
    result = {"success": False, "stdout": "", "stderr": "", "files": {}, "errors": []}
    name = None
    env = _docker_env()
    try:
        if not isinstance(image, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("Trusted immutable sha256 image ID required")
        if type(timeout) is not int or not 1 <= timeout <= 30:
            raise ValueError("Timeout must be an integer from 1 to 30")
        if not isinstance(code, str) or len(code.encode("utf-8")) > MAX_CODE_BYTES:
            raise ValueError("Code byte limit exceeded")
        _validate_files(files)
        payload = json.dumps({"code": code, "timeout": timeout, "files": {
            n: base64.b64encode(v).decode("ascii") for n, v in files.items()}}).encode()
        status, metadata, _ = _exchange(
            ["docker", "image", "inspect", "--format", "{{json .}}", image], b"", 5, env)
        if status:
            raise RuntimeError("Trusted Docker image unavailable")
        info = json.loads(metadata)
        if (info.get("Id") != image or info.get("Os") != "linux"
                or info.get("Config", {}).get("Volumes")):
            raise ValueError("Image must be Linux with no declared volumes")
        name = "hermes-guest-" + secrets.token_hex(16)
        status, raw, stderr = _exchange(_command(image, name), payload, timeout + 10, env)
        if status:
            result["stderr"] = stderr[:MAX_STREAM_BYTES].decode("utf-8", errors="replace")
            raise RuntimeError("Docker sandbox failed (exit " + str(status) + ")")
        result = _decode_result(raw)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        result["success"] = False
        result["files"] = {}
        result["errors"] = [str(exc)[:512]]
    finally:
        if name is not None:
            # Scope is solely this call's unpredictable name. No daemon operations.
            try:
                subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=5, shell=False, env=env, check=False)
                status, _, diagnostic = _exchange(
                    ["docker", "container", "inspect", name], b"", 5, env)
                if status != 1 or b"No such" not in diagnostic:
                    raise RuntimeError("Container removal was not verified")
            except (OSError, RuntimeError, subprocess.SubprocessError):
                result = {"success": False, "stdout": result["stdout"],
                          "stderr": result["stderr"], "files": {},
                          "errors": result["errors"] + ["Container cleanup could not be confirmed"]}
    return result
