"""SQLite 持久化：建表、连接与版本化仓储。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 2

SCHEMA = """
-- 机构主数据 + 风险快照（就地版本化，旧版本进入 *_history）
CREATE TABLE IF NOT EXISTS institutions (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    region          TEXT NOT NULL,
    industry        TEXT NOT NULL,
    risk_level      TEXT NOT NULL CHECK (risk_level IN ('高','中','低')),
    risk_score      REAL NOT NULL,
    tags            TEXT NOT NULL DEFAULT '[]',   -- JSON 数组
    status          TEXT NOT NULL DEFAULT '营业' CHECK (status IN ('营业','停业')),
    snapshot_at     TEXT NOT NULL,                -- 风险快照时间 ISO8601 UTC
    version         INTEGER NOT NULL DEFAULT 1,
    updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS institutions_history (
    version         INTEGER NOT NULL,
    id              TEXT NOT NULL,
    name            TEXT NOT NULL,
    region          TEXT NOT NULL,
    industry        TEXT NOT NULL,
    risk_level      TEXT NOT NULL,
    risk_score      REAL NOT NULL,
    tags            TEXT NOT NULL,
    status          TEXT NOT NULL,
    snapshot_at     TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    archived_at     TEXT NOT NULL,
    change_note     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (id, version)
);

-- 检查员（资格可多个，JSON 数组；季度容量=每人本季可承担任务数）
CREATE TABLE IF NOT EXISTS inspectors (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    qualifications  TEXT NOT NULL DEFAULT '[]',   -- JSON 数组，如 ["金融","医疗"]
    quarterly_capacity INTEGER NOT NULL DEFAULT 2,
    active          INTEGER NOT NULL DEFAULT 1,
    version         INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inspectors_history (
    version         INTEGER NOT NULL,
    id              TEXT NOT NULL,
    name            TEXT NOT NULL,
    qualifications  TEXT NOT NULL,
    quarterly_capacity INTEGER NOT NULL,
    active          INTEGER NOT NULL,
    updated_at      TEXT NOT NULL,
    archived_at     TEXT NOT NULL,
    change_note     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (id, version)
);

-- 回避关系（检查员 <-> 机构），删除也留痕
CREATE TABLE IF NOT EXISTS avoidances (
    inspector_id    TEXT NOT NULL,
    institution_id  TEXT NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    active          INTEGER NOT NULL DEFAULT 1,
    version         INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (inspector_id, institution_id)
);
CREATE TABLE IF NOT EXISTS avoidances_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    inspector_id    TEXT NOT NULL,
    institution_id  TEXT NOT NULL,
    reason          TEXT NOT NULL,
    active          INTEGER NOT NULL,
    version         INTEGER NOT NULL,
    updated_at      TEXT NOT NULL,
    archived_at     TEXT NOT NULL,
    change_note     TEXT NOT NULL DEFAULT ''
);

-- 区域季度容量
CREATE TABLE IF NOT EXISTS region_capacities (
    region          TEXT NOT NULL,
    quarter         TEXT NOT NULL,                -- 如 2026Q4
    capacity        INTEGER NOT NULL CHECK (capacity >= 0),
    version         INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (region, quarter)
);
CREATE TABLE IF NOT EXISTS region_capacities_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    region          TEXT NOT NULL,
    quarter         TEXT NOT NULL,
    capacity        INTEGER NOT NULL,
    version         INTEGER NOT NULL,
    updated_at      TEXT NOT NULL,
    archived_at     TEXT NOT NULL,
    change_note     TEXT NOT NULL DEFAULT ''
);

-- 抽检规则
CREATE TABLE IF NOT EXISTS sampling_rules (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    risk_levels         TEXT NOT NULL DEFAULT '["高","中","低"]', -- JSON
    min_score           REAL NOT NULL DEFAULT 0,
    industries          TEXT NOT NULL DEFAULT '[]',  -- 空=不限行业
    required_tag        TEXT NOT NULL DEFAULT '',    -- 空=不限标签
    required_qualification TEXT NOT NULL DEFAULT '', -- 候选检查员必须具备的资格
    quota               INTEGER NOT NULL,            -- 本规则入选名额
    priority            INTEGER NOT NULL DEFAULT 100,-- 小者优先
    active              INTEGER NOT NULL DEFAULT 1,
    version             INTEGER NOT NULL DEFAULT 1,
    updated_at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sampling_rules_history (
    version         INTEGER NOT NULL,
    id              TEXT NOT NULL,
    name            TEXT NOT NULL,
    risk_levels     TEXT NOT NULL,
    min_score       REAL NOT NULL,
    industries      TEXT NOT NULL,
    required_tag    TEXT NOT NULL,
    required_qualification TEXT NOT NULL,
    quota           INTEGER NOT NULL,
    priority        INTEGER NOT NULL,
    active          INTEGER NOT NULL,
    updated_at      TEXT NOT NULL,
    archived_at     TEXT NOT NULL,
    change_note     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (id, version)
);

-- 抽检计划（一个季度一个）
CREATE TABLE IF NOT EXISTS plans (
    id              TEXT PRIMARY KEY,
    quarter         TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT '草稿',
    target_count    INTEGER NOT NULL DEFAULT 0,
    generation_total INTEGER NOT NULL DEFAULT 0,   -- 候选生成需评估的机构总数
    generation_done  INTEGER NOT NULL DEFAULT 0,   -- 已评估数（断点续跑游标）
    generation_cursor TEXT,                        -- 最后处理的机构 id
    generation_token TEXT NOT NULL DEFAULT '',     -- 当前数据版本指纹，失效则需重算
    confirmed_at    TEXT,
    published_at    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS plans_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id         TEXT NOT NULL,
    quarter         TEXT NOT NULL,
    state           TEXT NOT NULL,
    target_count    INTEGER NOT NULL,
    generation_total INTEGER NOT NULL,
    generation_done  INTEGER NOT NULL,
    generation_cursor TEXT,
    generation_token TEXT NOT NULL,
    confirmed_at    TEXT,
    published_at    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    version         INTEGER NOT NULL,
    archived_at     TEXT NOT NULL,
    change_note     TEXT NOT NULL DEFAULT ''
);

-- 候选（计划草稿阶段逐机构写入；selected=1 为入选）
CREATE TABLE IF NOT EXISTS candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id         TEXT NOT NULL REFERENCES plans(id),
    institution_id  TEXT NOT NULL,
    matched_rule_id TEXT,                          -- 命中的最高优先级规则
    selected        INTEGER NOT NULL DEFAULT 0,
    reason_code     TEXT NOT NULL,
    reason_detail   TEXT NOT NULL DEFAULT '',
    proposed_inspector_id TEXT,
    rank_in_rule    INTEGER,
    seq             INTEGER NOT NULL,              -- 评估序号，保证顺序稳定
    UNIQUE (plan_id, institution_id)
);
CREATE INDEX IF NOT EXISTS idx_candidates_plan ON candidates(plan_id, selected);

