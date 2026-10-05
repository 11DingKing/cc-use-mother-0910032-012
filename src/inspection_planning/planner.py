"""候选计划生成器（确定性、可解释）。

规则（领域契约中的"风险抽样规则 / 检查员回避"）：

1. 仅纳入持有风险快照且在营的机构；停业、缺快照直接判为未入选并说明。
2. 排序确定：风险等级 高 > 中 > 低，同级按风险分降序，再按机构编号升序。
   高风险机构因此总是先占用有限的区域容量，低风险仅在容量有余时入选。
3. 逐条按排序贪心：
   - 区域容量（已发布/已锁定的其他计划占用 + 本次已入选）达到上限 => 未入选；
   - 无可派检查员（停用 / 资格不符 / 区域不符 / 回避生效 / 季度内已被占用 /
     本次已承担任务）=> 未入选，并给出逐项计数；
   - 否则入选，检查员在合格者中按"本次负载最少、编号最小"确定，过程可复现。
4. 每个入选 / 未入选结论都带结构化理由；同一输入必定得到同一结果。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    Candidate,
    DECISION_REJECTED,
    DECISION_SELECTED,
    Inspector,
    Institution,
)

RISK_PRIORITY = {"高": 0, "中": 1, "低": 2}
RISK_UNKNOWN = 9


@dataclass
class PlanningContext:
    """一次候选生成运行冻结下来的输入视图。"""

    quarter: str
    inspection_type: str
    institutions: list[Institution]
    inspectors: list[Inspector]
    # (检查员, 机构) -> 回避原因；仅包含"最新版本仍生效"的回避
    recusals: dict[tuple[str, str], str]
    # region -> 容量；未配置的区域视为不限
    capacities: dict[str, int]
    # 其他已锁定/已发布计划在本季度的占用
    occupied_institutions: dict[str, tuple[str, str | None]]
    # inspector_id -> 其他计划已占用的季度工作量
    external_inspector_loads: dict[str, int]
    external_region_counts: dict[str, int]


@dataclass
class _RunState:
    """生成过程中的增量状态（断点续跑时由已处理条目重建）。"""

    selected_keys: set[str] = field(default_factory=set)
    chosen_inspectors: dict[str, str] = field(default_factory=dict)  # inst -> insp
    region_selected: dict[str, int] = field(default_factory=dict)


class Planner:
    """无状态规则引擎；状态由调用方（service）持有并持久化。"""

    # ---- 排序 -------------------------------------------------------

    def ranked_institutions(self, ctx: PlanningContext) -> list[Institution]:
        def key(inst: Institution) -> tuple[int, float, str]:
            if inst.risk is None:
                return (RISK_UNKNOWN, 0.0, inst.institution_id)
            return (
                RISK_PRIORITY.get(inst.risk.risk_level, RISK_UNKNOWN),
                -float(inst.risk.risk_score),
                inst.institution_id,
            )

        return sorted(ctx.institutions, key=key)

    # ---- 检查员甄别 -------------------------------------------------

    def evaluate_inspectors(
        self, ctx: PlanningContext, inst: Institution
    ) -> tuple[list[Inspector], dict[str, list[str]]]:
        """返回（静态合格检查员列表, 被排除检查员 -> 原因列表）。

        只检查与具体任务无关的硬资格：停用、检查类型资格、服务区域、回避。
        季度工作量（有限检查力量）在派位阶段按检查员容量统一核算。
        同一名检查员可能因多个原因被排除，全部记录以便接口解释。
        """
        eligible: list[Inspector] = []
        excluded: dict[str, list[str]] = {}
        for insp in ctx.inspectors:
            reasons: list[str] = []
            if not insp.active:
                reasons.append("检查员已停用")
            if ctx.inspection_type not in insp.qualifications:
                reasons.append(
                    f"不具备{ctx.inspection_type}检查资格"
                    f"（现有资格：{'、'.join(insp.qualifications) or '无'}）"
                )
            if "*" not in insp.regions and inst.region not in insp.regions:
                reasons.append(
                    f"不服务该区域（服务区域：{'、'.join(insp.regions) or '无'}）"
                )
            recusal_reason = ctx.recusals.get((insp.inspector_id, inst.institution_id))
            if recusal_reason is not None:
                reasons.append(f"存在生效回避（{recusal_reason}）")
            if reasons:
                excluded[insp.inspector_id] = reasons
            else:
                eligible.append(insp)
        eligible.sort(key=lambda x: x.inspector_id)
        return eligible, excluded

    # ---- 单机构评估（可重入）---------------------------------------

    def evaluate_institution(
        self,
        ctx: PlanningContext,
        inst: Institution,
        rank: int,
        state: _RunState,
    ) -> dict:
        """评估排序中的一个机构；返回可落库的候选条目，并更新 state。

        该函数只依赖 ctx 与此前已入选条目，因此断点续跑按同一顺序重放时，
        结果与一次性跑完完全一致。
        """
        base = {
            "institution_id": inst.institution_id,
            "rank": rank,
        }

        def reject(reasons: list[str]) -> dict:
            return {
                **base,
                "decision": DECISION_REJECTED,
                "reasons": reasons,
                "proposed_inspector_id": None,
                "eligible_inspector_ids": [],
            }

        # 1) 停业
        if not inst.active:
            closed_at = (
                f"，停业版本 v{inst.closed_version}"
                if inst.closed_version is not None
                else ""
            )
            return reject([f"机构已停业{closed_at}，不纳入本季度抽检"])

        # 2) 缺少风险快照
        if inst.risk is None:
            return reject(["缺少风险快照，无法评估风险等级与抽样优先级"])
        risk = inst.risk

        # 3) 机构已被其他计划占用（不同小组重复占用的硬约束）
        occ_inst = ctx.occupied_institutions.get(inst.institution_id)
        if occ_inst is not None:
            return reject([
                f"机构本季度已被计划{occ_inst[0]}锁定"
                f"（检查员{occ_inst[1] or '未指派'}），避免跨小组重复占用",
            ])

        # 4) 区域容量
        cap = ctx.capacities.get(inst.region)
        external = ctx.external_region_counts.get(inst.region, 0)
        internal = state.region_selected.get(inst.region, 0)
        used = external + internal
        if cap is not None and used >= cap:
            region_by_id = {i.institution_id: i.region for i in ctx.institutions}
            slot_holders = sorted(
                k for k in state.selected_keys
                if region_by_id.get(k) == inst.region
            )
            detail = (
                f"本次已入选占槽机构：{'、'.join(slot_holders)}"
                if slot_holders
                else "容量全部由其他已锁定计划占用"
            )
            return reject([
                f"区域{inst.region}抽检容量已满（占用{used}/{cap}）",
                detail,
                f"风险排序第{rank}，排在容量之后未获名额",
            ])

        # 5) 检查员甄别（静态资格）
        eligible, excluded_map = self.evaluate_inspectors(ctx, inst)

        # 6) 季度工作量约束：外部计划已占用 + 本次已派任务 < 检查员季度容量
        available: list[Inspector] = []
        load_excluded: dict[str, list[str]] = {}
        for insp in eligible:
            external_load = ctx.external_inspector_loads.get(insp.inspector_id, 0)
            internal_load = sum(
                1 for i in state.chosen_inspectors.values() if i == insp.inspector_id
            )
            total_load = external_load + internal_load
            if total_load >= insp.quarterly_capacity:
                parts = []
                if external_load:
                    parts.append(f"其他已锁定计划占用{external_load}项")
                if internal_load:
                    parts.append(f"本次候选已派{internal_load}项")
                load_excluded[insp.inspector_id] = [
                    f"季度检查工作量已满（{'、'.join(parts)}，"
                    f"容量{insp.quarterly_capacity}项）"
                ]
            else:
                available.append(insp)

        if not available:
            reasons = ["无可用检查员（有限检查力量已占满）"]
            # 汇总各类排除原因，给出确定性的解释
            buckets: dict[str, list[str]] = {}
            for insp_id, why in excluded_map.items():
                for w in why:
                    buckets.setdefault(w, []).append(insp_id)
            for insp_id, why in load_excluded.items():
                for w in why:
                    buckets.setdefault(w, []).append(insp_id)
            for w in sorted(buckets):
                ids = "、".join(sorted(buckets[w]))
                reasons.append(f"{w}：{ids}")
            reasons.append(f"风险排序第{rank}，因无可派检查员未入选")
            return reject(reasons)

        # 7) 入选：在可用者中按「已承担总工作量最少、编号最小」派位
        def total_load_of(insp: Inspector) -> int:
            return (
                ctx.external_inspector_loads.get(insp.inspector_id, 0)
                + sum(
                    1
                    for i in state.chosen_inspectors.values()
                    if i == insp.inspector_id
                )
            )

        chosen = min(
            available, key=lambda x: (total_load_of(x), x.inspector_id)
        )
        chosen_load_before = total_load_of(chosen)
        state.selected_keys.add(inst.institution_id)
        state.chosen_inspectors[inst.institution_id] = chosen.inspector_id
        state.region_selected[inst.region] = internal + 1

        after = external + internal + 1
        cap_text = (
            f"区域{inst.region}容量可承载（拟选后{after}/{cap}）"
            if cap is not None
            else f"区域{inst.region}未配置容量上限，按不限处理（拟选后{after}个任务）"
        )
        factors = list(risk.risk_factors)
        reasons = [
            f"风险等级{risk.risk_level}、风险分{risk.risk_score:g}，"
            f"按风险优先规则排序第{rank}",
            *[f"风险因子：{f}" for f in factors],
            cap_text,
            f"拟派检查员{chosen.name}（{chosen.inspector_id}）："
            f"具备{ctx.inspection_type}资格、服务区域匹配、无生效回避，"
            f"派位后季度工作量{chosen_load_before + 1}/{chosen.quarterly_capacity}",
        ]
        if chosen_load_before > 0:
            reasons.append(
                f"该检查员季度内已承担{chosen_load_before}项任务，仍在容量内"
            )
        if len(available) > 1:
            reasons.append(
                f"合格且有余量的检查员共{len(available)}人，按负载均衡规则"
                f"（工作量最少、编号最小）选定{chosen.inspector_id}"
            )
        return {
            **base,
            "decision": DECISION_SELECTED,
            "reasons": reasons,
            "proposed_inspector_id": chosen.inspector_id,
            "eligible_inspector_ids": [x.inspector_id for x in available],
        }

    # ---- 全量运行（用于不走断点的场景/测试） ------------------------

    def run(self, ctx: PlanningContext) -> list[dict]:
        state = _RunState()
        items: list[dict] = []
        for rank, inst in enumerate(self.ranked_institutions(ctx), start=1):
            items.append(self.evaluate_institution(ctx, inst, rank, state))
        return items

    # ---- 条目 -> 对外 Candidate -------------------------------------

    @staticmethod
    def to_candidate(
        item: dict,
        institutions: dict[str, Institution],
        inspectors: dict[str, Inspector],
    ) -> Candidate:
        inst = institutions[item["institution_id"]]
        proposed = inspectors.get(item["proposed_inspector_id"] or "")
        return Candidate(
            institution_id=inst.institution_id,
            institution_name=inst.name,
            region=inst.region,
            risk_level=inst.risk.risk_level if inst.risk else "未知",
            risk_score=inst.risk.risk_score if inst.risk else 0.0,
            decision=item["decision"],
            reasons=list(item["reasons"]),
            proposed_inspector_id=None if proposed is None else proposed.inspector_id,
            proposed_inspector_name=None if proposed is None else proposed.name,
            eligible_inspector_ids=list(item["eligible_inspector_ids"]),
            rank=item["rank"],
        )
