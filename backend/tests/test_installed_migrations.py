"""Exercise migration resources from built distributions outside the checkout."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def distributions(tmp_path_factory):
    work = tmp_path_factory.mktemp("installed-migrations")
    output = work / "distributions"
    subprocess.run(
        [
            "uv", "build", "--sdist", "--wheel", "--out-dir", str(output),
            "--build-constraints", str(ROOT / "requirements/release-build-constraints.txt"),
            "--no-build-logs",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return work, next(output.glob("*.whl")), next(output.glob("*.tar.gz"))


@pytest.fixture(scope="module", params=["wheel", "sdist"])
def wheel_installation(distributions, request):
    build_root, wheel, sdist = distributions
    work = build_root / request.param
    work.mkdir()
    if request.param == "sdist":
        subprocess.run(
            [
                "uv", "build", str(sdist), "--wheel", "--out-dir", str(work),
                "--build-constraints", str(ROOT / "requirements/release-build-constraints.txt"),
                "--no-build-logs",
            ],
            cwd=work,
            capture_output=True,
            text=True,
            check=True,
        )
        wheel = next(work.glob("*.whl"))
    target = work / "site-packages"
    with zipfile.ZipFile(wheel) as archive:
        members = archive.namelist()
        assert all(not name.endswith((".pyc", ".pyo")) for name in members)
    subprocess.run(
        [
            "uv", "pip", "install", "--python", sys.executable, "--no-deps",
            "--offline", "--target", str(target), str(wheel),
        ],
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
    )
    return work, target, members


def test_wheel_contains_complete_migration_resources(wheel_installation):
    _, _, members = wheel_installation
    expected = {"app/alembic.ini"}
    expected.update(
        "app/migrations/" + path.relative_to(ROOT / "backend/migrations").as_posix()
        for path in (ROOT / "backend/migrations").rglob("*")
        if path.is_file() and path.suffix in {".py", ".mako"}
    )
    assert expected <= set(members)


def test_installed_wheel_migrates_without_checkout_or_cwd_configuration(wheel_installation):
    work, target, _ = wheel_installation
    unrelated = work / "unrelated"
    unrelated.mkdir()
    (unrelated / "alembic.ini").write_text("[alembic]\nscript_location = missing\n")
    legacy_config = work / "backend/alembic.ini"
    legacy_config.parent.mkdir()
    legacy_config.write_text("[alembic]\nscript_location = missing\n")
    script = """
import json
from pathlib import Path
import app
from app.services.schema_readiness import (
    ALEMBIC_CONFIG_PATH, downgrade_database, inspect_schema, upgrade_database,
)
target, database = map(Path, __import__('sys').argv[1:])
assert Path(app.__file__).resolve().is_relative_to(target)
assert ALEMBIC_CONFIG_PATH.is_relative_to(target)
database_url = 'sqlite:///' + str(database)
assert not inspect_schema(database_url).database_exists
initial = upgrade_database(database_url)
assert initial.ready
previous = downgrade_database(database_url, '-1')
assert not previous.ready
final = upgrade_database(database_url)
assert final.ready and final.current_heads == initial.current_heads
assert inspect_schema(database_url, deep=True).ready
print(json.dumps(final.to_dict()))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(target), str(work / "synthetic.sqlite3")],
        cwd=unrelated,
        env={**os.environ, "PYTHONPATH": str(target)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["ready"] is True


def test_installed_database_command_uses_packaged_migrations(wheel_installation):
    work, target, _ = wheel_installation
    unrelated = work / "cli-working-directory"
    unrelated.mkdir()
    environment = {**os.environ, "PYTHONPATH": str(target)}
    command = target / "bin/forecast-loop"
    database_url = "sqlite:///" + str(work / "cli-synthetic.sqlite3")
    for action in ("migrate", "status"):
        arguments = [str(command), "database", action, "--database-url", database_url]
        if action == "status":
            arguments.append("--deep")
        result = subprocess.run(
            arguments, cwd=unrelated, env=environment, capture_output=True,
            text=True, check=True,
        )
        assert json.loads(result.stdout)["ready"] is True
