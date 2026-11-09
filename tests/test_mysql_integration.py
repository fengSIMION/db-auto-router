# -*- coding: utf-8 -*-
"""本机 stu MySQL 的真实事务集成测试。

默认跳过。仅在显式设置 MCP_MYSQL_INTEGRATION=1 时运行，并再次校验数据源必须是
本机 MySQL、db_code 必须为 stu。所有对象使用随机 mcp_it_* 表名并在 finally 清理。
"""
import csv
import os
import time
import uuid

import pytest
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from db_auto_router.datasources import load_datasources
from db_auto_router.directconn import _open_direct_connection
from db_auto_router.csvio import export_csv_impl, import_csv_impl
from db_auto_router.explain import explain_query_impl
from db_auto_router.introspection import get_table_schema, search_tables_by_keyword
from db_auto_router.pool import get_connection, pool_key_for, put_connection
from db_auto_router.readengine import execute_read_only_query
from db_auto_router.readonly import enable_read_only, release_read_only
from db_auto_router.transactions import (
    _end_transaction,
    _transactions,
    begin_transaction_impl,
    execute_in_transaction,
    extend_transaction_impl,
    list_active_transactions,
)
from db_auto_router.writeengine import execute_any_sql


pytestmark = pytest.mark.skipif(
    os.environ.get("MCP_MYSQL_INTEGRATION") != "1",
    reason="设置 MCP_MYSQL_INTEGRATION=1 后才运行本机 MySQL 集成测试",
)


def _stu_datasource():
    ds = load_datasources(force_refresh=True).get("stu")
    assert ds is not None, "未找到 stu 数据源"
    assert ds.get("db_type") == "mysql", "stu 必须是 MySQL"
    assert ds.get("db_code") == "stu", "只允许测试 stu 数据库"
    host = (ds.get("conn_url") or "").split(":", 1)[0].strip().lower()
    assert host in {"127.0.0.1", "localhost", "::1"}, "只允许测试本机 MySQL"
    return ds


def _admin_sql(ds, sql, *, fetch=False):
    conn = _open_direct_connection(ds)
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows
    finally:
        if cur is not None:
            cur.close()
        conn.close()


@pytest.fixture
def live_table():
    ds = _stu_datasource()
    table = f"mcp_it_txn_{uuid.uuid4().hex[:12]}"
    _admin_sql(
        ds,
        f"CREATE TABLE `{table}` ("
        "id BIGINT NOT NULL PRIMARY KEY, note VARCHAR(100) NOT NULL"
        ") ENGINE=InnoDB",
    )
    try:
        yield ds, table
    finally:
        for txn_id, info in list(_transactions.items()):
            if info.get("db_code") == "stu":
                try:
                    _end_transaction(txn_id, False, reason="集成测试清理")
                except Exception:
                    pass
        _admin_sql(ds, f"DROP TABLE IF EXISTS `{table}`")


def _count(ds, table):
    return _admin_sql(ds, f"SELECT COUNT(*) FROM `{table}`", fetch=True)[0][0]


def test_real_mysql_commit_persists_rows(live_table):
    ds, table = live_table
    txn = begin_transaction_impl(ds, "stu")["txn_id"]
    execute_in_transaction(
        txn, f"INSERT INTO `{table}` (id, note) VALUES (1, 'commit')"
    )

    result = _end_transaction(txn, True)

    assert result["action"] == "已提交"
    assert _count(ds, table) == 1


def test_real_mysql_rollback_removes_rows(live_table):
    ds, table = live_table
    txn = begin_transaction_impl(ds, "stu")["txn_id"]
    execute_in_transaction(
        txn, f"INSERT INTO `{table}` (id, note) VALUES (1, 'rollback')"
    )

    result = _end_transaction(txn, False)

    assert result["action"] == "已回滚"
    assert _count(ds, table) == 0


def test_real_mysql_statement_error_rolls_back_whole_transaction(live_table):
    ds, table = live_table
    txn = begin_transaction_impl(ds, "stu")["txn_id"]
    execute_in_transaction(
        txn, f"INSERT INTO `{table}` (id, note) VALUES (1, 'before-error')"
    )

    with pytest.raises(RuntimeError, match="已自动回滚并终止"):
        execute_in_transaction(
            txn, f"INSERT INTO `{table}` (missing_column) VALUES (1)"
        )

    assert txn not in _transactions
    assert _count(ds, table) == 0


def test_real_mysql_idle_timeout_rolls_back_transaction(live_table):
    ds, table = live_table
    txn = begin_transaction_impl(ds, "stu", idle_timeout=1)["txn_id"]
    execute_in_transaction(
        txn, f"INSERT INTO `{table}` (id, note) VALUES (1, 'expired')"
    )

    time.sleep(1.1)
    active = list_active_transactions()

    assert txn in active["expired_cleaned"]
    assert txn not in _transactions
    assert _count(ds, table) == 0


