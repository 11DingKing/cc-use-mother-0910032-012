"""候选生成引擎：规则匹配、贪心分配、逐机构可解释原因，支持断点续跑。"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import (
    DEFAULT_SNAPSHOT_MAX_AGE_DAYS,
    LOCK_INSTITUTION,
    LOCK_INSPECTOR,
    LOCK_REGION,
    REASON_ALREADY_PLANNED,
    REASON_AVOIDANCE,
    REASON_BELOW_CUTOFF,
    REASON_INSPECTOR_CAPACITY,
    REASON_INSTITUTION_SUSPENDED,
    REASON_NO_QUALIFIED_INSPECTOR,
    REASON_REGION_CAPACITY,
    REASON_RULE_FILTERED,
    REASON_SELECTED,
    REASON_SNAPSHOT_STALE,
)
from .database import dumps, utcnow
from .repositories import (
    AvoidanceRepository,
    InstitutionRepository,
    InspectorRepository,
    RegionCapacityRepository,
    SamplingRuleRepository,
)

RISK_ORDER = {"高": 0, "中": 1, "低": 2}


def parse_quarter(quarter: str) -> tuple[int, int]:
    """'2026Q4' -> (2026, 4)。"""
    year, q = quarter.upper().split("Q")
    y, qn = int(year), int(q)
    if not 1 <= qn <= 4:
        raise ValueError("季度格式应为 YYYYQn，如 2026Q4")
    return y, qn


def quarter_start(quarter: str) -> datetime:
    y, qn = parse_quarter(quarter)
    return datetime(y, (qn - 1) * 3 + 1, 1, tzinfo=timezone.utc)


def default_schedule(quarter: str, seq: int) -> str:
    """确认时未显式给日期则按季度内每周三排期（第一个周三起）。"""
    first = quarter_start(quarter)
    offset_to_wed = (2 - first.weekday()) % 7  # weekday(): 周一=0 … 周三=2
    day = first + timedelta(days=offset_to_wed + 7 * seq)
    return day.strftime("%Y-%m-%d")


class GenerationContext:
    """加载一次生成所需的全部主数据快照。"""

    def __init__(self, conn: sqlite3.Connection, quarter: str) -> None:
        self.conn = conn
        self.quarter = quarter
        self.institutions = InstitutionRepository(conn).list()
        self.rules = SamplingRuleRepository(conn).list(active_only=True)
        self.inspectors = InspectorRepository(conn).list()
        self.avoid_repo = AvoidanceRepository(conn)
        cap_rows = RegionCapacityRepository(conn).list(quarter)
        self.region_capacity = {r["region"]: int(r["capacity"]) for r in cap_rows}
        # 已确认/已发布计划持有的本季度锁用量
        self.used_institution: set[str] = set()
        self.used_inspector: dict[str, int] = {}
        self.used_region: dict[str, int] = {}
        for row in conn.execute(
            "SELECT lock_type, lock_key FROM resource_locks WHERE quarter=?", (quarter,)
        ):
            lt, key = row["lock_type"], row["lock_key"]
            if lt == LOCK_INSTITUTION:
                self.used_institution.add(key.rsplit(":", 1)[-1])
            elif lt == LOCK_INSPECTOR:
                insp = key.rsplit(":", 1)[-1]
                self.used_inspector[insp] = self.used_inspector.get(insp, 0) + 1
            elif lt == LOCK_REGION:
                region = key.rsplit(":", 1)[-1]
                self.used_region[region] = self.used_region.get(region, 0) + 1

    def data_token(self) -> str:
        """主数据指纹：任何机构/规则/检查员/回避/容量变更都会使旧候选失效。"""
        h = hashlib.sha256()
        for table in (
            "institutions", "sampling_rules", "inspectors",
            "avoidances", "region_capacities",
        ):
            row = self.conn.execute(
                f"SELECT COUNT(*) c, COALESCE(SUM(version),0) v, COALESCE(MAX(updated_at),'') u "
                f"FROM {table}"
            ).fetchone()
            h.update(f"{table}:{row['c']}:{row['v']}:{row['u']}".encode())
        h.update(self.quarter.encode())
        return h.hexdigest()[:16]


def _is_snapshot_fresh(snapshot_at: str, max_age_days: int) -> bool:
    ts = datetime.strptime(snapshot_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - ts <= timedelta(days=max_age_days)


def _match_rule(inst: dict[str, Any], rules: list[dict[str, Any]]) -> dict[str, Any] | None:
    """返回命中的最高优先级规则；规则表已按 priority, id 排序。"""
    for rule in rules:
        if inst["risk_level"] not in rule["risk_levels"]:
            continue
        if inst["risk_score"] < rule["min_score"]:
            continue
        if rule["industries"] and inst["industry"] not in rule["industries"]:
            continue
        if rule["required_tag"] and rule["required_tag"] not in inst["tags"]:
            continue
        return rule
    return None


def _choose_inspector(
    ctx: GenerationContext,
    inst: dict[str, Any],
    qualification: str,
    sim_inspector_used: dict[str, int],
) -> tuple[str | None, str, str]:
    """返回 (检查员id|None, 原因码, 说明)。原因码为空串表示成功。"""
    qualified = [
        i for i in ctx.inspectors
        if i["active"] and (not qualification or qualification in i["qualifications"])
    ]
    if not qualification:
        # 规则无资格要求：所有在职检查员都算合格
        qualified = [i for i in ctx.inspectors if i["active"]]
    if not qualified:
        return None, REASON_NO_QUALIFIED_INSPECTOR, f"要求资格「{qualification or '通用'}」"

    avoiding = [i for i in qualified if ctx.avoid_repo.is_avoiding(i["id"], inst["id"])]
    eligible = [i for i in qualified if i not in avoiding]
    if not eligible:
        names = "、".join(f"{i['name']}({i['id']})" for i in avoiding)
        return None, REASON_AVOIDANCE, names

    for insp in sorted(eligible, key=lambda i: i["id"]):
        used = ctx.used_inspector.get(insp["id"], 0) + sim_inspector_used.get(insp["id"], 0)
        if used < insp["quarterly_capacity"]:
            return insp["id"], "", ""
    return None, REASON_INSPECTOR_CAPACITY, ""


def evaluate_institution(
    ctx: GenerationContext, inst: dict[str, Any]
) -> tuple[dict[str, Any] | None, str, str]:
    """阶段 A：单机构粗筛。返回 (命中规则|None, 原因码, 说明)。"""
    if inst["status"] == "停业":
        return None, REASON_INSTITUTION_SUSPENDED, ""
    if not _is_snapshot_fresh(inst["snapshot_at"], DEFAULT_SNAPSHOT_MAX_AGE_DAYS):
        return None, REASON_SNAPSHOT_STALE, str(DEFAULT_SNAPSHOT_MAX_AGE_DAYS)
    if inst["id"] in ctx.used_institution:
        plan = ctx.conn.execute(
            "SELECT ref_plan_id FROM resource_locks WHERE lock_type=? AND lock_key=?",
            (LOCK_INSTITUTION, f"institution:{ctx.quarter}:{inst['id']}"),
        ).fetchone()
        return None, REASON_ALREADY_PLANNED, plan["ref_plan_id"] if plan else ""
    rule = _match_rule(inst, ctx.rules)
    if rule is None:
        return None, REASON_RULE_FILTERED, ""
    return rule, "", ""


def generate_candidates(
    conn: sqlite3.Connection,
    plan_id: str,
    batch_size: int = 100,
) -> dict[str, Any]:
    """推进候选生成。阶段 A 逐机构批量提交（断点续跑），阶段 B 一次性分配。

    返回进度与（完成后的）入选数。
    """
    plan = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if plan is None:
        raise LookupError(plan_id)
    if plan["state"] not in ("草稿", "候选就绪"):
        raise ValueError(f"计划状态为 {plan['state']}，不能生成候选")

    quarter = plan["quarter"]
    ctx = GenerationContext(conn, quarter)
    token = ctx.data_token()

    # 全量重算的两种情形：
    # 1) 主数据指纹变化（风险快照/规则/检查员/回避/容量被改）
    # 2) 候选已就绪后再次触发（重新评估，以感知其他小组新确认的资源锁）
    need_reset = (
        (plan["generation_token"] and plan["generation_token"] != token)
        or plan["state"] == "候选就绪"
    )
    if need_reset:
        conn.execute("DELETE FROM candidates WHERE plan_id=?", (plan_id,))
        conn.execute(
            "UPDATE plans SET state='草稿', generation_total=?, generation_done=0, "
            "generation_cursor=NULL, generation_token=?, updated_at=? WHERE id=?",
            (len(ctx.institutions), token, utcnow(), plan_id),
        )
        plan = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    elif not plan["generation_token"]:
        conn.execute(
            "UPDATE plans SET generation_total=?, generation_token=?, updated_at=? WHERE id=?",
            (len(ctx.institutions), token, utcnow(), plan_id),
        )

    # ---------- 阶段 A：粗筛（按机构 id 稳定排序，按游标续跑） ----------
    ordered_ids = [i["id"] for i in sorted(ctx.institutions, key=lambda x: x["id"])]
    done = plan["generation_done"]
    remaining = ordered_ids[done:]
    batch = remaining[:batch_size]
    inst_by_id = {i["id"]: i for i in ctx.institutions}

    for offset, inst_id in enumerate(batch, start=1):
        inst = inst_by_id[inst_id]
        rule, code, extra = evaluate_institution(ctx, inst)
        if code:
            detail_params: dict[str, Any] = {}
            if code == REASON_SNAPSHOT_STALE:
                detail_params = {"days": extra}
            elif code == REASON_ALREADY_PLANNED:
                detail_params = {"plan": extra}
            conn.execute(
                """INSERT INTO candidates
                   (plan_id, institution_id, matched_rule_id, selected, reason_code,
                    reason_detail, proposed_inspector_id, rank_in_rule, seq)
                   VALUES (?,?,?,0,?,?,?,NULL,?)""",
                (
                    plan_id, inst_id, None, code,
                    dumps({**detail_params, "snapshot_at": inst["snapshot_at"]}),
                    None, done + offset,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO candidates
                   (plan_id, institution_id, matched_rule_id, selected, reason_code,
                    reason_detail, proposed_inspector_id, rank_in_rule, seq)
                   VALUES (?,?,?,0,'','',NULL,?,?)""",
                (plan_id, inst_id, rule["id"], done + offset, done + offset),
            )

    done += len(batch)
    finished = done >= len(ordered_ids)
    cursor = ordered_ids[done - 1] if done > 0 else None
    conn.execute(
        "UPDATE plans SET generation_done=?, generation_cursor=?, updated_at=? WHERE id=?",
        (done, cursor, utcnow(), plan_id),
    )

    if not finished:
        return {
            "finished": False,
            "generation_done": done,
            "generation_total": len(ordered_ids),
            "data_token": token,
        }

    # ---------- 阶段 B：按规则优先级 + 风险排序贪心分配（单事务） ----------
    _allocate(conn, plan_id, ctx)
    conn.execute(
        "UPDATE plans SET state='候选就绪', updated_at=? WHERE id=?", (utcnow(), plan_id)
    )
    selected = conn.execute(
        "SELECT COUNT(*) c FROM candidates WHERE plan_id=? AND selected=1", (plan_id,)
    ).fetchone()["c"]
    return {
        "finished": True,
        "generation_done": done,
        "generation_total": len(ordered_ids),
        "selected": selected,
        "data_token": token,
    }


