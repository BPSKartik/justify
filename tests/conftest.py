"""Builds small throwaway repositories so each rule can be tested on code made for it."""

from __future__ import annotations

import os
import pathlib
import subprocess
import textwrap

import pytest


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("JUSTIFY_HOME", str(tmp_path / "justify-home"))
    monkeypatch.delenv("JUSTIFY_TEST_COMMAND", raising=False)


@pytest.fixture
def make_repo(tmp_path):
    def _make(files: dict[str, str], name: str = "repo") -> pathlib.Path:
        root = tmp_path / name
        for rel, text in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
        return root
    return _make


def git(root: pathlib.Path, *args: str, env_extra: dict | None = None) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
           "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com", **(env_extra or {})}
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True,
                          env=env).stdout


@pytest.fixture
def git_repo():
    def _init(root: pathlib.Path) -> pathlib.Path:
        git(root, "init", "-q")
        git(root, "config", "commit.gpgsign", "false")
        return root
    return _init
