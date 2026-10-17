/*
 Navicat MySQL Dump SQL

 Source Server         : 本地
 Source Server Type    : MySQL
 Source Server Version : 50744 (5.7.44-log)
 Source Host           : localhost:3306
 Source Schema         : mcp

 Target Server Type    : MySQL
 Target Server Version : 50744 (5.7.44-log)
 File Encoding         : 65001

 Date: 13/04/2026 17:40:28
*/

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ----------------------------
-- Table structure for mcp_datasource_config
-- ----------------------------
DROP TABLE IF EXISTS `mcp_datasource_config`;
CREATE TABLE `mcp_datasource_config`  (
  `dir_path` varchar(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '1' COMMENT '工作空间',
  `db_name` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '业务表述',
  `db_code` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '数据库',
  `db_type` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '数据库类型',
  `conn_url` varchar(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '数据库ip+端口',
  `db_user` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '数据库名称',
  `db_password` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '数据库密码',
  `db_schema` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT 'PG/GaussDB的schema或Oracle/达梦的owner；MySQL留空(库名取db_code)',
  `is_default` tinyint(4) NULL DEFAULT 0 COMMENT '是否默认数据源：0-否，1-是',
  PRIMARY KEY (`dir_path`, `db_code`) USING BTREE
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci ROW_FORMAT = Dynamic;

SET FOREIGN_KEY_CHECKS = 1;

-- ============================================================================
-- v4.4 迁移：老配置库补 db_schema 列（新建库无需执行）
-- ----------------------------------------------------------------------------
-- 背景：表结构查询（get_schema_info / search_tables）原来把 db_code 当 schema 用。
-- 这对 MySQL 成立（db_code 就是库名），但对另外四个方言不成立——PG/GaussDB 的
-- 库名来自 conn_url 的 dbname，Oracle/达梦的 schema 来自连接账号，db_code 对
-- 它们只是业务标签。于是 PG 上 `nspname = 'report'`（真实 schema 是 public）、
-- Oracle 上 `OWNER = 'REPORT'`（真实 owner 是登录用户），永远查不到表，且不报错。
--
-- 现在的规则：调用方显式写的 schema. 前缀 > 本列 > 方言默认
-- （Oracle=USER / PG=current_schema() / DM=当前用户的 USER_* 视图 / MySQL=db_code）。
-- 所以本列通常留空即可，只有"默认 schema 不是 public / 登录用户"时才需要填。
--
-- 注意在 mcp 配置库上执行，不要在执行数据源所在的业务库上执行：
--   USE mcp;
-- ============================================================================
ALTER TABLE `mcp_datasource_config`
  ADD COLUMN `db_schema` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
  NULL DEFAULT NULL
  COMMENT 'PG/GaussDB的schema或Oracle/达梦的owner；MySQL留空(库名取db_code)'
  AFTER `db_password`;

-- 补列后让运行中的 MCP 立刻感知：调用 refresh_datasources 工具
-- （或重启 AI 会话）。缺列期间 MCP 会自动降级为旧行为，不会报错。

-- ============================================================================
-- v4.6 迁移：老配置库补 write_confirm 列（新建库无需执行）
-- ----------------------------------------------------------------------------
-- 背景：写操作确认（execute/import_csv/dump_database 的服务端弹窗）原来只有全局
-- 开关 MCP_WRITE_CONFIRM，一开全开、一关全关。开发库想"随便造数据不弹窗"、
-- 生产库仍要人工确认，这个粒度做不到。
--
-- 现在的规则：数据源级开关。write_confirm=1（默认）= 该数据源写操作需人工审核；
-- =0 = 免确认（GUI/CLI 里取消打勾）。两者与全局开关是"与"的关系：
-- 全局 MCP_WRITE_CONFIRM=off 仍然全量跳过；本列只能把单个数据源放宽到免确认，
-- 不能反向收紧。审计日志不受影响，每一次写操作照旧记录。
--
-- 缺列期间 MCP 自动按"全部需审核"处理（安全默认，行为与 v4.6 之前一致），不会报错。
--
-- 注意在 mcp 配置库上执行，不要在执行数据源所在的业务库上执行：
--   USE mcp;
-- ============================================================================
ALTER TABLE `mcp_datasource_config`
  ADD COLUMN `write_confirm` tinyint(4) NULL DEFAULT 1
  COMMENT '写操作是否需人工审核：0-否（开发库免确认），1-是'
  AFTER `db_schema`;

-- 补列后让运行中的 MCP 立刻感知：调用 refresh_datasources 工具
-- （或重启 AI 会话）。缺列期间 MCP 会自动降级为旧行为，不会报错。