def _allocate(conn: sqlite3.Connection, plan_id: str, ctx: GenerationContext) -> None:
    """对粗筛通过的机构按规则做名额/区域/检查员贪心分配，写回原因。"""
    sim_inspector_used: dict[str, int] = {}
    sim_region_used: dict[str, int] = {r: 0 for r in ctx.used_region}

    pending = conn.execute(
        "SELECT * FROM candidates WHERE plan_id=? AND matched_rule_id IS NOT NULL",
        (plan_id,),
    ).fetchall()
    inst_by_id = {i["id"]: i for i in ctx.institutions}
    rules_by_id = {r["id"]: r for r in ctx.rules}

    # 按规则 priority 分组，组内按风险分降序、风险等级、id 排序
    by_rule: dict[str, list[sqlite3.Row]] = {}
    for cand in pending:
        by_rule.setdefault(cand["matched_rule_id"], []).append(cand)

    ordered_rules = sorted(
        (rules_by_id[r] for r in by_rule if r in rules_by_id),
        key=lambda r: (r["priority"], r["id"]),
    )

    for rule in ordered_rules:
        ranked = sorted(
            by_rule[rule["id"]],
            key=lambda c: (
                -inst_by_id[c["institution_id"]]["risk_score"],
                RISK_ORDER[inst_by_id[c["institution_id"]]["risk_level"]],
                c["institution_id"],
            ),
        )
        selected_count = 0
        for rank, cand in enumerate(ranked, start=1):
            inst = inst_by_id[cand["institution_id"]]
            if selected_count >= rule["quota"]:
                conn.execute(
                    "UPDATE candidates SET reason_code=?, reason_detail=?, rank_in_rule=? "
                    "WHERE id=?",
                    (
                        REASON_BELOW_CUTOFF,
                        dumps({"rule": rule["name"], "quota": rule["quota"], "rank": rank}),
                        rank, cand["id"],
                    ),
                )
                continue
            # 区域容量
            cap = ctx.region_capacity.get(inst["region"])
            region_used = ctx.used_region.get(inst["region"], 0) + sim_region_used.get(
                inst["region"], 0
            )
            if cap is None or region_used >= cap:
                conn.execute(
                    "UPDATE candidates SET reason_code=?, reason_detail=?, rank_in_rule=? "
                    "WHERE id=?",
                    (
                        REASON_REGION_CAPACITY,
                        dumps({
                            "region": inst["region"],
                            "cap": cap if cap is not None else 0,
                            "configured": cap is not None,
                            "rank": rank,
                        }),
                        rank, cand["id"],
                    ),
                )
                continue
            # 检查员
            insp_id, reason, extra = _choose_inspector(
                ctx, inst, rule["required_qualification"], sim_inspector_used
            )
            if insp_id is None:
                detail: dict[str, Any] = {"rule": rule["name"], "rank": rank}
                if reason == REASON_AVOIDANCE:
                    detail["names"] = extra
                elif reason == REASON_NO_QUALIFIED_INSPECTOR:
                    detail["qualification"] = rule["required_qualification"] or "通用"
                conn.execute(
                    "UPDATE candidates SET reason_code=?, reason_detail=?, rank_in_rule=? "
                    "WHERE id=?",
                    (reason, dumps(detail), rank, cand["id"]),
                )
                continue
            # 入选
            sim_inspector_used[insp_id] = sim_inspector_used.get(insp_id, 0) + 1
            sim_region_used[inst["region"]] = sim_region_used.get(inst["region"], 0) + 1
            selected_count += 1
            conn.execute(
                "UPDATE candidates SET selected=1, reason_code=?, reason_detail=?, "
                "proposed_inspector_id=?, rank_in_rule=? WHERE id=?",
                (
                    REASON_SELECTED,
                    dumps({"rule": rule["name"], "inspector": insp_id, "rank": rank}),
                    insp_id, rank, cand["id"],
                ),
            )
