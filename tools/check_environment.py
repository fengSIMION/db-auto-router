# -*- coding: utf-8 -*-
"""Inspect launcher dependencies using only the Python standard library."""
from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Iterable


BASE_REQUIREMENTS = (
    {"module": "mcp", "distribution": "mcp", "pip_spec": "mcp>=2,<3",
     "min_version": (2,), "max_version": (3,), "reason": "MCP protocol SDK"},
    {"module": "dotenv", "distribution": "python-dotenv", "pip_spec": "python-dotenv>=1.0",
     "min_version": (1, 0), "reason": ".env loader"},
    {"module": "sqlglot", "distribution": "sqlglot", "pip_spec": "sqlglot>=30,<31",
     "min_version": (30,), "max_version": (31,), "reason": "SQL AST guard"},
)

DRIVER_REQUIREMENTS = {
    "mysql": {
        "module": "mysql.connector",
        "distribution": "mysql-connector-python",
        "pip_spec": "mysql-connector-python>=8.0",
        "min_version": (8, 0),
        "reason": "MySQL datasource",
    },
    "dm": {
        "module": "dmPython",
        "distribution": "dmPython",
        "pip_spec": "dmPython>=1.0",
        "min_version": (1, 0),
        "reason": "DM datasource",
    },
    "postgres": {
        "module": "psycopg2",
        "distribution": "psycopg2-binary",
        "pip_spec": "psycopg2-binary>=2.9",
        "min_version": (2, 9),
        "reason": "PostgreSQL/GaussDB datasource",
    },
    "gaussdb": {
        "module": "psycopg2",
        "distribution": "psycopg2-binary",
        "pip_spec": "psycopg2-binary>=2.9",
        "min_version": (2, 9),
        "reason": "PostgreSQL/GaussDB datasource",
    },
    "oracle": {
        "module": "oracledb",
        "distribution": "oracledb",
        "pip_spec": "oracledb>=1.0",
        "min_version": (1, 0),
        "reason": "Oracle datasource",
    },
}


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def configured_db_types(project_root: Path) -> set[str]:
    root = Path(project_root).resolve()
    env = read_env(root / ".env")
    mode = env.get("MCP_CONFIG_MODE", "sqlite").strip().lower()
    if mode == "mysql":
        return {"mysql"}

    raw_path = env.get("MCP_CONFIG_SQLITE_PATH", "datasources.db").strip()
    db_path = Path(raw_path)
    if not db_path.is_absolute():
        db_path = root / db_path
    if not db_path.exists():
        return set()

    try:
        with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                "SELECT DISTINCT LOWER(TRIM(db_type)) "
                "FROM mcp_datasource_config WHERE db_type IS NOT NULL"
            ).fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[0]) for row in rows if row and row[0]}


def build_requirements(db_types: Iterable[str]) -> list[dict[str, str]]:
    requirements = [dict(item) for item in BASE_REQUIREMENTS]
    seen = {item["module"] for item in requirements}
    for db_type in sorted({str(item).lower().strip() for item in db_types}):
        requirement = DRIVER_REQUIREMENTS.get(db_type)
        if requirement and requirement["module"] not in seen:
            requirements.append(dict(requirement))
            seen.add(requirement["module"])
    return requirements


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = []
    for chunk in str(value).split("."):
        match = re.match(r"(\d+)", chunk)
        if not match:
            break
        parts.append(int(match.group(1)))
    return tuple(parts)


def requirement_satisfied(requirement: dict, module_finder=importlib.util.find_spec,
                          version_getter=importlib.metadata.version) -> bool:
    try:
        if module_finder(requirement["module"]) is None:
            return False
        installed = _version_tuple(version_getter(requirement["distribution"]))
    except (ImportError, ModuleNotFoundError, ValueError, importlib.metadata.PackageNotFoundError):
        return False
    if not installed:
        return False
    minimum = tuple(requirement.get("min_version", ()))
    maximum = tuple(requirement.get("max_version", ()))
    if minimum and installed < minimum:
        return False
    if maximum and installed >= maximum:
        return False
    return True


def inspect_environment(project_root: Path) -> dict:
    db_types = configured_db_types(project_root)
    requirements = build_requirements(db_types)
    missing = [item for item in requirements if not requirement_satisfied(item)]
    version = sys.version_info
    return {
        "python": {
            "version": f"{version.major}.{version.minor}.{version.micro}",
            "executable": sys.executable,
            "compatible": version >= (3, 10),
            "minimum": "3.10",
        },
        "configured_db_types": sorted(db_types),
        "requirements": requirements,
        "missing": missing,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(inspect_environment(args.project_root), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
