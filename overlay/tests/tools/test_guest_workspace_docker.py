"""Contract tests; real Docker tests skip explicitly when local Docker is unavailable."""
import io
import json
import subprocess

import pytest

from tools import guest_workspace_docker as sandbox

IMAGE = "sha256:" + "a" * 64


def wire(**kwargs):
    result = {"success": True, "stdout": "", "stderr": "", "files": {}, "errors": []}
    result.update(kwargs)
    return json.dumps(result).encode()


def fake_exchange(monkeypatch, run_result=None, volumes=None):
    calls = []

    def exchange(command, payload, timeout, env):
        calls.append((command, payload, timeout, env))
        if command[1:3] == ["container", "inspect"]:
            return 1, b"", b"Error: No such container"
        if command[1:3] == ["image", "inspect"]:
            return 0, json.dumps({"Id": IMAGE, "Os": "linux", "Config": {"Volumes": volumes}}).encode(), b""
        return run_result or (0, wire(), b"")

    monkeypatch.setattr(sandbox, "_exchange", exchange)
    cleanup = []
    monkeypatch.setattr(sandbox.subprocess, "run", lambda command, **kw: cleanup.append((command, kw)))
    return calls, cleanup


def test_invocation_is_hardened_and_cleanup_scoped(monkeypatch):
    calls, cleanup = fake_exchange(monkeypatch)
    monkeypatch.setenv("SECRET_TEST_TOKEN", "never-forward")
    monkeypatch.setenv("DOCKER_HOST", "tcp://untrusted.invalid:2375")
    result = sandbox.run_python_sandbox("print('ok')", {"in.txt": b"hello"}, image=IMAGE)
    assert result["success"]
    command, payload, _, env = next(c for c in calls if c[0][1] == "run")
    assert "SECRET_TEST_TOKEN" not in env and "DOCKER_HOST" not in env
    for option, value in [("--network", "none"), ("--user", "65534:65534"),
                          ("--cap-drop", "ALL"), ("--security-opt", "no-new-privileges"),
                          ("--entrypoint", "python3"), ("--pull=never", None),
                          ("--pids-limit", "16"), ("--memory", "128m"),
                          ("--cpus", "0.5"), ("--read-only", None), ("--rm", None)]:
        assert option in command
        if value:
            assert command[command.index(option) + 1] == value
    assert not set(command) & {"--volume", "-v", "--mount", "--privileged", "--env", "-e"}
    assert command[-4:-1] == ["-I", "-B", "-c"]
    assert json.loads(payload)["files"] == {"in.txt": "aGVsbG8="}
    name = command[command.index("--name") + 1]
    assert name.startswith("hermes-guest-") and len(name) == len("hermes-guest-") + 32
    assert cleanup[0][0] == ["docker", "rm", "-f", name]
    assert cleanup[0][1]["shell"] is False


@pytest.mark.parametrize("name", ["/abs.txt", "../a.txt", "a/../b.txt", "C:/a.txt", "a\\b.txt",
                                   "NUL.txt", "a/COM1.csv", "LPT¹.txt", "a .txt/../b.txt",
                                   "a.txt:stream", "a\x00.txt", "a//b.txt", "a.exe", "a./x.txt"])
def test_invalid_paths_never_start_docker(monkeypatch, name):
    def forbidden(*a, **kw):
        pytest.fail("invalid input spawned Docker")
    monkeypatch.setattr(sandbox, "_exchange", forbidden)
    result = sandbox.run_python_sandbox("pass", {name: b"x"}, image=IMAGE)
    assert not result["success"] and not result["files"]


@pytest.mark.parametrize("overrides", [
    {"image": "python:latest"}, {"image": "--privileged"}, {"timeout": 0},
    {"timeout": 31}, {"timeout": True}, {"code": "x" * (sandbox.MAX_CODE_BYTES + 1)},
    {"files": {"x.txt": b"\xff"}}, {"files": {"x.txt": b"x" * (sandbox.MAX_FILE_BYTES + 1)}},
    {"files": {f"{i}.txt": b"" for i in range(33)}},
    {"files": {f"{i}.txt": b"a" * sandbox.MAX_FILE_BYTES for i in range(5)}},
    {"files": {"A.txt": b"", "a.txt": b""}},
])
def test_limits_fail_closed(monkeypatch, overrides):
    monkeypatch.setattr(sandbox, "_exchange", lambda *a: pytest.fail("must validate before Docker"))
    args = {"code": "pass", "files": {}, "image": IMAGE}
    args.update(overrides)
    result = sandbox.run_python_sandbox(**args)
    assert not result["success"] and result["errors"] and not result["files"]


def test_unavailable_docker_never_falls_back(monkeypatch):
    def unavailable(*args):
        raise FileNotFoundError("docker unavailable")
    monkeypatch.setattr(sandbox, "_exchange", unavailable)
    result = sandbox.run_python_sandbox("raise AssertionError('host execution')", {}, image=IMAGE)
    assert not result["success"] and "docker unavailable" in result["errors"][0]


