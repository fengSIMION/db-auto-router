# -*- coding: utf-8 -*-
"""通用解析工具：路径标准化 / SQL脱敏 / 跨库表名解析 / SQL 表名提取 / MySQL URL 解析
——原 166–238、610–641 行（散落在函数内的 import re 已收拢到模块顶部）

v4.3：`sanitize_sql_for_scan` 从 sqlguard 迁入本模块（sqlguard 改为 import），
使 util 内部的写语句目标表提取能复用同一份脱敏实现，避免两处逻辑漂移。"""

__author__ = "fengkeju"
import re
from urllib.parse import unquote


# ------------------------------------------------------------------------------
# SQL 脱敏：去字符串字面量与注释（供关键字/表名扫描，防止误报与绕过）
# ------------------------------------------------------------------------------
# 方言相关的两个词法开关，默认都是 False = 保守：
#   hash_is_comment    '#' 是否行注释。MySQL 系是；PostgreSQL/GaussDB 上是
#                      运算符（位运算/几何运算），Oracle/达梦没有 '#' 注释。
#                      保守取值下 'SELECT B'1' # B'0'; DROP TABLE t' 会在分号处
#                      被拆成两条从而拦住；按 MySQL 语义拆则只剩一条、蒙混过关。
#   backslash_escapes  反斜杠是否转义符。MySQL 是（'\'' 是转义引号）；
#                      PostgreSQL 标准串（standard_conforming_strings=on）里
#                      反斜杠就是普通字符，Oracle/达梦同样不认。
#                      保守取值下 "SELECT '\'; DELETE FROM t; --'" 会在分号处
#                      拆开从而拦住；按 MySQL 语义拆则只剩一条。
# 两个开关取 False 只可能多拦（把一条语句拆成多条、或让关键字裸露出来），
# 不可能少拦——所以"不知道方言"时的安全方向是都不认。
def sanitize_sql_for_scan(sql: str, hash_is_comment: bool = False,
                          backslash_escapes: bool = False) -> str:
    """脱敏 SQL 用于关键字扫描：字符串字面量与注释整体替换为占位符。

    实现为一次线性扫描的状态机，**顺序是"先字符串、后注释"**。原先用三条正则
    且注释在前，字面量里的 '--' / '#' 会被当成注释起点，把同行后续的真实子句
    从扫描文本里抹掉——实测 `SELECT a FROM t WHERE a='1--' FOR UPDATE` 脱敏后
    只剩 `SELECT a FROM t WHERE a='1 `，锁定子句守卫直接失效，行锁从只读通道
    真实加上；同一手法还能藏 `INTO OUTFILE` 写服务器文件。
    """
    out = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        # 行注释：-- 在所有方言都是注释；# 仅 MySQL 系（见上方开关说明）
        if ch == "-" and nxt == "-":
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl + 1
            out.append(" ")
            continue
        if ch == "#" and hash_is_comment:
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl + 1
            out.append(" ")
            continue
        # 块注释
        if ch == "/" and nxt == "*":
            close = sql.find("*/", i + 2)
            i = n if close == -1 else close + 2
            out.append(" ")
            continue

        # 字符串字面量：整段替换为占位符，内部一个字符都不进扫描文本。
        # 引号定界优先于注释判定，所以字面量里的 '--' / '#' / ';' 一律安全。
        if ch in ("'", '"', "`"):
            quote = ch
            j = i + 1
            while j < n:
                c = sql[j]
                if backslash_escapes and c == "\\" and j + 1 < n:
                    j += 2
                    continue
                if c == quote:
                    # 双写转义（'' / "" / ``）：引号本身，不是结束符
                    if j + 1 < n and sql[j + 1] == quote:
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(quote * 2)
            i = j
            continue

        out.append(ch)
        i += 1
    return "".join(out)


