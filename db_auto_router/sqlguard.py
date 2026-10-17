# -*- coding: utf-8 -*-
"""SQL 安全校验与解析：LIMIT 检查 / 只读校验 / 写操作判定 / 多语句拆分
——原 643–833、980–988、993–1099 行。
安全清理：删除无调用方的死代码 execute_with_retry（原 672–695 行）；
散落在函数内的 import re 已收拢到模块顶部。"""

__author__ = "fengkeju"
import re
from typing import Optional, List

from .config import MAX_RESULT_ROWS, READ_ONLY_SQL_PREFIXES, WRITE_SQL_PREFIXES
from .util import sanitize_sql_for_scan as _sanitize_sql_for_scan


def check_sql_limit(sql: str, default_limit: int = MAX_RESULT_ROWS) -> dict:
    """
    检查 SQL 是否需要添加 LIMIT 保护。

    default_limit：调用方的有效行数上限（query 工具的 max_rows 覆盖后的值）。
    缺省为配置的 MAX_RESULT_ROWS。建议值必须跟随调用方的上限，否则
    max_rows=5000 时自动追加的仍是 LIMIT 1000，与真正的行数硬上限自相矛盾。

    返回 dict，包含:
    - need_protect: bool 是否需要保护
    - suggested_limit: int 建议的 LIMIT 值（需要保护时）
    - has_limit: bool 是否已有 LIMIT
    - existing_limit: int 已有的 LIMIT 值（有 LIMIT 时）
    """
    sql_upper = _sanitize_sql_for_scan(sql).strip().upper()

    # 非 SELECT 不需要保护
    if not sql_upper.startswith("SELECT"):
        return {"need_protect": False, "suggested_limit": None, "has_limit": False, "existing_limit": None}

    # 检查是否已有 LIMIT（脱敏后扫描：注释/字符串里的 "LIMIT 5" 不算真 LIMIT，
    # 否则 `SELECT * FROM t -- LIMIT 5` 会被误判为已限流而一条都不加，整表全量拉取）
    limit_match = re.search(r'\bLIMIT\s+(\d+)', sql_upper, re.IGNORECASE)
    has_limit = limit_match is not None

    if has_limit:
        existing_limit = int(limit_match.group(1))
        return {"need_protect": False, "suggested_limit": None, "has_limit": True, "existing_limit": existing_limit}

    # 没有 LIMIT，需要保护
    return {"need_protect": True, "suggested_limit": default_limit, "has_limit": False, "existing_limit": None}


def _find_top_level_write_keyword(sanitized_sql_upper: str) -> Optional[str]:
    """
    在括号深度为 0 的顶层查找写操作/DDL 关键字。
    用于封堵 WITH...DELETE（MySQL8/PG 主语句写）等绕过；传入前必须先 _sanitize_sql_for_scan。
    返回命中的关键字，未命中返回 None。
    """
    # 反复折叠最内层括号组，直到没有括号——剩下的就是顶层文本
    top = sanitized_sql_upper
    prev = None
    while prev != top:
        prev = top
        top = re.sub(r'\([^()]*\)', ' ', top)
    for kw in ("INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE",
               "DROP", "CREATE", "ALTER", "TRUNCATE", "CALL",
               "GRANT", "REVOKE", "LOCK"):
        if re.search(r'\b' + kw + r'\b', top):
            return kw
    # SELECT ... INTO 新表 / INTO OUTFILE（建表或写文件）
    if re.search(r'\bINTO\s+\w+', top):
        return "INTO"
    return None


