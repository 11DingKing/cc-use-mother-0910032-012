"""用例编排层。

把 store 的事务原语与 planner 的规则引擎组合成业务用例：

- 候选生成：冻结输入版本 -> 逐机构确定式评估 -> 断点落库（可重启续跑）
  -> 提交"候选待确认"计划版本；
- 负责人确认：单事务内复核回避/资格/占用/容量/停业，原子锁定全部资源，
  与并发的其他确认通过 SQLite 写锁串行化，失败者拿到确定性冲突理由；
- 改期 / 替换检查员 / 机构停业联动：均追加计划新版本，旧版本完整保留；
- 并发发布：已锁定计划发布，冲突时拒绝。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .models import (
    Assignment,
    Inspector,
    Institution,
    PLAN_ARCHIVED,
    PLAN_CANDIDATE,
    PLAN_DRAFT,
    PLAN_LOCKED,
    PLAN_PUBLISHED,
    TASK_CANCELLED,
    TASK_LOCKED,
    TASK_REPLACED,
    TASK_RESCHEDULED,
)
from .planner import Planner, PlanningContext, _RunState
from .store import ConflictError, NotFoundError, StateError, Store

BATCH_SIZE = 25  # 每批处理机构数后提交一次断点游标


class PlanningService:
    def __init__(self, store: Store, planner: Planner | None = None) -> None:
        self.store = store
        self.planner = planner or Planner()

    # ---- 输入视图 ---------------------------------------------------

    def _build_context(
        self,
        conn,
        quarter: str,
        inspection_type: str,
        exclude_plan: str | None,
    ) -> tuple[PlanningContext, int, dict[str, Institution], dict[str, Inspector]]:
        institutions = self.store.list_institutions(conn)
        inspectors = self.store.list_inspectors(conn)
        capacities = {
            region: cap
            for region, (cap, _v) in self.store.active_capacities(conn).items()
        }
        # 生效回避：每个 (检查员,机构) 取最新版本，lifted=0 才生效
        recusal_rows = self.store.list_recusal_versions(conn)
        latest_recusal: dict[tuple[str, str], dict[str, Any]] = {}
        for r in recusal_rows:
            key = (r["inspector_id"], r["institution_id"])
            old = latest_recusal.get(key)
            if old is None or r["version"] > old["version"]:
                latest_recusal[key] = r
        recusals = {
            key: r["reason"]
            for key, r in latest_recusal.items()
            if not r["lifted"]
        }
        occ = self.store.active_occupancy(conn, quarter, exclude_plan=exclude_plan)
        ctx = PlanningContext(
            quarter=quarter,
            inspection_type=inspection_type,
            institutions=institutions,
            inspectors=inspectors,
            recusals=recusals,
            capacities=capacities,
            occupied_institutions=occ["institutions"],
            external_inspector_loads=occ["inspectors"],
            external_region_counts=occ["region_counts"],
        )
        inst_map = {i.institution_id: i for i in institutions}
        insp_map = {i.inspector_id: i for i in inspectors}
        input_version = self.store._current_meta_version(conn)
        return ctx, input_version, inst_map, insp_map

    # ---- 候选生成（断点续跑）---------------------------------------

    def generate_candidates(
        self,
        plan_id: str,
        *,
        resume: bool = True,
        batch_size: int = BATCH_SIZE,
    ) -> dict[str, Any]:
        """生成（或续跑）候选。完成后返回候选清单与运行状态。

        - ``resume=True`` 且存在未完成运行：从断点继续；
        - 主数据在运行期间发生变化（回避/资格/快照/容量新版本）时，
          旧运行作废并按最新输入重新开始，保证候选解释与当前规则一致；
        - 每次调用只推进有限批数，配合持久化游标实现"重启后续跑"。
        """
        plan = self.store.get_plan(plan_id)
        if plan is None:
            raise NotFoundError(f"计划不存在：{plan_id}")
        if plan.status not in (PLAN_DRAFT, PLAN_CANDIDATE):
            raise StateError(f"计划当前状态为{plan.status}，不能重新生成候选")

        # 先在只读事务内确定输入与（可能存在的）运行
        with self.store.read_only() as conn:
            ctx, input_version, _i, _e = self._build_context(
                conn, plan.quarter, plan.inspection_type, exclude_plan=plan_id
            )
            existing = self.store.get_run_for_update(conn, plan_id)
        ranked = self.planner.ranked_institutions(ctx)

        run_id: str
        start_rank: int
        items: list[dict[str, Any]]
        if resume and existing is not None and existing["state"] == "running":
            if int(existing["input_version"]) == input_version:
                run_id = existing["run_id"]
                start_rank = int(existing["processed"])
                items = self.store.load_run_items(run_id)
            else:
                # 输入已变：旧运行作废后重开
                run_id = uuid.uuid4().hex
                start_rank = 0
                items = []
        else:
            run_id = uuid.uuid4().hex
            start_rank = 0
            items = []

        if start_rank == 0 and not items:
            self.store.create_run(run_id, plan_id, len(ranked), input_version)
            if existing is not None and existing["run_id"] != run_id:
                # 旧运行（中途输入变更 / 强制重跑 / 已完成）标记作废，留痕可查
                with self.store.transaction() as conn:
                    self.store.update_run_progress(
                        conn, existing["run_id"], int(existing["processed"]),
                        existing["cursor_region"],
                        existing["cursor_institution_id"],
                        state="stale",
                    )

        # 由已处理条目重放增量状态（纯函数式，结果与一次跑完一致）
        state = _RunState()
        for item in items:
            if item["decision"] == "入选":
                inst = next(x for x in ranked if x.institution_id == item["institution_id"])
                state.selected_keys.add(inst.institution_id)
                state.chosen_inspectors[inst.institution_id] = item[
                    "proposed_inspector_id"
                ]
                state.region_selected[inst.region] = (
                    state.region_selected.get(inst.region, 0) + 1
                )

        processed_here = 0
        last_inst_id: str | None = None
        for rank in range(start_rank + 1, len(ranked) + 1):
            inst = ranked[rank - 1]
            item = self.planner.evaluate_institution(ctx, inst, rank, state)
            items.append(item)
            last_inst_id = inst.institution_id
            processed_here += 1
            if processed_here >= batch_size:
                break

        processed_total = start_rank + processed_here
        finished = processed_total >= len(ranked)

        with self.store.transaction() as conn:
            current_input_version = self.store._current_meta_version(conn)
            if current_input_version != input_version:
                # 运行期间主数据发生变化（新回避/资格/容量/并发锁定等），
                # 本批结果作废，下次调用从最新输入重跑。
                self.store.update_run_progress(
                    conn, run_id, processed_total, None, last_inst_id,
                    state="stale",
                )
                return {
                    "plan_id": plan_id,
                    "run_id": run_id,
                    "state": "invalidated",
                    "processed": processed_total,
                    "total": len(ranked),
                    "message": (
                        f"主数据在生成期间更新（v{input_version}→"
                        f"v{current_input_version}），候选已作废，"
                        "再次调用将按最新输入重新生成"
                    ),
                }
            self.store.save_run_items(conn, run_id, items)
            self.store.update_run_progress(
                conn,
                run_id,
                processed_total,
                None,
                last_inst_id,
                state="done" if finished else "running",
            )

        if not finished:
            return {
                "plan_id": plan_id,
                "run_id": run_id,
                "state": "running",
                "processed": processed_total,
                "total": len(ranked),
                "message": f"已处理{processed_total}/{len(ranked)}，"
                           f"可再次调用续跑（重启后同样可续）",
            }

        return self._finalize_candidates(plan_id, run_id, items, input_version, ranked)

    def _finalize_candidates(
        self,
        plan_id: str,
        run_id: str,
        items: list[dict[str, Any]],
        input_version: int,
        ranked: list[Institution],
    ) -> dict[str, Any]:
        """候选全部评估完：落计划新版本（候选待确认）。"""
        selected = [i for i in items if i["decision"] == "入选"]
        rejected = [i for i in items if i["decision"] == "未入选"]
        progress = (
            f"候选已生成：入选{len(selected)}家、未入选{len(rejected)}家，"
            "待负责人确认锁定"
        )
        payload = {
            "run_id": run_id,
            "input_version": input_version,
            "selected": len(selected),
            "rejected": len(rejected),
            "note": "候选由风险优先、容量约束、检查员资格与回避规则确定性生成",
        }
        with self.store.transaction() as conn:
            if self.store._current_meta_version(conn) != input_version:
                self.store.update_run_progress(
                    conn, run_id, len(ranked), None, None, state="stale"
                )
                return {
                    "plan_id": plan_id,
                    "run_id": run_id,
                    "state": "invalidated",
                    "processed": len(ranked),
                    "total": len(ranked),
                    "message": "定稿前检测到主数据已更新，候选作废，请重新生成",
                }
            self.store.update_run_progress(
                conn, run_id, len(ranked), None, None, state="finalized"
            )
            version = self.store.save_plan_version(
                conn,
                plan_id=plan_id,
                status=PLAN_CANDIDATE,
                progress=progress,
                change_kind="candidates",
                change_note=f"生成候选（输入版本v{input_version}）",
                payload=payload,
                candidates=items,
            )
        return {
            "plan_id": plan_id,
            "run_id": run_id,
            "state": "finalized",
            "processed": len(ranked),
            "total": len(ranked),
            "plan_version": version,
            "candidate_version": version,
        }

    def get_candidates(self, plan_id: str) -> dict[str, Any]:
        plan = self.store.get_plan(plan_id)
        if plan is None:
            raise NotFoundError(f"计划不存在：{plan_id}")
        raw = self.store.get_candidates(plan_id)
        inst_map = {i.institution_id: i for i in self.store.list_institutions()}
        insp_map = {i.inspector_id: i for i in self.store.list_inspectors()}
        candidates = [
            self.planner.to_candidate(item, inst_map, insp_map).to_dict()
            for item in raw
        ]
        run = None
        with self.store.read_only() as conn:
            row = self.store.get_run_for_update(conn, plan_id)
            if row is not None:
                run = {
                    "run_id": row["run_id"],
                    "state": row["state"],
                    "processed": row["processed"],
                    "total": row["total"],
                }
        return {
            "plan_id": plan_id,
            "status": plan.status,
            "plan_version": plan.version,
            "run": run,
            "candidates": candidates,
        }

    # ---- 负责人确认 + 原子锁定 --------------------------------------

    def confirm_plan(
        self,
        plan_id: str,
        *,
        candidate_version: int | None = None,
        confirm_all: bool = True,
        selected_institutions: list[str] | None = None,
    ) -> dict[str, Any]:
        """负责人确认候选并在单事务内原子锁定资源。

        - ``candidate_version``：负责人确认时看到的候选版本；若其间已生成更新的
          候选或计划状态已变，按并发冲突拒绝，要求重新核对（多版本保留，可追溯）；
        - 默认锁定全部入选机构；也可只确认显式给出的机构子集（其余候选保留解释）。
        """
        with self.store.transaction() as conn:
            plan_row = self.store.get_plan_version_meta(conn, plan_id)
            status = plan_row["status"]
            if status != PLAN_CANDIDATE:
                raise StateError(
                    f"计划当前状态为{status}：仅「候选待确认」可执行确认；"
                    "如需调整请改期或替换检查员（历史版本均保留）"
                )

            latest = self.store.latest_candidate_version_tx(conn, plan_id)
            if latest is None:
                raise StateError("计划尚无候选，无法确认")
            latest_cv, latest_cv_status = latest
            if candidate_version is None:
                candidate_version = latest_cv
            if candidate_version != latest_cv:
                raise ConflictError(
                    f"候选已过期：您依据v{candidate_version}确认，"
                    f"最新候选为v{latest_cv}，请重新核对后确认"
                )

            cand_rows = self.store.load_candidates_tx(
                conn, plan_id, candidate_version
            )
            proposed: dict[str, str] = {}
            rank_map: dict[str, int] = {}
            for r in cand_rows:
                if r["decision"] == "入选":
                    proposed[r["institution_id"]] = r["proposed_inspector_id"]
                    rank_map[r["institution_id"]] = r["rank"]

            if confirm_all:
                chosen_ids = sorted(proposed)
            else:
                wanted = set(selected_institutions or [])
                unknown = sorted(wanted - set(proposed))
                if unknown:
                    raise StateError(
                        "以下机构不在入选候选中，不能确认锁定："
                        + "、".join(unknown)
                    )
                chosen_ids = sorted(wanted)
            if not chosen_ids:
                raise StateError("没有任何入选机构可确认")

            # 以最新主数据重新校验（候选生成后世界可能已变），任一不过则整体失败，
            # 不会留下半个锁定 —— 事务回滚即原子性。
            ctx, input_version, inst_map, insp_map = self._build_context(
                conn, plan_row["quarter"], plan_row["inspection_type"],
                exclude_plan=plan_id,
            )

            assignments: list[Assignment] = []
            region_new: dict[str, int] = {}
            conflicts: list[str] = []

            # 校验：停业 / 占用
            chosen_order = sorted(chosen_ids, key=lambda x: rank_map[x])
            for inst_id in chosen_order:
                inst = inst_map.get(inst_id)
                if inst is None:
                    conflicts.append(f"机构{inst_id}：主档不存在")
                    continue
                if not inst.active:
                    conflicts.append(
                        f"机构{inst_id}（{inst.name}）：已停业（v{inst.closed_version}），"
                        "候选已失效，请重新生成"
                    )
                    continue
                if inst_id in ctx.occupied_institutions:
                    other = ctx.occupied_institutions[inst_id]
                    conflicts.append(
                        f"机构{inst_id}：已被并发计划{other[0]}锁定，"
                        "为避免跨小组重复占用，本次确认全部拒绝"
                    )

            # 校验：容量（外部占用 + 本批）
            for inst_id in chosen_order:
                inst = inst_map.get(inst_id)
                if inst is None or not inst.active or inst_id in ctx.occupied_institutions:
                    continue
                cap = ctx.capacities.get(inst.region)
                used = ctx.external_region_counts.get(inst.region, 0) + region_new.get(
                    inst.region, 0
                )
                if cap is not None and used >= cap:
                    conflicts.append(
                        f"区域{inst.region}容量{cap}已满"
                        f"（并发锁定占用{ctx.external_region_counts.get(inst.region, 0)}），"
                        f"机构{inst_id}无法锁定，本次确认全部拒绝"
                    )
                    continue
                region_new[inst.region] = region_new.get(inst.region, 0) + 1

            # 校验：检查员（停用/资格/区域/回避/季度工作量/本批重复）
            # 本批已为各检查员计入的工作量
            batch_loads: dict[str, int] = {}
            for inst_id in chosen_order:
                inst = inst_map.get(inst_id)
                if inst is None or not inst.active:
                    continue
                insp_id = proposed[inst_id]
                insp = insp_map.get(insp_id)
                why: list[str] = []
                if insp is None or not insp.active:
                    why.append("检查员已停用或不存在")
                else:
                    if plan_row["inspection_type"] not in insp.qualifications:
                        why.append("检查员不再具备该检查资格")
                    if "*" not in insp.regions and inst.region not in insp.regions:
                        why.append("检查员不再服务该区域")
                    if (insp_id, inst_id) in ctx.recusals:
                        why.append(
                            f"存在新生效回避（{ctx.recusals[(insp_id, inst_id)]}）"
                        )
                    external_load = ctx.external_inspector_loads.get(insp_id, 0)
                    batch_load = batch_loads.get(insp_id, 0)
                    if external_load + batch_load >= insp.quarterly_capacity:
                        why.append(
                            f"季度工作量已满（其他计划{external_load}项、"
                            f"本批{batch_load}项，容量{insp.quarterly_capacity}项）"
                        )
                if why:
                    conflicts.append(
                        f"机构{inst_id}的拟派检查员{insp_id}：{'；'.join(why)}，"
                        "本次确认全部拒绝，可重新生成候选或替换检查员"
                    )
                else:
                    batch_loads[insp_id] = batch_loads.get(insp_id, 0) + 1

            if conflicts:
                raise ConflictError("；".join(conflicts))

            # 全部通过：构造锁定任务（单计划版本，整批原子可见）
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for inst_id in chosen_order:
                inst = inst_map[inst_id]
                insp = insp_map[proposed[inst_id]]
                risk = inst.risk
                history = [{
                    "at": now_iso,
                    "action": "lock",
                    "from_status": "候选",
                    "to_status": TASK_LOCKED,
                    "note": f"负责人确认候选v{candidate_version}，原子锁定",
                    "inspector_id": insp.inspector_id,
                }]
                assignments.append(Assignment(
                    institution_id=inst_id,
                    institution_name=inst.name,
                    region=inst.region,
                    risk_level=risk.risk_level if risk else "未知",
                    risk_score=risk.risk_score if risk else 0.0,
                    inspector_id=insp.inspector_id,
                    inspector_name=insp.name,
                    status=TASK_LOCKED,
                    reasons=[
                        f"依据候选v{candidate_version}确认入选（风险排序第{rank_map[inst_id]}）",
                        f"锁定检查员{insp.name}（{insp.inspector_id}）",
                    ],
                    history=history,
                ))

            progress = (
                f"已锁定{len(assignments)}家机构及对应检查员/区域名额，待发布"
            )
            payload = {
                "candidate_version": candidate_version,
                "input_version": input_version,
                "locked": [a.institution_id for a in assignments],
                "note": "单事务原子锁定；未入选理由保留在候选版本中",
            }
            version = self.store.save_plan_version(
                conn,
                plan_id=plan_id,
                status=PLAN_LOCKED,
                progress=progress,
                change_kind="confirm",
                change_note=f"负责人确认并原子锁定{len(assignments)}家",
                payload=payload,
                assignments=assignments,
            )

        plan = self.store.get_plan(plan_id)
        assert plan is not None
        return {
            "plan_id": plan_id,
            "plan_version": version,
            "status": plan.status,
            "locked": [a.to_dict() for a in plan.assignments],
            "progress": plan.progress,
        }

    # ---- 发布（并发发布保留版本）-----------------------------------

    def publish_plan(
        self, plan_id: str, *, expected_version: int | None = None
    ) -> dict[str, Any]:
        with self.store.transaction() as conn:
            plan_row = self.store.get_plan_version_meta(conn, plan_id)
            if expected_version is not None and plan_row["current_version"] != expected_version:
                raise ConflictError(
                    f"发布基于v{expected_version}，计划当前已为"
                    f"v{plan_row['current_version']}，请基于最新版本发布"
                )
            if plan_row["status"] != PLAN_LOCKED:
                raise StateError(f"计划当前状态为{plan_row['status']}，仅已锁定可发布")
            rows = self.store.load_current_assignments_tx(conn, plan_id)
            active_locks = [
                r for r in rows
                if r["status"] in ("已锁定", "已换人", "已改期")
            ]
            if not active_locks:
                raise StateError("计划内已无有效锁定任务，不能发布")
            assignments = [self.store.row_to_assignment_row(r) for r in rows]
            payload = {
                "locked_version": plan_row["current_version"],
                "published_tasks": [r["institution_id"] for r in active_locks],
            }
            version = self.store.save_plan_version(
                conn,
                plan_id=plan_id,
                status=PLAN_PUBLISHED,
                progress=f"已发布{len(active_locks)}项检查任务",
                change_kind="publish",
                change_note="负责人发布计划",
                payload=payload,
                assignments=assignments,
            )
        return {"plan_id": plan_id, "plan_version": version, "status": PLAN_PUBLISHED}

    # ---- 改期 -------------------------------------------------------

    def reschedule_assignment(
        self,
        plan_id: str,
        institution_id: str,
        new_date: str,
        reason: str,
    ) -> dict[str, Any]:
        """改期：任务保留在锁定资源中，仅改期信息随新版本留存。"""
        try:
            datetime.fromisoformat(new_date)
        except ValueError as exc:
            raise ValueError("new_date 需为 ISO 日期（YYYY-MM-DD）") from exc
        with self.store.transaction() as conn:
            plan_row = self.store.get_plan_version_meta(conn, plan_id)
            if plan_row["status"] not in (PLAN_LOCKED, PLAN_PUBLISHED):
                raise StateError(f"计划状态为{plan_row['status']}，不能改期")
            assignments = self._load_assignments_as_models(conn, plan_id)
            target = next(
                (a for a in assignments if a.institution_id == institution_id), None
            )
            if target is None:
                raise NotFoundError(f"计划内无该机构任务：{institution_id}")
            if target.status not in ("已锁定", "已换人", "已改期"):
                raise StateError(f"任务状态为{target.status}，不能改期")
            history = target.history + [{
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "action": "reschedule",
                "from_status": target.status,
                "to_status": TASK_RESCHEDULED,
                "scheduled_date": new_date,
                "reason": reason,
            }]
            updated = Assignment(
                institution_id=target.institution_id,
                institution_name=target.institution_name,
                region=target.region,
                risk_level=target.risk_level,
                risk_score=target.risk_score,
                inspector_id=target.inspector_id,
                inspector_name=target.inspector_name,
                status=TASK_RESCHEDULED,
                reasons=target.reasons
                + [f"改期至{new_date}（{reason}），资源锁定保持不变"],
                history=history,
            )
            replacements = {institution_id: updated}
            new_assignments = [replacements.get(a.institution_id, a) for a in assignments]
            version = self.store.save_plan_version(
                conn,
                plan_id=plan_id,
                status=plan_row["status"],
                progress=plan_row["progress"],
                change_kind="reschedule",
                change_note=f"{institution_id}改期至{new_date}",
                payload={"institution_id": institution_id, "new_date": new_date,
                         "reason": reason},
                assignments=new_assignments,
            )
        return {"plan_id": plan_id, "plan_version": version,
                "institution_id": institution_id, "new_date": new_date}

    # ---- 替换检查员 -------------------------------------------------

    def replace_inspector(
        self,
        plan_id: str,
        institution_id: str,
        new_inspector_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """换人：旧检查员释放、新检查员在同事务内重新校验并锁定。"""
        with self.store.transaction() as conn:
            plan_row = self.store.get_plan_version_meta(conn, plan_id)
            if plan_row["status"] not in (PLAN_LOCKED, PLAN_PUBLISHED):
                raise StateError(f"计划状态为{plan_row['status']}，不能替换检查员")
            assignments = self._load_assignments_as_models(conn, plan_id)
            target = next(
                (a for a in assignments if a.institution_id == institution_id), None
            )
            if target is None:
                raise NotFoundError(f"计划内无该机构任务：{institution_id}")
            if target.status not in ("已锁定", "已换人", "已改期"):
                raise StateError(f"任务状态为{target.status}，不能换人")
            if target.inspector_id == new_inspector_id:
                raise StateError("新检查员与当前检查员相同")

            ctx, input_version, inst_map, insp_map = self._build_context(
                conn, plan_row["quarter"], plan_row["inspection_type"],
                exclude_plan=plan_id,
            )
            inst = inst_map.get(institution_id)
            if inst is None or not inst.active:
                raise ConflictError("机构已停业，请先按停业流程处理而非换人")
            new_insp = insp_map.get(new_inspector_id)
            why: list[str] = []
            if new_insp is None or not new_insp.active:
                why.append("检查员不存在或已停用")
            else:
                if plan_row["inspection_type"] not in new_insp.qualifications:
                    why.append("不具备该检查资格")
                if "*" not in new_insp.regions and inst.region not in new_insp.regions:
                    why.append("不服务该区域")
                if (new_inspector_id, institution_id) in ctx.recusals:
                    why.append(
                        f"对该机构存在生效回避（{ctx.recusals[(new_inspector_id, institution_id)]}）"
                    )
                # 季度工作量：外部计划占用 + 本计划内其他任务
                external_load = ctx.external_inspector_loads.get(new_inspector_id, 0)
                internal_other = sum(
                    1
                    for a in assignments
                    if a.institution_id != institution_id
                    and a.status in ("已锁定", "已换人", "已改期")
                    and a.inspector_id == new_inspector_id
                )
                if (
                    new_insp is not None
                    and external_load + internal_other
                    >= new_insp.quarterly_capacity
                ):
                    why.append(
                        f"季度工作量已满（其他计划{external_load}项、"
                        f"本计划其他任务{internal_other}项，"
                        f"容量{new_insp.quarterly_capacity}项）"
                    )
            if why:
                raise ConflictError(
                    f"检查员{new_inspector_id}不可替换：{'；'.join(why)}"
                )

            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            history = target.history + [{
                "at": now_iso,
                "action": "replace_inspector",
                "from_inspector_id": target.inspector_id,
                "to_inspector_id": new_inspector_id,
                "reason": reason,
            }]
            updated = Assignment(
                institution_id=target.institution_id,
                institution_name=target.institution_name,
                region=target.region,
                risk_level=target.risk_level,
                risk_score=target.risk_score,
                inspector_id=new_inspector_id,
                inspector_name=new_insp.name,
                status=TASK_REPLACED,
                reasons=target.reasons + [
                    f"检查员由{target.inspector_id}替换为{new_inspector_id}"
                    f"（{new_insp.name}）：{reason}",
                    "替换经资格/区域/回避/占用重新校验，在同一事务内完成锁定",
                ],
                history=history,
            )
            new_assignments = [
                updated if a.institution_id == institution_id else a
                for a in assignments
            ]
            version = self.store.save_plan_version(
                conn,
                plan_id=plan_id,
                status=plan_row["status"],
                progress=plan_row["progress"],
                change_kind="replace_inspector",
                change_note=f"{institution_id}检查员替换为{new_inspector_id}",
                payload={"institution_id": institution_id,
                         "from_inspector_id": target.inspector_id,
                         "to_inspector_id": new_inspector_id, "reason": reason,
                         "input_version": input_version},
                assignments=new_assignments,
            )
        return {"plan_id": plan_id, "plan_version": version,
                "institution_id": institution_id,
                "inspector_id": new_inspector_id}

    # ---- 机构停业联动 -----------------------------------------------

    def close_institution(
        self, institution_id: str, reason: str
    ) -> dict[str, Any]:
        """停业登记 + 自动联动所有引用该机构的未归档计划（追加版本）。"""
        result = self.store.close_institution(institution_id, reason)
        affected: list[dict[str, Any]] = []
        for plan in self.store.list_plans():
            if plan.status == PLAN_ARCHIVED:
                continue
            if not any(a.institution_id == institution_id for a in plan.assignments):
                continue
            if plan.status in (PLAN_LOCKED, PLAN_PUBLISHED):
                affected.append(
                    self._cancel_institution_in_plan(
                        plan.plan_id, institution_id, reason
                    )
                )
            # 候选阶段不动版本：负责人确认时会以停业事实拒绝并提示重新生成；
            # 重新生成候选时停业机构自然判为未入选并给出理由。
        return {"institution_id": institution_id,
                "version": result["version"], "affected_plans": affected}

    def _cancel_institution_in_plan(
        self, plan_id: str, institution_id: str, reason: str
    ) -> dict[str, Any]:
        with self.store.transaction() as conn:
            plan_row = self.store.get_plan_version_meta(conn, plan_id)
            assignments = self._load_assignments_as_models(conn, plan_id)
            target = next(
                a for a in assignments if a.institution_id == institution_id
            )
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            history = target.history + [{
                "at": now_iso,
                "action": "cancel_closed",
                "from_status": target.status,
                "to_status": TASK_CANCELLED,
                "reason": f"机构停业：{reason}",
                "released_inspector_id": target.inspector_id,
            }]
            updated = Assignment(
                institution_id=target.institution_id,
                institution_name=target.institution_name,
                region=target.region,
                risk_level=target.risk_level,
                risk_score=target.risk_score,
                inspector_id=None,
                inspector_name=None,
                status=TASK_CANCELLED,
                reasons=target.reasons + [
                    f"机构停业（{reason}）：取消任务并释放检查员"
                    f"{target.inspector_id}与区域名额"
                ],
                history=history,
            )
            new_assignments = [
                updated if a.institution_id == institution_id else a
                for a in assignments
            ]
            active = [
                a for a in new_assignments
                if a.status in ("已锁定", "已换人", "已改期")
            ]
            progress = (
                f"机构停业联动：{institution_id}已取消，"
                f"剩余有效任务{len(active)}项"
            )
            version = self.store.save_plan_version(
                conn,
                plan_id=plan_id,
                status=plan_row["status"],
                progress=progress,
                change_kind="institution_closed",
                change_note=f"{institution_id}停业，任务取消并释放资源",
                payload={"institution_id": institution_id, "reason": reason},
                assignments=new_assignments,
            )
        return {"plan_id": plan_id, "plan_version": version,
                "institution_id": institution_id, "status": TASK_CANCELLED}

    # ---- 归档 -------------------------------------------------------

    def archive_plan(self, plan_id: str) -> dict[str, Any]:
        with self.store.transaction() as conn:
            plan_row = self.store.get_plan_version_meta(conn, plan_id)
            if plan_row["status"] != PLAN_PUBLISHED:
                raise StateError(f"计划状态为{plan_row['status']}，仅已发布可归档")
            assignments = self._load_assignments_as_models(conn, plan_id)
            version = self.store.save_plan_version(
                conn,
                plan_id=plan_id,
                status=PLAN_ARCHIVED,
                progress="计划已归档（全部历史版本保留）",
                change_kind="archive",
                change_note="归档",
                payload={},
                assignments=assignments,
            )
        return {"plan_id": plan_id, "plan_version": version, "status": PLAN_ARCHIVED}

    # ---- 辅助：在事务内把当前版本任务读为完整 Assignment -----------

    def _load_assignments_as_models(self, conn, plan_id: str) -> list[Assignment]:
        plan_row = self.store.get_plan_version_meta(conn, plan_id)
        ver = plan_row["current_version"]
        rows = conn.execute(
            "SELECT a.institution_id, i.name AS institution_name, i.region AS region, "
            "s.risk_level AS risk_level, s.risk_score AS risk_score, "
            "a.inspector_id, iv.name AS inspector_name, a.status, a.reasons, a.history "
            "FROM assignments a "
            "JOIN institutions i ON i.institution_id=a.institution_id "
            "LEFT JOIN inspector_versions iv ON iv.inspector_id=a.inspector_id "
            "AND iv.version=(SELECT MAX(version) FROM inspector_versions "
            "WHERE inspector_id=a.inspector_id) "
            "LEFT JOIN risk_snapshots s ON s.institution_id=a.institution_id "
            "AND s.version=(SELECT MAX(version) FROM risk_snapshots "
            "WHERE institution_id=a.institution_id) "
            "WHERE a.plan_id=? AND a.version=? ORDER BY a.institution_id",
            (plan_id, ver),
        ).fetchall()
        return [
            Assignment(
                institution_id=r["institution_id"],
                institution_name=r["institution_name"],
                region=r["region"],
                risk_level=r["risk_level"] or "未知",
                risk_score=r["risk_score"] or 0.0,
                inspector_id=r["inspector_id"],
                inspector_name=r["inspector_name"],
                status=r["status"],
                reasons=json.loads(r["reasons"]),
                history=json.loads(r["history"]),
            )
            for r in rows
        ]
