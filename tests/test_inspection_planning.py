"""监管抽检计划服务端测试。

覆盖领域契约四大不变量：风险抽样规则、检查员回避、资源原子锁定、计划断点续跑，
以及多版本保留、停业联动、并发发布与候选可解释性。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from inspection_planning.api import create_server  # noqa: E402
from inspection_planning.service import PlanningService  # noqa: E402
from inspection_planning.store import (  # noqa: E402
    ConflictError,
    NotFoundError,
    StateError,
    Store,
)

QUARTER = "2026Q4"
ITYPE = "财务"


def build_world(store: Store) -> None:
    """构造一个区域/风险/检查员各异的标准数据集。"""
    # A 区域：高/中/低各一；B 区域：两家高风险
    store.upsert_institution("I-A1", "A区高风险机构", "A", "高", 92.0,
                             ["近12个月投诉9起", "上次抽检不合格"])
    store.upsert_institution("I-A2", "A区中风险机构", "A", "中", 55.0,
                             ["近12个月投诉2起"])
    store.upsert_institution("I-A3", "A区低风险机构", "A", "低", 20.0,
                             ["近两年无异常"])
    store.upsert_institution("I-B1", "B区高风险机构甲", "B", "高", 88.0,
                             ["存在重大举报线索"])
    store.upsert_institution("I-B2", "B区高风险机构乙", "B", "高", 81.0,
                             ["整改复查逾期"])
    # 检查员：X1 全域；X2 仅 A 区；X3 仅 B 区、且与 I-B1 回避
    store.upsert_inspector("X1", "张三", ["财务", "消防"], ["*"],
                           quarterly_capacity=2)
    store.upsert_inspector("X2", "李四", ["财务"], ["A"], quarterly_capacity=2)
    store.upsert_inspector("X3", "王五", ["财务"], ["B"], quarterly_capacity=2)
    store.add_recusal("X3", "I-B1", "王五近亲属任职于该机构")
    # 容量：A 区每季度 1 家，B 区 2 家
    store.set_capacity("A", 1)
    store.set_capacity("B", 2)


def gen_and_fetch(svc: PlanningService, plan_id: str, **kw) -> list[dict]:
    result = svc.generate_candidates(plan_id, **kw)
    assert result["state"] == "finalized", result
    return svc.get_candidates(plan_id)["candidates"]


class CandidateGenerationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.svc = PlanningService(self.store)
        build_world(self.store)
        self.store.create_plan("P1", QUARTER, ITYPE)

    def decisions(self, cands):
        return {c["institution_id"]: c for c in cands}

    def test_every_institution_has_decision_and_reasons(self) -> None:
        cands = gen_and_fetch(self.svc, "P1")
        self.assertEqual({c["institution_id"] for c in cands},
                         {"I-A1", "I-A2", "I-A3", "I-B1", "I-B2"})
        for c in cands:
            self.assertIn(c["decision"], ("入选", "未入选"))
            self.assertTrue(c["reasons"], "每个结论都必须给出理由")

    def test_risk_priority_and_region_capacity(self) -> None:
        cands = gen_and_fetch(self.svc, "P1")
        d = self.decisions(cands)
        # 高风险先占容量：A 区容量 1，I-A1 入选，中/低被容量挤出
        self.assertEqual(d["I-A1"]["decision"], "入选")
        # A 区 X1（全域）与 X2 均可、初始负载相同，按编号最小派 X1
        self.assertEqual(d["I-A1"]["proposed_inspector_id"], "X1")
        # B 区两家都入选；X3 回避 I-B1，因此 I-B1 只能派 X1
        self.assertEqual(d["I-B1"]["decision"], "入选")
        self.assertEqual(d["I-B2"]["decision"], "入选")
        # 未入选理由明确点到容量
        rejected_a = d["I-A2"]
        self.assertEqual(rejected_a["decision"], "未入选")
        self.assertTrue(any("容量已满" in r for r in rejected_a["reasons"]))

    def test_inspector_balancing_is_deterministic(self) -> None:
        # X1 全域，X2 服务 A。两者初始负载相同，编号小者 X1 先派。
        cands = gen_and_fetch(self.svc, "P1")
        d = self.decisions(cands)
        self.assertEqual(d["I-A1"]["proposed_inspector_id"], "X1")
        # 再跑一次结果完全一致
        cands2 = gen_and_fetch(self.svc, "P1")
        self.assertEqual(
            [(c["institution_id"], c["decision"], c["proposed_inspector_id"])
             for c in cands],
            [(c["institution_id"], c["decision"], c["proposed_inspector_id"])
             for c in cands2],
        )

    def test_recusal_excludes_inspector_with_reason(self) -> None:
        cands = gen_and_fetch(self.svc, "P1")
        d = self.decisions(cands)
        # I-B1：X3 被回避，可派的只有 X1
        self.assertEqual(d["I-B1"]["proposed_inspector_id"], "X1")
        self.assertNotIn("X3", d["I-B1"]["eligible_inspector_ids"])
        # 解除回避后重新生成，X3 进入候选合格集；按负载排序 I-B1 先评估时
        # X1 已承担 I-A1（负载1），X3 空闲（负载0），应派 X3
        self.store.lift_recusal("X3", "I-B1", "亲属关系已结束")
        cands = gen_and_fetch(self.svc, "P1")
        d = self.decisions(cands)
        self.assertEqual(d["I-B1"]["proposed_inspector_id"], "X3")

    def test_no_qualification_no_inspector_rejection(self) -> None:
        # 新开一类只有 X1 能做的检查：把 B 区容量放大，I-B2 无合格检查员时拒绝
        self.store.set_capacity("B", 5)
        self.store.create_plan("P2", QUARTER, "消防")
        cands = gen_and_fetch(self.svc, "P2")
        d = self.decisions(cands)
        # 只有 X1 有消防资格且容量 2：高风险排序 I-A1(92)、I-B1(88) 拿到 X1，
        # 其余高/中风险因检查员工作量或资格被拒
        selected = [k for k, v in d.items() if v["decision"] == "入选"]
        self.assertEqual(set(selected), {"I-A1", "I-B1"})
        self.assertTrue(
            any("不具备消防检查资格" in r
                for c in d.values() if c["decision"] == "未入选"
                for r in c["reasons"])
        )

    def test_closed_institution_rejected(self) -> None:
        self.store.close_institution("I-A2", "机构主动停业整改")
        cands = gen_and_fetch(self.svc, "P1")
        d = self.decisions(cands)
        self.assertEqual(d["I-A2"]["decision"], "未入选")
        self.assertTrue(any("停业" in r for r in d["I-A2"]["reasons"]))


class AtomicLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.svc = PlanningService(self.store)
        build_world(self.store)
        self.store.create_plan("P1", QUARTER, ITYPE)
        self.store.create_plan("P2", QUARTER, ITYPE)
        gen_and_fetch(self.svc, "P1")
        gen_and_fetch(self.svc, "P2")

    def test_confirm_locks_atomically_and_publishes(self) -> None:
        res = self.svc.confirm_plan("P1")
        self.assertEqual(res["status"], "已锁定")
        locked = {a["institution_id"] for a in res["locked"]}
        self.assertEqual(locked, {"I-A1", "I-B1", "I-B2"})
        plan = self.store.get_plan("P1")
        self.assertEqual(plan.status, "已锁定")

    def test_stale_candidate_version_rejected(self) -> None:
        first = self.svc.generate_candidates("P1")
        cv_first = first["plan_version"]
        # 输入变化后重新生成 -> 候选版本更新
        self.store.set_capacity("A", 2)
        second = self.svc.generate_candidates("P1")
        self.assertNotEqual(cv_first, second["plan_version"])
        with self.assertRaises(ConflictError) as ctx:
            self.svc.confirm_plan("P1", candidate_version=cv_first)
        self.assertIn("候选已过期", str(ctx.exception))

    def test_concurrent_confirms_only_one_wins_institution(self) -> None:
        # P1 / P2 都想锁 I-B2；两个确认并发，只有一个能成功
        barrier = threading.Barrier(2)
        outcomes: list[object] = []

        def confirm(plan_id: str) -> None:
            barrier.wait()
            try:
                outcomes.append(("ok", plan_id,
                                  self.svc.confirm_plan(plan_id)))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(("err", plan_id, str(exc)))

        t1 = threading.Thread(target=confirm, args=("P1",))
        t2 = threading.Thread(target=confirm, args=("P2",))
        t1.start(); t2.start(); t1.join(); t2.join()

        oks = [o for o in outcomes if o[0] == "ok"]
        errs = [o for o in outcomes if o[0] == "err"]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(errs), 1)
        self.assertIn("重复占用", errs[0][2])

    def test_confirm_revalidates_world_after_recusal_added(self) -> None:
        # 候选已生成后新增回避：确认时必须按最新数据拒绝
        cands = self.svc.get_candidates("P1")["candidates"]
        ib1 = next(c for c in cands if c["institution_id"] == "I-B1")
        # 让 I-B1 的拟派检查员恰好是 X3 需要先解除既有回避并重生
        self.store.lift_recusal("X3", "I-B1", "临时")
        gen_and_fetch(self.svc, "P1")
        self.store.add_recusal("X3", "I-B1", "新发现利益关系")
        with self.assertRaises(ConflictError) as ctx:
            self.svc.confirm_plan("P1")
        self.assertIn("回避", str(ctx.exception))

    def test_concurrent_publish_conflict_on_version(self) -> None:
        self.svc.confirm_plan("P1")
        plan = self.store.get_plan("P1")
        locked_version = plan.version
        # 基于旧版本号发布，同时改期推进了版本
        self.svc.reschedule_assignment(
            "P1", "I-A1", "2026-11-20", "检查时间调整"
        )
        with self.assertRaises(ConflictError):
            self.svc.publish_plan("P1", expected_version=locked_version)
        # 基于最新版本可发布
        res = self.svc.publish_plan("P1")
        self.assertEqual(res["status"], "已发布")


class VersionRetentionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.svc = PlanningService(self.store)
        build_world(self.store)
        self.store.create_plan("P1", QUARTER, ITYPE)
        gen_and_fetch(self.svc, "P1")
        self.svc.confirm_plan("P1")

    def test_each_change_appends_recoverable_version(self) -> None:
        plan0 = self.store.get_plan("P1")
        v_lock = plan0.version

        self.svc.reschedule_assignment("P1", "I-B2", "2026-12-01", "错开检查")
        v_sched = self.store.get_plan("P1").version

        self.svc.replace_inspector("P1", "I-A1", "X2", "原检查员出差")
        v_replace = self.store.get_plan("P1").version

        self.svc.publish_plan("P1")

        versions = self.store.list_plan_versions("P1")
        version_nums = [v["version"] for v in versions]
        self.assertEqual(version_nums, sorted(version_nums))
        kinds = [v["change_kind"] for v in versions]
        self.assertIn("reschedule", kinds)
        self.assertIn("replace_inspector", kinds)
        self.assertIn("publish", kinds)

        # 旧版本完整可读：锁定版本里 I-A1 仍是 X1，且无改期记录
        old = self.store.get_plan("P1", version=v_lock)
        a1_old = next(a for a in old.assignments if a.institution_id == "I-A1")
        self.assertEqual(a1_old.inspector_id, "X1")

        sched_snapshot = self.store.get_plan("P1", version=v_sched)
        b2 = next(a for a in sched_snapshot.assignments
                  if a.institution_id == "I-B2")
        self.assertEqual(b2.status, "已改期")
        self.assertTrue(any(
            h["action"] == "reschedule" and h["scheduled_date"] == "2026-12-01"
            for h in b2.history
        ))

        # 最新版本 I-A1 已换为 X2，换人历史保留
        latest = self.store.get_plan("P1")
        a1 = next(a for a in latest.assignments if a.institution_id == "I-A1")
        self.assertEqual(a1.inspector_id, "X2")
        self.assertEqual(a1.status, "已换人")
        self.assertTrue(any(
            h["action"] == "replace_inspector"
            and h["from_inspector_id"] == "X1"
            and h["to_inspector_id"] == "X2"
            for h in a1.history
        ))
        # 改期状态在换人后仍保留为已改期？换人任务自身是 I-A1，I-B2 仍为已改期
        self.assertGreater(v_replace, v_sched)

    def test_replace_inspector_rejects_recusal_and_full_workload(self) -> None:
        with self.assertRaises(ConflictError) as ctx:
            # X3 对 I-B1 存在回避；把 I-A1 换成不服务 A 区的 X3 也应被拒
            self.svc.replace_inspector("P1", "I-A1", "X3", "测试")
        msg = str(ctx.exception)
        self.assertTrue("回避" in msg or "不服务该区域" in msg)

    def test_close_institution_cancels_locked_task_and_keeps_versions(self) -> None:
        before = self.store.get_plan("P1").version
        result = self.svc.close_institution("I-B1", "被吊销许可证")
        self.assertTrue(result["affected_plans"])
        after = self.store.get_plan("P1")
        self.assertGreater(after.version, before)
        b1 = next(a for a in after.assignments if a.institution_id == "I-B1")
        self.assertEqual(b1.status, "已取消")
        self.assertIsNone(b1.inspector_id)
        self.assertTrue(any(
            h["action"] == "cancel_closed"
            and h["released_inspector_id"] == "X1"
            for h in b1.history
        ))
        # 其他任务不受影响
        b2 = next(a for a in after.assignments if a.institution_id == "I-B2")
        self.assertEqual(b2.status, "已锁定")
        # 风险快照历史保留停业版本
        snaps = self.store.list_risk_versions("I-B1")
        self.assertFalse(snaps[-1].active)
        self.assertIn("停业", snaps[-1].snapshot_note)

    def test_inspector_qualification_change_keeps_versions(self) -> None:
        self.store.upsert_inspector("X2", "李四", ["消防"], ["A"],
                                    quarterly_capacity=2)
        inspectors = {i.inspector_id: i for i in self.store.list_inspectors()}
        self.assertEqual(inspectors["X2"].qualifications, ["消防"])


class ResumeTest(unittest.TestCase):
    def test_resume_in_batches_matches_one_shot(self) -> None:
        ref_store = Store()
        build_world(ref_store)
        ref_store.create_plan("P1", QUARTER, ITYPE)
        ref_svc = PlanningService(ref_store)
        expected = gen_and_fetch(ref_svc, "P1")

        store = Store()
        build_world(store)
        store.create_plan("P1", QUARTER, ITYPE)
        svc = PlanningService(store)
        states = []
        for _ in range(20):
            r = svc.generate_candidates("P1", batch_size=2)
            states.append(r["state"])
            if r["state"] == "finalized":
                break
        self.assertIn("running", states)
        cands = svc.get_candidates("P1")["candidates"]
        key = lambda rows: [  # noqa: E731
            (c["institution_id"], c["decision"], c["proposed_inspector_id"],
             tuple(c["reasons"]))
            for c in rows
        ]
        self.assertEqual(key(cands), key(expected))

    def test_resume_after_process_restart_with_file_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "plan.db")
            store = Store(db)
            build_world(store)
            store.create_plan("P1", QUARTER, ITYPE)
            svc = PlanningService(store)
            r1 = svc.generate_candidates("P1", batch_size=2)
            self.assertEqual(r1["state"], "running")
            processed = r1["processed"]
            store.close()

            # 模拟重启：新 Store/Service 指向同一数据库文件续跑
            store2 = Store(db)
            svc2 = PlanningService(store2)
            r2 = svc2.generate_candidates("P1", batch_size=2)
            self.assertGreaterEqual(r2["processed"], processed)
            while r2["state"] != "finalized":
                r2 = svc2.generate_candidates("P1", batch_size=2)
            cands = svc2.get_candidates("P1")["candidates"]
            self.assertEqual(len(cands), 5)
            store2.close()

    def test_input_change_mid_run_invalidates_and_restarts(self) -> None:
        store = Store()
        build_world(store)
        store.create_plan("P1", QUARTER, ITYPE)
        svc = PlanningService(store)
        r1 = svc.generate_candidates("P1", batch_size=2)
        self.assertEqual(r1["state"], "running")
        old_run = r1["run_id"]
        # 主数据变化 -> 旧运行作废，从头开始
        store.set_capacity("A", 2)
        r2 = svc.generate_candidates("P1", batch_size=2)
        self.assertNotEqual(r2["run_id"], old_run)
        self.assertEqual(r2["processed"], 2)
        while r2["state"] != "finalized":
            r2 = svc.generate_candidates("P1", batch_size=2)
        d = {c["institution_id"]: c for c in svc.get_candidates("P1")["candidates"]}
        # A 区容量 2：I-A1 与 I-A2 均可入选
        self.assertEqual(d["I-A2"]["decision"], "入选")

    def test_input_change_during_active_batch_invalidates(self) -> None:
        # 一批处理进行中主数据被并发写入（其他小组锁定/容量调整）：
        # 本批在落库时检出输入版本变化，标记 invalidated，绝不基于过期输入定稿。
        import time

        from inspection_planning.planner import Planner

        store = Store()
        n = 200
        for i in range(n):
            store.upsert_institution(f"I{i:05d}", f"机构{i}", "A", "高",
                                     50.0 + (n - i) * 0.001, [])
        store.upsert_inspector("X1", "张", ["财务"], ["*"],
                               quarterly_capacity=n)
        store.set_capacity("A", n)
        store.create_plan("Q1", QUARTER, ITYPE)

        class SlowPlanner(Planner):
            def evaluate_institution(self, ctx, inst, rank, state):
                if rank == n // 2:
                    time.sleep(0.05)  # 稳定地让并发写入落在本批评估窗口内
                return super().evaluate_institution(ctx, inst, rank, state)

        svc = PlanningService(store, SlowPlanner())

        def change_capacity_soon() -> None:
            time.sleep(0.01)
            store.set_capacity("A", n - 1)

        t = threading.Thread(target=change_capacity_soon)
        t.start()
        r = svc.generate_candidates("Q1", batch_size=n)
        t.join()
        self.assertEqual(r["state"], "invalidated")
        self.assertEqual(store.get_plan("Q1").status, "草稿")
        # 再次生成：按最新输入重跑并正常定稿
        svc = PlanningService(store, SlowPlanner())
        r = svc.generate_candidates("Q1", batch_size=n)
        self.assertEqual(r["state"], "finalized")
        cands = svc.get_candidates("Q1")["candidates"]
        self.assertEqual(sum(c["decision"] == "入选" for c in cands), n - 1)


class PlannerUnitTest(unittest.TestCase):
    def test_ranking_tie_break_is_stable(self) -> None:
        store = Store()
        store.upsert_institution("I9", "九", "A", "高", 50.0, [])
        store.upsert_institution("I1", "一", "A", "高", 50.0, [])
        store.upsert_inspector("X1", "张", ["财务"], ["*"])
        store.set_capacity("A", 1)
        store.create_plan("P", QUARTER, ITYPE)
        svc = PlanningService(store)
        cands = gen_and_fetch(svc, "P")
        # 同分按机构编号升序：I1 占唯一名额
        self.assertEqual(cands[0]["institution_id"], "I1")
        self.assertEqual(cands[0]["decision"], "入选")
        self.assertEqual(cands[1]["decision"], "未入选")

    def test_inspector_workload_capacity_caps_tasks(self) -> None:
        # 检查员季度容量 1，三家高风险分属不同区域：最多入选一家
        store = Store()
        for idx, region in enumerate(("A", "B", "C"), start=1):
            store.upsert_institution(f"I{idx}", f"机构{idx}", region,
                                     "高", 80.0 + idx, [])
        store.upsert_inspector("X1", "张", ["财务"], ["*"],
                               quarterly_capacity=1)
        for region in ("A", "B", "C"):
            store.set_capacity(region, 5)
        store.create_plan("P", QUARTER, ITYPE)
        svc = PlanningService(store)
        cands = gen_and_fetch(svc, "P")
        selected = [c for c in cands if c["decision"] == "入选"]
        rejected = [c for c in cands if c["decision"] == "未入选"]
        self.assertEqual(len(selected), 1)
        self.assertTrue(any(
            "工作量已满" in r for c in rejected for r in c["reasons"]
        ))

    def test_missing_risk_snapshot_is_explained(self) -> None:
        store = Store()
        # 直接插主档但不写快照（构造缺快照的异常数据）
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO institutions(institution_id,name,region,"
                "created_version,active) VALUES('I0','无档','A',1,1)"
            )
        store.upsert_inspector("X1", "张", ["财务"], ["*"])
        store.set_capacity("A", 5)
        store.create_plan("P", QUARTER, ITYPE)
        svc = PlanningService(store)
        cands = gen_and_fetch(svc, "P")
        only = cands[0]
        self.assertEqual(only["decision"], "未入选")
        self.assertTrue(any("缺少风险快照" in r for r in only["reasons"]))


class StateGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.svc = PlanningService(self.store)
        build_world(self.store)
        self.store.create_plan("P1", QUARTER, ITYPE)

    def test_cannot_confirm_twice_or_publish_before_lock(self) -> None:
        gen_and_fetch(self.svc, "P1")
        self.svc.confirm_plan("P1")
        with self.assertRaises(StateError):
            # 已锁定后不能再次确认覆盖（调整须改期/换人）
            self.svc.confirm_plan("P1")
        with self.assertRaises(NotFoundError):
            # 改期一个计划内不存在的任务
            self.svc.reschedule_assignment("P1", "NO-SUCH", "2026-12-01", "x")

        # 草稿计划不能直接发布
        self.store.create_plan("P2", QUARTER, ITYPE)
        with self.assertRaises(StateError):
            self.svc.publish_plan("P2")

    def test_unknown_inspector_and_duplicate_recusal_rejected(self) -> None:
        with self.assertRaises(NotFoundError):
            self.store.add_recusal("NOPE", "I-A1", "无此人")
        with self.assertRaises(StateError):
            self.store.add_recusal("X3", "I-B1", "重复登记回避")


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = create_server(":memory:", "127.0.0.1", 0, quiet=True)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.server.store.close()

    def call(self, method: str, path: str, body: dict | None = None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def test_full_lifecycle_over_http(self) -> None:
        status, _ = self.call("GET", "/health")
        self.assertEqual(status, 200)

        status, _ = self.call("POST", "/institutions", {
            "institution_id": "I1", "name": "甲", "region": "A",
            "risk_level": "高", "risk_score": 90,
            "risk_factors": ["投诉多"],
        })
        self.assertEqual(status, 201)
        status, _ = self.call("POST", "/inspectors", {
            "inspector_id": "X1", "name": "张三",
            "qualifications": ["财务"], "regions": ["A"],
        })
        self.assertEqual(status, 201)
        status, _ = self.call("POST", "/regions/capacity",
                              {"region": "A", "capacity": 1})
        self.assertEqual(status, 201)
        status, _ = self.call("POST", "/plans", {
            "plan_id": "P1", "quarter": QUARTER, "inspection_type": ITYPE,
        })
        self.assertEqual(status, 201)

        status, data = self.call("POST", "/plans/P1/candidates/generate",
                                 {"batch_size": 10})
        self.assertEqual(status, 200)
        self.assertEqual(data["state"], "finalized")

        status, data = self.call("GET", "/plans/P1/candidates")
        self.assertEqual(status, 200)
        self.assertTrue(all("reasons" in c for c in data["candidates"]))

        status, data = self.call("POST", "/plans/P1/confirm", {})
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "已锁定")

        status, data = self.call("POST", "/plans/P1/reschedule", {
            "institution_id": "I1", "new_date": "2026-11-11", "reason": "调整",
        })
        self.assertEqual(status, 200)

        status, data = self.call("POST", "/plans/P1/publish", {})
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "已发布")

        status, data = self.call("GET", "/plans/P1/versions")
        self.assertEqual(status, 200)
        kinds = [v["change_kind"] for v in data["versions"]]
        self.assertEqual(kinds, ["create", "candidates", "confirm",
                                 "reschedule", "publish"])

    def test_http_error_mapping(self) -> None:
        status, data = self.call("POST", "/plans/P/candidates/generate", {})
        self.assertEqual(status, 404)
        self.assertIn("error", data)

        self.call("POST", "/institutions", {
            "institution_id": "I1", "name": "甲", "region": "A",
            "risk_level": "高", "risk_score": 90, "risk_factors": [],
        })
        status, data = self.call("POST", "/institutions", {
            "institution_id": "I1", "name": "甲", "region": "A",
            "risk_level": "超高", "risk_score": 90, "risk_factors": [],
        })
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
