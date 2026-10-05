"""SQLite 持久化层。

设计要点：
- 全部主数据（风险快照、检查员资格、回避、容量）与计划任务都按「版本」追加保存，
  更新从不覆盖历史，任何时刻可还原任一版本（满足"改期/换人/停业/并发发布要保留版本"）。
- 所有写操作在 ``BEGIN IMMEDIATE`` 事务内完成；配合 ``busy_timeout``，并发请求
  被数据库串行化，第二个请求在第一个提交后读到最新数据再做冲突校验，
  从而实现资源的原子锁定（机构 / 检查员 / 区域容量不会被两个计划同时拿到）。
- ``planning_runs`` / ``run_items`` 保存候选生成的断点游标，进程重启后可续跑，
  且处理顺序确定，续跑结果与一次跑完一致。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .models import (
    Assignment,
    Inspector,
    Institution,
    Plan,
    PLAN_STATES,
    RiskSnapshot,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS institutions (
    institution_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    region TEXT NOT NULL,
    created_version INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    closed_version INTEGER
);

CREATE TABLE IF NOT EXISTS risk_snapshots (
    institution_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    risk_level TEXT NOT NULL,
    risk_score REAL NOT NULL,
    risk_factors TEXT NOT NULL,
    active INTEGER NOT NULL,
    snapshot_note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (institution_id, version)
);

CREATE TABLE IF NOT EXISTS inspectors (
    inspector_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_version INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS inspector_versions (
    inspector_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    qualifications TEXT NOT NULL,
    regions TEXT NOT NULL,
    active INTEGER NOT NULL,
    quarterly_capacity INTEGER NOT NULL DEFAULT 2,
    PRIMARY KEY (inspector_id, version)
);

CREATE TABLE IF NOT EXISTS recusals (
    inspector_id TEXT NOT NULL,
    institution_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    reason TEXT NOT NULL,
    lifted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (inspector_id, institution_id, version)
);

CREATE TABLE IF NOT EXISTS region_capacity (
    region TEXT NOT NULL,
    version INTEGER NOT NULL,
    capacity INTEGER NOT NULL,
    PRIMARY KEY (region, version)
);

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    quarter TEXT NOT NULL,
    inspection_type TEXT NOT NULL,
    status TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    progress TEXT NOT NULL,
    created_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_versions (
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    progress TEXT NOT NULL,
    change_kind TEXT NOT NULL,
    change_note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (plan_id, version)
);

CREATE TABLE IF NOT EXISTS plan_candidates (
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    institution_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    decision TEXT NOT NULL,
    reasons TEXT NOT NULL,
    proposed_inspector_id TEXT,
    eligible_inspector_ids TEXT NOT NULL,
    PRIMARY KEY (plan_id, version, institution_id)
);

CREATE TABLE IF NOT EXISTS assignments (
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    institution_id TEXT NOT NULL,
    inspector_id TEXT,
    status TEXT NOT NULL,
    reasons TEXT NOT NULL,
    history TEXT NOT NULL,
    PRIMARY KEY (plan_id, version, institution_id)
);

CREATE TABLE IF NOT EXISTS planning_runs (
    run_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    state TEXT NOT NULL,
    input_version INTEGER NOT NULL DEFAULT 0,
    cursor_region TEXT,
    cursor_institution_id TEXT,
    total INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_items (
    run_id TEXT NOT NULL,
    institution_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    decision TEXT NOT NULL,
    reasons TEXT NOT NULL,
    proposed_inspector_id TEXT,
    eligible_inspector_ids TEXT NOT NULL,
    PRIMARY KEY (run_id, institution_id)
);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ConflictError(Exception):
    """并发或资源冲突（机构 / 检查员 / 容量已被占用等）。"""


class NotFoundError(Exception):
    """对象不存在。"""


class StateError(Exception):
    """对象当前状态不允许该操作。"""


class Store:
    """线程友好的 SQLite 封装：每次取连接，写事务串行化。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.in_memory = self.path == ":memory:"
        self._keeper: sqlite3.Connection | None = None
        # 进程内写事务串行化：共享缓存内存库并发写会直接报 LOCKED
        # （不可重试），文件库则同时借此避免 BUSY；跨进程仍由 SQLite 锁处理。
        self._write_lock = threading.RLock()
        if self.in_memory:
            # 共享缓存内存库：同一 Store 内所有连接访问同一份数据；
            # keeper 常驻，避免最后一个连接关闭后内存库被回收。
            self._uri = (
                f"file:inspection_planning_{uuid.uuid4().hex}?mode=memory&cache=shared"
            )
            self._keeper = sqlite3.connect(self._uri, uri=True, timeout=10,
                                           isolation_level=None)
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    # ---- 连接与事务 -------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self.in_memory:
            conn = sqlite3.connect(self._uri, uri=True, timeout=10,
                                   isolation_level=None)
        else:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        if not self.in_memory:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def close(self) -> None:
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None

    @contextmanager
    def _reader(self, conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
        """复用调用方持有的连接（避免在写事务内另开连接自锁）。"""
        if conn is not None:
            yield conn
        else:
            with self.read_only() as owned:
                yield owned

    def init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            # 轻量迁移：旧库补齐后加的列
            run_cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(planning_runs)"
            ).fetchall()}
            if "input_version" not in run_cols:
                conn.execute(
                    "ALTER TABLE planning_runs ADD COLUMN input_version INTEGER DEFAULT 0"
                )
            insp_cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(inspector_versions)"
            ).fetchall()}
            if "quarterly_capacity" not in insp_cols:
                conn.execute(
                    "ALTER TABLE inspector_versions "
                    "ADD COLUMN quarterly_capacity INTEGER NOT NULL DEFAULT 2"
                )
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('version', '0')"
            )
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """立即取写锁的串行化事务，提交或回滚由上下文自动完成。"""
        conn = self._connect()
        with self._write_lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    @contextmanager
    def read_only(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            yield conn
        finally:
            conn.rollback()
            conn.close()

    # ---- 通用 -------------------------------------------------------

    @staticmethod
    def next_version(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "UPDATE meta SET value = CAST(value AS INTEGER) + 1 "
            "WHERE key='version' RETURNING CAST(value AS INTEGER)"
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _current_meta_version(conn: sqlite3.Connection) -> int:
        return int(
            conn.execute("SELECT CAST(value AS INTEGER) AS v FROM meta WHERE key='version'")
            .fetchone()["v"]
        )

    @staticmethod
    def log_event(
        conn: sqlite3.Connection, version: int, kind: str, payload: dict[str, Any]
    ) -> None:
        conn.execute(
            "INSERT INTO events(version, ts, kind, payload) VALUES(?,?,?,?)",
            (version, utc_now(), kind, json.dumps(payload, ensure_ascii=False)),
        )

    # ---- 机构与风险快照 ---------------------------------------------

    def upsert_institution(
        self,
        institution_id: str,
        name: str,
        region: str,
        risk_level: str,
        risk_score: float,
        risk_factors: Iterable[str],
        snapshot_note: str = "",
        active: bool = True,
    ) -> dict[str, Any]:
        """登记机构或追加一版风险快照（主档区域/名称可同步修订）。"""
        factors = list(risk_factors)
        with self.transaction() as conn:
            version = self.next_version(conn)
            conn.execute(
                "INSERT INTO institutions(institution_id, name, region, "
                "created_version, active, closed_version) VALUES(?,?,?,?,1,NULL) "
                "ON CONFLICT(institution_id) DO UPDATE SET "
                "name=excluded.name, region=excluded.region, "
                "active=CASE WHEN ? THEN 1 ELSE institutions.active END, "
                "closed_version=CASE WHEN ? THEN NULL ELSE institutions.closed_version END",
                (institution_id, name, region, version, active, active),
            )
            conn.execute(
                "INSERT INTO risk_snapshots(institution_id, version, risk_level, "
                "risk_score, risk_factors, active, snapshot_note) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    institution_id,
                    version,
                    risk_level,
                    float(risk_score),
                    json.dumps(factors, ensure_ascii=False),
                    1 if active else 0,
                    snapshot_note,
                ),
            )
            self.log_event(
                conn,
                version,
                "institution.risk_snapshot",
                {"institution_id": institution_id, "risk_level": risk_level,
                 "risk_score": risk_score, "region": region},
            )
        return {"institution_id": institution_id, "version": version}

    def close_institution(self, institution_id: str, reason: str) -> dict[str, Any]:
        """机构停业：主档置停并追加停业版快照（历史快照全部保留）。"""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM institutions WHERE institution_id=?",
                (institution_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"机构不存在：{institution_id}")
            if not row["active"]:
                raise StateError(f"机构已停业：{institution_id}")
            version = self.next_version(conn)
            latest = conn.execute(
                "SELECT * FROM risk_snapshots WHERE institution_id=? "
                "ORDER BY version DESC LIMIT 1",
                (institution_id,),
            ).fetchone()
            conn.execute(
                "UPDATE institutions SET active=0, closed_version=? "
                "WHERE institution_id=?",
                (version, institution_id),
            )
            if latest is not None:
                conn.execute(
                    "INSERT INTO risk_snapshots(institution_id, version, "
                    "risk_level, risk_score, risk_factors, active, snapshot_note) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        institution_id,
                        version,
                        latest["risk_level"],
                        latest["risk_score"],
                        latest["risk_factors"],
                        0,
                        f"机构停业：{reason}",
                    ),
                )
            self.log_event(
                conn, version, "institution.close",
                {"institution_id": institution_id, "reason": reason},
            )
        return {"institution_id": institution_id, "version": version}

    @staticmethod
    def _row_to_institution(row: sqlite3.Row | None) -> Institution | None:
        if row is None:
            return None
        risk = RiskSnapshot(
            institution_id=row["institution_id"],
            version=row["risk_version"],
            risk_level=row["risk_level"],
            risk_score=row["risk_score"],
            risk_factors=json.loads(row["risk_factors"]),
            active=bool(row["risk_active"]),
            snapshot_note=row["snapshot_note"],
        ) if row["risk_version"] is not None else None
        return Institution(
            institution_id=row["institution_id"],
            name=row["name"],
            region=row["region"],
            active=bool(row["active"]),
            closed_version=row["closed_version"],
            risk=risk,
        )

    def list_institutions(
        self, conn: sqlite3.Connection | None = None
    ) -> list[Institution]:
        sql = (
            "SELECT i.institution_id, i.name, i.region, i.active, i.closed_version, "
            "s.version AS risk_version, s.risk_level, s.risk_score, s.risk_factors, "
            "s.active AS risk_active, s.snapshot_note "
            "FROM institutions i LEFT JOIN risk_snapshots s "
            "ON s.institution_id=i.institution_id AND s.version=("
            "SELECT MAX(version) FROM risk_snapshots WHERE institution_id=i.institution_id)"
        )
        with self._reader(conn) as c:
            rows = c.execute(sql + " ORDER BY i.institution_id").fetchall()
        return [inst for r in rows if (inst := self._row_to_institution(r))]

    def list_risk_versions(self, institution_id: str) -> list[RiskSnapshot]:
        with self.read_only() as conn:
            rows = conn.execute(
                "SELECT * FROM risk_snapshots WHERE institution_id=? ORDER BY version",
                (institution_id,),
            ).fetchall()
        return [
            RiskSnapshot(
                institution_id=institution_id,
                version=r["version"],
                risk_level=r["risk_level"],
                risk_score=r["risk_score"],
                risk_factors=json.loads(r["risk_factors"]),
                active=bool(r["active"]),
                snapshot_note=r["snapshot_note"],
            )
            for r in rows
        ]

    # ---- 检查员与资格 -----------------------------------------------

    def upsert_inspector(
        self,
        inspector_id: str,
        name: str,
        qualifications: Iterable[str],
        regions: Iterable[str],
        active: bool = True,
        quarterly_capacity: int = 2,
    ) -> dict[str, Any]:
        """登记检查员或追加一版资格（资格/服务区域/季度容量调整全部留版本）。"""
        quals = list(qualifications)
        serves = list(regions)
        if int(quarterly_capacity) < 1:
            raise ValueError("季度检查容量至少为 1")
        with self.transaction() as conn:
            version = self.next_version(conn)
            conn.execute(
                "INSERT INTO inspectors(inspector_id, name, created_version, active) "
                "VALUES(?,?,?,?) ON CONFLICT(inspector_id) DO UPDATE SET "
                "name=excluded.name, active=excluded.active",
                (inspector_id, name, version, 1 if active else 0),
            )
            conn.execute(
                "INSERT INTO inspector_versions(inspector_id, version, name, "
                "qualifications, regions, active, quarterly_capacity) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    inspector_id,
                    version,
                    name,
                    json.dumps(quals, ensure_ascii=False),
                    json.dumps(serves, ensure_ascii=False),
                    1 if active else 0,
                    int(quarterly_capacity),
                ),
            )
            self.log_event(
                conn, version, "inspector.qualification",
                {"inspector_id": inspector_id, "qualifications": quals,
                 "regions": serves, "active": active,
                 "quarterly_capacity": int(quarterly_capacity)},
            )
        return {"inspector_id": inspector_id, "version": version}

    def list_inspectors(
        self, conn: sqlite3.Connection | None = None
    ) -> list[Inspector]:
        sql = (
            "SELECT v.* FROM inspector_versions v WHERE v.version=("
            "SELECT MAX(version) FROM inspector_versions WHERE inspector_id=v.inspector_id)"
        )
        with self._reader(conn) as c:
            rows = c.execute(sql + " ORDER BY v.inspector_id").fetchall()
        return [
            Inspector(
                inspector_id=r["inspector_id"],
                name=r["name"],
                qualifications=json.loads(r["qualifications"]),
                regions=json.loads(r["regions"]),
                active=bool(r["active"]),
                version=r["version"],
                quarterly_capacity=int(r["quarterly_capacity"]),
            )
            for r in rows
        ]

    def list_inspector_versions(self, inspector_id: str) -> list[dict[str, Any]]:
        with self.read_only() as conn:
            rows = conn.execute(
                "SELECT inspector_id, version, name, qualifications, regions, "
                "active, quarterly_capacity FROM inspector_versions "
                "WHERE inspector_id=? ORDER BY version",
                (inspector_id,),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["qualifications"] = json.loads(d["qualifications"])
            d["regions"] = json.loads(d["regions"])
            d["active"] = bool(d["active"])
            result.append(d)
        return result

    # ---- 回避 -------------------------------------------------------

    def add_recusal(
        self, inspector_id: str, institution_id: str, reason: str
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM inspectors WHERE inspector_id=?", (inspector_id,)
            ).fetchone() is None:
                raise NotFoundError(f"检查员不存在：{inspector_id}")
            if conn.execute(
                "SELECT 1 FROM institutions WHERE institution_id=?", (institution_id,)
            ).fetchone() is None:
                raise NotFoundError(f"机构不存在：{institution_id}")
            latest = conn.execute(
                "SELECT lifted FROM recusals WHERE inspector_id=? AND institution_id=? "
                "ORDER BY version DESC LIMIT 1",
                (inspector_id, institution_id),
            ).fetchone()
            if latest is not None and not latest["lifted"]:
                raise StateError("该回避关系已存在且生效中")
            version = self.next_version(conn)
            conn.execute(
                "INSERT INTO recusals(inspector_id, institution_id, version, reason, "
                "lifted) VALUES(?,?,?,?,0)",
                (inspector_id, institution_id, version, reason),
            )
            self.log_event(
                conn, version, "recusal.add",
                {"inspector_id": inspector_id, "institution_id": institution_id,
                 "reason": reason},
            )
        return {"inspector_id": inspector_id, "institution_id": institution_id,
                "version": version}

    def lift_recusal(
        self, inspector_id: str, institution_id: str, reason: str
    ) -> dict[str, Any]:
        """解除回避同样追加版本，不删除原始回避记录。"""
        with self.transaction() as conn:
            latest = conn.execute(
                "SELECT * FROM recusals WHERE inspector_id=? AND institution_id=? "
                "ORDER BY version DESC LIMIT 1",
                (inspector_id, institution_id),
            ).fetchone()
            if latest is None:
                raise NotFoundError("回避关系不存在")
            if latest["lifted"]:
                raise StateError("该回避关系已处于解除状态")
            version = self.next_version(conn)
            conn.execute(
                "INSERT INTO recusals(inspector_id, institution_id, version, reason, "
                "lifted) VALUES(?,?,?,?,1)",
                (inspector_id, institution_id, version, f"解除回避：{reason}"),
            )
            self.log_event(
                conn, version, "recusal.lift",
                {"inspector_id": inspector_id, "institution_id": institution_id,
                 "reason": reason},
            )
        return {"inspector_id": inspector_id, "institution_id": institution_id,
                "version": version}

    def list_recusal_versions(
        self, conn: sqlite3.Connection | None = None
    ) -> list[dict[str, Any]]:
        with self._reader(conn) as c:
            rows = c.execute(
                "SELECT * FROM recusals ORDER BY version, inspector_id, institution_id"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- 区域容量 ---------------------------------------------------

    def set_capacity(self, region: str, capacity: int) -> dict[str, Any]:
        if capacity < 0:
            raise ValueError("容量不能为负")
        with self.transaction() as conn:
            version = self.next_version(conn)
            conn.execute(
                "INSERT INTO region_capacity(region, version, capacity) VALUES(?,?,?)",
                (region, version, int(capacity)),
            )
            self.log_event(
                conn, version, "region.capacity",
                {"region": region, "capacity": int(capacity)},
            )
        return {"region": region, "capacity": int(capacity), "version": version}

    def active_capacities(
        self, conn: sqlite3.Connection | None = None
    ) -> dict[str, tuple[int, int]]:
        """region -> (capacity, version)，取每区域最新版本。"""
        with self._reader(conn) as c:
            rows = c.execute(
                "SELECT c.* FROM region_capacity c WHERE c.version=("
                "SELECT MAX(version) FROM region_capacity WHERE region=c.region)"
            ).fetchall()
        return {r["region"]: (r["capacity"], r["version"]) for r in rows}

    # ---- 计划版本 ---------------------------------------------------

    def create_plan(
        self, plan_id: str, quarter: str, inspection_type: str
    ) -> Plan:
        now = utc_now()
        with self.transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM plans WHERE plan_id=?", (plan_id,)
            ).fetchone() is not None:
                raise StateError(f"计划已存在：{plan_id}")
            version = self.next_version(conn)
            payload = {"candidates": [], "note": "计划已创建"}
            conn.execute(
                "INSERT INTO plans(plan_id, quarter, inspection_type, status, "
                "current_version, progress, created_version, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (plan_id, quarter, inspection_type, "草稿", 1,
                 "计划已创建，尚未生成候选", version, now, now),
            )
            conn.execute(
                "INSERT INTO plan_versions(plan_id, version, status, progress, "
                "change_kind, change_note, created_at, payload) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (plan_id, 1, "草稿", "计划已创建，尚未生成候选", "create",
                 "创建计划", now, json.dumps(payload, ensure_ascii=False)),
            )
            self.log_event(
                conn, version, "plan.create",
                {"plan_id": plan_id, "quarter": quarter,
                 "inspection_type": inspection_type},
            )
        plan = self.get_plan(plan_id)
        assert plan is not None
        return plan

    def _row_to_assignment(self, row: sqlite3.Row) -> Assignment:
        return Assignment(
            institution_id=row["institution_id"],
            institution_name=row["institution_name"],
            region=row["region"],
            risk_level=row["risk_level"],
            risk_score=row["risk_score"],
            inspector_id=row["inspector_id"],
            inspector_name=row["inspector_name"],
            status=row["status"],
            reasons=json.loads(row["reasons"]),
            history=json.loads(row["history"]),
        )

    def get_plan(self, plan_id: str, version: int | None = None) -> Plan | None:
        with self.read_only() as conn:
            prow = conn.execute(
                "SELECT * FROM plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if prow is None:
                return None
            ver = prow["current_version"] if version is None else version
            vrow = conn.execute(
                "SELECT * FROM plan_versions WHERE plan_id=? AND version=?",
                (plan_id, ver),
            ).fetchone()
            if vrow is None:
                raise NotFoundError(f"计划版本不存在：{plan_id} v{ver}")
            rows = conn.execute(
                "WITH latest_inst AS ("
                "SELECT institution_id, name AS institution_name, region FROM institutions"
                "), latest_insp AS ("
                "SELECT inspector_id, name FROM inspector_versions iv WHERE version=("
                "SELECT MAX(version) FROM inspector_versions WHERE inspector_id=iv.inspector_id)"
                "), latest_risk AS ("
                "SELECT institution_id, risk_level, risk_score FROM risk_snapshots s "
                "WHERE version=(SELECT MAX(version) FROM risk_snapshots "
                "WHERE institution_id=s.institution_id)"
                ") "
                "SELECT a.institution_id, COALESCE(li.institution_name,'') AS institution_name, "
                "COALESCE(li.region,'') AS region, "
                "COALESCE(lr.risk_level,'') AS risk_level, "
                "COALESCE(lr.risk_score,0) AS risk_score, "
                "a.inspector_id, COALESCE(lp.name,'') AS inspector_name, "
                "a.status, a.reasons, a.history "
                "FROM assignments a "
                "LEFT JOIN latest_inst li ON li.institution_id=a.institution_id "
                "LEFT JOIN latest_insp lp ON lp.inspector_id=a.inspector_id "
                "LEFT JOIN latest_risk lr ON lr.institution_id=a.institution_id "
                "WHERE a.plan_id=? AND a.version=? ORDER BY a.institution_id",
                (plan_id, ver),
            ).fetchall()
            versions = [
                r["version"]
                for r in conn.execute(
                    "SELECT version FROM plan_versions WHERE plan_id=? ORDER BY version",
                    (plan_id,),
                ).fetchall()
            ]
        return Plan(
            plan_id=prow["plan_id"],
            quarter=prow["quarter"],
            inspection_type=prow["inspection_type"],
            status=vrow["status"],
            version=ver,
            progress=vrow["progress"],
            created_at=prow["created_at"],
            updated_at=prow["updated_at"],
            assignments=tuple(self._row_to_assignment(r) for r in rows),
            versions=tuple(versions),
        )

    def list_plans(self) -> list[Plan]:
        with self.read_only() as conn:
            ids = [
                r["plan_id"]
                for r in conn.execute("SELECT plan_id FROM plans ORDER BY plan_id")
            ]
        return [p for pid in ids if (p := self.get_plan(pid))]

    def get_plan_version_meta(
        self, conn: sqlite3.Connection, plan_id: str
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"计划不存在：{plan_id}")
        return row

    def save_plan_version(
        self,
        conn: sqlite3.Connection,
        plan_id: str,
        status: str,
        progress: str,
        change_kind: str,
        change_note: str,
        payload: dict[str, Any],
        assignments: list[Assignment] | None = None,
        candidates: list[dict[str, Any]] | None = None,
    ) -> int:
        """在已开启的写事务内追加一个计划版本，返回新版本号。"""
        if status not in PLAN_STATES:
            raise ValueError(f"非法计划状态：{status}")
        row = conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 AS v FROM plan_versions WHERE plan_id=?",
            (plan_id,),
        ).fetchone()
        version = int(row["v"])
        now = utc_now()
        conn.execute(
            "INSERT INTO plan_versions(plan_id, version, status, progress, "
            "change_kind, change_note, created_at, payload) VALUES(?,?,?,?,?,?,?,?)",
            (plan_id, version, status, progress, change_kind, change_note, now,
             json.dumps(payload, ensure_ascii=False)),
        )
        conn.execute(
            "UPDATE plans SET status=?, current_version=?, progress=?, updated_at=? "
            "WHERE plan_id=?",
            (status, version, progress, now, plan_id),
        )
        self.log_event(
            conn,
            self._current_meta_version(conn),
            "plan.version",
            {"plan_id": plan_id, "plan_version": version, "status": status,
             "change_kind": change_kind, "change_note": change_note},
        )
        if assignments is not None:
            for a in assignments:
                conn.execute(
                    "INSERT INTO assignments(plan_id, version, institution_id, "
                    "inspector_id, status, reasons, history) VALUES(?,?,?,?,?,?,?)",
                    (plan_id, version, a.institution_id, a.inspector_id, a.status,
                     json.dumps(a.reasons, ensure_ascii=False),
                     json.dumps(a.history, ensure_ascii=False)),
                )
        if candidates is not None:
            for c in candidates:
                conn.execute(
                    "INSERT INTO plan_candidates(plan_id, version, institution_id, "
                    "rank, decision, reasons, proposed_inspector_id, "
                    "eligible_inspector_ids) VALUES(?,?,?,?,?,?,?,?)",
                    (plan_id, version, c["institution_id"], c["rank"], c["decision"],
                     json.dumps(c["reasons"], ensure_ascii=False),
                     c.get("proposed_inspector_id"),
                     json.dumps(c.get("eligible_inspector_ids", []),
                                ensure_ascii=False)),
                )
        return version

    def get_candidates(
        self, plan_id: str, version: int | None = None
    ) -> list[dict[str, Any]]:
        with self.read_only() as conn:
            if version is None:
                version = int(conn.execute(
                    "SELECT current_version FROM plans WHERE plan_id=?", (plan_id,)
                ).fetchone()["current_version"])
            rows = conn.execute(
                "SELECT * FROM plan_candidates WHERE plan_id=? AND version=? "
                "ORDER BY rank",
                (plan_id, version),
            ).fetchall()
        return [
            {
                "institution_id": r["institution_id"],
                "rank": r["rank"],
                "decision": r["decision"],
                "reasons": json.loads(r["reasons"]),
                "proposed_inspector_id": r["proposed_inspector_id"],
                "eligible_inspector_ids": json.loads(r["eligible_inspector_ids"]),
                "version": version,
            }
            for r in rows
        ]

    def latest_candidate_version_tx(
        self, conn: sqlite3.Connection, plan_id: str
    ) -> tuple[int, str] | None:
        """最近一份候选所在的计划版本及其状态（供确认时做并发校验）。"""
        row = conn.execute(
            "SELECT c.version AS version, pv.status AS status FROM ("
            "SELECT MAX(version) AS version FROM plan_candidates WHERE plan_id=?"
            ") c JOIN plan_versions pv "
            "ON pv.plan_id=? AND pv.version=c.version",
            (plan_id, plan_id),
        ).fetchone()
        if row is None or row["version"] is None:
            return None
        return int(row["version"]), row["status"]

    def load_candidates_tx(
        self, conn: sqlite3.Connection, plan_id: str, version: int
    ) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM plan_candidates WHERE plan_id=? AND version=? ORDER BY rank",
            (plan_id, version),
        ).fetchall()

    def list_plan_versions(self, plan_id: str) -> list[dict[str, Any]]:
        with self.read_only() as conn:
            rows = conn.execute(
                "SELECT plan_id, version, status, progress, change_kind, change_note, "
                "created_at FROM plan_versions WHERE plan_id=? ORDER BY version",
                (plan_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def load_current_assignments_tx(
        self, conn: sqlite3.Connection, plan_id: str
    ) -> list[sqlite3.Row]:
        """在写事务内读取当前版本任务行（供改期/换人/停业联动复用）。"""
        row = self.get_plan_version_meta(conn, plan_id)
        return conn.execute(
            "SELECT * FROM assignments WHERE plan_id=? AND version=? "
            "ORDER BY institution_id",
            (plan_id, row["current_version"]),
        ).fetchall()

    @staticmethod
    def row_to_assignment_row(row: sqlite3.Row) -> Assignment:
        return Assignment(
            institution_id=row["institution_id"],
            institution_name="",
            region="",
            risk_level="",
            risk_score=0.0,
            inspector_id=row["inspector_id"],
            inspector_name=None,
            status=row["status"],
            reasons=json.loads(row["reasons"]),
            history=json.loads(row["history"]),
        )

    # ---- 当前资源占用（原子锁定的依据） -----------------------------

    # 仍占用资源（机构名额 / 检查员工作量 / 区域容量）的任务状态
    OCCUPYING_STATUSES = ("已锁定", "已换人", "已改期")

    def active_occupancy(
        self, conn: sqlite3.Connection, quarter: str, exclude_plan: str | None = None
    ) -> dict[str, Any]:
        """汇总某季度所有「已锁定/已发布」计划当前版本的资源占用。

        返回：
        - ``institutions``: institution_id -> (plan_id, inspector_id)，
          同一机构最多被一个计划占用（硬约束）；
        - ``inspector_loads``: inspector_id -> 已承担任务数（受其季度容量限制）；
        - ``region_counts``: region -> 占用任务数。
        """
        sql = (
            "SELECT p.plan_id, p.quarter, a.institution_id, a.inspector_id, "
            "i.region AS region FROM plans p "
            "JOIN assignments a ON a.plan_id=p.plan_id AND a.version=p.current_version "
            "JOIN institutions i ON i.institution_id=a.institution_id "
            "WHERE p.status IN ('已锁定','已发布') "
            f"AND a.status IN ({','.join('?' for _ in self.OCCUPYING_STATUSES)}) "
            "AND p.quarter=?"
        )
        params: list[Any] = list(self.OCCUPYING_STATUSES) + [quarter]
        rows = conn.execute(sql, params).fetchall()
        institutions: dict[str, tuple[str, str | None]] = {}
        inspector_loads: dict[str, int] = {}
        region_counts: dict[str, int] = {}
        for r in rows:
            if exclude_plan is not None and r["plan_id"] == exclude_plan:
                continue
            institutions[r["institution_id"]] = (r["plan_id"], r["inspector_id"])
            if r["inspector_id"]:
                inspector_loads[r["inspector_id"]] = (
                    inspector_loads.get(r["inspector_id"], 0) + 1
                )
            region_counts[r["region"]] = region_counts.get(r["region"], 0) + 1
        return {"institutions": institutions, "inspectors": inspector_loads,
                "region_counts": region_counts}

    # ---- 断点续跑游标 -----------------------------------------------

    def create_run(
        self, run_id: str, plan_id: str, total: int, input_version: int
    ) -> None:
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO planning_runs(run_id, plan_id, state, input_version, "
                "total, processed, created_at, updated_at) "
                "VALUES(?,?,'running',?,?,0,?,?)",
                (run_id, plan_id, input_version, total, now, now),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.read_only() as conn:
            row = conn.execute(
                "SELECT * FROM planning_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            return None if row is None else dict(row)

    def get_run_for_update(
        self, conn: sqlite3.Connection, plan_id: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM planning_runs WHERE plan_id=? ORDER BY rowid DESC LIMIT 1",
            (plan_id,),
        ).fetchone()

    def save_run_items(
        self, conn: sqlite3.Connection, run_id: str, items: list[dict[str, Any]]
    ) -> None:
        for item in items:
            conn.execute(
                "INSERT OR REPLACE INTO run_items(run_id, institution_id, rank, "
                "decision, reasons, proposed_inspector_id, eligible_inspector_ids) "
                "VALUES(?,?,?,?,?,?,?)",
                (run_id, item["institution_id"], item["rank"], item["decision"],
                 json.dumps(item["reasons"], ensure_ascii=False),
                 item.get("proposed_inspector_id"),
                 json.dumps(item["eligible_inspector_ids"], ensure_ascii=False)),
            )

    def load_run_items(self, run_id: str) -> list[dict[str, Any]]:
        with self.read_only() as conn:
            rows = conn.execute(
                "SELECT * FROM run_items WHERE run_id=? ORDER BY rank", (run_id,)
            ).fetchall()
        return [
            {
                "institution_id": r["institution_id"],
                "rank": r["rank"],
                "decision": r["decision"],
                "reasons": json.loads(r["reasons"]),
                "proposed_inspector_id": r["proposed_inspector_id"],
                "eligible_inspector_ids": json.loads(r["eligible_inspector_ids"]),
            }
            for r in rows
        ]

    def load_run_items_tx(
        self, conn: sqlite3.Connection, run_id: str
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM run_items WHERE run_id=? ORDER BY rank", (run_id,)
        ).fetchall()
        return [
            {
                "institution_id": r["institution_id"],
                "rank": r["rank"],
                "decision": r["decision"],
                "reasons": json.loads(r["reasons"]),
                "proposed_inspector_id": r["proposed_inspector_id"],
                "eligible_inspector_ids": json.loads(r["eligible_inspector_ids"]),
            }
            for r in rows
        ]

    def update_run_progress(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        processed: int,
        cursor_region: str | None,
        cursor_institution_id: str | None,
        state: str = "running",
    ) -> None:
        conn.execute(
            "UPDATE planning_runs SET state=?, processed=?, cursor_region=?, "
            "cursor_institution_id=?, updated_at=? WHERE run_id=?",
            (state, processed, cursor_region, cursor_institution_id, utc_now(),
             run_id),
        )

    def list_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.read_only() as conn:
            rows = conn.execute(
                "SELECT seq, version, ts, kind, payload FROM events "
                "ORDER BY seq DESC LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            result.append(d)
        return result
