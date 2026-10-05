"""灌入演示数据：机构风险快照、检查员、回避关系、区域容量、抽检规则。

用法：python3 -m sampling_planner.seed --db data/sampling.db
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .database import connect, init_db, utcnow
from .repositories import (
    AvoidanceRepository,
    InstitutionRepository,
    InspectorRepository,
    RegionCapacityRepository,
    SamplingRuleRepository,
)

QUARTER = "2026Q4"


def _iso(days_ago: int) -> str:
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def seed(db_path: str) -> dict[str, int]:
    conn = connect(db_path)
    init_db(conn)

    insts = InstitutionRepository(conn)
    inspectors = InspectorRepository(conn)
    avoid = AvoidanceRepository(conn)
    caps = RegionCapacityRepository(conn)
    rules = SamplingRuleRepository(conn)

    # ---------- 机构（华东/华南/华北，覆盖高/中/低风险与停业、快照过期） ----------
    institutions = [
        ("I001", "华信融资担保公司", "华东", "金融", "高", 92.5, ["重点监控"]),
        ("I002", "浦东健康体检中心", "华东", "医疗", "高", 88.0, []),
        ("I003", "申城小额贷款股份", "华东", "金融", "高", 81.5, ["重点监控"]),
        ("I004", "长三角医疗器械公司", "华东", "医疗", "中", 74.0, []),
        ("I005", "外滩财富管理公司", "华东", "金融", "中", 66.0, []),
        ("I006", "珠江融资租赁集团", "华南", "金融", "高", 90.0, ["重点监控"]),
        ("I007", "深圳和睦门诊部", "华南", "医疗", "中", 70.5, []),
        ("I008", "羊城普惠小贷", "华南", "金融", "中", 62.0, []),
        ("I009", "南海保健品连锁", "华南", "食品", "低", 41.0, []),
        ("I010", "中关村科技金融", "华北", "金融", "高", 85.5, []),
        ("I011", "亦庄康复医院", "华北", "医疗", "中", 72.0, []),
        ("I012", "渤海典当行", "华北", "金融", "低", 35.0, []),
        ("I013", "已歇业投资咨询公司", "华东", "金融", "高", 95.0, ["重点监控"]),  # 停业
        ("I014", "旧档案信托公司", "华北", "金融", "高", 89.0, []),              # 快照过期
    ]
    for iid, name, region, industry, level, score, tags in institutions:
        insts.create({
            "id": iid, "name": name, "region": region, "industry": industry,
            "risk_level": level, "risk_score": score, "tags": tags,
            "snapshot_at": _iso(120 if iid == "I014" else 10),
            "status": "停业" if iid == "I013" else "营业",
        })

    # ---------- 检查员（资格 + 季度容量） ----------
    people = [
        ("E01", "王监管", ["金融"], 2),
        ("E02", "李核查", ["金融", "医疗"], 2),
        ("E03", "张风控", ["金融"], 1),
        ("E04", "赵医监", ["医疗"], 2),
        ("E05", "钱巡查", ["金融", "医疗"], 3),
    ]
    for eid, name, quals, cap in people:
        inspectors.create({
            "id": eid, "name": name, "qualifications": quals,
            "quarterly_capacity": cap,
        })

    # ---------- 回避关系 ----------
    # E01 与 I001 有亲属回避；E02 与 I006 有业务回避；
    # I003 与所有金融资格检查员回避（演示 AVOIDANCE 全员回避）
    avoid.add("E01", "I001", "检查员配偶任职该机构")
    avoid.add("E02", "I006", "近两年曾为该机构提供咨询")
    avoid.add("E03", "I003", "亲属任职")
    # E01 也回避 I003 → 金融检查员 E01/E03 全回避，仅剩 E02/E05 可用
    avoid.add("E01", "I003", "曾参与该机构年审")

    # ---------- 区域季度容量 ----------
    caps.set("华东", QUARTER, 3)
    caps.set("华南", QUARTER, 2)
    caps.set("华北", QUARTER, 2)

    # ---------- 抽检规则 ----------
    rules.create({
        "id": "R-HIGH-FIN",
        "name": "高风险金融机构重点抽检",
        "risk_levels": ["高"],
        "min_score": 85,
        "industries": ["金融"],
        "required_tag": "",
        "required_qualification": "金融",
        "quota": 3,
        "priority": 10,
    })
    rules.create({
        "id": "R-TAG-WATCH",
        "name": "重点监控标签机构抽检",
        "risk_levels": ["高", "中"],
        "min_score": 0,
        "industries": [],
        "required_tag": "重点监控",
        "required_qualification": "金融",
        "quota": 2,
        "priority": 20,
    })
    rules.create({
        "id": "R-MED",
        "name": "医疗机构常规抽检",
        "risk_levels": ["高", "中"],
        "min_score": 70,
        "industries": ["医疗"],
        "required_tag": "",
        "required_qualification": "医疗",
        "quota": 2,
        "priority": 30,
    })

    conn.close()
    return {"institutions": len(institutions), "inspectors": len(people),
            "rules": 3, "quarter": QUARTER}


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="灌入演示数据")
    parser.add_argument("--db", default="data/sampling.db")
    args = parser.parse_args()
    print(seed(args.db))


if __name__ == "__main__":
    main()
