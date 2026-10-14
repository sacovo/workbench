import shutil

import pytest
from support import MINIMAL, NOISY, KernelProcess


@pytest.fixture(scope="module")
def minimal_kernel(tmp_path_factory):
    """One kernel on examples/minimal for a test module; at the end its stdout must hold nothing
    but the responses the tests read, and it must exit cleanly on end of stdin."""
    kernel = KernelProcess(MINIMAL, tmp_path_factory.mktemp("minimal") / "stderr.log")
    yield kernel
    assert kernel.close() == b""
    assert kernel.proc.returncode == 0


@pytest.fixture
def noisy_project(tmp_path):
    """A copy of the noisy fixture project (its blobs land in the copy), importable via PYTHONPATH."""
    project = tmp_path / "noisy"
    shutil.copytree(NOISY, project)
    return project