def test_real_mysql_multi_statement_commits_and_restores_autocommit(live_table):
    ds, table = live_table

    result = execute_any_sql(
        f"INSERT INTO `{table}` (id, note) VALUES (1, 'first');"
        f"INSERT INTO `{table}` (id, note) VALUES (2, 'second')",
        ds,
        "stu",
    )

    assert result["batch"] is True
    assert result["statements_executed"] == 2
    assert result["total_affected_rows"] == 2
    assert result["committed"] is True
    assert result["atomicity"] is True
    assert _count(ds, table) == 2

    conn = get_connection(ds)
    try:
        assert conn.autocommit is True
    finally:
        put_connection(ds, conn)


def test_real_mysql_read_only_guard_rejects_direct_write(live_table):
    ds, table = live_table
    conn = get_connection(ds)
    key = pool_key_for(ds)
    reusable = True
    try:
        assert enable_read_only(conn, "mysql", key) is True
        cur = conn.cursor()
        try:
            with pytest.raises(Exception, match=r"(?i)read.only|read only"):
                cur.execute(
                    f"INSERT INTO `{table}` (id, note) VALUES (1, 'blocked')"
                )
        finally:
            cur.close()
        reusable = release_read_only(conn, "mysql", key, ds)
        assert reusable is True
    finally:
        if reusable:
            put_connection(ds, conn)

    assert _count(ds, table) == 0
    execute_any_sql(
        f"INSERT INTO `{table}` (id, note) VALUES (2, 'after-read-only')",
        ds,
        "stu",
    )
    assert _count(ds, table) == 1


def test_real_mysql_introspection_finds_columns_primary_key_and_table(live_table):
    ds, table = live_table

    schema = get_table_schema(table, ds, "stu")
    columns = {column["field"]: column for column in schema["columns"]}

    assert set(columns) == {"id", "note"}
    assert columns["id"]["key_type"] == "PRI"
    assert columns["note"]["nullable"] == "NO"

    unique_fragment = table.removeprefix("mcp_it_txn_")
    search = search_tables_by_keyword(unique_fragment, ds, "stu")
    assert table in {row["table_name"] for row in search["data"]}


def test_real_mysql_57_explain_analyze_truthfully_downgrades(live_table):
    ds, table = live_table

    result = explain_query_impl(
        f"SELECT id, note FROM `{table}` WHERE id = 1",
        ds,
        "stu",
        analyze=True,
    )

    assert result["dialect"] == "mysql"
    assert result["mode"] == "explain"
    assert result["analyze_supported"] is False
    assert result["plan"]
    assert "不支持 EXPLAIN ANALYZE" in result["note"]
    assert "已退回普通 EXPLAIN" in result["note"]


def test_real_mysql_csv_export_is_complete_and_read_only(live_table, tmp_path):
    ds, table = live_table
    _admin_sql(
        ds,
        f"INSERT INTO `{table}` (id, note) VALUES "
        "(1, 'alpha'), (2, '包含中文')",
    )
    target = tmp_path / "mysql-export.csv"

    result = export_csv_impl(
        f"SELECT id, note FROM `{table}` ORDER BY id",
        str(target),
        ds,
        "stu",
    )

    assert result["success"] is True
    assert result["rows_exported"] == 2
    assert result["read_only_enforced"] is True
    assert target.read_bytes().startswith(b"\xef\xbb\xbf")
    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        assert list(csv.reader(handle)) == [
            ["id", "note"],
            ["1", "alpha"],
            ["2", "包含中文"],
        ]


def test_real_mysql_failed_dml_batch_rolls_back_and_restores_connection(live_table):
    ds, table = live_table

    with pytest.raises(RuntimeError, match="前 1 条已回滚"):
        execute_any_sql(
            f"INSERT INTO `{table}` (id, note) VALUES (1, 'before-error');"
            f"INSERT INTO `{table}` (id, note) VALUES (1, 'duplicate')",
            ds,
            "stu",
        )

    assert _count(ds, table) == 0
    conn = get_connection(ds)
    try:
        assert conn.autocommit is True
    finally:
        put_connection(ds, conn)


def test_real_mysql_read_engine_limits_rows_and_leaves_connection_writable(live_table):
    ds, table = live_table
    _admin_sql(
        ds,
        f"INSERT INTO `{table}` (id, note) VALUES "
        "(1, 'one'), (2, 'two'), (3, 'three')",
    )

    result = execute_read_only_query(
        f"SELECT id, note FROM `{table}` ORDER BY id",
        ds,
        "stu",
        max_rows=2,
    )

    assert result["total_rows"] == 2
    assert [row["id"] for row in result["data"]] == [1, 2]
    assert result["read_only_enforced"] is True
    assert "自动限制返回 2 行" in result["warning"]

    execute_any_sql(
        f"INSERT INTO `{table}` (id, note) VALUES (4, 'after-query')",
        ds,
        "stu",
    )
    assert _count(ds, table) == 4


