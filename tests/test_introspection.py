# -*- coding: utf-8 -*-
"""get_table_schema 连接合并的回归网。

背景：get_table_schema 原先自己借一次连接查 columns，再分别调用
get_table_indexes / get_table_constraints——后两者各自又借一次，一次
get_schema_info 实际借 3 回连接。本测试锁定两件事：

1. 返回值结构/映射不变（characterization，重构的安全网）；
2. 一次调用只借 1 回连接、只还 1 回，且三路 catalog 查询都仍在同一条连接上跑。

不碰真数据库：用 tests.dbfakes 的 FakeConn/FakeCursor，按 SQL 特征返回
罐头数据；get_connection / put_connection 用 monkeypatch 换成计数假件。
"""
from db_auto_router import introspection
from tests.dbfakes import FakeConn, FakeCursor


class _PgSchemaCursor(FakeCursor):
    """按 SQL 特征返回 postgres 各方言查询的罐头结果。

    三个查询靠这些独有片段区分（避免误判）：
      columns     → "format_type(a.atttypid"
      indexes     → "array_position(ix.indkey"
      constraints → "table_constraints"
    """

    def execute(self, sql, params=None):
        super().execute(sql, params)
        low = sql.lower()
        if "format_type(a.atttypid" in low:
            self.description = [("field",), ("type",), ("nullable",), ("key_type",),
                                ("default_value",), ("extra",), ("comment",)]
            self._rows = [[
                ("id", "integer", "NO", "PRI", "nextval('s'::regclass)", "", "主键"),
                ("name", "character varying", "YES", "", "", "", "名称"),
            ]]
        elif "array_position(ix.indkey" in low:
            self.description = [("index_name",), ("column_name",), ("is_unique",),
                                ("is_primary",), ("seq_in_index",), ("index_type",),
                                ("index_comment",)]
            self._rows = [[("orders_pkey", "id", 1, 1, 1, "btree", "")]]
        elif "table_constraints" in low:
            self.description = [("constraint_name",), ("constraint_type",), ("column_name",),
                                ("referenced_table",), ("referenced_column",), ("delete_rule",)]
            self._rows = [[("orders_pkey", "PRIMARY KEY", "id", None, None, None)]]
        else:
            self.description = []
            self._rows = []


class _PgSchemaConn(FakeConn):
    def cursor(self):
        return _PgSchemaCursor(self)


def test_get_table_schema_returns_full_structure_postgres(monkeypatch):
    """锁返回值：columns/indexes/constraints 的字段映射与合并前完全一致。"""
    conn = _PgSchemaConn()
    monkeypatch.setattr(introspection, "get_connection", lambda ds, target_db=None: conn)
    monkeypatch.setattr(introspection, "put_connection", lambda ds, c, target_db=None: None)

    result = introspection.get_table_schema("orders", {"db_type": "postgres"}, "defdb")

    assert result["columns"] == [
        {"field": "id", "type": "integer", "nullable": "NO", "key_type": "PRI",
         "default_value": "nextval('s'::regclass)", "extra": "", "comment": "主键"},
        {"field": "name", "type": "character varying", "nullable": "YES", "key_type": "",
         "default_value": "", "extra": "", "comment": "名称"},
    ]
    assert result["indexes"] == [
        {"index_name": "orders_pkey", "column_name": "id", "is_unique": 1, "is_primary": 1,
         "seq_in_index": 1, "index_type": "btree", "index_comment": ""},
    ]
    assert result["constraints"] == [
        {"constraint_name": "orders_pkey", "constraint_type": "PRIMARY KEY",
         "column_name": "id", "referenced_table": None, "referenced_column": None,
         "delete_rule": None},
    ]


def test_get_table_schema_borrows_one_connection(monkeypatch):
    """一次 get_schema_info 只借/还 1 回连接，且三路 catalog 查询都在这条连接上跑。"""
    conn = _PgSchemaConn()
    borrows = []
    puts = []

    def fake_get(ds, target_db=None):
        borrows.append(target_db)
        return conn

    def fake_put(ds, c, target_db=None):
        puts.append(c)

    monkeypatch.setattr(introspection, "get_connection", fake_get)
    monkeypatch.setattr(introspection, "put_connection", fake_put)

    introspection.get_table_schema("orders", {"db_type": "postgres"}, "defdb")

    assert len(borrows) == 1, f"应只借 1 回连接，实际借了 {len(borrows)} 回"
    assert len(puts) == 1, f"应只还 1 回连接，实际还了 {len(puts)} 回"
    joined = " ".join(conn.executed)
    assert "format_type(a.atttypid" in joined, "columns 查询未执行"
    assert "array_position(ix.indkey" in joined, "indexes 查询未执行"
    assert "table_constraints" in joined, "constraints 查询未执行"


def test_get_table_indexes_standalone_still_borrows_and_returns(monkeypatch):
    """conn 不传时保持原行为：自行借用/归还一次，并正确返回索引行。

    覆盖合并时新增的 own = conn is None 分支的另一侧（自借路径）——
    get_table_schema 现在总传 conn，不走这条，但没有用例守着它就会悄悄烂掉。
    """
    conn = _PgSchemaConn()
    borrows, puts = [], []

    def fake_get(ds, target_db=None):
        borrows.append(target_db)
        return conn

    def fake_put(ds, c, target_db=None):
        puts.append(c)

    monkeypatch.setattr(introspection, "get_connection", fake_get)
    monkeypatch.setattr(introspection, "put_connection", fake_put)

    result = introspection.get_table_indexes("orders", {"db_type": "postgres"}, "defdb")

    assert len(borrows) == 1
    assert len(puts) == 1
    assert result == [
        {"index_name": "orders_pkey", "column_name": "id", "is_unique": 1, "is_primary": 1,
         "seq_in_index": 1, "index_type": "btree", "index_comment": ""},
    ]