# NOTE: 瞬态错误判定已迁至 errutil.is_transient_error（原在此处名为 _is_retryable_error，
# 但放在 SQL 守卫模块违反单一职责原则）。
def _raise_if_body_writes(sanitized_upper: str, check_lock: bool) -> None:
    """对一条已脱敏/大写/去左括号的语句体做写操作检查。

    抽出来供两个入口复用：原始语句，以及 EXPLAIN ANALYZE 剥掉选项后的语句体。
    后者必须重新过一遍门——PG/GaussDB 上 EXPLAIN ANALYZE 对 DML 是**真实执行**，
    只判前缀会漏掉 `EXPLAIN ANALYZE WITH d AS (DELETE FROM t RETURNING id) SELECT * FROM d`
    （body 以 WITH 开头，前缀检查放行，DELETE 真实删行且无确认）。

    传入前必须先 _sanitize_sql_for_scan，字符串字面量里的 "AS (DELETE ..." 才不误报。
    """
    # WITH CTE 内部不能包含写操作关键字
    if sanitized_upper.startswith("WITH"):
        for kw in ("INSERT", "DELETE", "UPDATE", "DROP", "TRUNCATE", "ALTER", "CREATE"):
            # 匹配 CTE 定义中的写操作：WITH xxx AS (DELETE FROM ...)
            if re.search(r'\bAS\s*\(\s*' + kw + r'\b', sanitized_upper):
                raise PermissionError(
                    f"安全拦截：WITH CTE 中包含写操作关键字 {kw}，"
                    "包含写操作的 CTE 请使用 execute 工具"
                )
        # WITH 主语句不能是写操作（MySQL 8 / PG 支持 WITH ... UPDATE/DELETE，
        # CTE 内部检查覆盖不到主语句，必须单独在括号顶层扫描）
        kw = _find_top_level_write_keyword(sanitized_upper)
        if kw:
            raise PermissionError(
                f"安全拦截：WITH 主语句包含写操作 {kw}，请使用 execute 工具"
            )

    # SELECT INTO（Oracle/达梦创建新表、MySQL INTO OUTFILE/DUMPFILE 写服务器文件）
    if sanitized_upper.startswith("SELECT") and re.search(r'\bINTO\s+\w+', sanitized_upper):
        raise PermissionError(
            "安全拦截：SELECT INTO 是写操作（创建新表/写文件），请使用 execute 工具"
        )

    # 锁定子句：会真实加行锁/表锁。连接归还池后锁可能一直持有到空闲超时
    # （默认 300s），只读通道不应具备加锁能力；确需锁定请走 execute（有服务端确认）。
    if check_lock:
        lock_hit = _LOCKING_CLAUSE_RE.search(sanitized_upper)
        if lock_hit:
            raise PermissionError(
                f"安全拦截：{lock_hit.group(0).upper()} 会真实对数据加锁，"
                "只读查询通道不允许加锁，请使用 execute 工具执行"
            )


def validate_read_only_sql(sql: str) -> None:
    """
    校验 SQL 是否为只读语句，防止绕过。

    拦截场景：
    - 多语句拼接：SELECT 1; DELETE FROM users
    - WITH CTE 包含写操作：WITH d AS (DELETE ...) SELECT ...
    - WITH 主语句写操作（MySQL 8/PG）：WITH x AS (SELECT 1) DELETE FROM t
    - SELECT INTO（Oracle/达梦）：SELECT * INTO new_table FROM ...
    - EXPLAIN ANALYZE 执行 DML（PG/GaussDB 会真实执行，含数据修改 CTE）
    - FOR UPDATE / FOR SHARE / LOCK IN SHARE MODE 加锁
    """
    sql_trimmed = sql.strip()

    # 拦截1：禁止多语句。用状态机拆分判定（split_sql_statements 尊重字符串/注释），
    # 字符串字面量或注释里的分号（如 SELECT ';'、-- 备注; 完成时间）不再误判为多语句
    stripped = sql_trimmed.rstrip(";").strip()
    if len(split_sql_statements(sql_trimmed)) > 1:
        raise PermissionError(
            "安全拦截：query 工具禁止执行多语句，"
            "如需执行多条语句请分别调用，写操作请使用 execute 工具"
        )

    # 脱敏文本（去字符串字面量/注释），供顶层关键字扫描。
    # **所有前缀判定都必须用它，不能用原文**：原文开头的注释
    # （`-- 备注\nSELECT 1`、`/* 备注 */\nSELECT 1`）会让 `stripped.upper()`
    # 以 "--" 开头，合法只读查询被误判成"仅允许执行只读查询"而遭拦截。
    sanitized_upper = _sanitize_sql_for_scan(stripped).upper().lstrip("(").lstrip()
    sql_upper = sanitized_upper

    # 拦截2：必须以只读关键字开头
    if not any(sql_upper.startswith(prefix) for prefix in READ_ONLY_SQL_PREFIXES):
        raise PermissionError(
            "安全拦截：query 工具仅允许执行只读查询 (SELECT/SHOW/DESCRIBE/EXPLAIN/WITH)，"
            "写操作请使用 execute 工具"
        )

    # 拦截3/3b/4/6：CTE 写、主语句写、SELECT INTO、加锁子句
    _raise_if_body_writes(sanitized_upper, check_lock=True)

    # 拦截5：EXPLAIN ANALYZE 会真实执行语句（PG/GaussDB 对 DML 同样生效）
    if sql_upper.startswith("EXPLAIN"):
        body = sanitized_upper[len("EXPLAIN"):].strip()
        has_analyze = False
        # PG 选项语法：EXPLAIN (ANALYZE, FORMAT JSON) ...
        if body.startswith("("):
            close = body.find(")")
            opts = body[1:close] if close != -1 else body
            has_analyze = "ANALYZE" in opts
            if close != -1:
                body = body[close + 1:].strip()
        # 通用语法：EXPLAIN ANALYZE ...
        if body.startswith("ANALYZE"):
            has_analyze = True
            body = body[len("ANALYZE"):].strip()
        # MySQL 语法：EXPLAIN FORMAT=JSON/TREE [ANALYZE] ...
        body = re.sub(r'^FORMAT\s*=\s*\w+\s*', '', body).strip()
        if body.startswith("ANALYZE"):
            has_analyze = True
            body = body[len("ANALYZE"):].strip()
        if has_analyze:
            # 剥壳后的语句体在 PG 上会被真实执行，必须重新过一遍写门
            _raise_if_body_writes(body, check_lock=True)
            if not any(body.startswith(p) for p in ("SELECT", "WITH", "TABLE", "VALUES")):
                raise PermissionError(
                    "安全拦截：EXPLAIN ANALYZE 会真实执行非 SELECT 语句，请使用 execute 工具"
                )


