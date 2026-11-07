# -*- coding: utf-8 -*-
"""只读通道第二层（sqlglot AST 三态判定）测试。

设计文档：docs/superpowers/specs/2026-11-07-read-guard-three-layers-design.md
守卫契约见该文档「第二层」节；本文件是 A5（判不了必须放行、不许株连）的守门测试。
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from db_auto_router import sqlparse
from db_auto_router.sqlparse import validate_read_statement_ast as v

ALL = ["mysql", "postgres", "gaussdb", "oracle", "dm"]


# 1. 方言映射 ---------------------------------------------------------------

def test_dialect_map_covers_five_dialects():
    assert sqlparse.DIALECT_MAP == {"mysql": "mysql", "postgres": "postgres",
                                    "gaussdb": "postgres", "oracle": "oracle", "dm": "oracle"}


# 2. 写语句必须拒（sqlglot 建得了模的那些）-----------------------------------
#
# 注意 LOCK TABLES / CALL / GRANT 不在此列：它们在 sqlglot 里回落 exp.Command，
# 按设计走弃权（见 test_command_class_writes_belong_to_layer1）。第二层只对
# 自己建得了模的写形态负责。
@pytest.mark.parametrize("sql", [
    "DELETE FROM t", "UPDATE t SET a=1", "INSERT INTO t VALUES (1)",
    "DROP TABLE t", "CREATE TABLE x(a int)", "TRUNCATE TABLE t",
    "SET @x=1", "USE otherdb", "ANALYZE TABLE t",
])
def test_rejects_write_statements(sql):
    with pytest.raises(PermissionError):
        v(sql, "mysql")


# 2b. sqlglot 建模不了、但开头就不是只读前缀的写语句：第一层的活 --------
#
# 这三类落 exp.Command，第二层按 A5 弃权。真正拦住它们的是第一层的前缀白名单
# （LOCK/CALL/GRANT 都不在 SELECT/SHOW/DESC/DESCRIBE/EXPLAIN/WITH 里）——
# 生产里它们根本到不了第二层。本测试把"谁负责哪类"这个分层契约钉住，
# 防止将来有人删掉第一层前缀检查还以为第二层能兜底。
@pytest.mark.parametrize("sql", [
    "LOCK TABLES t WRITE", "CALL my_proc()", "GRANT ALL ON *.* TO u",
])
def test_command_class_writes_belong_to_layer1(sql):
    from db_auto_router.sqlguard import validate_read_only_sql
    with pytest.raises(PermissionError):
        validate_read_only_sql(sql)   # 第一层必须拦下
    v(sql, "mysql")                   # 第二层弃权（不抛）——分层如上


# 3. 树里藏写必须拒（根节点是 Select，只查根必漏）---------------------------

def test_rejects_write_hidden_in_cte():
    with pytest.raises(PermissionError):
        v("WITH c AS (INSERT INTO t VALUES (1)) SELECT * FROM c", "mysql")


# 4. 合法只读必须放行 -------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT 1", "SELECT * FROM t WHERE a='LIMIT 5'", "SELECT COUNT(*) FROM t",
    "WITH x AS (SELECT 1) SELECT * FROM x", "SHOW TABLES", "DESC t",
    "EXPLAIN SELECT * FROM t", "EXPLAIN ANALYZE SELECT * FROM t",
])
def test_passes_read_statements(sql):
    v(sql, "mysql")  # 不抛即通过


# 5. Command 弃权：EXPLAIN/SHOW 在 PG/Oracle/DM 落 exp.Command，判不了就不判 -

@pytest.mark.parametrize("dialect,sql", [
    ("postgres", "EXPLAIN SELECT * FROM t"),
    ("postgres", "EXPLAIN ANALYZE SELECT * FROM t"),
    ("oracle", "EXPLAIN SELECT * FROM t"),
    ("dm", "SELECT * FROM t WHERE ROWNUM <= 10"),
    ("gaussdb", "SHOW TABLES"),
])
def test_abstains_when_unclassifiable(dialect, sql):
    v(sql, dialect)  # 不抛即弃权放行


# 6. 真 ParseError → 拒，且文案给逃生门（A2）--------------------------------

def test_rejects_parse_error_with_escape_hatch():
    with pytest.raises(PermissionError) as ei:
        v("SELECT FROM WHERE GROUP BY", "mysql")
    assert "execute" in str(ei.value)       # 逃生门
    assert "语法错误" not in str(ei.value)   # A2：不许把"解析不了"说成"你的 SQL 有语法错"


# 7. 未映射方言 → 弃权 ------------------------------------------------------

def test_abstains_on_unmapped_dialect():
    v("SELECT 1", "sqlserver")


# 8. 危险函数集 -------------------------------------------------------------

@pytest.mark.parametrize("dialect,sql", [
    ("postgres", "SELECT pg_read_file('/etc/passwd')"),
    ("postgres", "SELECT * FROM dblink('host', 'select 1')"),
    ("postgres", "SELECT pg_sleep(10)"),
    ("postgres", "SELECT nextval('s')"),
    ("mysql", "SELECT LOAD_FILE('/etc/passwd')"),
    ("mysql", "SELECT BENCHMARK(1000000, MD5('x'))"),
])
def test_rejects_dangerous_functions(dialect, sql):
    with pytest.raises(PermissionError):
        v(sql, dialect)


def test_unknown_function_abstains():
    v("SELECT my_custom_func(x) FROM t", "mysql")  # 不株连


# 9. 病态 SQL 确定性拒绝 ----------------------------------------------------

def test_oversized_sql_rejected():
    with pytest.raises(PermissionError):
        v("SELECT " + ",".join(f"c{i}" for i in range(20000)) + " FROM t", "mysql")


def test_deep_nesting_rejected():
    with pytest.raises(PermissionError):
        v("SELECT " + "(" * 200 + "1" + ")" * 200, "mysql")


# 10. 降级路径 --------------------------------------------------------------

def test_missing_sqlglot_degrades(monkeypatch):
    monkeypatch.setattr(sqlparse, "_SQLGLOT", None)          # 模拟导入失败后的状态
    monkeypatch.setattr(sqlparse, "_sqlglot_probe_failed", True)
    v("DELETE FROM t", "mysql")  # 第二层关闭 → 不抛，交给第一层


def test_guard_switch_off(monkeypatch):
    monkeypatch.setattr(sqlparse, "AST_GUARD", "off")
    v("DELETE FROM t", "mysql")


# 11. 五方言回归语料（A5 守门测试 + D4 版本漂移告警器）----------------------
#
# 语料原文抄自 tests/test_sqlguard.py section C（第一层必须放行的合法只读）。
# 第二层不许把其中任何一条拒掉——但"合法"是分方言的：反引号标识符只有 MySQL
# 认、EXPLAIN 选项括号只有 PG 认，这些语句在对方言上数据库本来也会报错，
# 第二层拒了是对的。故每条标注其真实有效方言，只在那些方言上断言不拒。
#
# （sqlglot 30.30.0 实测：SHOW/EXPLAIN 在 PG/Oracle/DM 落 exp.Command 走弃权，
#  MySQL 落 Show/Describe 直接放行——这条语料是"弃权"路径存在的理由。）

CORPUS = [
    ("SELECT 1", ALL),
    ("SELECT * FROM t", ALL),
    ("WITH x AS (SELECT 1) SELECT * FROM x", ALL),
    ("SELECT 1 -- 备注", ALL),
    ("SELECT 1 -- 备注; 完成时间", ALL),
    ("SELECT ';' FROM t", ALL),
    ("SELECT * FROM t WHERE a='LIMIT 5'", ALL),
    ("SELECT 'a--b' FROM t", ALL),
    ("SELECT COUNT(*) FROM t", ALL),
    ("SELECT CASE WHEN a>1 THEN 1 ELSE 0 END FROM t", ALL),
    ("SELECT * FROM (SELECT id FROM t) x", ALL),
    ("DESC t", ALL),
    ("EXPLAIN SELECT * FROM t", ALL),
    ("EXPLAIN ANALYZE SELECT * FROM t", ["mysql", "postgres", "gaussdb"]),
    ('SELECT "col" FROM "tbl"', ["postgres", "gaussdb", "oracle", "dm"]),
    ("SHOW TABLES", ["mysql"]),
    ("SELECT `select` FROM `my table`", ["mysql"]),
    ("EXPLAIN (ANALYZE, FORMAT JSON) SELECT * FROM t", ["postgres", "gaussdb"]),
]


@pytest.mark.parametrize("dialect", ALL)
@pytest.mark.parametrize("sql,dialects", CORPUS)
def test_five_dialect_corpus_never_false_rejects(sql, dialects, dialect):
    if dialect not in dialects:
        pytest.skip(f"{dialect} 上该语句本就非法（数据库同样会拒），不在语料内")
    v(sql, dialect)  # 第一层放行的合法只读，第二层一律不许拒