# ------------------------------------------------------------------------------
# 路径标准化函数
# ------------------------------------------------------------------------------
def normalize_path(path: str) -> str:
    """标准化路径格式：处理 Git Bash 格式、file:// 协议和 Windows 反斜杠"""
    normalized = path.replace('\\', '/')

    # 处理 file:// 协议（Claude Code roots 传递的格式）
    if normalized.startswith('file://'):
        normalized = normalized[7:]  # 去掉 file:// 前缀

    # 百分号解码：Claude Code roots 对含中文/空格/特殊字符的目录习惯做 URL 编码
    # （如 湖北孝感资料 → %E6%B9%96%E5%8C%97%E5%AD%9D%E6%84%9F%E8%B5%84%E6%96%99），
    # 而配置表存的是明文路径，二者必须统一为明文后才能等值匹配。
    # 仅在存在 %XX 形态时解码（显式路径中较少见字面「%」），避免误伤纯明文路径。
    if '%' in normalized and re.search(r'%[0-9A-Fa-f]{2}', normalized):
        normalized = unquote(normalized)

    # 处理 Git Bash 格式：/d/path -> D:/path 或 /D:/path -> D:/path
    if normalized.startswith('/') and len(normalized) > 2:
        parts = normalized.split('/')
        # parts[0] = "", parts[1] 可能是 "d" 或 "D:" 或其他
        if len(parts) >= 2:
            first_part = parts[1]
            # 处理 /D:/path 格式（Claude Code 的 roots 格式，如 file:///D:/path）
            if first_part.endswith(':'):
                drive_letter = first_part[:-1].upper()  # 去掉冒号，取驱动器字母
                rest_path = '/'.join(parts[2:]) if len(parts) > 2 else ''
                normalized = f"{drive_letter}:/{rest_path}"  # 注意这里加了一个 /
            # 处理 /d/path 格式（Git Bash 格式）
            elif len(first_part) == 1:
                drive_letter = first_part.upper()
                rest_path = '/'.join(parts[2:]) if len(parts) > 2 else ''
                normalized = f"{drive_letter}:/{rest_path}"  # 注意这里加了一个 /
    return normalized


# ------------------------------------------------------------------------------
# SQL 解析：提取跨库语法中的真实库名和表名
# ------------------------------------------------------------------------------

# 系统元数据 schema：作为 db.table 前缀出现时不得触发切库。
# 这类引用本身已是完全限定名，在任意默认库下都能正确执行；切库反而改写了连接的
# 默认库，使 DATABASE() 及依赖当前库的查询（如 table_schema = DATABASE()）错位。
SYSTEM_SCHEMAS = frozenset({
    "information_schema",                     # MySQL / PostgreSQL / GaussDB / DM
    "performance_schema", "mysql", "sys",     # MySQL
    "pg_catalog", "pg_toast",                 # PostgreSQL / GaussDB
})

# 标识符：反引号(MySQL) / 双引号(PostgreSQL、达梦) / 裸词
_IDENT = r'(?:`[^`]+`|"[^"]+"|\w+)'
# FROM / JOIN 后的表引用，可选 db. 前缀（点号两侧允许空白）
_TABLE_REF_RE = re.compile(
    rf'\b(?:FROM|JOIN)\s+({_IDENT})(?:\s*\.\s*({_IDENT}))?',
    re.IGNORECASE,
)


def _unquote(ident: str) -> str:
    """去掉标识符外层的反引号或双引号"""
    if len(ident) >= 2 and ident[0] == ident[-1] and ident[0] in '`"':
        return ident[1:-1]
    return ident


def _split_ref(match: tuple) -> tuple:
    """把 _TABLE_REF_RE 捕获的 (ident1, ident2) 还原成 (库名或 None, 表名)"""
    ident1, ident2 = match
    if ident2:
        return (_unquote(ident1), _unquote(ident2))
    return (None, _unquote(ident1))


_QUALIFIED_REF_RE = re.compile(
    r'^\s*(' + _IDENT + r')(?:\s*\.\s*(' + _IDENT + r'))?\s*$', re.IGNORECASE)


def _split_qualified_ref(text: str) -> tuple:
    """把 'db1.t' / 't' / '`db1`.`t`' 这类限定引用拆成 (库名或 None, 表名)"""
    m = _QUALIFIED_REF_RE.match(text)
    if not m:
        return (None, _unquote(text.strip()))
    return _split_ref((m.group(1), m.group(2)))


