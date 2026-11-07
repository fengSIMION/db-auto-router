# -*- coding: utf-8 -*-
"""只读通道第二层：sqlglot AST 三态判定。

设计文档：docs/superpowers/specs/2026-11-07-read-guard-three-layers-design.md

为什么正则守卫之后还要有一层：第一层（sqlguard.validate_read_only_sql）是
"可证明只读"的枚举式白名单，`_CONFIRM_EXTRA_WRITE_PREFIXES` 打过七次补丁——
枚举永远慢于绕过。本层用真解析器给语句类型一个**独立的第二意见**。

三态判定（本层最重要的设计，改前先读）：
  - 语法树里检出写形态节点        → 拒（第二意见命中）
  - 根是 Query/Show/Describe     → 放行
  - 根是 exp.Command / 方言未映射 → **弃权放行**（判不了就不判）
  - 真 ParseError                → 拒（附逃生门文案）

弃权不是偷懒：EXPLAIN/SHOW 在 PostgreSQL/GaussDB/Oracle/达梦上 sqlglot 建模不了
（回落 exp.Command），判不了就拒会把 explain_query 死在四个方言上；而达梦的
ROWNUM 等特有语法在映射方言下同样落 Command——株连拒绝等于天天误杀合法查询。
 Layer 1 的前缀白名单本来就拦得住这些位置上的写语句，第三层再兜 DML。
"""
from __future__ import annotations

__author__ = "fengkeju"
import logging
from typing import Optional

from .config import AST_GUARD
from .logcfg import logger
from .util import sanitize_sql_for_scan

# 方言映射：sqlglot 没有 dm / gaussdb / dameng 方言（34 个方言实测均无），
# 达梦高度兼容 Oracle、GaussDB 兼容 PG 协议，各映射到最近的原生方言。
# 映射是近似的，解析不了的语句走弃权，不假装覆盖。
DIALECT_MAP = {
    "mysql": "mysql",
    "postgres": "postgres",
    "gaussdb": "postgres",
    "oracle": "oracle",
    "dm": "oracle",
}

# 写形态节点集：逐个列，**不靠 isinstance 继承**——实测 exp.Set / exp.Use /
# exp.Lock / exp.Analyze / exp.TruncateTable / exp.Copy / exp.Into 都不是
# DML/DDL 的子类，"拒 DML+DDL"的白名单会把它们全放过去。
_WRITE_NODES = (
    "Insert", "Update", "Delete", "Merge", "Copy", "Drop", "Create", "Alter",
    "Grant", "Set", "Lock", "Transaction", "Use", "TruncateTable", "Analyze", "Into",
)

# 只读根形态：Query 覆盖 Select/Union/Except/Intersect/Subquery。
_READ_ROOTS = ("Query", "Show", "Describe")

# 高危函数集：AST 与正则**都漏的唯一大类**（函数副作用）。key 是映射后的
# sqlglot 方言名，value 小写。未知函数一律弃权，绝不株连。
#   postgres: 读服务器文件 / 建外联(SSRF) / DoS / 杀后端 / 大对象导入导出 / 序列推进
#   mysql:    读服务器文件 / 基准测试(DoS) / UDF 执行外壳
#   oracle:   空——Oracle 与达梦的文件网络函数多需 PL/SQL 门控，SELECT 层够不到
DANGEROUS_FUNCTIONS = {
    "postgres": frozenset({
        "pg_read_file", "pg_read_server_files", "dblink", "pg_sleep",
        "pg_terminate_backend", "lo_import", "lo_export", "nextval", "setval",
    }),
    "mysql": frozenset({"load_file", "benchmark", "sys_exec"}),
    "oracle": frozenset(),
}

# 病态 SQL 的确定性预检：解析超时在 asyncio.to_thread 的工作线程里杀不掉
# Python 线程（会变成挂住），所以用长度/嵌套深度做确定性拒绝，失败方向仍是拒。
MAX_SQL_CHARS = 100_000
MAX_PAREN_DEPTH = 100

# sqlglot 懒加载：MCP 启动不许被它拖慢（workspace.py 记着 import 10.8s→0.3s 的教训）。
# 缺失时本层整体关闭，启动 warning 一次，每次返回 note 由调用方说明。
_SQLGLOT = None
_sqlglot_probe_failed = False


