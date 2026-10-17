# -*- coding: utf-8 -*-
"""写操作确认：elicitation schema + 原生 tkinter 中文弹窗 + 确认编排
——原 2292–2617 行。tkinter 保持函数内懒加载，_native_confirm 仍由
asyncio.to_thread 在独立线程运行（行为与原版一致）。"""

__author__ = "fengkeju"
import os
import sys
import asyncio
from typing import Optional

from pydantic import BaseModel, Field
from mcp.server.fastmcp import Context

from .config import WRITE_CONFIRM_MODE, DIALOG_SQL_PREVIEW_LINES
from .errors import ToolFailure
from .logcfg import logger
from .sqlguard import is_write_operation, is_ddl_operation


class WriteConfirmation(BaseModel):
    """execute 写操作确认表单（elicitation schema，仅基础类型）——终端兜底确认用

    confirm 默认 True：确认框本身的 Accept 即代表"同意执行"，无需再交互勾选
    （Claude Code 表单对 required 字段未填会拒绝提交，默认勾选保证一键 Accept）。
    用户仍可通过 Decline 拦截，或主动取消勾选后 Accept 表示不执行。
    """
    confirm: bool = Field(
        default=True,
        description=(
            "确认执行这条写操作/DDL？⚠️ 不可自动撤销，执行前请确认 SQL 和影响范围。"
            "默认已勾选：点下方 Accept 即执行；拦截请直接点 Decline"
        )
    )


# ------------------------------------------------------------------------------
# 原生中文确认弹窗 (tkinter)：首选确认通道
# MCP 终端表单按钮固定为英文 Accept/Decline 且交互别扭，
# 服务端直接弹原生窗口：「接受执行 / 拒绝」中文按钮，一键决策。
# 弹窗失败（无显示环境等）自动降级回终端确认。
# ------------------------------------------------------------------------------
_NATIVE_UI_AVAILABLE: Optional[bool] = None  # None=未检测

# 原生弹窗串行化锁。tkinter 非线程安全，并发确认会同时建多个 Tcl 解释体。
# 模块级单例：同一进程内所有确认请求共用一把锁。
_confirm_lock = asyncio.Lock()


def _check_native_ui() -> bool:
    """检测 tkinter 是否可用（只检测一次）"""
    global _NATIVE_UI_AVAILABLE
    if _NATIVE_UI_AVAILABLE is None:
        try:
            import tkinter  # noqa: F401
            _NATIVE_UI_AVAILABLE = True
        except Exception:
            _NATIVE_UI_AVAILABLE = False
    return _NATIVE_UI_AVAILABLE


def _pick_cjk_font() -> str:
    """挑选系统中可用的中文字体"""
    from tkinter import font as tkfont
    try:
        fams = set(tkfont.families())
    except Exception:
        fams = set()
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑", "SimHei", "黑体"):
        if name in fams:
            return name
    return "TkDefaultFont"


def _format_estimate(estimate: Optional[dict]) -> tuple:
    """把预估结果格式化成 (文案, 级别)。级别决定配色：
    ok=0 行（绿，不会改数据）/ warn=有行数（橙）/ none=拿不到行数（灰）。

    none 一级必须和"没有预估"区分开——调用方传了 estimate 但 rows 是 None，
    说明试执行没跑通，此时说"无法预估"而不是沉默，避免用户把没数字当成没影响。
    """
    if not estimate:
        return "", "none"
    rows = estimate.get("rows")
    note = (estimate.get("note") or "").strip()
    if isinstance(rows, int):
        if rows == 0:
            return "预计影响 0 行（不会修改任何数据）", "ok"
        return f"预计影响 {rows} 行" + (f" · {note}" if note else ""), "warn"
    return "影响行数无法预估" + (f" · {note}" if note else ""), "none"