# ------------------------------------------------------------------------------
# 写语句目标表（决定"写入哪个库"，必须与 FROM/JOIN 一起参与切库判定）
# ------------------------------------------------------------------------------
# 只在语句起始处锚定匹配：`SELECT update FROM t` 这类把 update 当列名的读语句
# 不会被误判成写语句。已脱敏，字符串字面量/注释里的同形关键字不参与匹配。
_WRITE_TARGET_HEADS = (
    r'INSERT\s+(?:IGNORE\s+|LOW_PRIORITY\s+|DELAYED\s+|HIGH_PRIORITY\s+)*INTO\s+',
    r'REPLACE\s+(?:LOW_PRIORITY\s+|DELAYED\s+)*INTO\s+',
    r'UPDATE\s+(?:LOW_PRIORITY\s+|IGNORE\s+)*',
    r'DELETE\s+(?:LOW_PRIORITY\s+|QUICK\s+|IGNORE\s+)*FROM\s+',
    r'MERGE\s+INTO\s+',
    r'TRUNCATE\s+(?:TABLE\s+)?',
    r'(?:CREATE|DROP|ALTER)\s+(?:OR\s+REPLACE\s+)?'
    r'(?:TEMPORARY\s+|TEMP\s+|UNLOGGED\s+)?TABLE\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?',
    r'CREATE\s+(?:OR\s+REPLACE\s+)?(?:UNIQUE\s+|FULLTEXT\s+|SPATIAL\s+|BITMAP\s+)?'
    r'INDEX\s+(?:CONCURRENTLY\s+)?' + _IDENT + r'\s+ON\s+(?:ONLY\s+)?',
)
_WRITE_TARGET_HEAD_RE = re.compile(
    r'^\s*\(?\s*(?:' + '|'.join(_WRITE_TARGET_HEADS) + r')'
    r'(' + _IDENT + r'(?:\s*\.\s*' + _IDENT + r')?)',
    re.IGNORECASE,
)


def _write_target_refs(sql: str) -> list:
    """提取写语句的目标表引用 [(库名或 None, 表名)]；非写语句返回 []。

    UPDATE t1, t2 SET ...（MySQL 多表更新）会把全部目标表纳入——
    任一目标表未限定库都不能切库，否则未限定那张会解析到错误的库。
    """
    sanitized = sanitize_sql_for_scan(sql)
    m = _WRITE_TARGET_HEAD_RE.match(sanitized)
    if not m:
        return []
    refs = [_split_qualified_ref(m.group(1))]

    head = m.group(0).lstrip('( \t\r\n').upper()
    if head.startswith('UPDATE'):
        tail = sanitized[m.end():]
        stop = re.search(r'\bSET\b|\bWHERE\b|\bFROM\b|\bVALUES\b', tail, re.IGNORECASE)
        region = tail[:stop.start()] if stop else tail
        for extra in re.findall(r',\s*(' + _IDENT + r'(?:\s*\.\s*' + _IDENT + r')?)', region):
            refs.append(_split_qualified_ref(extra))
    return refs


def parse_cross_db_table(sql_or_table: str, default_db: str) -> tuple:
    """
    解析跨库语法，返回 (真实库名, 真实表名)

    支持格式：
    - table_name -> (default_db, table_name)
    - db_code.table_name -> (db_code, table_name)
    - database.table_name -> (database, table_name)
    """
    if '.' in sql_or_table:
        parts = sql_or_table.split('.', 1)
        return (parts[0], parts[1])
    return (default_db, sql_or_table)


def split_table_ref(table_name: str) -> tuple:
    """拆出 (schema 前缀 或 None, 表名)——表结构内省专用（v4.4）。

    与 parse_cross_db_table 的关键差别：**不填默认库**。内省必须区分
    「调用方显式写了 schema. 前缀」和「没写，该用配置/方言默认」——前者必须尊重，
    后者才有默认可言。parse_cross_db_table 把两者糊成同一个值，于是 PG 上
    `nspname = <db_code>`、Oracle 上 `OWNER = <db_code>`，而 db_code 只是业务标签，
    过滤条件永远不命中，表结构查询静默返回空。

    返回值里的 schema 前缀保持原样（含引号由调用方处理）；表名已去空白。
    """
    text = (table_name or "").strip()
    if "." in text:
        head, _, rest = text.partition(".")
        head, rest = head.strip(), rest.strip()
        if head and rest:
            return head, rest
    return None, text


