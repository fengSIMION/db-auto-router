# -*- coding: utf-8 -*-
"""数据库导出工具：调用原生 CLI (mysqldump/pg_dump 等) 将指定数据源导出为 .sql 文件。
Phase 1 仅实现 MySQL；其它方言抛 UnsupportedDialectError 占位。"""

__author__ = "fengkeju"
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, Any, List, Optional

from .datasources import load_datasources
from .util import parse_mysql_url
from .logcfg import logger


class DumpError(Exception):
    """导出失败的基类"""
    pass


class UnsupportedDialectError(DumpError):
    """当前方言尚未实现 dump 能力"""
    pass


# Phase 1 支持的方言白名单。抽成模块级函数是为了让工具层能在**弹确认框之前**
# 就调用它——原先方言检查长在 dump_database 内部，用户已经批准了
# "mysqldump xxx → D:/out/x.sql"、输出目录也已 mkdir 出来，才被告知
# "仅支持 MySQL"。让用户批准一个必然跑不了的操作属于确认疲劳，
# 几次之后用户就会养成"看到确认框就点接受"的习惯，真正的危险操作反而被放过。
SUPPORTED_DUMP_DIALECTS = ("mysql",)


def ensure_supported_dialect(dialect: str) -> None:
    """方言不在支持范围即抛 UnsupportedDialectError（幂等，可重复调用）。"""
    if dialect not in SUPPORTED_DUMP_DIALECTS:
        raise UnsupportedDialectError(
            f"dump_database Phase 1 仅支持 MySQL 系数据源，当前为 {dialect!r}。"
            f"PostgreSQL/Dameng/Oracle 将在后续版本加入。"
        )


class CliMissingError(DumpError):
    """本机找不到对应的 CLI 工具"""
    pass


class CredentialLookupError(DumpError):
    """无法从配置库中找到该 db_code 的连接信息"""
    pass


def _dump_env(pwd: str) -> dict:
    """mysqldump 的子进程环境：密码走 MYSQL_PWD 环境变量，不进命令行参数。

    `-pPASSWD` 会明文出现在进程命令行里（Windows 上同用户/管理员可用
    `wmic process get commandline` 或任务管理器读到），环境变量的暴露面小得多。
    密码为空时不设置该变量，保持"无密码连接"的原行为。
    """
    env = dict(os.environ)
    if pwd:
        env["MYSQL_PWD"] = pwd
    return env


def _build_mysqldump_argv(*, ds: Dict[str, str],
                           include_data: bool, exclude_patterns: Optional[List[str]],
                           routines: bool, charset: str, cli_path: Optional[str],
                           tables: Optional[List[str]] = None,
                           no_create_info: bool = False) -> List[str]:
    """构造 mysqldump argv（不含 shell=True，杜绝注入；不含密码）。

    密码一律通过 _dump_env() 以 MYSQL_PWD 环境变量传递，不出现在 argv 里。

    参数：
    - include_data: False 时加 --no-data（只导结构）
    - exclude_patterns: 展开为 --ignore-table=DB.xxx（整表排除，含结构）
    - tables: 只导这些表；None 表示导全库
    - no_create_info: True 时只导数据不导结构（种子表数据段用）

    返回格式：[cli_abs_path, -h, HOST, -P, PORT, -u, USER, ...flags..., DB, [tables...]]
    """
    cli = cli_path or shutil.which("mysqldump")
    if not cli:
        # 注意：cli_path 只是本模块内部函数的逃生舱，MCP 工具 dump_database 并未暴露该参数，
        # 所以这里不能提示"传 cli_path"——调用方照做会直接报未知参数。
        # 给一条 MCP 调用方真正能执行的补救动作。
        raise CliMissingError(
            "本机 PATH 中找不到 mysqldump。请安装 MySQL Client（或 MySQL Server，通常自带）"
            "并确认 mysqldump.exe 在 PATH 中，然后重启 MCP 服务再试。"
        )

    parsed = parse_mysql_url(ds["conn_url"])
    host = parsed["host"]
    port = parsed["port"]
    db_name = ds["db_code"]  # conn_url 可能不带 db，用 db_code 兜底
    user = ds["db_user"]

    argv = [cli, f"-h{host}", f"-P{port}", f"-u{user}"]
    argv += [
        f"--default-character-set={charset}",
        "--single-transaction",
        "--quick",
        "--set-gtid-purged=OFF",
    ]

    if routines:
        argv += ["--routines", "--events", "--triggers"]

    if not include_data:
        argv.append("--no-data")

    if no_create_info:
        argv.append("--no-create-info")

    if exclude_patterns:
        for pat in exclude_patterns:
            argv.append(f"--ignore-table={db_name}.{pat}")

    argv.append(db_name)
    if tables:
        argv += list(tables)

    return argv


