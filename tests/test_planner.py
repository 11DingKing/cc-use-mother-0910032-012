"""监管抽检计划服务端测试：候选解释、断点续跑、原子锁定、版本化变更、并发发布、HTTP API。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sampling_planner.database import (  # noqa: E402
    StateConflict,
    VersionConflict,
    connect,
    init_db,
    utcnow,
)
from sampling_planner.engine import GenerationContext  # noqa: E402
from sampling_planner.repositories import (  # noqa: E402
    AvoidanceRepository,
    InstitutionRepository,
    InspectorRepository,
    RegionCapacityRepository,
    SamplingRuleRepository,
)
from sampling_planner.seed import QUARTER, seed  # noqa: E402
from sampling_planner.service import PlanService  # noqa: E402


def iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


class PlannerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        seed(self.db_path)
        self.conn = connect(self.db_path)
        self.svc = PlanService(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def gen_until_done(self, plan_id: str, batch_size: int = 2) -> dict:
        # 小批量 + 每批独立连接，模拟崩溃重启后续跑
        self.conn.close()
        last = {"finished": False}
        for _ in range(100):
            conn = connect(self.db_path)
            last = PlanService(conn).generate(plan_id, batch_size=batch_size)
            conn.close()
            if last["finished"]:
                break
        self.conn = connect(self.db_path)
        self.svc = PlanService(self.conn)
        return last

    def reasons(self, plan_id: str) -> dict[str, str]:
        return {c["institution_id"]: c["reason_code"] for c in self.svc.explanations(plan_id)}


class CandidateGenerationTest(PlannerTestBase):
    def test_seed_full_flow_explanations(self) -> None:
        plan = self.svc.create_plan("P1", QUARTER)
        result = self.gen_until_done("P1", batch_size=3)
        self.assertTrue(result["finished"])
        self.assertEqual(result["generation_done"], 14)
        self.assertEqual(result["selected"], 6)

        reasons = self.reasons("P1")
        # 入选
        for iid in ("I001", "I002", "I003", "I006", "I010", "I011"):
            self.assertEqual(reasons[iid], "SELECTED", iid)
        # 各类未入选原因
        self.assertEqual(reasons["I005"], "RULE_FILTERED")
        self.assertEqual(reasons["I008"], "RULE_FILTERED")
        self.assertEqual(reasons["I009"], "RULE_FILTERED")
        self.assertEqual(reasons["I012"], "RULE_FILTERED")
        self.assertEqual(reasons["I004"], "REGION_CAPACITY")   # 华东容量 3 已满
        self.assertEqual(reasons["I007"], "BELOW_CUTOFF")     # 医疗规则名额 2
        self.assertEqual(reasons["I013"], "INSTITUTION_SUSPENDED")
        self.assertEqual(reasons["I014"], "SNAPSHOT_STALE")

        # 每条解释都有中文说明（入选与未入选）
        for cand in self.svc.explanations("P1"):
            self.assertTrue(cand["explanation"], cand["institution_id"])
            self.assertNotIn("{rule}", cand["explanation"])  # 模板已被填充

        # 回避关系影响预分配：I001 不分配给 E01，I006 不分配给 E02
        proposed = {c["institution_id"]: c["proposed_inspector_id"]
                    for c in self.svc.candidates("P1") if c["selected"]}
        self.assertNotEqual(proposed["I001"], "E01")
        self.assertNotEqual(proposed["I006"], "E02")

    def test_no_qualified_inspector(self) -> None:
        conn = connect(":memory:")
        init_db(conn)
        InstitutionRepository(conn).create({
            "id": "N1", "name": "核设施", "region": "华东", "industry": "核能",
            "risk_level": "高", "risk_score": 99, "tags": [], "snapshot_at": utcnow(),
        })
        InspectorRepository(conn).create({"id": "X1", "name": "金融员", "qualifications": ["金融"]})
        RegionCapacityRepository(conn).set("华东", "2026Q4", 5)
        SamplingRuleRepository(conn).create({
            "id": "RN", "name": "核能专项", "risk_levels": ["高"], "min_score": 0,
            "industries": ["核能"], "required_qualification": "核能", "quota": 1, "priority": 1,
        })
        svc = PlanService(conn)
        svc.create_plan("PN", "2026Q4")
        svc.resume_generation("PN")
        self.assertEqual(
            {c["institution_id"]: c["reason_code"] for c in svc.explanations("PN")},
            {"N1": "NO_QUALIFIED_INSPECTOR"},
        )
        conn.close()

    def test_all_avoiding(self) -> None:
        conn = connect(":memory:")
        init_db(conn)
        InstitutionRepository(conn).create({
            "id": "A1", "name": "敏感机构", "region": "华东", "industry": "金融",
            "risk_level": "高", "risk_score": 90, "tags": [], "snapshot_at": utcnow(),
        })
        InspectorRepository(conn).create({"id": "X1", "name": "甲", "qualifications": ["金融"]})
        InspectorRepository(conn).create({"id": "X2", "name": "乙", "qualifications": ["金融"]})
        av = AvoidanceRepository(conn)
        av.add("X1", "A1", "亲属")
        av.add("X2", "A1", "业务往来")
        RegionCapacityRepository(conn).set("华东", "2026Q4", 5)
        SamplingRuleRepository(conn).create({
            "id": "RA", "name": "金融高风险", "risk_levels": ["高"], "min_score": 0,
            "industries": ["金融"], "required_qualification": "金融", "quota": 1, "priority": 1,
        })
        svc = PlanService(conn)
        svc.create_plan("PA", "2026Q4")
        svc.resume_generation("PA")
        cand = svc.explanations("PA")[0]
        self.assertEqual(cand["reason_code"], "AVOIDANCE")
        self.assertIn("甲", cand["explanation"])
        conn.close()


class ResumeTest(PlannerTestBase):
    def test_resume_after_restart_is_complete_and_stable(self) -> None:
        self.svc.create_plan("PR", QUARTER)
        self.gen_until_done("PR", batch_size=1)
        rows = self.conn.execute(
            "SELECT COUNT(*) c FROM candidates WHERE plan_id='PR'"
        ).fetchone()
        self.assertEqual(rows["c"], 14)

        # 再次推进已是幂等完成态
        again = self.svc.generate("PR")
        self.assertTrue(again["finished"])

    def test_data_change_invalidates_partial_generation(self) -> None:
        self.svc.create_plan("PV", QUARTER)
        self.gen_until_done("PV", batch_size=5)  # 先跑 5 个
        # 主数据变化：新增机构，指纹改变
        InstitutionRepository(self.conn).create({
            "id": "I999", "name": "新划入机构", "region": "华东", "industry": "金融",
            "risk_level": "高", "risk_score": 99, "tags": [], "snapshot_at": utcnow(),
        })
        result = self.gen_until_done("PV", batch_size=4)
        self.assertTrue(result["finished"])
        self.assertEqual(result["generation_total"], 15)
        ids = {c["institution_id"] for c in self.svc.candidates("PV")}
        self.assertIn("I999", ids)


class ConfirmAndLockingTest(PlannerTestBase):
    def _ready_plan(self, plan_id: str) -> None:
        self.svc.create_plan(plan_id, QUARTER)
        self.gen_until_done(plan_id)

    def test_confirm_locks_all_three_resource_types(self) -> None:
        self._ready_plan("P1")
        version = self.svc.get_plan("P1")["version"]
        out = self.svc.confirm("P1", version, actor="负责人周")
        self.assertEqual(out["plan"]["state"], "已确认")
        self.assertEqual(len(out["items"]), 6)

        locks = self.conn.execute(
            "SELECT lock_type, COUNT(*) c FROM resource_locks WHERE ref_plan_id='P1' GROUP BY lock_type"
        ).fetchall()
        counts = {r["lock_type"]: r["c"] for r in locks}
        # 每个入选项一把机构锁、一把检查员锁、一把区域锁
        self.assertEqual(counts, {"institution": 6, "inspector": 6, "region": 6})

        # 机构季度锁唯一键
        dup = self.conn.execute(
            "SELECT COUNT(*) c FROM resource_locks WHERE lock_type='institution' "
            "AND lock_key=?", (f"institution:{QUARTER}:I001",)
        ).fetchone()
        self.assertEqual(dup["c"], 1)

    def test_second_team_cannot_double_occupy_and_rolls_back(self) -> None:
        # 两个小组在任何确认前各自生成候选，都看到资源空闲、选中了同一批机构
        conn_a = connect(self.db_path)
        svc_a = PlanService(conn_a)
        svc_a.create_plan("PA", QUARTER)
        svc_a.resume_generation("PA")
        pa_version = svc_a.get_plan("PA")["version"]
        conn_a.close()

        conn_b = connect(self.db_path)
        svc_b = PlanService(conn_b)
        svc_b.create_plan("PB", QUARTER)
        svc_b.resume_generation("PB")
        pb_version = svc_b.get_plan("PB")["version"]
        conn_b.close()

        # 小组 A 先确认成功
        conn_a = connect(self.db_path)
        PlanService(conn_a).confirm("PA", pa_version)
        conn_a.close()

        # 小组 B 随后确认：机构锁唯一约束冲突，整事务回滚
        conn_b = connect(self.db_path)
        svc_b = PlanService(conn_b)
        with self.assertRaises(VersionConflict) as ctx:
            svc_b.confirm("PB", pb_version)
        self.assertIn("并发占用", str(ctx.exception))
        conn_b.close()

        # B 没有留下任何半成品计划项或锁
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM plan_items WHERE plan_id='PB'").fetchone()["c"], 0
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM resource_locks WHERE ref_plan_id='PB'").fetchone()["c"], 0
        )
        # B 仍停留在候选就绪，可重新生成
        self.assertEqual(self.svc.get_plan("PB")["state"], "候选就绪")

        # 重新生成后，被 A 占用的机构在 B 的候选中给出 ALREADY_PLANNED 理由
        self.svc.resume_generation("PB")
        reasons = self.reasons("PB")
        self.assertEqual(reasons["I001"], "ALREADY_PLANNED")

    def test_inspector_and_region_slots_enforced(self) -> None:
        self._ready_plan("P1")
        self.svc.confirm("P1", self.svc.get_plan("P1")["version"])
        # E01 容量 2，已承担 I006、I010
        e01 = self.conn.execute(
            "SELECT COUNT(*) c FROM resource_locks WHERE lock_type='inspector' "
            "AND lock_key=?", (f"inspector:{QUARTER}:E01",)
        ).fetchone()["c"]
        self.assertEqual(e01, 2)
        # 华东容量 3
        hd = self.conn.execute(
            "SELECT COUNT(*) c FROM resource_locks WHERE lock_type='region' "
            "AND lock_key=?", (f"region:{QUARTER}:华东",)
        ).fetchone()["c"]
        self.assertEqual(hd, 3)


class VersionedMutationTest(PlannerTestBase):
    def _confirmed(self, plan_id: str = "P1") -> None:
        self.svc.create_plan(plan_id, QUARTER)
        self.gen_until_done(plan_id)
        self.svc.confirm(plan_id, self.svc.get_plan(plan_id)["version"], actor="负责人周")

    def test_reschedule_keeps_versions(self) -> None:
        self._confirmed()
        item = self.svc.list_items("P1")[0]
        v1 = item["version"]
        old_date = item["scheduled_date"]
        new_date = (datetime.strptime(old_date, "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d")

        updated = self.svc.reschedule_item(item["id"], new_date, v1, actor="调度员吴")
        self.assertEqual(updated["version"], v1 + 1)
        self.assertEqual(updated["item_state"], "已改期")

        history = self.svc.item_history(item["id"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["version"], v1)
        self.assertEqual(history[0]["scheduled_date"], old_date)
        self.assertIn("改期", history[0]["change_note"])

        # 乐观锁：旧版本再改被拒
        with self.assertRaises(VersionConflict):
            self.svc.reschedule_item(item["id"], new_date, v1)

        # 日期必须在同一季度
        with self.assertRaises(ValueError):
            self.svc.reschedule_item(item["id"], "2027-02-01", updated["version"])

    def test_swap_inspector_checks_and_releases_old_lock(self) -> None:
        self._confirmed()
        # I006 的检查员是 E02（回避）？实际预分配：I006 给 E01；换人给 E05
        item = next(i for i in self.svc.list_items("P1") if i["institution_id"] == "I006")
        self.assertEqual(item["inspector_id"], "E01")

        # 换成与 I006 有回避的 E02 → 拒绝
        with self.assertRaises(StateConflict):
            self.svc.swap_inspector(item["id"], "E02", item["version"])
        # 换成无金融资格的 E04 → 拒绝
        with self.assertRaises(StateConflict):
            self.svc.swap_inspector(item["id"], "E04", item["version"])

        updated = self.svc.swap_inspector(item["id"], "E05", item["version"], actor="组长郑")
        self.assertEqual(updated["inspector_id"], "E05")
        self.assertEqual(updated["item_state"], "已换人")
        self.assertEqual(updated["version"], 2)

        # 旧检查员锁已删除，新检查员锁存在，机构/区域锁不动
        row = self.conn.execute(
            "SELECT lock_key FROM resource_locks WHERE lock_type='inspector' AND ref_item_id=?",
            (item["id"],),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertTrue(row["lock_key"].endswith(":E05"))
        # 机构锁与区域锁仍在
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) c FROM resource_locks WHERE ref_item_id=? "
                "AND lock_type IN ('institution','region')", (item["id"],)
            ).fetchone()["c"], 2,
        )
        history = self.svc.item_history(item["id"])
        self.assertEqual(history[0]["inspector_id"], "E01")
        self.assertIn("换人", history[0]["change_note"])

    def _lock_is_e05(self, item_id: str) -> bool:
        row = self.conn.execute(
            "SELECT lock_key FROM resource_locks WHERE lock_type='inspector' AND ref_item_id=?",
            (item_id,),
        ).fetchone()
        return bool(row and row["lock_key"].endswith(":E05"))

    def test_swap_respects_capacity(self) -> None:
        self._confirmed()
        # E03 容量仅 1：把两个项都换成 E03，第二次应失败
        first = next(i for i in self.svc.list_items("P1") if i["institution_id"] == "I006")
        self.svc.swap_inspector(first["id"], "E03", first["version"])
        second = next(i for i in self.svc.list_items("P1") if i["institution_id"] == "I010")
        with self.assertRaises(StateConflict):
            self.svc.swap_inspector(second["id"], "E03", second["version"])

    def test_suspend_institution_cascades_and_releases_locks(self) -> None:
        self._confirmed()
        item = next(i for i in self.svc.list_items("P1") if i["institution_id"] == "I001")
        inst = InstitutionRepository(self.conn).get("I001")

        result = self.svc.suspend_institution("I001", inst["version"], actor="监管员陈")
        self.assertEqual(result["institution"]["status"], "停业")
        self.assertEqual(result["institution"]["version"], 2)
        self.assertIn(item["id"], result["dropped_items"])

        dropped = self.svc.get_item(item["id"])
        self.assertEqual(dropped["item_state"], "已剔除")
        self.assertEqual(dropped["version"], 2)
        # 该项的全部三把锁已释放
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) c FROM resource_locks WHERE ref_item_id=?", (item["id"],)
            ).fetchone()["c"], 0,
        )
        # 历史保留 v1
        hist = self.svc.item_history(item["id"])
        self.assertEqual(hist[0]["item_state"], "待检")
        self.assertIn("停业", hist[0]["change_note"])
        # 已剔除项不能改期/换人
        with self.assertRaises(StateConflict):
            self.svc.reschedule_item(item["id"], "2026-11-11", dropped["version"])
        with self.assertRaises(StateConflict):
            self.svc.swap_inspector(item["id"], "E05", dropped["version"])

    def test_temp_avoidance_change_invalidates_candidates(self) -> None:
        self.svc.create_plan("PT", QUARTER)
        self.gen_until_done("PT")
        before = next(c for c in self.svc.candidates("PT") if c["institution_id"] == "I001")
        self.assertEqual(before["proposed_inspector_id"], "E02")

        # 临时回避增加：E02 也回避 I001
        AvoidanceRepository(self.conn).add("E02", "I001", "临时调查关联")
        # 用旧候选确认 → 指纹不符，拒绝
        with self.assertRaises(VersionConflict):
            self.svc.confirm("PT", self.svc.get_plan("PT")["version"])
        # 重新生成后预分配改变
        self.svc.resume_generation("PT")
        after = next(c for c in self.svc.candidates("PT") if c["institution_id"] == "I001")
        self.assertNotEqual(after["proposed_inspector_id"], "E02")

        # 解除临时回避后历史可查（回避关系版本保留）
        AvoidanceRepository(self.conn).remove("E02", "I001")
        row = self.conn.execute(
            "SELECT * FROM avoidances_history WHERE inspector_id='E02' AND institution_id='I001'"
        ).fetchall()
        self.assertTrue(row)

    def test_snapshot_optimistic_lock(self) -> None:
        repo = InstitutionRepository(self.conn)
        with self.assertRaises(VersionConflict):
            repo.update_snapshot("I001", 999, {"risk_score": 50})


class PublishConcurrencyTest(PlannerTestBase):
    def test_concurrent_publish_optimistic_version(self) -> None:
        self.svc.create_plan("PP", QUARTER)
        self.gen_until_done("PP")
        self.svc.confirm("PP", self.svc.get_plan("PP")["version"])
        v_confirmed = self.svc.get_plan("PP")["version"]

        # 两个发布者持有同一版本，只有一个成功
        self.svc.publish("PP", v_confirmed, actor="发布者A")
        with self.assertRaises(VersionConflict):
            self.svc.publish("PP", v_confirmed, actor="发布者B")

        plan = self.svc.get_plan("PP")
        self.assertEqual(plan["state"], "已发布")
        # 计划历史保留了草稿→确认→发布的所有版本
        hist = self.svc.plan_history("PP")
        states = [h["state"] for h in hist]
        self.assertEqual(states, ["候选就绪", "已确认"])
        events = [e["event"] for e in self.svc.events("PP")]
        self.assertIn("published", events)


class HttpApiTest(PlannerTestBase):
    def setUp(self) -> None:
        super().setUp()
        from sampling_planner.api import build_server
        self.server = build_server(self.db_path, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def _req(self, method: str, path: str, body: dict | None = None, expect: int = 200):
        data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data if method != "GET" else None,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(resp.status, expect, payload)
                return payload
        except HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            self.assertEqual(exc.code, expect, payload)
            return payload

    def test_end_to_end_api(self) -> None:
        self.assertEqual(self._req("GET", "/health")["status"], "ok")

        # 创建计划并跑完生成
        self._req("POST", "/plans", {"id": "W1", "quarter": QUARTER}, expect=201)
        gen = self._req("POST", "/plans/W1/generate", {"until_done": True, "batch_size": 4})
        self.assertTrue(gen["finished"])
        self.assertEqual(gen["selected"], 6)

        explanations = self._req("GET", "/plans/W1/explanations")
        self.assertEqual(len(explanations), 14)
        self.assertTrue(all(e["explanation"] for e in explanations))

        plan = self._req("GET", "/plans/W1")
        confirmed = self._req("POST", "/plans/W1/confirm",
                              {"version": plan["version"], "actor": "负责人周"})
        self.assertEqual(confirmed["plan"]["state"], "已确认")

        # 改期走 HTTP，版本错误返回 409
        item = self._req("GET", "/plans/W1/items")[0]
        self._req("POST", f"/items/{item['id']}/reschedule",
                  {"scheduled_date": "2026-12-15", "version": item["version"]})
        err = self._req("POST", f"/items/{item['id']}/reschedule",
                        {"scheduled_date": "2026-12-16", "version": item["version"]},
                        expect=409)
        self.assertEqual(err["error"], "VERSION_CONFLICT")

        # 发布
        plan = self._req("GET", "/plans/W1")
        self._req("POST", "/plans/W1/publish", {"version": plan["version"]})

    def test_api_404_and_bad_request(self) -> None:
        self._req("GET", "/institutions/NOPE", expect=404)
        self._req("POST", "/plans", {"id": "BAD", "quarter": "2026-四季度"}, expect=400)


if __name__ == "__main__":
    unittest.main()
