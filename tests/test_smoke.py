"""Smoke tests for update-submodules.py's pure helpers.

The script's file name has a hyphen, so it is loaded with importlib rather than imported.
"""
import importlib.util
import pathlib

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "update-submodules.py"
_spec = importlib.util.spec_from_file_location("update_submodules", _SCRIPT)
us = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(us)


def test_merge_back_branch_name_and_detection():
    name = us.merge_back_branch_name("1.28.9", "develop")
    assert name == "merge-1.28.9-into-develop"
    assert us.is_merge_back_branch(name)
    assert us.merge_back_branch_name("1.28.9", "release/x") == "merge-1.28.9-into-release-x"
    assert not us.is_merge_back_branch("update-submodules-for-1.28.9")


@pytest.mark.parametrize("url,expected", [
    ("git@gitlab.syncad.com:hive/wax.git", "wax"),
    ("https://gitlab.syncad.com/hive/wax.git", "wax"),
    ("https://gitlab.syncad.com/hive/wax/", "wax"),
])
def test_get_repo_name(url, expected):
    assert us.get_repo_name(url) == expected


def test_normalize_gitlab_url_https_to_ssh():
    assert us.normalize_gitlab_url("https://gitlab.syncad.com/hive/wax.git").startswith("git@gitlab.syncad.com:hive/wax")


@pytest.mark.parametrize("ref,expected", [
    ("6d57a4f", True), ("6d57a4f0d0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5", True),
    ("develop", False), ("1.28.9", False), ("abc", False),
])
def test_looks_like_sha(ref, expected):
    assert us._looks_like_sha(ref) is expected


def test_topological_sort_orders_dependencies_first():
    order = us.topological_sort({"a": ["b"], "b": ["c"], "c": []})
    assert order.index("a") < order.index("b") < order.index("c") or order.index("c") < order.index("b") < order.index("a")
