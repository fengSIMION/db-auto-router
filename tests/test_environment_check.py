import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "check_environment.py"
POWERSHELL_SCRIPT = ROOT / "tools" / "check_environment.ps1"


def _load_checker():
    assert SCRIPT.exists(), "environment checker has not been implemented"
    spec = importlib.util.spec_from_file_location("check_environment", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_reads_configured_database_types_from_sqlite(tmp_path):
    checker = _load_checker()
    db_path = tmp_path / "datasources.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE mcp_datasource_config (db_type TEXT NOT NULL)")
    conn.executemany(
        "INSERT INTO mcp_datasource_config (db_type) VALUES (?)",
        [("mysql",), ("postgres",), ("gaussdb",)],
    )
    conn.commit()
    conn.close()
    (tmp_path / ".env").write_text(
        "MCP_CONFIG_MODE=sqlite\nMCP_CONFIG_SQLITE_PATH=datasources.db\n",
        encoding="utf-8",
    )

    assert checker.configured_db_types(tmp_path) == {
        "mysql",
        "postgres",
        "gaussdb",
    }


def test_mysql_config_mode_requires_mysql_driver_even_without_sqlite(tmp_path):
    checker = _load_checker()
    (tmp_path / ".env").write_text("MCP_CONFIG_MODE=mysql\n", encoding="utf-8")

    requirements = checker.build_requirements(checker.configured_db_types(tmp_path))

    assert "mysql.connector" in {item["module"] for item in requirements}


def test_driver_requirements_are_deduplicated_for_postgres_and_gaussdb():
    checker = _load_checker()

    requirements = checker.build_requirements({"postgres", "gaussdb"})
    psycopg = [item for item in requirements if item["module"] == "psycopg2"]

    assert len(psycopg) == 1
    assert psycopg[0]["pip_spec"] == "psycopg2-binary>=2.9"


def test_requirement_rejects_versions_outside_supported_range():
    checker = _load_checker()
    requirement = next(
        item for item in checker.build_requirements(set()) if item["module"] == "mcp"
    )
    module_finder = lambda _module: object()

    assert checker.requirement_satisfied(
        requirement, module_finder=module_finder, version_getter=lambda _name: "1.9.0"
    ) is False
    assert checker.requirement_satisfied(
        requirement, module_finder=module_finder, version_getter=lambda _name: "2.4.1"
    ) is True
    assert checker.requirement_satisfied(
        requirement, module_finder=module_finder, version_getter=lambda _name: "3.0.0"
    ) is False


def test_cli_reports_missing_modules_as_json(tmp_path):
    (tmp_path / ".env").write_text("MCP_CONFIG_MODE=sqlite\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--project-root", str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    payload = json.loads(completed.stdout)

    assert payload["python"]["compatible"] is True
    assert payload["configured_db_types"] == []
    assert {item["module"] for item in payload["requirements"]} >= {
        "mcp",
        "dotenv",
        "sqlglot",
    }
    assert isinstance(payload["missing"], list)


def test_launcher_runs_environment_check_before_gui():
    launcher = (ROOT / "start_datasource_manager.bat").read_text(encoding="ascii")

    check_pos = launcher.find("check_environment.ps1")
    gui_pos = launcher.find('start "" "%PYW%" tools\\manage_datasources_gui.py')

    assert check_pos >= 0
    assert gui_pos >= 0
    assert check_pos < gui_pos


def test_powershell_launcher_uses_utf8_bom_for_windows_powershell_compatibility():
    assert POWERSHELL_SCRIPT.read_bytes().startswith(b"\xef\xbb\xbf")
