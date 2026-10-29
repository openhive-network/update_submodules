"""Release-mode git logic of update-submodules.py, against throwaway repositories.

`create_merge_back_branch` and `find_rebase_base` are exercised on small histories
built with `git` in `tmp_path`: local bare remotes only, no network, no GitLab.
"""
import importlib.util
import pathlib
import subprocess
from typing import Any, Dict, List, Optional, Tuple

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "update-submodules.py"
_spec = importlib.util.spec_from_file_location("update_submodules", _SCRIPT)
if _spec is None or _spec.loader is None:
    raise ImportError(f"cannot load {_SCRIPT}")
us = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(us)

REPO_URL = "git@gitlab.syncad.com:hive/example.git"
TAG = "1.0.0"
RC_TAG = "1.0.0rc1"


@pytest.fixture(autouse=True)
def isolated_git(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the user's and the system's git config (and any hook-set GIT_* paths) out."""
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_CEILING_DIRECTORIES"):
        monkeypatch.delenv(var, raising=False)


def git(cwd: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def is_ancestor(cwd: pathlib.Path, ancestor: str, descendant: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, descendant],
                          cwd=cwd, capture_output=True).returncode == 0


def init_repo(path: pathlib.Path, branch: str = "develop") -> pathlib.Path:
    path.mkdir()
    git(path, "init", "-q", "-b", branch)
    _set_identity(path)
    return path


def _set_identity(path: pathlib.Path) -> None:
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@example.com")


def commit(repo: pathlib.Path, filename: str, content: str, message: Optional[str] = None) -> str:
    (repo / filename).write_text(content)
    git(repo, "add", filename)
    git(repo, "commit", "-q", "-m", message or f"{filename}: {content}")
    return git(repo, "rev-parse", "HEAD")


def rebased_copies(repo: pathlib.Path, *commits: str) -> str:
    """Cherry-pick onto the current branch: same trees, different SHAs (how releases land on master)."""
    for c in commits:
        git(repo, "cherry-pick", "-x", c)
    return git(repo, "rev-parse", "HEAD")


def tree(repo: pathlib.Path, rev: str) -> str:
    return git(repo, "rev-parse", f"{rev}^{{tree}}")


def merge_back_branches(repo: pathlib.Path) -> List[str]:
    return git(repo, "branch", "--list", "merge-*", "--format=%(refname:short)").split()


@pytest.fixture
def released(tmp_path: pathlib.Path) -> Tuple[pathlib.Path, str]:
    """A clone mid-release: origin has develop (rc-tagged) and master; HEAD is the tagged release tip.

    develop:  b - d1 - d2(rc1) - d3
    master:   b - m1 - m2 - r(TAG)       m1/m2 are rebased copies of d1/d2, r is the release commit
    """
    seed = init_repo(tmp_path / "seed")
    commit(seed, "a.txt", "base")
    git(seed, "branch", "master")
    d1 = commit(seed, "a.txt", "one")
    d2 = commit(seed, "b.txt", "two")
    git(seed, "tag", RC_TAG)
    commit(seed, "c.txt", "three")
    git(seed, "checkout", "-q", "master")
    rebased_copies(seed, d1, d2)

    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    _set_identity(clone)
    git(clone, "checkout", "-q", "-b", f"release/{TAG}", "origin/master")
    release_tip = commit(clone, "VERSION", TAG, f"Release {TAG}")
    git(clone, "tag", TAG)
    return clone, release_tip


def run_merge_back(clone: pathlib.Path, settings: Optional[Dict[str, Any]] = None) -> Any:
    result = us.RepoOperationResult(repo_url=REPO_URL)
    us.create_merge_back_branch(str(clone), REPO_URL, TAG, settings or {}, result)
    return result


# --- create_merge_back_branch ------------------------------------------------

def test_merge_back_branch_is_ours_merge_of_release_tip_into_develop(released: Tuple[pathlib.Path, str]) -> None:
    clone, release_tip = released
    develop_tip = git(clone, "rev-parse", "origin/develop")
    assert git(clone, "describe", "--tags", "origin/develop").startswith(RC_TAG)
    assert not is_ancestor(clone, TAG, "origin/develop")

    result = run_merge_back(clone)

    branch = "merge-1.0.0-into-develop"
    assert branch == us.merge_back_branch_name(TAG, "develop")
    assert merge_back_branches(clone) == [branch]
    assert result.merge_back_branch == branch
    assert result.merge_back_target == "develop"
    assert tree(clone, branch) == tree(clone, "origin/develop")
    assert git(clone, "rev-list", "--parents", "-n", "1", branch).split()[1:] == [develop_tip, release_tip]
    assert is_ancestor(clone, TAG, branch)
    assert git(clone, "describe", "--tags", branch).split("-")[0] == TAG
    # The release branch checked out for the sanity check is untouched.
    assert git(clone, "rev-parse", "HEAD") == release_tip
    assert git(clone, "status", "--porcelain") == ""


def test_merge_back_disabled_creates_nothing(released: Tuple[pathlib.Path, str]) -> None:
    clone, _ = released
    result = run_merge_back(clone, {"merge_back": False})
    assert merge_back_branches(clone) == []
    assert result.merge_back_branch is None and result.merge_back_target is None


def test_merge_back_skipped_without_origin_target_branch(tmp_path: pathlib.Path) -> None:
    """A master-only repo: origin has no develop."""
    seed = init_repo(tmp_path / "seed", branch="master")
    commit(seed, "a.txt", "base")
    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    _set_identity(clone)
    commit(clone, "VERSION", TAG)
    git(clone, "tag", TAG)

    result = run_merge_back(clone)
    assert merge_back_branches(clone) == []
    assert result.merge_back_branch is None and result.merge_back_target is None


def test_merge_back_skipped_for_fast_forward_release(tmp_path: pathlib.Path) -> None:
    """master fast-forwarded to develop: the release tip is already reachable from origin/develop."""
    seed = init_repo(tmp_path / "seed")
    commit(seed, "a.txt", "base")
    commit(seed, "a.txt", "one")
    git(seed, "branch", "master", "develop")
    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    git(clone, "checkout", "-q", "-b", f"release/{TAG}", "origin/master")
    git(clone, "tag", TAG)

    result = run_merge_back(clone)
    assert merge_back_branches(clone) == []
    assert result.merge_back_branch is None and result.merge_back_target is None


def test_merge_back_into_configured_branch(released: Tuple[pathlib.Path, str]) -> None:
    clone, release_tip = released
    git(clone, "push", "-q", "origin", "origin/develop~1:refs/heads/release/x")
    git(clone, "fetch", "-q", "origin")
    target_tip = git(clone, "rev-parse", "origin/release/x")

    result = run_merge_back(clone, {"merge_back_branch": "release/x"})

    branch = "merge-1.0.0-into-release-x"
    assert merge_back_branches(clone) == [branch]
    assert (result.merge_back_branch, result.merge_back_target) == (branch, "release/x")
    assert tree(clone, branch) == tree(clone, "origin/release/x")
    assert git(clone, "rev-list", "--parents", "-n", "1", branch).split()[1:] == [target_tip, release_tip]


# --- find_rebase_base --------------------------------------------------------

def test_rebase_base_for_master_as_rebased_copy_of_develop(tmp_path: pathlib.Path) -> None:
    """Shape: master is a rebased copy of develop's first commits (same trees, different SHAs)."""
    repo = init_repo(tmp_path / "repo")
    commit(repo, "a.txt", "base")
    git(repo, "branch", "master")
    d1 = commit(repo, "a.txt", "one")
    d2 = commit(repo, "b.txt", "two")
    commit(repo, "c.txt", "three")
    git(repo, "checkout", "-q", "master")
    master_head = rebased_copies(repo, d1, d2)
    assert master_head != d2

    assert us.find_rebase_base(str(repo), "develop", "master") == d2


def test_rebase_base_after_merge_back_is_a_develop_commit(tmp_path: pathlib.Path) -> None:
    """Shape: a merge-back has landed, so master's rebased copies are reachable from develop
    through the merge's second parent.  The base must still be on develop's own line —
    matching master against itself would replay every develop commit onto master.

    develop:  b - d1 - d2 - M - d3       M = ours-merge of r, tree of d2
    master:   b - m1 - m2 - r            r = release commit (adds VERSION)
    """
    repo = init_repo(tmp_path / "repo")
    commit(repo, "a.txt", "base")
    git(repo, "branch", "master")
    d1 = commit(repo, "a.txt", "one")
    d2 = commit(repo, "b.txt", "two")
    git(repo, "checkout", "-q", "master")
    rebased_copies(repo, d1, d2)
    release = commit(repo, "VERSION", TAG, f"Release {TAG}")
    git(repo, "checkout", "-q", "develop")
    git(repo, "merge", "-q", "-s", "ours", "--no-edit", release)
    merge_back = git(repo, "rev-parse", "HEAD")
    commit(repo, "c.txt", "three")
    assert is_ancestor(repo, "master", "develop")

    base = us.find_rebase_base(str(repo), "develop", "master")

    # master~1 (m2) is master's newest tree also on develop; M is the newest develop commit with it.
    assert base == merge_back
    assert tree(repo, base) == tree(repo, d2) == tree(repo, "master~1")
    assert not is_ancestor(repo, base, "master")


def test_rebase_base_returns_early_when_source_already_in_target(tmp_path: pathlib.Path) -> None:
    """Shape: the release after a merge-back fast-forwards master to develop, so develop is
    contained in master and the source commit itself is the base."""
    repo = init_repo(tmp_path / "repo")
    commit(repo, "a.txt", "base")
    git(repo, "branch", "master")
    d1 = commit(repo, "a.txt", "one")
    git(repo, "checkout", "-q", "master")
    rebased_copies(repo, d1)
    commit(repo, "VERSION", TAG, f"Release {TAG}")
    git(repo, "checkout", "-q", "develop")
    git(repo, "merge", "-q", "-s", "ours", "--no-edit", "master")
    develop_tip = commit(repo, "b.txt", "two")
    git(repo, "checkout", "-q", "master")
    git(repo, "merge", "-q", "--ff-only", "develop")

    assert us.find_rebase_base(str(repo), "develop", "master") == develop_tip
    assert us.find_rebase_base(str(repo), "develop~1", "master") == git(repo, "rev-parse", "develop~1")


def test_rebase_base_for_fast_forwarded_master(tmp_path: pathlib.Path) -> None:
    """wax's shape: master was fast-forwarded to a develop commit (shares develop's SHAs).
    No develop commit outside master has master's tree; develop's first-parent line does."""
    repo = init_repo(tmp_path / "repo")
    commit(repo, "a.txt", "base")
    synced = commit(repo, "a.txt", "one")
    git(repo, "branch", "master")
    commit(repo, "b.txt", "two")

    assert us.find_rebase_base(str(repo), "develop", "master") == synced


def test_rebase_base_for_rc_tag_on_side_branch(tmp_path: pathlib.Path) -> None:
    """block_explorer_ui's shape: the rc tag sits on an MR side branch merged into develop
    later, and master was released from that rc.  Its tree is on no first-parent develop
    commit (develop moved on in parallel), only on the side branch.

    develop:  b - d1 ------ D - d3
                \\          /
    feature:     f1(rc1) --
    master:   b - m(f1's tree)
    """
    repo = init_repo(tmp_path / "repo")
    commit(repo, "a.txt", "base")
    git(repo, "branch", "master")
    git(repo, "checkout", "-q", "-b", "feature")
    rc = commit(repo, "f.txt", "feature")
    git(repo, "tag", RC_TAG)
    git(repo, "checkout", "-q", "develop")
    commit(repo, "a.txt", "one")
    git(repo, "merge", "-q", "--no-ff", "--no-edit", "feature")
    commit(repo, "c.txt", "three")
    git(repo, "checkout", "-q", "master")
    rebased_copies(repo, rc)
    for first_parent in git(repo, "rev-list", "--first-parent", "develop").split():
        assert tree(repo, first_parent) != tree(repo, "master")

    assert us.find_rebase_base(str(repo), "develop", "master") == rc