def _native_confirm(sql_preview: str, db_code: str, dialect: str, is_ddl: bool,
                    est_text: str = "", est_level: str = "none",
                    atomicity_warning: Optional[str] = None) -> Optional[bool]:
    """弹出原生中文确认窗口（深色主题，警示图标 + 数据源徽章 + SQL 预览区）。
    返回 True=接受执行 / False=拒绝 / None=弹窗失败(调用方降级终端确认)。
    本函数在独立线程中运行(asyncio.to_thread)，tkinter 主循环跑在该线程内。"""
    try:
        import tkinter as tk
    except Exception as e:
        logger.warning(f"tkinter 不可用，无法弹原生确认窗: {e}")
        return None

    answer = [None]

    # 完整语句先记录到 stderr 日志（弹窗只展示前几行，排查时以日志为准）
    sql_lines = sql_preview.splitlines()
    total_lines = len(sql_lines)
    truncated = total_lines > DIALOG_SQL_PREVIEW_LINES
    display_sql = "\n".join(sql_lines[:DIALOG_SQL_PREVIEW_LINES]) if truncated else sql_preview
    omitted_lines = total_lines - DIALOG_SQL_PREVIEW_LINES
    logger.info(
        f"写操作确认弹窗: 数据源={db_code} 类型={dialect} DDL={is_ddl} "
        f"SQL共{total_lines}行/{len(sql_preview)}字符\n{sql_preview}"
    )

    try:
        root = tk.Tk()
    except Exception as e:
        logger.warning(f"原生确认窗创建失败: {e}")
        return None

    try:
        family = _pick_cjk_font()
        mono = "Consolas" if sys.platform == "win32" else "Courier"
        F_TITLE = (family, 14, "bold")
        F_SUB   = (family, 9)
        F_CHIP  = (family, 9)
        F_BODY  = (family, 10)
        F_SQL   = (mono, 10)
        F_BTN   = (family, 10, "bold")
        F_HINT  = (family, 9)

        # ---- 配色（深色主题，警示橙 / DDL 红）----
        C_BG     = "#1e2330"   # 主背景
        C_CARD   = "#2b3348"   # 徽章底
        C_SQL_BG = "#151923"   # 代码区
        C_BORDER = "#39415a"   # 边框/分割线
        C_FG     = "#eef1f8"   # 主文字
        C_DIM    = "#8f97ab"   # 次要文字
        C_ACCENT = "#f5a623"   # 警示橙
        C_DANGER = "#ef5350"   # DDL 红
        C_OK     = "#2ea043"   # 主按钮绿
        C_OK_H   = "#3fbf5e"
        C_NO     = "#3d4559"   # 次按钮灰
        C_NO_H   = "#4e5871"

        root.title("数据库写操作确认 - db-auto-router")
        root.configure(bg=C_BG)
        root.resizable(False, False)
        root.attributes("-topmost", True)

        # ---- 顶部主题色横条（DDL 红色 / DML 橙色）----
        tk.Frame(root, bg=C_DANGER if is_ddl else C_ACCENT, height=4).pack(fill="x")

        # ---- 头部：警示图标 + 标题 ----
        header = tk.Frame(root, bg=C_BG)
        header.pack(fill="x", padx=20, pady=(16, 12))

        icon_color = C_DANGER if is_ddl else C_ACCENT
        cv = tk.Canvas(header, width=44, height=44, bg=C_BG, highlightthickness=0)
        cv.pack(side="left")
        cv.create_oval(2, 2, 42, 42, fill=icon_color, outline=icon_color)
        cv.create_text(22, 21, text="!", font=(family, 20, "bold"), fill="#ffffff")

        title_box = tk.Frame(header, bg=C_BG)
        title_box.pack(side="left", padx=(14, 0))
        tk.Label(title_box, text="数据库写操作确认", font=F_TITLE, bg=C_BG,
                 fg=C_FG, anchor="w").pack(anchor="w")
        tk.Label(title_box, text="MCP 服务端拦截 · 执行前需要您的明确授权",
                 font=F_SUB, bg=C_BG, fg=C_DIM, anchor="w").pack(anchor="w", pady=(2, 0))

        # ---- 徽章行：数据源 / 数据库类型 / 操作类型 ----
        chip_row = tk.Frame(root, bg=C_BG)
        chip_row.pack(fill="x", padx=20, pady=(0, 12))

        def chip(text, bg=C_CARD, fg=C_FG):
            lb = tk.Label(chip_row, text=f"  {text}  ", font=F_CHIP, bg=bg, fg=fg)
            lb.pack(side="left", padx=(0, 8), ipady=2)

        chip(f"数据源  {db_code}")
        chip(f"数据库  {dialect}")
        if is_ddl:
            chip("⚠ DDL · 修改表结构", bg="#4a2a31", fg="#ff8a80")
        else:
            chip("DML · 修改数据", bg="#233a4a", fg="#8ecdf7")

        # ---- SQL 预览区 ----
        sql_head = tk.Frame(root, bg=C_BG)
        sql_head.pack(fill="x", padx=20)
        tk.Label(sql_head, text="SQL 语句预览", font=F_SUB, bg=C_BG,
                 fg=C_DIM).pack(side="left")
        stat_text = (f"共 {total_lines} 行 · {len(sql_preview)} 字符"
                     if truncated else f"{total_lines} 行 · {len(sql_preview)} 字符")
        tk.Label(sql_head, text=stat_text, font=F_SUB, bg=C_BG,
                 fg=C_DIM).pack(side="right")

        box_frame = tk.Frame(root, bg=C_BORDER, padx=1, pady=1)  # 细边框
        box_frame.pack(fill="x", padx=20, pady=(4, 12))
        txt = tk.Text(box_frame, height=12, wrap="word", font=F_SQL,
                      bg=C_SQL_BG, fg="#d6dcea", insertbackground=C_FG,
                      relief="flat", bd=0, highlightthickness=0,
                      selectbackground="#3e5a8f", selectforeground="#ffffff",
                      padx=12, pady=10, spacing1=2, spacing3=2)
        txt.insert("1.0", display_sql)
        txt.configure(state="disabled")
        txt.pack(fill="x")

        # 折叠提示：仅当语句超过预览行数时显示
        if truncated:
            tk.Label(root,
                     text=(f"⋯ 已省略后续 {omitted_lines} 行（完整语句见服务端日志）。"
                           f"点击「接受执行」即按完整语句执行"),
                     font=F_SUB, bg=C_BG, fg=C_ACCENT, anchor="w").pack(fill="x", padx=20)

        # ---- 影响行数预估（v4.4）：SQL 预览与警告语之间，决策前最后一眼 ----
        if est_text:
            est_color = {"ok": C_OK, "warn": C_ACCENT}.get(est_level, C_DIM)
            tk.Label(root, text=est_text, font=F_BODY, bg=C_BG, fg=est_color,
                     anchor="w", wraplength=620, justify="left")\
                .pack(fill="x", padx=20, pady=(0, 10))

        # ---- 批量原子性预警（v4.8）：DDL 混批时"整批回滚"不成立，
        #      必须在用户点"接受执行"之前看到，不能等失败后再解释 ----
        if atomicity_warning:
            tk.Label(root, text=atomicity_warning, font=F_BODY, bg=C_BG,
                     fg=C_DANGER, anchor="w", wraplength=620, justify="left")\
                .pack(fill="x", padx=20, pady=(0, 10))

        # ---- 警告语 ----
        warn = ("‼ 该语句将修改表结构，影响不可逆，请务必仔细核对！"
                if is_ddl else
                "⚠ 写操作无法自动撤销，请核对 SQL 与影响范围。")
        tk.Label(root, text=warn, font=F_BODY, bg=C_BG,
                 fg=C_DANGER if is_ddl else C_ACCENT, anchor="w",
                 wraplength=620, justify="left").pack(fill="x", padx=20)

        # ---- 分割线 ----
        tk.Frame(root, bg=C_BORDER, height=1).pack(fill="x", padx=20, pady=(14, 16))

        # ---- 底栏：左侧快捷键提示 + 右侧按钮 ----
        bottom = tk.Frame(root, bg=C_BG)
        bottom.pack(fill="x", padx=20, pady=(0, 18))

        tk.Label(bottom, text="Enter 接受 · Esc 拒绝 · 关窗视为拒绝",
                 font=F_HINT, bg=C_BG, fg=C_DIM).pack(side="left")

        closed = [False]  # 防重入：回车可能同时命中按钮与窗口绑定

        def on_accept():
            if closed[0]:
                return
            closed[0] = True
            answer[0] = True
            root.destroy()

        def on_decline():
            if closed[0]:
                return
            closed[0] = True
            answer[0] = False
            root.destroy()

        def make_button(text, bg, bg_hover, cmd):
            b = tk.Button(bottom, text=text, font=F_BTN, width=13, cursor="hand2",
                          bg=bg, fg="#ffffff", activebackground=bg_hover,
                          activeforeground="#ffffff", relief="flat", bd=0,
                          highlightthickness=0, command=cmd)
            b.bind("<Enter>", lambda e, b=b, c=bg_hover: b.configure(bg=c))
            b.bind("<Leave>", lambda e, b=b, c=bg: b.configure(bg=c))
            b.pack(side="right", padx=(10, 0), ipady=5)
            return b

        btn_accept = make_button("✓  接受执行", C_OK, C_OK_H, on_accept)
        make_button("✗  拒绝", C_NO, C_NO_H, on_decline)

        # 快捷键：回车=接受，Esc=拒绝；关闭窗口=拒绝（安全默认）
        root.bind("<Return>", lambda e: on_accept())
        root.bind("<Escape>", lambda e: on_decline())
        root.protocol("WM_DELETE_WINDOW", on_decline)

        # 居中显示
        root.update_idletasks()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = root.winfo_reqwidth(), root.winfo_reqheight()
        root.geometry(f"+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")

        root.lift()
        root.focus_force()
        btn_accept.focus_set()

        root.mainloop()
    except Exception as e:
        logger.warning(f"原生确认窗运行异常: {e}")
        try:
            root.destroy()
        except Exception:
            pass
        return None

    return answer[0]


