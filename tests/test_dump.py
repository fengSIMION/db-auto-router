# -*- coding: utf-8 -*-
"""数据库 dump 的文件完整性边界。"""
import subprocess

import pytest

from db_auto_router import dump


def _mysql_ds():
    return {
        "db_code": "demo",
        "db_type": "mysql",
        "conn_url": "127.0.0.1:3306",
        "db_user": "root",
        "db_password": "secret",
    }


def test_failed_dump_preserves_existing_target(monkeypatch, tmp_path):
    target = tmp_path / "backup.sql"
    target.write_bytes(b"known-good-backup")

    monkeypatch.setattr(
        dump, "load_datasources",
        lambda force_refresh=False: {"demo": _mysql_ds(), "_default": "demo"},
    )
    monkeypatch.setattr(
        dump.subprocess, "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 2, stderr=b"dump failed"),
    )

    with pytest.raises(dump.DumpError, match="退出码 2"):
        dump.dump_database(
            db_code="demo", file_path=target, cli_path="mysqldump",
        )

    assert target.read_bytes() == b"known-good-backup"
    assert not target.with_name(target.name + ".part").exists()
