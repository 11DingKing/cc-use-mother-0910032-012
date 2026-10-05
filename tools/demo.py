"""端到端演示：维护主数据 -> 生成可解释候选 -> 负责人确认锁定 ->
改期 / 换人 / 停业 / 发布，并打印每个入选或未入选机构的理由与版本轨迹。

用法：
    python3 tools/demo.py                 # 内存数据，直接演示
    python3 tools/demo.py --db data/demo.db
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from inspection_planning.service import PlanningService  # noqa: E402
from inspection_planning.store import Store  # noqa: E402


def line(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_candidates(svc: PlanningService, plan_id: str) -> None:
    result = svc.get_candidates(plan_id)
    print(f"计划 {plan_id}（状态：{result['status']}，"
          f"版本 v{result['plan_version']}）候选解释：")
    for c in result["candidates"]:
        mark = "✅ 入选" if c["decision"] == "入选" else "❌ 未入选"
        inspector = (
            f"，拟派 {c['proposed_inspector_name']}"
            f"（{c['proposed_inspector_id']}）"
            if c["proposed_inspector_id"]
            else ""
        )
        print(f"  第{c['rank']}名 {mark} {c['institution_id']} "
              f"{c['institution_name']} [{c['region']}/{c['risk_level']}/"
              f"{c['risk_score']:g}]{inspector}")
        for reason in c["reasons"]:
            print(f"      · {reason}")


def main() -> None:
    parser = argparse.ArgumentParser(description="监管抽检计划端到端演示")
    parser.add_argument("--db", default=":memory:")
    args = parser.parse_args()

    store = Store(args.db)
    svc = PlanningService(store)

    line("1. 维护机构风险快照（多版本留存）")
    store.upsert_institution(
        "INS-001", "春晖康复医院", "城东", "高", 92.0,
        ["近12个月投诉9起", "上次抽检财务不合格", "存在群众举报线索"],
        snapshot_note="季度风险模型 v3",
    )
    store.upsert_institution(
        "INS-002", "同和护理院", "城东", "中", 58.0, ["近12个月投诉2起"],
    )
    store.upsert_institution(
        "INS-003", "民健门诊部", "城西", "高", 85.0, ["整改复查逾期"],
    )
    store.upsert_institution(
        "INS-004", "安心养老院", "城西", "低", 18.0, ["近两年无异常"],
    )
    print("已登记 4 家机构及风险快照")

    line("2. 维护检查员资格 / 回避关系 / 区域容量")
    store.upsert_inspector("INSP-01", "张检", ["财务", "消防"], ["*"],
                           quarterly_capacity=2)
    store.upsert_inspector("INSP-02", "李检", ["财务"], ["城东"],
                           quarterly_capacity=2)
    store.upsert_inspector("INSP-03", "王检", ["财务"], ["城西"],
                           quarterly_capacity=2)
    store.add_recusal("INSP-03", "INS-003", "王检配偶在该机构任职")
    store.set_capacity("城东", 1)
    store.set_capacity("城西", 2)
    print("检查员 3 名；王检对 INS-003 回避；城东容量 1、城西容量 2")

    line("3. 创建季度计划并生成可解释候选（断点续跑，每批 2 家）")
    store.create_plan("PLAN-2026Q4-01", "2026Q4", "财务")
    while True:
        r = svc.generate_candidates("PLAN-2026Q4-01", batch_size=2)
        print(f"  运行 {r['run_id'][:8]}：{r['state']} "
              f"{r['processed']}/{r['total']}")
        if r["state"] == "finalized":
            break
    show_candidates(svc, "PLAN-2026Q4-01")

    line("4. 负责人确认，单事务原子锁定全部资源")
    locked = svc.confirm_plan("PLAN-2026Q4-01")
    print(f"计划状态：{locked['status']}（计划版本 v{locked['plan_version']}）")
    for a in locked["locked"]:
        print(f"  🔒 {a['institution_id']} -> {a['inspector_name']}"
              f"（{a['inspector_id']}），{a['status']}")

    line("5. 改期与替换检查员（均追加版本，历史可回溯）")
    svc.reschedule_assignment(
        "PLAN-2026Q4-01", "INS-001", "2026-11-18", "错开局内其他专项"
    )
    svc.replace_inspector(
        "PLAN-2026Q4-01", "INS-001", "INSP-02", "张检临时抽调"
    )
    print("INS-001 已改期并将检查员替换为李检（经资格/区域/回避/工作量重校）")

    line("6. 机构停业联动：INS-003 停业，自动取消任务并释放资源")
    result = svc.close_institution("INS-003", "被吊销执业许可证")
    for aff in result["affected_plans"]:
        print(f"  联动计划 {aff['plan_id']} 新版本 v{aff['plan_version']}："
              f"{aff['institution_id']} {aff['status']}")

    line("7. 并发发布（版本校验）与发布")
    publish = svc.publish_plan("PLAN-2026Q4-01")
    print(f"已发布：{publish}")

    line("8. 版本轨迹（改期/换人/停业/发布全部留版本）")
    for v in store.list_plan_versions("PLAN-2026Q4-01"):
        print(f"  v{v['version']:<2} {v['status']:<8} "
              f"{v['change_kind']:<18} {v['change_note']}")

    line("9. 最新计划明细")
    plan = svc.store.get_plan("PLAN-2026Q4-01")
    print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2,
                     default=list))

    store.close()


if __name__ == "__main__":
    main()
