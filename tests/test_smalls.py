"""smalls.py's migration runner: a failure is never recorded as applied.

Before, any run outside DEV (the production migrate Job sets SMALLS_ENV=PROD)
recorded a failed migration in MigrationHistory and carried on, so the deploy
went green and the migration never ran again. Ported from new-d8's fix.

smalls.py does database work at import time, so SMALLS_MODEL points at a
throwaway SQLite module before ``import smalls``.
"""

import os
import sys

os.environ["SMALLS_MODEL"] = "tests._support_smalls_db"
os.environ.setdefault("SMALLS_VERSION", "999")

import pytest  # noqa: E402

import smalls  # noqa: E402

FAILING = "def migrate():\n    raise RuntimeError('boom')\n"
PASSING = "def migrate():\n    pass\n\n\ndef rollback():\n    pass\n"


def _write(directory, number, body=PASSING, name="fake"):
    (directory / f"{number}_{name}.py").write_text(body)


@pytest.fixture
def migrations_dir(tmp_path, monkeypatch):
    """smalls.py globs and imports "migrations/..." from the working
    directory: point both at a fresh directory."""
    directory = tmp_path / "migrations"
    directory.mkdir()

    def _forget():
        for name in list(sys.modules):
            if name == "migrations" or name.startswith("migrations."):
                del sys.modules[name]

    _forget()
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    yield directory
    _forget()


@pytest.fixture
def create(mocker):
    return mocker.patch.object(smalls.MigrationHistory, "create")


def test_a_failure_in_prod_exits_1_and_is_not_recorded(
    migrations_dir, create, monkeypatch
):
    monkeypatch.setattr(smalls, "ENVIRONMENT", "PROD")
    _write(migrations_dir, "0001", FAILING)
    with pytest.raises(SystemExit) as exc:
        smalls.run_migration("migrations/0001_fake.py")
    assert exc.value.code == 1
    create.assert_not_called()


def test_a_failure_in_dev_without_a_terminal_is_not_recorded(
    migrations_dir, create, mocker, monkeypatch
):
    """A DEV run in a pod or a pipe: no prompt (input() would hang or read
    EOF), just a failure."""
    monkeypatch.setattr(smalls, "ENVIRONMENT", "DEV")
    mocker.patch.object(smalls.sys.stdin, "isatty", return_value=False)
    ask = mocker.patch("builtins.input")
    _write(migrations_dir, "0001", FAILING)
    with pytest.raises(SystemExit) as exc:
        smalls.run_migration("migrations/0001_fake.py")
    assert exc.value.code == 1
    ask.assert_not_called()
    create.assert_not_called()


@pytest.mark.parametrize("answer, recorded", [("Y", True), ("", True), ("n", False)])
def test_at_a_dev_terminal_the_person_decides(
    migrations_dir, create, mocker, monkeypatch, answer, recorded
):
    monkeypatch.setattr(smalls, "ENVIRONMENT", "DEV")
    mocker.patch.object(smalls.sys.stdin, "isatty", return_value=True)
    mocker.patch("builtins.input", return_value=answer)
    _write(migrations_dir, "0001", FAILING)
    if recorded:
        smalls.run_migration("migrations/0001_fake.py")
        create.assert_called_once()
    else:
        with pytest.raises(SystemExit) as exc:
            smalls.run_migration("migrations/0001_fake.py")
        assert exc.value.code == 1
        create.assert_not_called()


def test_a_success_is_recorded(migrations_dir, create, monkeypatch):
    monkeypatch.setattr(smalls, "ENVIRONMENT", "PROD")
    _write(migrations_dir, "0001")
    smalls.run_migration("migrations/0001_fake.py")
    create.assert_called_once()


def test_a_missing_file_exits_1(migrations_dir, create):
    with pytest.raises(SystemExit) as exc:
        smalls.run_migration("migrations/0009_nothing.py")
    assert exc.value.code == 1
    create.assert_not_called()


def test_a_failed_rollback_exits_1(migrations_dir, mocker):
    delete = mocker.patch.object(smalls.MigrationHistory, "delete")
    _write(migrations_dir, "0001", "def rollback():\n    raise ImportError('gone')\n")
    with pytest.raises(SystemExit) as exc:
        smalls.run_rollback("0001_fake")
    assert exc.value.code == 1
    delete.assert_not_called()


def test_two_migrations_with_one_number_stop_the_run(migrations_dir):
    _write(migrations_dir, "0001")
    _write(migrations_dir, "0002", name="one")
    _write(migrations_dir, "0002", name="two")
    with pytest.raises(SystemExit) as exc:
        smalls.lets_migrate()
    assert exc.value.code == 1


def test_a_gap_in_the_numbers_stops_the_run(migrations_dir):
    _write(migrations_dir, "0001")
    _write(migrations_dir, "0003")
    with pytest.raises(SystemExit) as exc:
        smalls._validate_migrations_directory()
    assert exc.value.code == 1


def test_this_repos_migrations_are_numbered_without_gaps():
    """The real migrations/ passes the check every run now makes."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    numbers = smalls._validate_migrations_directory(os.path.join(root, "migrations"))
    assert numbers == list(range(1, len(numbers) + 1))
