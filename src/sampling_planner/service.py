"""计划服务：生命周期、原子锁定、改期、换人、停业级联、并发发布。"""
from __future__ import annotations

import re
import sqlite3
from datetime import date
from typing import Any

from .config import (
    ITEM_DROPPED,
    ITEM_INSPECTOR_SWAPPED,
    ITEM_PENDING,
    ITEM_RESCHEDULED,
    LOCK_INSPECTOR,
    LOCK_INSTITUTION,
    LOCK_REGION,
    PLAN_CANDIDATES_READY,
    PLAN_CANCELLED,
    PLAN_CONFIRMED,
    PLAN_DRAFT,
    PLAN_PUBLISHED,
    PLAN_STATES,
    PLAN_TRANSITIONS,
    REASON_TEXT,
)
from .database import (
    NotFound,
    StateConflict,
    VersionConflict,
    immediate_tx,
    loads,
    utcnow,
)
from .engine import GenerationContext, default_schedule, generate_candidates, parse_quarter

QUARTER_RE = re.compile(r"^\d{4}Q[1-4]$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PlanService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ---------------- 计划创建与查询 ----------------
    def create_plan(self, plan_id: str, quarter: str, target_count: int = 0) -> dict[str, Any]:
        if not QUARTER_RE.match(quarter):
            raise ValueError("季度格式应为 YYYYQn，如 2026Q4")
        now = utcnow()
        try:
            self.conn.execute(
                """INSERT INTO plans (id, quarter, state, target_count, created_at, updated_at, version)
                   VALUES (?,?, '草稿', ?, ?, ?, 1)""",
                (plan_id, quarter, int(target_count), now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"计划 id 已存在：{plan_id}") from exc
        self._event(plan_id, "created", detail={"quarter": quarter})
        return self.get_plan(plan_id)

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFound(f"计划不存在：{plan_id}")
        return dict(row)

    def list_plans(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM plans ORDER BY quarter")]

    # ---------------- 候选生成（断点续跑） ----------------
    def generate(self, plan_id: str, batch_size: int = 100) -> dict[str, Any]:
        """推进一批候选生成；每批独立事务提交，崩溃后可续跑。"""
        with immediate_tx(self.conn):
            return generate_candidates(self.conn, plan_id, batch_size=batch_size)

    def resume_generation(self, plan_id: str, batch_size: int = 100, max_batches: int = 1000) -> dict[str, Any]:
        """重启后续跑：循环推进直到完成（供 API 与 CLI 调用）。"""
        last: dict[str, Any] = {"finished": False}
        for _ in range(max_batches):
            last = self.generate(plan_id, batch_size=batch_size)
            if last["finished"]:
                break
        return last

    def candidates(self, plan_id: str) -> list[dict[str, Any]]:
        self.get_plan(plan_id)
        rows = self.conn.execute(
            """SELECT c.*, i.name AS institution_name, i.region, i.risk_level, i.risk_score,
                      insp.name AS inspector_name
               FROM candidates c
               JOIN institutions i ON i.id = c.institution_id
               LEFT JOIN inspectors insp ON insp.id = c.proposed_inspector_id
               WHERE c.plan_id=? ORDER BY c.seq""",
            (plan_id,),
        ).fetchall()
        return [self._candidate_dict(r) for r in rows]

    def explanations(self, plan_id: str) -> list[dict[str, Any]]:
        """每个入选或未入选机构的结构化理由 + 中文说明。"""
        self.get_plan(plan_id)
        rows = self.conn.execute(
            """SELECT c.*, i.name AS institution_name, i.region, i.risk_level, i.risk_score,
                      i.status AS institution_status,
                      insp.name AS inspector_name, sr.name AS rule_name
               FROM candidates c
               JOIN institutions i ON i.id = c.institution_id
               LEFT JOIN inspectors insp ON insp.id = c.proposed_inspector_id
               LEFT JOIN sampling_rules sr ON sr.id = c.matched_rule_id
               WHERE c.plan_id=? ORDER BY c.selected DESC, c.seq""",
            (plan_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = self._candidate_dict(r)
            detail = d["reason_detail"] if isinstance(d["reason_detail"], dict) else {}
            d["explanation"] = _format_reason(d["reason_code"], detail)
            result.append(d)
        return result

    # ---------------- 确认并原子锁定 ----------------
    def confirm(self, plan_id: str, expected_version: int, actor: str = "") -> dict[str, Any]:
        """负责人确认：单事务把入选候选转为计划项并原子占用三类资源。

        冲突（机构被并发计划占用、检查员/区域容量被并发抢满、候选数据过期、版本不符）
        全部回滚并抛出异常，绝不部分成功。
        """
        with immediate_tx(self.conn):
            plan = self._locked_plan(plan_id)
            if plan["state"] != PLAN_CANDIDATES_READY:
                raise StateConflict(f"计划状态为 {plan['state']}，仅 {PLAN_CANDIDATES_READY} 可确认")
            if plan["version"] != expected_version:
                raise VersionConflict(
                    f"计划版本冲突：当前 v{plan['version']}，确认基于 v{expected_version}"
                )
            # 候选是否仍对应当前主数据
            current_token = GenerationContext(self.conn, plan["quarter"]).data_token()
            if current_token != plan["generation_token"]:
                raise VersionConflict("主数据已变更（风险快照/规则/检查员/回避/容量），请重新生成候选")

            selected = self.conn.execute(
                "SELECT * FROM candidates WHERE plan_id=? AND selected=1 ORDER BY rank_in_rule, seq",
                (plan_id,),
            ).fetchall()
            if not selected:
                raise StateConflict("没有入选候选，无法确认")

            quarter = plan["quarter"]
            now = utcnow()
            items: list[dict[str, Any]] = []
            try:
                for seq, cand in enumerate(selected):
                    inst = self.conn.execute(
                        "SELECT * FROM institutions WHERE id=?", (cand["institution_id"],)
                    ).fetchone()
                    insp = self.conn.execute(
                        "SELECT * FROM inspectors WHERE id=?", (cand["proposed_inspector_id"],)
                    ).fetchone()
                    item_id = f"{plan_id}-I{seq + 1:03d}"
                    scheduled = default_schedule(quarter, seq)
                    # 1) 机构季度唯一锁
                    self._insert_lock(
                        LOCK_INSTITUTION, f"institution:{quarter}:{inst['id']}", 0,
                        quarter, plan_id, item_id, now,
                    )
                    # 2) 检查员槽位锁
                    insp_slot = self._next_free_slot(
                        LOCK_INSPECTOR, f"inspector:{quarter}:{insp['id']}",
                        insp["quarterly_capacity"],
                    )
                    self._insert_lock(
                        LOCK_INSPECTOR, f"inspector:{quarter}:{insp['id']}", insp_slot,
                        quarter, plan_id, item_id, now,
                    )
                    # 3) 区域槽位锁
                    cap_row = self.conn.execute(
                        "SELECT capacity FROM region_capacities WHERE region=? AND quarter=?",
                        (inst["region"], quarter),
                    ).fetchone()
                    if cap_row is None:
                        raise StateConflict(f"区域 {inst['region']} 未配置 {quarter} 容量")
                    region_slot = self._next_free_slot(
                        LOCK_REGION, f"region:{quarter}:{inst['region']}", cap_row["capacity"]
                    )
                    self._insert_lock(
                        LOCK_REGION, f"region:{quarter}:{inst['region']}", region_slot,
                        quarter, plan_id, item_id, now,
                    )
                    self.conn.execute(
                        """INSERT INTO plan_items
                           (id, plan_id, institution_id, inspector_id, rule_id,
                            scheduled_date, item_state, seq, version, created_at, updated_at)
                           VALUES (?,?,?,?,?,?, '待检', ?, 1, ?, ?)""",
                        (item_id, plan_id, inst["id"], insp["id"], cand["matched_rule_id"],
                         scheduled, seq + 1, now, now),
                    )
                    items.append({"item_id": item_id, "date": scheduled})
            except sqlite3.IntegrityError as exc:
                # 唯一约束冲突 = 并发计划抢先占用；事务整体回滚
                raise VersionConflict(f"资源已被其他计划并发占用：{exc}") from exc

            self._archive_plan(plan, "负责人确认，资源锁定")
            self.conn.execute(
                "UPDATE plans SET state='已确认', confirmed_at=?, version=version+1, updated_at=? "
                "WHERE id=?",
                (now, now, plan_id),
            )
            self._event(plan_id, "confirmed", actor, {"items": len(items)}, plan["version"], plan["version"] + 1)
            return {"plan": self.get_plan(plan_id), "items": items}

    # ---------------- 发布（乐观并发控制） ----------------
    def publish(self, plan_id: str, expected_version: int, actor: str = "") -> dict[str, Any]:
        with immediate_tx(self.conn):
            plan = self._locked_plan(plan_id)
            if plan["version"] != expected_version:
                raise VersionConflict(
                    f"计划版本冲突：当前 v{plan['version']}，发布基于 v{expected_version}"
                )
            if plan["state"] != PLAN_CONFIRMED:
                raise StateConflict(f"计划状态为 {plan['state']}，仅 {PLAN_CONFIRMED} 可发布")
            cur = self.conn.execute(
                "UPDATE plans SET state='已发布', published_at=?, version=version+1, updated_at=? "
                "WHERE id=? AND version=?",
                (utcnow(), utcnow(), plan_id, expected_version),
            )
            if cur.rowcount == 0:  # 极端并发兜底
                raise VersionConflict("发布并发冲突，请重试")
            fresh = self._locked_plan(plan_id)
            self._archive_plan(plan, "计划发布")
            self._event(plan_id, "published", actor, {}, expected_version, fresh["version"])
            return self.get_plan(plan_id)

    def cancel(self, plan_id: str, expected_version: int, actor: str = "") -> dict[str, Any]:
        """取消草稿/候选计划；已确认计划不允许直接取消（须先释放资源的归档流程）。"""
        with immediate_tx(self.conn):
            plan = self._locked_plan(plan_id)
            if plan["state"] not in (PLAN_DRAFT, PLAN_CANDIDATES_READY):
                raise StateConflict(f"计划状态为 {plan['state']}，不可取消")
            if plan["version"] != expected_version:
                raise VersionConflict("计划版本冲突")
            self._archive_plan(plan, "计划取消")
            self.conn.execute(
                "UPDATE plans SET state='已取消', version=version+1, updated_at=? WHERE id=?",
                (utcnow(), plan_id),
            )
            self._event(plan_id, "cancelled", actor, {}, plan["version"], plan["version"] + 1)
            return self.get_plan(plan_id)

    # ---------------- 改期 ----------------
    def reschedule_item(
        self, item_id: str, new_date: str, expected_version: int, actor: str = ""
    ) -> dict[str, Any]:
        if not DATE_RE.match(new_date):
            raise ValueError("日期格式应为 YYYY-MM-DD")
        with immediate_tx(self.conn):
            item = self._locked_item(item_id)
            if item["version"] != expected_version:
                raise VersionConflict(
                    f"计划项版本冲突：当前 v{item['version']}，提交基于 v{expected_version}"
                )
            if item["item_state"] == ITEM_DROPPED:
                raise StateConflict("计划项已因机构停业剔除，不能改期")
            plan = self._locked_plan(item["plan_id"])
            if plan["state"] not in (PLAN_CONFIRMED, PLAN_PUBLISHED):
                raise StateConflict(f"计划状态为 {plan['state']}，不能改期")
            _assert_date_in_quarter(new_date, plan["quarter"])
            old_date = item["scheduled_date"]
            self._archive_item(item, f"改期：{old_date} -> {new_date}（操作人 {actor or '未注明'}）")
            self.conn.execute(
                "UPDATE plan_items SET scheduled_date=?, item_state='已改期', "
                "version=version+1, updated_at=? WHERE id=?",
                (new_date, utcnow(), item_id),
            )
            self._event(item["plan_id"], "item_rescheduled", actor,
                        {"item": item_id, "from": old_date, "to": new_date},
                        expected_version, expected_version + 1)
            return self.get_item(item_id)

    # ---------------- 替换检查员 ----------------
    def swap_inspector(
        self, item_id: str, new_inspector_id: str, expected_version: int, actor: str = ""
    ) -> dict[str, Any]:
        with immediate_tx(self.conn):
            item = self._locked_item(item_id)
            if item["version"] != expected_version:
                raise VersionConflict(
                    f"计划项版本冲突：当前 v{item['version']}，提交基于 v{expected_version}"
                )
            if item["item_state"] == ITEM_DROPPED:
                raise StateConflict("计划项已剔除，不能换人")
            plan = self._locked_plan(item["plan_id"])
            if plan["state"] not in (PLAN_CONFIRMED, PLAN_PUBLISHED):
                raise StateConflict(f"计划状态为 {plan['state']}，不能换人")

            new_insp = self.conn.execute(
                "SELECT * FROM inspectors WHERE id=?", (new_inspector_id,)
            ).fetchone()
            if new_insp is None:
                raise NotFound(f"检查员不存在：{new_inspector_id}")
            if not new_insp["active"]:
                raise StateConflict(f"检查员 {new_inspector_id} 已停用")
            # 资格校验（按入选规则）
            if item["rule_id"]:
                rule = self.conn.execute(
                    "SELECT * FROM sampling_rules WHERE id=?", (item["rule_id"],)
                ).fetchone()
                if rule and rule["required_qualification"]:
                    quals = loads(new_insp["qualifications"])
                    if rule["required_qualification"] not in quals:
                        raise StateConflict(
                            f"检查员缺少规则要求资格：{rule['required_qualification']}"
                        )
            # 回避校验
            av = self.conn.execute(
                "SELECT 1 FROM avoidances WHERE inspector_id=? AND institution_id=? AND active=1",
                (new_inspector_id, item["institution_id"]),
            ).fetchone()
            if av:
                raise StateConflict(f"检查员 {new_inspector_id} 与该机构存在回避关系")

            quarter = plan["quarter"]
            try:
                slot = self._next_free_slot(
                    LOCK_INSPECTOR, f"inspector:{quarter}:{new_inspector_id}",
                    new_insp["quarterly_capacity"],
                )
            except StateConflict:
                raise StateConflict(
                    f"检查员 {new_inspector_id} 的 {quarter} 槽位已满，无法替换"
                )
            # 原子切换锁：删旧增新
            self.conn.execute(
                "DELETE FROM resource_locks WHERE lock_type=? AND ref_item_id=? AND ref_plan_id=?",
                (LOCK_INSPECTOR, item_id, item["plan_id"]),
            )
            self.conn.execute(
                """INSERT INTO resource_locks
                   (lock_type, lock_key, slot, quarter, ref_plan_id, ref_item_id, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (LOCK_INSPECTOR, f"inspector:{quarter}:{new_inspector_id}", slot,
                 quarter, item["plan_id"], item_id, utcnow()),
            )
            old_insp = item["inspector_id"]
            self._archive_item(
                item, f"换人：{old_insp} -> {new_inspector_id}（操作人 {actor or '未注明'}）"
            )
            self.conn.execute(
                "UPDATE plan_items SET inspector_id=?, item_state='已换人', "
                "version=version+1, updated_at=? WHERE id=?",
                (new_inspector_id, utcnow(), item_id),
            )
            self._event(item["plan_id"], "inspector_swapped", actor,
                        {"item": item_id, "from": old_insp, "to": new_inspector_id},
                        expected_version, expected_version + 1)
            return self.get_item(item_id)

    # ---------------- 机构停业（级联释放，保留版本） ----------------
    def suspend_institution(
        self, institution_id: str, expected_version: int | None, actor: str = ""
    ) -> dict[str, Any]:
        """停业主数据版本 +1；已确认/已发布计划中的待办项标记剔除（版本 +1）并释放全部资源锁。"""
        from .repositories import InstitutionRepository

        with immediate_tx(self.conn):
            repo = InstitutionRepository(self.conn)
            if expected_version is None:
                current = repo.get(institution_id)
                expected_version = current["version"]
            inst = repo.suspend(institution_id, expected_version,
                                note=f"机构停业（操作人 {actor or '未注明'}）")

            active_items = self.conn.execute(
                """SELECT pi.* FROM plan_items pi JOIN plans p ON p.id = pi.plan_id
                   WHERE pi.institution_id=? AND pi.item_state != '已剔除'
                     AND p.state IN ('已确认','已发布')""",
                (institution_id,),
            ).fetchall()
            dropped = []
            for item in active_items:
                self._archive_item(item, f"机构 {institution_id} 停业，计划项剔除")
                self.conn.execute(
                    "UPDATE plan_items SET item_state='已剔除', version=version+1, updated_at=? "
                    "WHERE id=?",
                    (utcnow(), item["id"]),
                )
                self.conn.execute(
                    "DELETE FROM resource_locks WHERE ref_item_id=? AND ref_plan_id=?",
                    (item["id"], item["plan_id"]),
                )
                dropped.append(item["id"])
                self._event(item["plan_id"], "item_dropped_suspension", actor,
                            {"item": item["id"], "institution": institution_id},
                            item["version"], item["version"] + 1)
            return {"institution": inst, "dropped_items": dropped}

    # ---------------- 计划项与历史查询 ----------------
    def list_items(self, plan_id: str) -> list[dict[str, Any]]:
        self.get_plan(plan_id)
        rows = self.conn.execute(
            """SELECT pi.*, i.name AS institution_name, insp.name AS inspector_name
               FROM plan_items pi
               JOIN institutions i ON i.id=pi.institution_id
               JOIN inspectors insp ON insp.id=pi.inspector_id
               WHERE pi.plan_id=? ORDER BY pi.seq""",
            (plan_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_item(self, item_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            """SELECT pi.*, i.name AS institution_name, insp.name AS inspector_name
               FROM plan_items pi
               JOIN institutions i ON i.id=pi.institution_id
               JOIN inspectors insp ON insp.id=pi.inspector_id
               WHERE pi.id=?""",
            (item_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"计划项不存在：{item_id}")
        return dict(row)

    def item_history(self, item_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM plan_items_history WHERE id=? ORDER BY version", (item_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def plan_history(self, plan_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM plans_history WHERE plan_id=? ORDER BY version", (plan_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def events(self, plan_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM plan_events WHERE plan_id=? ORDER BY id", (plan_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 内部工具 ----------------
    def _locked_plan(self, plan_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFound(f"计划不存在：{plan_id}")
        return row

    def _locked_item(self, item_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM plan_items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFound(f"计划项不存在：{item_id}")
        return row

    def _insert_lock(self, lock_type: str, key: str, slot: int, quarter: str,
                     plan_id: str, item_id: str, now: str) -> None:
        self.conn.execute(
            """INSERT INTO resource_locks
               (lock_type, lock_key, slot, quarter, ref_plan_id, ref_item_id, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (lock_type, key, slot, quarter, plan_id, item_id, now),
        )

    def _next_free_slot(self, lock_type: str, key: str, capacity: int) -> int:
        used = {
            r["slot"]
            for r in self.conn.execute(
                "SELECT slot FROM resource_locks WHERE lock_type=? AND lock_key=?",
                (lock_type, key),
            ).fetchall()
        }
        for slot in range(capacity):
            if slot not in used:
                return slot
        raise StateConflict(f"{key} 容量 {capacity} 已满")

    def _archive_plan(self, row: sqlite3.Row, note: str) -> None:
        self.conn.execute(
            """INSERT INTO plans_history
               (plan_id, quarter, state, target_count, generation_total, generation_done,
                generation_cursor, generation_token, confirmed_at, published_at,
                created_at, updated_at, version, archived_at, change_note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["id"], row["quarter"], row["state"], row["target_count"],
             row["generation_total"], row["generation_done"], row["generation_cursor"],
             row["generation_token"], row["confirmed_at"], row["published_at"],
             row["created_at"], row["updated_at"], row["version"], utcnow(), note),
        )

    def _archive_item(self, row: sqlite3.Row, note: str) -> None:
        self.conn.execute(
            """INSERT INTO plan_items_history
               (version, id, plan_id, institution_id, inspector_id, rule_id, scheduled_date,
                item_state, seq, created_at, updated_at, archived_at, change_note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["version"], row["id"], row["plan_id"], row["institution_id"],
             row["inspector_id"], row["rule_id"], row["scheduled_date"], row["item_state"],
             row["seq"], row["created_at"], row["updated_at"], utcnow(), note),
        )

    def _event(self, plan_id: str, event: str, actor: str = "", detail: dict[str, Any] | None = None,
               version_from: int | None = None, version_to: int | None = None) -> None:
        self.conn.execute(
            """INSERT INTO plan_events (plan_id, event, actor, detail, version_from, version_to, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (plan_id, event, actor, _dumps(detail or {}), version_from, version_to, utcnow()),
        )

    @staticmethod
    def _candidate_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["selected"] = bool(d["selected"])
        if d.get("reason_detail"):
            try:
                d["reason_detail"] = loads(d["reason_detail"])
            except (ValueError, TypeError):
                pass
        return d


def _assert_date_in_quarter(value: str, quarter: str) -> None:
    y, qn = parse_quarter(quarter)
    d = date.fromisoformat(value)
    if d.year != y or (d.month - 1) // 3 + 1 != qn:
        raise ValueError(f"日期 {value} 不属于季度 {quarter}")


def _dumps(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _format_reason(code: str, detail: dict[str, Any]) -> str:
    if not code:
        return "评估通过但最终未分配（请检查规则名额与资源竞争）"
    if code == "REGION_CAPACITY" and detail.get("configured") is False:
        return f"所在区域「{detail.get('region', '')}」未配置本季度抽检容量"
    template = REASON_TEXT.get(code, code)
    try:
        return template.format(**detail)
    except KeyError:
        return template
