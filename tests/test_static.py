"""Byte-compile check for the repository's top-level scripts.

Only update-submodules.py is imported by other tests, so a syntax error in any
other script would otherwise go unnoticed until someone runs it.
"""
import pathlib
import py_compile

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SCRIPTS = [
    "update-submodules.py",
    "checkout_develop_versions.py",
    "resume_release_push.py",
    "revert_update_submodules.py",
]


@pytest.mark.parametrize("script", _SCRIPTS)
def test_script_byte_compiles(script: str, tmp_path: pathlib.Path) -> None:
    py_compile.compile(str(_ROOT / script), cfile=str(tmp_path / "out.pyc"), doraise=True)
