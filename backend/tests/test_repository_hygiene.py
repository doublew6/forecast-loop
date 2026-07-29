from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).parents[2]


def _git_check_ignore(
    path: str,
    *,
    verbose: bool,
) -> subprocess.CompletedProcess[str]:
    mode = "--verbose" if verbose else "--quiet"
    return subprocess.run(
        ["git", "check-ignore", "--no-index", mode, "--", path],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "path",
    [
        "data/snapshots/2026-07-13.json",
        "data/snapshots/nested/2026-07-13.json",
        "data/judgment-bundles/example/manifest.json",
        "data/future-runtime-output/example.json",
    ],
)
def test_private_runtime_data_is_gitignored(path: str) -> None:
    result = _git_check_ignore(path, verbose=True)

    assert result.returncode == 0, result.stderr
    assert "/data/*" in result.stdout


def test_root_data_rule_does_not_ignore_public_example_data() -> None:
    result = _git_check_ignore(
        "examples/adapters/data/public-evidence.json",
        verbose=False,
    )

    assert result.returncode == 1


def test_data_readme_remains_trackable() -> None:
    ignore_result = _git_check_ignore("data/README.md", verbose=False)
    tracked_result = subprocess.run(
        ["git", "ls-files", "--", "data"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert ignore_result.returncode == 1
    assert tracked_result.returncode == 0, tracked_result.stderr
    assert tracked_result.stdout.splitlines() == ["data/README.md"]