def extract_db_and_table_from_sql(sql: str, default_db: str) -> tuple:
    """
    从 SQL 语句中提取数据库名和表名
    返回 (执行时使用的库, 表名, 是否需要跨库)

    切库判定分两步，写语句的目标表优先于 FROM/JOIN 源表：

    第一步——写语句目标表（决定"写到哪个库"）：
    - 目标表限定到某个非系统库，且 FROM/JOIN 引用要么同库要么不存在 → 切到目标库
    - 目标表未限定库 → 其解析依赖当前库，绝不切库
    - 目标库已明确但源引用跨库/未限定 → 保留 default_db（完全限定名在任意默认库下都能执行）

    第二步——读语句 / 无写目标时，只看 FROM/JOIN：
    仅当 FROM/JOIN 的全部表引用都限定到"同一个非系统库"时才切。
    其余情况一律留在 default_db —— 完全限定名在任意默认库下都能执行，而切库会让
    未限定的表名和 DATABASE() 解析到错误的库。

    示例：
    - SELECT * FROM users                     -> (default_db, users, False)
    - SELECT * FROM ydyc_ydjc.users           -> (ydyc_ydjc, users, True)
    - SELECT * FROM `ydyc`.`users`            -> (ydyc, users, True)  反引号
    - SELECT * FROM information_schema.tables -> (default_db, tables, False)  系统库不切
    - SELECT * FROM db1.t1 JOIN db2.t2        -> (default_db, t1, False)  多库不切
    - SELECT * FROM db1.t1 JOIN t2            -> (default_db, t1, False)  混用不切

    v4.3 修复（此前只扫 FROM/JOIN，写语句目标表不参与判定）：
    - UPDATE otherdb.t SET x=1                -> (otherdb, t, True)   原先落到 default_db，写错库
    - DROP TABLE otherdb.t                    -> (otherdb, t, True)   原先落到 default_db，删错库
    - INSERT INTO t SELECT * FROM db1.x       -> (default_db, t, False) 原先被源表带偏切到 db1，插错库
    """
    matches = _TABLE_REF_RE.findall(sql)
    _, first_from_table = _split_ref(matches[0]) if matches else (None, None)

    qualified_dbs = set()      # 出现过的非系统库前缀（FROM/JOIN 侧）
    has_unqualified = False    # 是否存在裸表名（其解析依赖默认库，故不能切库）
    for m in matches:
        db_name, _ = _split_ref(m)
        if db_name is None:
            has_unqualified = True
        elif db_name.lower() not in SYSTEM_SCHEMAS:
            qualified_dbs.add(db_name)

    # ---- 写语句：目标表优先 ----
    write_refs = _write_target_refs(sql)
    if write_refs:
        write_qualified = {db for db, _ in write_refs
                           if db and db.lower() not in SYSTEM_SCHEMAS}
        write_unqualified = any(db is None for db, _ in write_refs)
        first_table = write_refs[0][1] or first_from_table

        if write_unqualified:
            # 目标表未限定库，解析依赖当前库 → 绝不切库
            return (default_db, first_table, False)
        if len(write_qualified) == 1 and not has_unqualified and qualified_dbs <= write_qualified:
            return (next(iter(write_qualified)), first_table, True)
        # 目标库已明确但源引用跨库/未限定：保留 default_db。
        # 此时目标表名已是完全限定名，在任意默认库下都能正确执行。
        return (default_db, first_table, False)

    # ---- 读语句：仅凭 FROM/JOIN 判定 ----
    if not matches:
        return (default_db, None, False)

    is_cross_db = len(qualified_dbs) == 1 and not has_unqualified
    target_db = qualified_dbs.pop() if is_cross_db else default_db
    return (target_db, first_from_table, is_cross_db)


def sql_placeholders(typ: str, count: int) -> str:
    """按方言生成 count 个占位符，逗号分隔。

    驱动只支持两种参数风格，必须分开处理——统一用 %s 会让 Oracle/达梦的
    导入直接失败（它们不认 %s）：
    - mysql / postgres / gaussdb ：psycopg2 与 mysql-connector 均为 %s
    - dm / oracle                 ：数字定位占位符 :1 :2 …
      （与 introspection.py 中 dm/oracle 分支的写法保持一致）
    """
    if count <= 0:
        raise ValueError(f"占位符数量必须为正数: {count}")
    if (typ or "").lower().strip() in ("dm", "oracle"):
        return ", ".join(f":{i}" for i in range(1, count + 1))
    return ", ".join(["%s"] * count)


def parse_mysql_url(url: str) -> dict:
    """解析 MySQL 连接URL，支持多种格式"""
    result = {"host": "127.0.0.1", "port": 3306, "database": ""}

    if not url:
        return result

    if "/" in url:
        parts = url.split("/", 1)
        host_port = parts[0]
        result["database"] = parts[1] if len(parts) > 1 else ""

        if ":" in host_port:
            host, port = host_port.split(":", 1)
            result["host"] = host
            result["port"] = int(port)
        else:
            result["host"] = host_port if host_port else "127.0.0.1"
    else:
        if ":" in url:
            parts = url.split(":")
            if len(parts) == 2 and parts[1].isdigit():
                result["host"] = parts[0]
                result["port"] = int(parts[1])
            else:
                result["database"] = url
        elif "." in url and not url.startswith("/"):
            result["host"] = url
        else:
            result["database"] = url

    return result