def _load_sqlglot():
    """首次使用时导入 sqlglot；失败只标记不抛（本层关闭，交给其它两层）。"""
    global _SQLGLOT, _sqlglot_probe_failed
    if _SQLGLOT is not None or _sqlglot_probe_failed:
        return _SQLGLOT
    try:
        import sqlglot
        from sqlglot import exp as _exp  # noqa: F401  确认 expressions 可用
        _SQLGLOT = sqlglot
    except Exception as e:
        _sqlglot_probe_failed = True
        logger.warning(
            f"sqlglot 导入失败，只读通道第二层（AST 校验）已关闭：{type(e).__name__}: {e}。"
            f"仍有一层正则守卫与三层数据库只读兜底；安装 sqlglot>=30,<31 可恢复本层")
    return _SQLGLOT


def _reject(reason: str) -> None:
    """统一的拒绝出口：一律 PermissionError（与第一层同类型，错误形状一致），
    文案必带逃生门（A2：不许把"解析器不懂"说成"你的 SQL 有错"）。"""
    raise PermissionError(
        f"安全拦截：{reason}。如确认安全，请改用 execute 工具"
        f"（写操作有服务端确认兜底，模型无法绕过）"
    )


def _check_not_pathological(sql: str) -> None:
    """长度/嵌套深度预检。用脱敏文本数括号——字符串字面量里的括号不算，
    否则 `SELECT '((((…'` 这种合法 SQL 会被误判为病态。"""
    if len(sql) > MAX_SQL_CHARS:
        _reject(f"语句长度 {len(sql)} 超过 {MAX_SQL_CHARS} 字符，疑似病态 SQL")
    depth = 0
    for ch in sanitize_sql_for_scan(sql):
        if ch == "(":
            depth += 1
            if depth > MAX_PAREN_DEPTH:
                _reject(f"语句括号嵌套深度超过 {MAX_PAREN_DEPTH}，疑似病态 SQL")
        elif ch == ")":
            depth -= 1


def validate_read_statement_ast(sql: str, dialect: str) -> None:
    """第二层入口：对一条已过第一层的 SQL 做 AST 二次判定。

    拒绝抛 PermissionError；放行与弃权都返回 None。
    dialect 为数据源的 db_type（小写）；未映射的方言本层弃权。
    """
    if AST_GUARD != "on":
        return
    mapped = DIALECT_MAP.get((dialect or "").lower().strip())
    if not mapped:
        return  # 未映射方言：本层弃权，note 由调用方说明
    sg = _load_sqlglot()
    if sg is None:
        return  # sqlglot 缺失：本层关闭

    _check_not_pathological(sql)

    from sqlglot.errors import ParseError  # noqa: F401  仅在 sg 可用时导入

    try:
        expr = sg.parse_one(sql, read=mapped)
    except PermissionError:
        raise
    except Exception as e:
        # 解析失败 ≠ 用户的 SQL 有错（A2）。方言边缘语法、驱动专有扩展都会落到这里，
        # 一律按"无法证明只读"拒绝，但把话说明白，并给逃生门。
        _reject(f"本地解析器（sqlglot {mapped} 方言）无法解析该语句"
                f"（{type(e).__name__}），可能是方言边缘语法")

    exp = sg.exp
    # 弃权：sqlglot 建模不了的语句（EXPLAIN/SHOW 在 PG/Oracle/达梦上落 Command）
    if isinstance(expr, exp.Command):
        logger.debug(f"第二层弃权：sqlglot 回落 Command，交由一三层把关: {sql[:80]}")
        return
    if not isinstance(expr, tuple(getattr(exp, n) for n in _READ_ROOTS)):
        _reject(f"第二层解析判定该语句不是只读形态（{type(expr).__name__}）")

    # 全树 walk，不能只查根：WITH c AS (INSERT ...) SELECT * FROM c 的根是 Select
    for node in expr.walk():
        name = type(node).__name__
        if name in _WRITE_NODES:
            _reject(f"第二层在语法树中检出写操作节点 {name}（第一层未覆盖的路径）")

    dangerous = DANGEROUS_FUNCTIONS.get(mapped) or frozenset()
    if dangerous:
        for func in expr.find_all(exp.Func):
            fname = (getattr(func, "name", "") or "").lower()
            if fname in dangerous:
                _reject(f"第二层检出高危函数 {fname}()（可读服务器文件/建外联/拖库/DoS），"
                        f"只读通道不允许")