def dump_database(*, db_code: str, file_path: Path,
                   include_data: bool = True,
                   seed_only_tables: Optional[List[str]] = None,
                   exclude_patterns: Optional[List[str]] = None,
                   routines: bool = False,
                   charset: str = "utf8mb4",
                   cli_path: Optional[str] = None) -> Dict[str, Any]:
    """
    将指定数据源的完整结构与（可选）数据导出为一个 .sql 文件。

    Args:
        db_code: 已在 mcp_datasource_config 注册的 db_code
        file_path: 输出的 .sql 文件绝对路径
        include_data: 非种子表是否带数据。True=全库数据；
            False=仅 seed_only_tables 里的表带数据，其余表纯 DDL
        seed_only_tables: 这些表必须带 INSERT（不论 include_data 取值）。
            include_data=True 时它们本就包含在全库数据里，不会重复追加
        exclude_patterns: 要排除的表名列表（展开为 --ignore-table=DB.xxx，整表排除）
        routines: 是否一并导出存储过程/函数/触发器/事件
        charset: 字符集，默认 utf8mb4
        cli_path: 显式指定 mysqldump 绝对路径（可选，缺省走 PATH）

    Returns:
        {file_path, size_bytes, table_count, duration_sec, used_cli, note?}

    Raises:
        UnsupportedDialectError / CliMissingError / CredentialLookupError / DumpError
    """
    t0 = time.time()
    file_path = Path(file_path).expanduser().resolve()
    file_path.parent.mkdir(parents=True, exist_ok=True)

    # ---- 1. 查凭据 --------------------------------------------------------
    all_ds = load_datasources(force_refresh=False)
    if db_code not in all_ds:
        raise CredentialLookupError(f"未在配置库中找到 db_code='{db_code}'")
    ds = all_ds[db_code]
    dialect = ds.get("db_type", "").lower().strip()

    ensure_supported_dialect(dialect)

    # ---- 2. 拼 argv & 执行 ------------------------------------------------
    # 语义（与工具 docstring 一致）：
    #   include_data     : 非种子表是否带数据
    #                      True  → 全库数据；False → 仅种子表有数据，其余纯 DDL
    #   seed_only_tables : 这些表必须带 INSERT（不论 include_data 取值）
    #
    # 实现：第一段导「所有表的结构」+（include_data 时）全部数据；
    #       第二段只对种子表 --no-create-info 追补 INSERT。
    #
    # 关键：种子表绝不能进第一段的 --ignore-table。--ignore-table 是整表排除，
    # 会把建表语句一起排掉；而第二段用 --no-create-info 只产 INSERT，
    # 两头一凑种子表就只剩 INSERT 没有 CREATE TABLE，导入新库必然失败。
    # （这正是本函数此前的实际 bug，已用真 mysqldump 复现。）
    dump_env = _dump_env(ds.get("db_password") or "")

    base_argv = _build_mysqldump_argv(
        ds=ds, include_data=include_data,
        exclude_patterns=exclude_patterns or None,
        routines=routines, charset=charset, cli_path=cli_path,
    )

    logger.info(f"[dump] 开始导出 {db_code} → {file_path}")
    logger.debug(f"[dump] argv: {base_argv}")  # 密码已移至 MYSQL_PWD 环境变量，argv 不含敏感信息

    try:
        with open(file_path, "wb") as fh:
            proc = subprocess.run(base_argv, stdout=fh, stderr=subprocess.PIPE,
                                  text=False, timeout=600, env=dump_env)
        if proc.returncode != 0:
            err_msg = proc.stderr.decode("utf-8", errors="replace")
            # 保险起见：万一 stderr 里泄露了密码片段，做一次最小化脱敏
            err_msg = err_msg.replace(ds["db_password"], "***") if ds.get("db_password") else err_msg
            raise DumpError(f"mysqldump 退出码 {proc.returncode}\nstderr:\n{err_msg}")
    except FileNotFoundError as e:
        raise CliMissingError(f"找不到 mysqldump: {e}")
    except subprocess.TimeoutExpired:
        raise DumpError("mysqldump 超过 600 秒仍未结束，已强制终止")

    # ---- 3. 种子表数据段 ------------------------------------------------
    # 仅 include_data=False 时需要：True 时第一段已含全部数据，
    # 再补一遍会产生重复 INSERT。此时 seed_only_tables 属冗余参数，
    # 在返回摘要里说明，避免调用方误以为做了额外的事。
    seed_note = None
    if seed_only_tables and include_data:
        seed_note = (f"include_data=True 时全库数据已包含种子表 {list(seed_only_tables)}，"
                     f"未重复追加；如只想让种子表带数据，请设 include_data=False")
    elif seed_only_tables:
        seed_argv = _build_mysqldump_argv(
            ds=ds, include_data=True,
            exclude_patterns=None, routines=False,
            charset=charset, cli_path=cli_path,
            tables=list(seed_only_tables), no_create_info=True,
        )

        logger.info(f"[dump] 追加种子表数据: {seed_only_tables}")
        with open(file_path, "ab") as fh:  # append binary
            fh.write(b"\n\n-- ============================================================\n")
            fh.write(b"-- SECTION: Seed Data (standard code tables)\n")
            fh.write(b"-- ============================================================\n\n")
            proc2 = subprocess.run(seed_argv, stdout=fh, stderr=subprocess.PIPE,
                                   text=False, timeout=300, env=dump_env)
        if proc2.returncode != 0:
            err2 = proc2.stderr.decode("utf-8", errors="replace")
            if ds.get("db_password"):
                err2 = err2.replace(ds["db_password"], "***")
            raise DumpError(f"种子表 mysqldump 失败，退出码 {proc2.returncode}\nstderr:\n{err2}")

    # ---- 4. 统计 & 返回 ---------------------------------------------------
    elapsed = round(time.time() - t0, 2)
    size = file_path.stat().st_size

    # 粗略估计表数量：扫一遍 '^CREATE TABLE'
    table_count = 0
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as fr:
            for line in fr:
                if line.lstrip().startswith("CREATE TABLE"):
                    table_count += 1
    except Exception as e:
        logger.warning(f"[dump] 统计 CREATE TABLE 行数失败: {e}")

    summary = {
        "file_path": str(file_path),
        "size_bytes": size,
        "table_count": table_count,
        "duration_sec": elapsed,
        "used_cli": base_argv[0],
    }
    if seed_note:
        summary["note"] = seed_note
    logger.info(f"[dump] 完成: {summary}")
    return summary
