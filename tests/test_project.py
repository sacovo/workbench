"""Project discovery and the project's private state directory."""

from __future__ import annotations

import stat

import pytest

from hub_helpers import git, isolate_git, make_repo
from workbench.buildid import build_id
from workbench.errors import ProjectError
from workbench.project import find_project, read_hub_info, write_hub_info


def test_walks_up_to_the_first_pyproject_with_a_workbench_table(tmp_path):
    root = tmp_path / "outer"
    (root / "pkg" / "deep").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[tool.workbench]\nmanifest = "outer.manifest"\n')
    # A nested package's pyproject without the table is passed over...
    (root / "pkg" / "pyproject.toml").write_text('[project]\nname = "pkg"\n')
    assert find_project(root / "pkg" / "deep").root == root.resolve()
    # ...but a nested one with the table is a project of its own.
    (root / "pkg" / "pyproject.toml").write_text('[tool.workbench]\nmanifest = "inner.manifest"\n')
    inner = find_project(root / "pkg" / "deep")
    assert (inner.root, inner.manifest) == ((root / "pkg").resolve(), "inner.manifest")


def test_a_project_without_the_table_is_an_error_that_says_what_to_add(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "plain"\n')
    with pytest.raises(ProjectError) as excinfo:
        find_project(tmp_path)
    message = str(excinfo.value)
    assert "[tool.workbench]" in message
    assert str(tmp_path / "pyproject.toml") in message  # names the file it looked at
    assert 'manifest = "' in message


@pytest.mark.parametrize(
    ("table", "needle"),
    [
        ('manfest = "a.manifest"', "unknown key(s) manfest"),  # a typo is not silently ignored
        ('watch = ["src"]', '"manifest" is required'),
        ('manifest = "robot/manifest.py"', "dotted module path"),
        ('manifest = "a.manifest"\nwatch = "src"', "watch must be a list"),
        ('manifest = "a.manifest"\nout = "../elsewhere"', "inside the project"),
        ('manifest = "a.manifest"\nwatch = ["/abs"]', "inside the project"),
    ],
)
def test_the_workbench_table_is_validated_strictly(tmp_path, table, needle):
    (tmp_path / "pyproject.toml").write_text(f"[tool.workbench]\n{table}\n")
    with pytest.raises(ProjectError) as excinfo:
        find_project(tmp_path)
    assert needle in str(excinfo.value)


def test_state_dir_stays_out_of_git_and_the_build_id(tmp_path, monkeypatch):
    # The project's .gitignore does not list .workbench/; the directory must ignore itself, or
    # every wb call would change the build id and a snapshot would commit the hub's token.
    isolate_git(monkeypatch)
    project = find_project(make_repo(tmp_path / "proj"))
    before = build_id(project.root)
    write_hub_info(project, port=4321, token="secret-token", pid=12345)
    with project.lock():
        pass
    project.ensure_blobs_dir()
    project.ensure_logs_dir()
    (project.logs_dir / "hub.log").write_text("log\n")
    assert git(project.root, "status", "--porcelain", "--untracked-files=all") == ""
    assert build_id(project.root) == before
    info = read_hub_info(project)
    assert (info.pid, info.port, info.token) == (12345, 4321, "secret-token")
    assert stat.S_IMODE(project.hub_json.stat().st_mode) == 0o600  # holds the token