# execute 确认门的补充写关键字：与守卫的写关键字面保持一致
# （原实现只有 WRITE_SQL_PREFIXES 六个前缀，REPLACE/CALL/GRANT 等会漏确认）
_CONFIRM_EXTRA_WRITE_PREFIXES = ("REPLACE", "MERGE", "CALL", "GRANT", "REVOKE", "LOCK", "RENAME")

# 只读语句上的锁定子句：会真实加行锁/表锁。
# 命中即拦截——连接归还池后锁可能一直持有到空闲超时（默认 300s），
# 只读通道不应具备加锁能力；确需锁定请走 execute（有服务端确认）。
# 覆盖 MySQL 的 LOCK IN SHARE MODE，与 PG/Oracle/达梦的 FOR UPDATE/SHARE 家族
# （含 PG 的 NO KEY UPDATE / KEY SHARE）。OF/NOWAIT/SKIP LOCKED 等后缀不需要单独列。
_LOCKING_CLAUSE_RE = re.compile(
    r'\bFOR\s+(?:UPDATE|SHARE|NO\s+KEY\s+UPDATE|KEY\s+SHARE)\b'
    r'|\bLOCK\s+IN\s+SHARE\s+MODE\b',
    re.IGNORECASE,
)


def is_write_operation(sql: str) -> bool:
    """判断是否为写操作（execute 确认门的判定依据，必须防绕过）。

    - 判定前先脱敏（去注释/字符串字面量）：封堵 `/* c */ DELETE ...`、`-- x\\nDELETE ...`
      等注释前缀绕过，也避免字符串里的关键字误报
    - 写前缀在 WRITE_SQL_PREFIXES 基础上补齐 REPLACE/MERGE/CALL/GRANT/REVOKE/LOCK/RENAME
    - WITH 主语句写（MySQL8/PG）、SELECT INTO、SET GLOBAL/PERSIST 同样判为写
    """
    sanitized_upper = _sanitize_sql_for_scan(sql).upper().lstrip("(").lstrip()
    if not sanitized_upper:
        return False
    if any(sanitized_upper.startswith(prefix)
           for prefix in WRITE_SQL_PREFIXES | set(_CONFIRM_EXTRA_WRITE_PREFIXES)):
        return True
    if sanitized_upper.startswith("WITH"):
        return _find_top_level_write_keyword(sanitized_upper) is not None
    if sanitized_upper.startswith("SELECT") and re.search(r'\bINTO\s+\w+', sanitized_upper):
        return True
    if re.match(r'^SET\s+(?:GLOBAL|PERSIST(?:ENT)?)\b', sanitized_upper):
        return True
    return False


def is_ddl_operation(sql: str) -> bool:
    """判断是否为 DDL 操作（CREATE/ALTER/DROP/TRUNCATE/RENAME）。先脱敏再判定，
    防止注释前缀（/* c */ DROP ...）让 DDL 标注与确认弹窗警示失效"""
    sanitized_upper = _sanitize_sql_for_scan(sql).upper().lstrip("(").lstrip()
    return any(sanitized_upper.startswith(prefix)
               for prefix in ("CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME"))