async def _confirm_write_if_needed(ctx: Context, sql: str, db_code: str, dialect: str,
                                   force_confirm: bool = False,
                                   estimate: Optional[dict] = None,
                                   write_confirm: bool = True,
                                   atomicity_warning: Optional[str] = None) -> None:
    """写操作服务端确认（物理拦截，无法绕过）。
    通道优先级（可用 MCP_WRITE_CONFIRM_UI 覆盖：auto=默认 / native=仅弹窗 / elicit=仅终端）：
      1. 原生中文弹窗（tkinter）：「接受执行 / 拒绝」一键决策
      2. MCP elicitation 终端确认（弹窗不可用时兜底）
    安全规则：
      - 读操作 / 确认关闭 → 直接放行
      - 用户拒绝/取消/关窗 → 不执行
      - 确认通道全部失败 → 不静默放行，返回警告让用户显式决策
    force_confirm=True（多语句批量含写操作/导入等场景）：跳过首语句前缀判断强制确认。
    estimate：调用方预先试执行得到的影响行数 {"rows": int|None, "note": str}，
    显示在弹窗/终端确认里；None 表示没做预估（如 DDL、事务内执行、功能被关闭）。
    write_confirm=False（v4.6 数据源级开关，mcp_datasource_config.write_confirm=0）：
    该数据源被标记为「开发库，写操作免人工审核」时直接放行——只放宽单个数据源，
    不动全局 MCP_WRITE_CONFIRM；审计日志照旧记录每一次写操作。
    atomicity_warning（v4.8）：批内混入 DDL 且方言 DDL 隐式提交时，由调用方
    预先算好的"这批无法整批回滚"警示语，弹窗与终端确认两个通道都会显示。
    """
    if WRITE_CONFIRM_MODE != "on":
        logger.info(f"写操作确认已关闭(MCP_WRITE_CONFIRM={WRITE_CONFIRM_MODE})，直接执行")
        return

    if not write_confirm:
        logger.info(f"数据源 [{db_code}] 已标记免人工审核（write_confirm=0），写操作直接执行")
        return

    if ctx is None:
        # 防御性兜底：写操作必须经过确认，没有会话上下文时不静默放行
        raise ToolFailure("写操作缺少确认上下文（ctx），拒绝执行")

    if not force_confirm and not is_write_operation(sql):
        return

    sql_preview = sql.strip()
    if len(sql_preview) > 1000:
        sql_preview = sql_preview[:1000] + " ...（已截断）"
    ddl_note = "（DDL 操作，影响表结构！）" if is_ddl_operation(sql) else ""
    est_text, est_level = _format_estimate(estimate)
    ui_mode = os.environ.get("MCP_WRITE_CONFIRM_UI", "auto").strip().lower()

    decline_msg = (
        f"用户在确认弹窗中拒绝了该写操作，SQL 未执行。\n"
        f"待执行语句: {sql_preview}\n"
        "如需执行请重新发起并确认。"
    )

    # --- 通道1：原生中文弹窗 ---
    if ui_mode in ("auto", "native") and _check_native_ui():
        # 串行化弹窗：_native_confirm 在 asyncio.to_thread 的工作线程里各自 tk.Tk()，
        # 而 tkinter 明确不是线程安全的——两个写操作并发确认时会同时存在两个 Tcl
        # 解释体在两个线程，可能直接崩。加锁让确认一个接一个来，UX 上也更合理
        # （一次只处理一个决策，而不是两个弹窗叠在一起）。
        async with _confirm_lock:
            accepted = await asyncio.to_thread(
                _native_confirm, sql_preview, db_code, dialect, is_ddl_operation(sql),
                est_text, est_level, atomicity_warning
            )
        if accepted is True:
            logger.info(f"用户已在原生弹窗确认写操作: {sql[:120]}...")
            return
        if accepted is False:
            raise ToolFailure(decline_msg)
        # accepted is None → 弹窗失败，降级终端确认
        logger.warning("原生确认弹窗失败，降级为终端确认（elicitation）")
    elif ui_mode == "native" and not _check_native_ui():
        logger.warning("MCP_WRITE_CONFIRM_UI=native 但 tkinter 不可用，降级为终端确认")

    # --- 通道2：MCP elicitation 终端确认（兜底）---
    est_block = f"\n\n{est_text}" if est_text else ""
    atomicity_block = f"\n\n{atomicity_warning}" if atomicity_warning else ""
    message = (
        f"⚠️ db-auto-router 即将在数据源 [{db_code}]（{dialect}）执行写操作{ddl_note}：\n\n"
        f"{sql_preview}{est_block}{atomicity_block}\n\n"
        f"确认执行？"
    )

    try:
        result = await ctx.elicit(message=message, schema=WriteConfirmation)
    except Exception as e:
        # 客户端不支持 elicitation（旧版 Claude Code / 其他客户端）或确认组件异常。
        # 安全默认：不静默执行，把决策权交回用户。
        raise ToolFailure(
            f"写操作确认失败（客户端不支持确认组件或组件异常）：{type(e).__name__}: {e}\n"
            "为保证安全，SQL 未执行。\n"
            "处理方式（任选其一）：\n"
            "1. 升级 Claude Code 到支持 MCP elicitation 的版本后重试；\n"
            "2. 如确认可控，设置环境变量 MCP_WRITE_CONFIRM=off 后重启 MCP 服务再执行。"
        )

    action = getattr(result, "action", None)
    if action == "accept" and getattr(result, "data", None) and result.data.confirm:
        logger.info(f"用户已确认写操作: {sql[:120]}...")
        return

    action_label = {"decline": "已拒绝", "cancel": "已取消"}.get(action, f"未确认({action})")
    raise ToolFailure(
        f"用户{action_label}该写操作，SQL 未执行。\n"
        f"待执行语句: {sql_preview}\n"
        "如需执行请重新发起并确认。"
    )