def test_real_mysql_csv_import_commits_and_failure_rolls_back_whole_file(
    live_table, tmp_path
):
    ds, table = live_table
    good_csv = tmp_path / "good.csv"
    good_csv.write_text("id,note\n1,alpha\n2,beta\n", encoding="utf-8")

    result = import_csv_impl(str(good_csv), table, ds, "stu", batch_size=1)

    assert result["success"] is True
    assert result["rows_inserted"] == 2
    assert _count(ds, table) == 2

    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text("id,note\n3,before-error\n2,duplicate\n", encoding="utf-8")

    with pytest.raises(Exception, match="Duplicate entry"):
        import_csv_impl(str(bad_csv), table, ds, "stu", batch_size=1)

    assert _count(ds, table) == 2
    rows = _admin_sql(
        ds, f"SELECT id, note FROM `{table}` ORDER BY id", fetch=True
    )
    assert rows == [(1, "alpha"), (2, "beta")]


def test_real_mysql_transaction_extension_resets_idle_deadline(live_table):
    ds, table = live_table
    txn = begin_transaction_impl(ds, "stu", idle_timeout=1)["txn_id"]

    time.sleep(0.7)
    extended = extend_transaction_impl(txn, extend_seconds=2)
    time.sleep(0.6)
    execute_in_transaction(
        txn, f"INSERT INTO `{table}` (id, note) VALUES (1, 'extended')"
    )
    committed = _end_transaction(txn, True)

    assert extended["extended"] is True
    assert extended["previous_idle_timeout_seconds"] == 1
    assert extended["idle_timeout_seconds"] == 2
    assert committed["action"] == "已提交"
    assert _count(ds, table) == 1


def test_real_mysql_uncommitted_rows_are_invisible_until_commit(live_table):
    ds, table = live_table
    txn = begin_transaction_impl(ds, "stu")["txn_id"]
    execute_in_transaction(
        txn, f"INSERT INTO `{table}` (id, note) VALUES (1, 'not-visible-yet')"
    )

    assert _count(ds, table) == 0

    _end_transaction(txn, True)
    assert _count(ds, table) == 1


def test_real_mysql_failed_truncate_first_import_restores_original_rows(
    live_table, tmp_path
):
    ds, table = live_table
    _admin_sql(
        ds,
        f"INSERT INTO `{table}` (id, note) VALUES (1, 'original-one'), "
        "(2, 'original-two')",
    )
    source = tmp_path / "replace-fails.csv"
    source.write_text(
        "id,note\n3,replacement\n3,duplicate\n", encoding="utf-8"
    )

    with pytest.raises(Exception, match="Duplicate entry"):
        import_csv_impl(
            str(source), table, ds, "stu", batch_size=1, truncate_first=True
        )

    rows = _admin_sql(
        ds, f"SELECT id, note FROM `{table}` ORDER BY id", fetch=True
    )
    assert rows == [(1, "original-one"), (2, "original-two")]


def test_real_mysql_qualified_stu_table_routes_reads_and_writes(live_table):
    ds, table = live_table

    write = execute_any_sql(
        f"INSERT INTO `stu`.`{table}` (id, note) VALUES (1, 'qualified')",
        ds,
        "not_the_default_database",
    )
    read = execute_read_only_query(
        f"SELECT id, note FROM `stu`.`{table}` WHERE id = 1",
        ds,
        "not_the_default_database",
    )

    assert write["affected_rows"] == 1
    assert read["data"] == [{"id": 1, "note": "qualified"}]
    assert read["read_only_enforced"] is True


def test_real_mysql_query_and_export_failures_leave_clean_reusable_state(
    live_table, tmp_path
):
    ds, table = live_table
    target = tmp_path / "must-not-exist.csv"

    with pytest.raises(Exception, match="missing_column"):
        export_csv_impl(
            f"SELECT missing_column FROM `{table}`", str(target), ds, "stu"
        )

    assert not target.exists()
    assert not target.with_name(target.name + ".part").exists()

    with pytest.raises(Exception, match="missing_column"):
        execute_read_only_query(
            f"SELECT missing_column FROM `{table}`", ds, "stu"
        )

    execute_any_sql(
        f"INSERT INTO `{table}` (id, note) VALUES (1, 'after-errors')",
        ds,
        "stu",
    )
    result = execute_read_only_query(
        f"SELECT id, note FROM `{table}` WHERE id = 1", ds, "stu"
    )
    assert result["data"] == [{"id": 1, "note": "after-errors"}]
