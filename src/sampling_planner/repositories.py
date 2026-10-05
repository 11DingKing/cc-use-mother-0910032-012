"""版本化仓储：机构、检查员、回避、区域容量、规则的增改与历史归档。"""
from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from .database import NotFound, VersionConflict, dumps, loads, utcnow


def _history_archive(conn: sqlite3.Connection, table: str, row: sqlite3.Row, note: str) -> None:
    """把主表当前行复制到 history 表（history 表多 archived_at/change_note 两列）。"""
    cols = [k for k in row.keys()]
    placeholders = ", ".join([f":{c}" for c in cols] + [":archived_at", ":change_note"])
    conn.execute(
        f"INSERT INTO {table}_history ({', '.join(cols)}, archived_at, change_note) "
        f"VALUES ({placeholders})",
        {**dict(row), "archived_at": utcnow(), "change_note": note},
    )


class InstitutionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        self.conn.execute(
            """INSERT INTO institutions
               (id, name, region, industry, risk_level, risk_score, tags, status,
                snapshot_at, version, updated_at)
               VALUES (:id,:name,:region,:industry,:risk_level,:risk_score,:tags,:status,
                       :snapshot_at,1,:updated_at)""",
            {
                "id": data["id"],
                "name": data["name"],
                "region": data["region"],
                "industry": data.get("industry", ""),
                "risk_level": data["risk_level"],
                "risk_score": float(data["risk_score"]),
                "tags": dumps(data.get("tags", [])),
                "status": data.get("status", "营业"),
                "snapshot_at": data["snapshot_at"],
                "updated_at": now,
            },
        )
        return self.get(data["id"])

    def get(self, inst_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM institutions WHERE id=?", (inst_id,)).fetchone()
        if row is None:
            raise NotFound(f"机构不存在：{inst_id}")
        return self._to_dict(row)

    def list(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM institutions ORDER BY id").fetchall()
        return [self._to_dict(r) for r in rows]

    def update_snapshot(
        self,
        inst_id: str,
        expected_version: int,
        changes: dict[str, Any],
        note: str = "风险快照更新",
    ) -> dict[str, Any]:
        """更新风险快照或状态，版本号 +1，旧版本入历史。expected_version 做乐观锁。"""
        row = self.conn.execute("SELECT * FROM institutions WHERE id=?", (inst_id,)).fetchone()
        if row is None:
            raise NotFound(f"机构不存在：{inst_id}")
        if row["version"] != expected_version:
            raise VersionConflict(
                f"机构版本冲突：当前 v{row['version']}，提交基于 v{expected_version}"
            )
        _history_archive(self.conn, "institutions", row, note)

        merged = {
            "name": changes.get("name", row["name"]),
            "region": changes.get("region", row["region"]),
            "industry": changes.get("industry", row["industry"]),
            "risk_level": changes.get("risk_level", row["risk_level"]),
            "risk_score": float(changes["risk_score"]) if "risk_score" in changes else row["risk_score"],
            "tags": dumps(changes["tags"]) if "tags" in changes else row["tags"],
            "status": changes.get("status", row["status"]),
            "snapshot_at": changes.get("snapshot_at", utcnow()),
        }
        self.conn.execute(
            """UPDATE institutions SET name=:name, region=:region, industry=:industry,
                  risk_level=:risk_level, risk_score=:risk_score, tags=:tags, status=:status,
                  snapshot_at=:snapshot_at, version=version+1, updated_at=:updated_at
               WHERE id=:id""",
            {"id": inst_id, "updated_at": utcnow(), **merged},
        )
        return self.get(inst_id)

    def suspend(self, inst_id: str, expected_version: int, note: str = "机构停业") -> dict[str, Any]:
        return self.update_snapshot(
            inst_id, expected_version, {"status": "停业"}, note=note
        )

    def history(self, inst_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM institutions_history WHERE id=? ORDER BY version", (inst_id,)
        ).fetchall()
        return [self._to_dict(r, archived=True) for r in rows]

    @staticmethod
    def _to_dict(row: sqlite3.Row, archived: bool = False) -> dict[str, Any]:
        d = dict(row)
        d["tags"] = loads(d["tags"])
        d["risk_score"] = float(d["risk_score"])
        if archived:
            d.pop("id", None)
            d["id"] = row["id"]
        return d


class InspectorRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        self.conn.execute(
            """INSERT INTO inspectors (id, name, qualifications, quarterly_capacity, active, version, updated_at)
               VALUES (:id,:name,:qualifications,:capacity,1,1,:now)""",
            {
                "id": data["id"],
                "name": data["name"],
                "qualifications": dumps(data.get("qualifications", [])),
                "capacity": int(data.get("quarterly_capacity", 2)),
                "now": utcnow(),
            },
        )
        return self.get(data["id"])

    def get(self, insp_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM inspectors WHERE id=?", (insp_id,)).fetchone()
        if row is None:
            raise NotFound(f"检查员不存在：{insp_id}")
        return self._to_dict(row)

    def list(self, active_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM inspectors"
        if active_only:
            sql += " WHERE active=1"
        rows = self.conn.execute(sql + " ORDER BY id").fetchall()
        return [self._to_dict(r) for r in rows]

    def update(
        self, insp_id: str, expected_version: int, changes: dict[str, Any], note: str = "检查员信息更新"
    ) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM inspectors WHERE id=?", (insp_id,)).fetchone()
        if row is None:
            raise NotFound(f"检查员不存在：{insp_id}")
        if row["version"] != expected_version:
            raise VersionConflict(
                f"检查员版本冲突：当前 v{row['version']}，提交基于 v{expected_version}"
            )
        _history_archive(self.conn, "inspectors", row, note)
        self.conn.execute(
            """UPDATE inspectors SET name=:name, qualifications=:qualifications,
                  quarterly_capacity=:capacity, active=:active,
                  version=version+1, updated_at=:now
               WHERE id=:id""",
            {
                "id": insp_id,
                "name": changes.get("name", row["name"]),
                "qualifications": dumps(changes["qualifications"])
                if "qualifications" in changes else row["qualifications"],
                "capacity": int(changes["quarterly_capacity"])
                if "quarterly_capacity" in changes else row["quarterly_capacity"],
                "active": int(changes["active"]) if "active" in changes else row["active"],
                "now": utcnow(),
            },
        )
        return self.get(insp_id)

    def history(self, insp_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM inspectors_history WHERE id=? ORDER BY version", (insp_id,)
        ).fetchall()
        return [self._to_dict(r) for r in rows]

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["qualifications"] = loads(d["qualifications"])
        d["active"] = bool(d["active"])
        d["quarterly_capacity"] = int(d["quarterly_capacity"])
        return d


class AvoidanceRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add(self, inspector_id: str, institution_id: str, reason: str = "") -> dict[str, Any]:
        now = utcnow()
        row = self.conn.execute(
            "SELECT * FROM avoidances WHERE inspector_id=? AND institution_id=?",
            (inspector_id, institution_id),
        ).fetchone()
        if row is None:
            self.conn.execute(
                """INSERT INTO avoidances (inspector_id, institution_id, reason, active, version, updated_at)
                   VALUES (?,?,?,1,1,?)""",
                (inspector_id, institution_id, reason, now),
            )
        elif not row["active"]:
            # 重新生效：先归档旧行，再版本 +1 激活
            self._archive(row, "回避关系恢复")
            self.conn.execute(
                "UPDATE avoidances SET active=1, reason=?, version=version+1, updated_at=? "
                "WHERE inspector_id=? AND institution_id=?",
                (reason, now, inspector_id, institution_id),
            )
        else:
            self.conn.execute(
                "UPDATE avoidances SET reason=?, updated_at=? WHERE inspector_id=? AND institution_id=?",
                (reason, now, inspector_id, institution_id),
            )
        return self.get(inspector_id, institution_id)

    def remove(self, inspector_id: str, institution_id: str, note: str = "临时回避解除") -> None:
        row = self.get_row(inspector_id, institution_id)
        if row is not None and row["active"]:
            self._archive(row, note)
            self.conn.execute(
                "UPDATE avoidances SET active=0, version=version+1, updated_at=? "
                "WHERE inspector_id=? AND institution_id=?",
                (utcnow(), inspector_id, institution_id),
            )

    def get_row(self, inspector_id: str, institution_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM avoidances WHERE inspector_id=? AND institution_id=?",
            (inspector_id, institution_id),
        ).fetchone()

    def get(self, inspector_id: str, institution_id: str) -> dict[str, Any]:
        row = self.get_row(inspector_id, institution_id)
        if row is None:
            raise NotFound("回避关系不存在")
        return dict(row)

    def list_for_institution(self, institution_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM avoidances WHERE institution_id=? AND active=1 ORDER BY inspector_id",
            (institution_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def is_avoiding(self, inspector_id: str, institution_id: str) -> bool:
        row = self.get_row(inspector_id, institution_id)
        return row is not None and bool(row["active"])

    def _archive(self, row: sqlite3.Row, note: str) -> None:
        self.conn.execute(
            """INSERT INTO avoidances_history
               (inspector_id, institution_id, reason, active, version, updated_at, archived_at, change_note)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                row["inspector_id"], row["institution_id"], row["reason"], row["active"],
                row["version"], row["updated_at"], utcnow(), note,
            ),
        )


class RegionCapacityRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def set(self, region: str, quarter: str, capacity: int, note: str = "区域容量设置") -> dict[str, Any]:
        now = utcnow()
        row = self.conn.execute(
            "SELECT * FROM region_capacities WHERE region=? AND quarter=?", (region, quarter)
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO region_capacities (region, quarter, capacity, version, updated_at) "
                "VALUES (?,?,?,1,?)",
                (region, quarter, int(capacity), now),
            )
        else:
            self.conn.execute(
                """INSERT INTO region_capacities_history
                   (region, quarter, capacity, version, updated_at, archived_at, change_note)
                   VALUES (?,?,?,?,?,?,?)""",
                (region, quarter, row["capacity"], row["version"], row["updated_at"], now, note),
            )
            self.conn.execute(
                "UPDATE region_capacities SET capacity=?, version=version+1, updated_at=? "
                "WHERE region=? AND quarter=?",
                (int(capacity), now, region, quarter),
            )
        return self.get(region, quarter)

    def get(self, region: str, quarter: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM region_capacities WHERE region=? AND quarter=?", (region, quarter)
        ).fetchone()
        if row is None:
            raise NotFound(f"区域容量未配置：{region}/{quarter}")
        return dict(row)

    def list(self, quarter: str | None = None) -> list[dict[str, Any]]:
        if quarter:
            rows = self.conn.execute(
                "SELECT * FROM region_capacities WHERE quarter=? ORDER BY region", (quarter,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM region_capacities ORDER BY quarter, region"
            ).fetchall()
        return [dict(r) for r in rows]


class SamplingRuleRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        self.conn.execute(
            """INSERT INTO sampling_rules
               (id, name, risk_levels, min_score, industries, required_tag,
                required_qualification, quota, priority, active, version, updated_at)
               VALUES (:id,:name,:risk_levels,:min_score,:industries,:required_tag,
                       :required_qualification,:quota,:priority,:active,1,:now)""",
            {
                "id": data["id"],
                "name": data["name"],
                "risk_levels": dumps(data.get("risk_levels", ["高", "中", "低"])),
                "min_score": float(data.get("min_score", 0)),
                "industries": dumps(data.get("industries", [])),
                "required_tag": data.get("required_tag", ""),
                "required_qualification": data.get("required_qualification", ""),
                "quota": int(data["quota"]),
                "priority": int(data.get("priority", 100)),
                "active": int(data.get("active", 1)),
                "now": utcnow(),
            },
        )
        return self.get(data["id"])

    def get(self, rule_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM sampling_rules WHERE id=?", (rule_id,)).fetchone()
        if row is None:
            raise NotFound(f"抽检规则不存在：{rule_id}")
        return self._to_dict(row)

    def list(self, active_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sampling_rules"
        if active_only:
            sql += " WHERE active=1"
        rows = self.conn.execute(sql + " ORDER BY priority, id").fetchall()
        return [self._to_dict(r) for r in rows]

    def update(
        self, rule_id: str, expected_version: int, changes: dict[str, Any], note: str = "规则更新"
    ) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM sampling_rules WHERE id=?", (rule_id,)).fetchone()
        if row is None:
            raise NotFound(f"抽检规则不存在：{rule_id}")
        if row["version"] != expected_version:
            raise VersionConflict(
                f"规则版本冲突：当前 v{row['version']}，提交基于 v{expected_version}"
            )
        _history_archive(self.conn, "sampling_rules", row, note)
        self.conn.execute(
            """UPDATE sampling_rules SET name=:name, risk_levels=:risk_levels,
                  min_score=:min_score, industries=:industries, required_tag=:required_tag,
                  required_qualification=:req_qual, quota=:quota, priority=:priority,
                  active=:active, version=version+1, updated_at=:now WHERE id=:id""",
            {
                "id": rule_id,
                "name": changes.get("name", row["name"]),
                "risk_levels": dumps(changes["risk_levels"]) if "risk_levels" in changes else row["risk_levels"],
                "min_score": float(changes["min_score"]) if "min_score" in changes else row["min_score"],
                "industries": dumps(changes["industries"]) if "industries" in changes else row["industries"],
                "required_tag": changes.get("required_tag", row["required_tag"]),
                "req_qual": changes.get("required_qualification", row["required_qualification"]),
                "quota": int(changes["quota"]) if "quota" in changes else row["quota"],
                "priority": int(changes["priority"]) if "priority" in changes else row["priority"],
                "active": int(changes["active"]) if "active" in changes else row["active"],
                "now": utcnow(),
            },
        )
        return self.get(rule_id)

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for k in ("risk_levels", "industries"):
            d[k] = loads(d[k])
        d["min_score"] = float(d["min_score"])
        d["quota"] = int(d["quota"])
        d["priority"] = int(d["priority"])
        d["active"] = bool(d["active"])
        return d
