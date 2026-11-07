# -*- coding: utf-8 -*-
"""测试共用的假连接/假游标：让只读层（第三层）与读引擎的接线测试不碰真数据库。

约定：
- `conn.executed` 记录连接上执行过的**所有** SQL（含游标执行的），测试从连接视角断言；
  游标自己的 `executed` 也保留，供需要区分"哪条语句哪个游标跑的"的场景
- `fail_on` 模拟"数据库不认这条语句"或"连接层故障"：key 是 SQL 子串，
  命中即抛对应异常——用来验证能力缓存只记"明确拒绝"、不记瞬时故障
"""
from __future__ import annotations


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.executed: list[str] = []
        self.description = None      # 读引擎接线测试用：置成列名列表即"有结果集"
        self._rows = []              # fetchmany 的返回值序列
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.executed.append(sql)
        self.conn.executed.append(sql)      # 连接视角也看得到
        for pat, exc in self.conn.fail_on.items():
            if pat in sql:
                raise exc
        self.conn.last_sql = sql

    def fetchmany(self, n):
        if self._rows:
            return self._rows.pop(0)
        return []

    def fetchall(self):
        out = []
        while True:
            batch = self.fetchmany(500)
            if not batch:
                break
            out.extend(batch)
        return out

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


class FakeConn:
    """假连接。fail_on: {SQL 子串: 命中时抛出的异常}。"""

    def __init__(self, fail_on=None):
        self.fail_on = fail_on or {}
        self.executed: list[str] = []
        self.closed = False
        self.last_sql = None
        self.autocommit = True

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        self.executed.append("--rollback--")

    def commit(self):
        self.executed.append("--commit--")

    def close(self):
        self.closed = True