-- 确认后的计划项（带独立版本，改期/换人/停业剔除都递增）
CREATE TABLE IF NOT EXISTS plan_items (
    id              TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL REFERENCES plans(id),
    institution_id  TEXT NOT NULL,
    inspector_id    TEXT NOT NULL,
    rule_id         TEXT,
    scheduled_date  TEXT NOT NULL,
    item_state      TEXT NOT NULL DEFAULT '待检',
    seq             INTEGER NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (plan_id, institution_id)
);
CREATE TABLE IF NOT EXISTS plan_items_history (
    version         INTEGER NOT NULL,
    id              TEXT NOT NULL,
    plan_id         TEXT NOT NULL,
    institution_id  TEXT NOT NULL,
    inspector_id    TEXT NOT NULL,
    rule_id         TEXT,
    scheduled_date  TEXT NOT NULL,
    item_state      TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    archived_at     TEXT NOT NULL,
    change_note     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (id, version)
);

-- 资源锁：确认时一次性插入；取消/归档时释放
-- 机构季度唯一：lock_type='institution' 时 slot=0，靠 UNIQUE 保证不重占
-- 检查员/区域：slot 0..capacity-1，按槽位计数实现容量
CREATE TABLE IF NOT EXISTS resource_locks (
    lock_type   TEXT NOT NULL CHECK (lock_type IN ('institution','inspector','region')),
    lock_key    TEXT NOT NULL,          -- institution:{quarter}:{inst_id} / inspector:{quarter}:{insp_id} / region:{quarter}:{region}
    slot        INTEGER NOT NULL DEFAULT 0,
    quarter     TEXT NOT NULL,
    ref_plan_id TEXT NOT NULL,
    ref_item_id TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (lock_type, lock_key, slot)
);
CREATE INDEX IF NOT EXISTS idx_locks_plan ON resource_locks(ref_plan_id);

-- 并发发布/确认用的轻量事件日志（审计）
CREATE TABLE IF NOT EXISTS plan_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id     TEXT NOT NULL,
    event       TEXT NOT NULL,
    actor       TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '{}',
    version_from INTEGER,
    version_to   INTEGER,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA wal_autocheckpoint=1000;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
        )


# 各连接当前事务嵌套深度（sqlite3.Connection 不支持自定义属性，用 id 映射）
_tx_depths: dict[int, int] = {}


@contextmanager
def immediate_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """开启写事务（BEGIN IMMEDIATE），用于原子锁定与状态迁移。

    可重入：外层开真实事务，内层使用 SAVEPOINT，内层异常只回滚到保存点。
    """
    depth = _tx_depths.get(id(conn), 0)
    if depth == 0:
        conn.execute("BEGIN IMMEDIATE")
        _tx_depths[id(conn)] = 1
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            _tx_depths[id(conn)] = 0
            raise
        else:
            conn.execute("COMMIT")
            _tx_depths[id(conn)] = 0
    else:
        sp = f"sp_{depth}"
        conn.execute(f"SAVEPOINT {sp}")
        _tx_depths[id(conn)] = depth + 1
        try:
            yield conn
        except Exception:
            conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            conn.execute(f"RELEASE SAVEPOINT {sp}")
            _tx_depths[id(conn)] = depth
            raise
        else:
            conn.execute(f"RELEASE SAVEPOINT {sp}")
            _tx_depths[id(conn)] = depth


# ---------- JSON 字段辅助 ----------
def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(value: str) -> Any:
    return json.loads(value) if value else None


class VersionConflict(Exception):
    """乐观锁版本冲突（HTTP 409）。"""


class StateConflict(Exception):
    """状态机冲突（HTTP 409）。"""


class NotFound(Exception):
    """资源不存在（HTTP 404）。"""
