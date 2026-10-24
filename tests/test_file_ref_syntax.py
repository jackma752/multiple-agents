"""T-1e: file reference syntax, exclusions and range merging (pure, offline)."""

import pytest

from agenthub.files import FileRef, excluded, merge_ranges, parse_ref


@pytest.mark.parametrize("spec, ref", [
    ("src/app.py", FileRef("src/app.py")),
    ("@src/app.py", FileRef("src/app.py")),
    ("src/app.py:L10-20", FileRef("src/app.py", 10, 20)),
    ("src/app.py:10-20", FileRef("src/app.py", 10, 20)),
    ("src/app.py:L10-L20", FileRef("src/app.py", 10, 20)),
    ("src/app.py:L7", FileRef("src/app.py", 7, 7)),
    ('@"docs/my notes.md":L3-5', FileRef("docs/my notes.md", 3, 5)),
    ("docs/my notes.md", FileRef("docs/my notes.md")),
    ("文档/说明 一.md:L2-3", FileRef("文档/说明 一.md", 2, 3)),
    (r"C:\repo\a.py:L3-5", FileRef(r"C:\repo\a.py", 3, 5)),
    (r"C:\repo\a.py", FileRef(r"C:\repo\a.py")),
])
def test_specs_parse_unambiguously(spec, ref):
    assert parse_ref(spec) == ref


@pytest.mark.parametrize("spec", ["", "  ", "@", ":L1-2", "a.py:L0-3", "a.py:L5-3", '"a".py', "x" * 1200])
def test_bad_specs_are_refused(spec):
    with pytest.raises(ValueError):
        parse_ref(spec)


@pytest.mark.parametrize("path, refused", [
    ("src/app.py", False), ("docs/notes.md", False),
    (".env", True), ("config/.env.local", True), ("prod.env", True), ("keys/server.PEM", True),
    (".git/config", True), ("node_modules/x/index.js", True), ("var/agent-hub.db", True),
    ("VAR/notes.md", True), ("evidence/run.log", True), ("a/__pycache__/m.pyc", True),
    ("home/.ssh/config", True), ("auth.json", True), ("my_secret_notes.txt", True),
])
def test_sensitive_paths_are_excluded(path, refused):
    assert excluded(path.split("/")) is refused


def test_ranges_merge_and_a_whole_file_absorbs_them():
    assert merge_ranges([(10, 20), (15, 30), (31, 35), (50, 60), (10, 20)]) == [(10, 35), (50, 60)]
    assert merge_ranges([(5, 9), None, (1, 2)]) == [None]