def test_declared_image_volume_rejected(monkeypatch):
    calls, cleanup = fake_exchange(monkeypatch, volumes={"/config": {}})
    assert not sandbox.run_python_sandbox("pass", {}, image=IMAGE)["success"]
    assert len(calls) == 1 and not cleanup


@pytest.mark.parametrize("files", [{"../escape.txt": "eA=="}, {"NUL.txt": "eA=="},
                                    {"x.txt": "////"}, {"x.py": "eA=="}])
def test_result_paths_and_content_revalidated(monkeypatch, files):
    fake_exchange(monkeypatch, run_result=(0, wire(files=files), b""))
    result = sandbox.run_python_sandbox("pass", {}, image=IMAGE)
    assert not result["success"] and not result["files"]


def test_failed_execution_discards_files(monkeypatch):
    fake_exchange(monkeypatch, run_result=(0, wire(success=False, files={"x.txt": "eA=="}), b""))
    result = sandbox.run_python_sandbox("pass", {}, image=IMAGE)
    assert not result["success"] and not result["files"]


@pytest.mark.parametrize("reason", ["Docker execution timeout", "Docker output limit exceeded"])
def test_outer_timeout_and_output_overrun_cleanup(monkeypatch, reason):
    _calls, cleanup = fake_exchange(monkeypatch)
    original = sandbox._exchange
    def exchange(command, *args):
        if command[1] == "run":
            raise RuntimeError(reason)
        return original(command, *args)
    monkeypatch.setattr(sandbox, "_exchange", exchange)
    result = sandbox.run_python_sandbox("pass", {}, image=IMAGE)
    assert not result["success"] and result["errors"] == [reason]
    assert len(cleanup) == 1


def test_popen_no_shell_and_bounded_capture(monkeypatch):
    class Process:
        def __init__(self):
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(b"x" * (sandbox.MAX_WIRE_BYTES + 1))
            self.stderr = io.BytesIO()
        def poll(self):
            return 0
        def wait(self, timeout):
            return 0
        def kill(self):
            pass
    options = []
    def popen(command, **kwargs):
        options.append(kwargs)
        return Process()
    monkeypatch.setattr(sandbox.subprocess, "Popen", popen)
    with pytest.raises(RuntimeError, match="output limit"):
        sandbox._exchange(["docker", "run"], b"{}", 2, {})
    assert options[0]["shell"] is False and options[0]["env"] == {}


@pytest.fixture(scope="module")
def real_image():
    # Discover only already-installed images. Never pull or inherit Docker credentials.
    try:
        rc, raw, _ = sandbox._exchange(["docker", "image", "ls", "--no-trunc", "--quiet"],
                                      b"", 5, sandbox._docker_env())
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        pytest.skip("Real Docker unavailable in isolated test environment: " + str(exc)[:160])
    if rc:
        pytest.skip("Real Docker daemon unavailable in isolated test environment")
    for image in dict.fromkeys(raw.decode().splitlines()):
        if not image.startswith("sha256:"):
            continue
        result = sandbox.run_python_sandbox("print('guest-ready')", {}, image=image, timeout=3)
        if result["success"] and result["stdout"] == "guest-ready\n":
            return image
    pytest.skip("No installed suitable Linux python3 image (nothing was pulled)")


def test_real_transform_and_isolation(real_image):
    code = """
import os, socket
assert os.getuid() == 65534
assert not os.path.exists('/var/run/docker.sock')
try:
    open('/root-write-test', 'w')
except OSError:
    pass
else:
    raise AssertionError('writable root')
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
except OSError:
    pass
else:
    raise AssertionError('network enabled')
with open('input.txt') as f:
    text=f.read()
with open('answer.md','w') as f:
    f.write(text.upper())
print('done')
"""
    result = sandbox.run_python_sandbox(code, {"input.txt": b"hello sandbox"}, image=real_image)
    assert result["success"], result
    assert result["stdout"] == "done\n"
    assert result["files"]["answer.md"] == b"HELLO SANDBOX"


@pytest.mark.parametrize("code", [
    "open('ok.txt','w').write('not persisted'); raise RuntimeError('bad')",
    "import os; os.symlink('/etc/passwd','bad.txt')",
    "import os; os.mkdir('d'); os.symlink('/tmp','d/link')",
    "import os; open('a.txt','w').write('a'); os.link('a.txt','b.txt')",
    "import os; os.mkfifo('bad.txt')",
    "import socket; s=socket.socket(socket.AF_UNIX); s.bind('bad.txt')",
    "open('NUL.txt','w').write('bad')",
    "open('bad.exe','w').write('bad')",
    "open('bad.txt','wb').write(bytes([255]))",
    "open('big.txt','w').write('x'*300000)",
    "[open(str(i)+'.txt','w').close() for i in range(33)]",
    "print('x'*100000)",
    "while True: pass",
    # Guest monkeypatching cannot affect trusted serialization or validation.
    "import json,os; json.dumps=lambda *a,**k:'forged'; os.symlink('/etc/passwd','bad.txt')",
    "open('/proc/1/fd/1','w').write('forged response')",
])
def test_real_adversarial_fail_closed(real_image, code):
    result = sandbox.run_python_sandbox(code, {}, image=real_image, timeout=1)
    assert not result["success"], result
    assert not result["files"]