# ------------------------------------------------------------------------------
# DDL 与事务原子性（v4.8）——本表是全簇唯一事实源，别处 MUST NOT 另立口径
# ------------------------------------------------------------------------------
# 多语句批量（分号分隔）走 autocommit=False + 末尾统一 commit / 异常统一 rollback，
# 但"任一语句失败整批回滚"这个承诺有真实边界，只在下面两种情况下成立：
#   1. 批内没有 DDL（纯 DML，所有方言都可回滚）；
#   2. 批内有 DDL，且该方言的 DDL 本身是事务性的。
#
#   mysql    隐式提交。DDL 一旦执行，它之前的语句立即落地，rollback 撤不回来。
#   oracle   在执行 DDL 前后各隐式提交一次，同样撤不回。
#   dm       部分版本支持事务型 DDL，但未逐版本实测 → 按"不可回滚"处理。
#            方向选择：误报只让确认弹窗多一句警告；漏报等于向用户假承诺
#            "整批回滚"，正是红线禁止的"事实错告"。
#   postgres / gaussdb   DDL 是事务性的，可随批回滚，承诺成立。
#
# 未知方言一律按 True（保守）：宁可多警告，不可假承诺。
_DDL_BREAKS_TXN = {
    "mysql": True,
    "oracle": True,
    "dm": True,
    "postgres": False,
    "gaussdb": False,
}


def ddl_breaks_transaction(dialect: str) -> bool:
    """该方言的 DDL 是否会破坏外层事务（隐式提交）。

    True = 批内一旦出现 DDL，"任一失败整批回滚"即失效，
    已执行的语句可能已提交生效，必须如实告知用户。
    """
    return _DDL_BREAKS_TXN.get((dialect or "").lower().strip(), True)


# 纯 DML 前缀：影响行数预估只对这些开放（见 is_pure_dml）
_DML_PREFIXES = ("INSERT", "UPDATE", "DELETE", "REPLACE")


def is_provably_read_only(sql: str) -> bool:
    """单条语句是否"可证明只读"——通过 validate_read_only_sql 即算。

    写通道（execute）的确认门用它做判据，而不是枚举写关键字：
    黑名单永远漏"能间接写但不以写前缀开头"的语句，实测这些全部漏过——
        SET @s='DELETE FROM t'; PREPARE p FROM @s; EXECUTE p
        EXPLAIN ANALYZE DELETE FROM orders     （PG 单语句即真实执行）
        LOAD DATA INFILE '...' INTO TABLE t / COPY t FROM '...'
        FLUSH TABLES WITH READ LOCK / ANALYZE TABLE / KILL / DO / USE
    白名单不会漏：凡不能证明只读的一律要求确认，代价只是对 SET/USE 这类
    无害语句也多弹一次窗，方向是安全的。
    """
    try:
        validate_read_only_sql(sql)
        return True
    except Exception:
        return False


def is_pure_dml(sql: str) -> bool:
    """单条语句是否是 INSERT/UPDATE/DELETE/REPLACE 之一。

    影响行数预估（estimate_affected_rows）会先真实试执行一遍再回滚，
    所以只能对纯 DML 开放：DDL 回滚不了，SET/PREPARE/EXECUTE/LOAD DATA/
    EXPLAIN ANALYZE 这类语句要么有确认无法撤销的副作用，要么根本不是
    "影响 N 行"能表达的形状。此前只排除了 DDL，预估面过宽。
    """
    sanitized_upper = _sanitize_sql_for_scan(sql).upper().lstrip("(").lstrip()
    return bool(sanitized_upper) and sanitized_upper.startswith(_DML_PREFIXES)


