"""Opt-in canonical-runner bootstrap experiment; no production state or guard patches.

Load with: scripts/run_tests.sh <paths> -- -p tests.qq_role_runtime_bootstrap
"""
import importlib
import os
import re

import pytest


def pytest_addoption(parser):
    parser.addoption('--qqbot-test-python-image', default='',
                     help='Explicit immutable local Python Docker image for QQ tests')


def pytest_configure(config):
    image = config.getoption('--qqbot-test-python-image')
    if image:
        if re.fullmatch(r'sha256:[0-9a-f]{64}', image) is None:
            raise pytest.UsageError('--qqbot-test-python-image requires a full sha256 image ID')
        # The canonical runner drops non-allowlisted environment variables.
        # Restore only this explicit, non-secret opt-in inside each pytest child.
        os.environ['QQBOT_TEST_PYTHON_IMAGE'] = image


def pytest_collection(session):
    # conftest has already isolated HERMES_HOME and disabled lazy installs.
    # Loading as a -p hermes_bootstrap plugin would precede those protections.
    importlib.import_module("hermes_bootstrap")
    importlib.import_module("pm.install")
    local = importlib.import_module("tools.environments.local")
    # Resolve the real launcher once during collection, using its existing cache.
    local._resolve_hermes_bin_dir()


@pytest.fixture(autouse=True)
def _qq_role_runtime_store(tmp_path, monkeypatch):
    # Documented process-local PM store override, not a manifest substitute.
    store = tmp_path / "runtime-store"
    store.mkdir()
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(store))