# ------------------------------------------------------------------------------
# 多语句拆分（v4.0）
# ------------------------------------------------------------------------------
def strip_trailing_comments(sql: str) -> str:
    """去掉 SQL 末尾的注释（行注释 --/#、块注释 /* */），引号内的同形字符不受影响。

    必须在"原文末尾追加子句"之前调用。典型事故：
        SELECT * FROM t -- 备注
    直接拼成
        SELECT * FROM t -- 备注 LIMIT 1000
    LIMIT 落在行注释里被一起注释掉，行数保护完全失效，整表照旧全量拉取。
    skill 层生成的 SQL 经常带中文行注释，命中概率不低。

    实现为一次线性扫描的状态机（与 split_sql_statements 同范式），只记录最后一个
    "非注释"字符的位置——比正则安全：正则无法区分字符串里的 '--' 和真正的注释。
    行注释连同其结尾换行一起吞掉，否则 `-- a\n-- b` 只会剥掉最后一条。
    返回值已去除末尾空白。

    与 split_sql_statements / sanitize_sql_for_scan 刻意不同，本函数**仍然认 `#`**：
    那两个的失败方向是"少拦"（放走写操作），必须保守；本函数只用于"追加 LIMIT 之前
    剥尾巴"，失败方向相反——不剥 `# 备注` 会让 LIMIT 落进行注释里，行数保护失效。
    MySQL 的 `#` 注释很常见，PG 的 `#` 是几何运算符、在分析型 SQL 里几乎不出现，
    两害相权取其轻。真正要两边都对，得把方言传进来（readengine 手里有 db_type）。
    """
    n = len(sql)
    i = 0
    in_single = in_double = in_backtick = False
    last_code_end = 0  # 最后一个非注释字符的下标 + 1
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_single:
            if ch == "\\" and nxt:
                i += 2
                continue
            if ch == "'":
                in_single = False
            last_code_end = i + 1
            i += 1
            continue
        if in_double:
            if ch == "\\" and nxt:
                i += 2
                continue
            if ch == '"':
                in_double = False
            last_code_end = i + 1
            i += 1
            continue
        if in_backtick:
            if ch == "`":
                in_backtick = False
            last_code_end = i + 1
            i += 1
            continue

        if ch == "-" and nxt == "-":
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl + 1  # 行注释连同结尾换行一起跳过
            continue
        if ch == "#":
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl + 1
            continue
        if ch == "/" and nxt == "*":
            close = sql.find("*/", i + 2)
            i = n if close == -1 else close + 2  # 块注释整体跳过
            continue

        if ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "`":
            in_backtick = True
        last_code_end = i + 1
        i += 1
    return sql[:last_code_end].rstrip()


def split_sql_statements(sql: str) -> List[str]:
    """按顶层分号拆分多语句，尊重字符串字面量与注释。

    规则：
    - 单引号/双引号/反引号内的分号不拆分
    - 行注释（--）与块注释（/* */）内的分号不拆分
    - CREATE PROCEDURE/FUNCTION/TRIGGER 等带 BEGIN...END 体的对象定义：
      检测到这类语句时整条保留（体内存量分号交给驱动按单语句执行，
      服务端协议支持存储过程体内的分号，mysql CLI 的 DELIMITER 只是客户端切分需要）

    刻意**不**认两个 MySQL 专有词法，方向是保守（只多拆、不会少拆）：
    - `#` 行注释：PostgreSQL/GaussDB 上 `#` 是运算符。认了它，
      `SELECT B'1' # B'0'; DROP TABLE t` 在 PG 上会被拆成 1 条，
      而服务器按简单查询协议收到 2 条——DROP 从只读通道零确认真实执行。
      代价只是 MySQL 用户写 `# 备注` 时分号后的内容会被当成新语句（罕见，
      且失败方向是"提示你用 execute"）。
    - 反斜杠转义（'\\'' 是转义引号）：PostgreSQL 标准串里反斜杠是普通字符。
      认了它，`SELECT '\\'; DELETE FROM t; --'` 在 PG 上会被拆成 1 条，
      服务器同样执行 2 条。代价只是 MySQL 的 `'it\\'s'` 会被多拆一次。
    两处与 util.sanitize_sql_for_scan 的默认开关保持一致（都不认）。
    """
    stripped = sql.strip()
    # 存储过程/函数/触发器/包定义：整条不拆分（体内存量分号）
    if re.match(r'^\s*CREATE\s+(OR\s+REPLACE\s+)?(DEFINER\s*=\s*\S+\s+)?(PROCEDURE|FUNCTION|TRIGGER|PACKAGE|TYPE|VIEW)\b',
                stripped, re.IGNORECASE):
        return [stripped.rstrip(';').strip()] if stripped.strip() else []

    statements: List[str] = []
    buf: List[str] = []
    i = 0
    n = len(sql)
    in_single = in_double = in_backtick = False
    in_line_comment = in_block_comment = False

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_single:
            buf.append(ch)
            if ch == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            buf.append(ch)
            if ch == '"':
                in_double = False
            i += 1
            continue
        if in_backtick:
            buf.append(ch)
            if ch == "`":
                in_backtick = False
            i += 1
            continue

        # 顶层状态机
        if ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "`":
            in_backtick = True
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements
