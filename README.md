# 监管抽检计划

监管处在有限检查力量下安排季度抽检的 Python 服务端：维护机构风险快照、检查员资格、回避关系、区域容量与抽检规则；先生成**可解释候选**，由负责人确认后**原子锁定资源**；改期、换人、机构停业、并发发布全部保留版本；候选生成支持崩溃/重启后的**断点续跑**；API 对每个入选与未入选机构都给出结构化原因码与中文理由。

## 设计概览

```
主数据维护                计划两阶段
┌──────────────┐      ┌─────────────────────────────┐
│ 机构风险快照  │      │ 草稿 ──生成候选(可续跑)──► 候选就绪 │
│ 检查员资格    │─────►│   阶段A 逐机构粗筛（分批提交）  │
│ 回避关系      │      │   阶段B 规则名额/区域/检查员分配 │
│ 区域容量      │      │        │ 负责人确认(版本+1)     │
│ 抽检规则      │      │        ▼                      │
└──────────────┘      │ 已确认 ──发布(乐观锁)──► 已发布 │
                      └─────────────────────────────┘
```

- **零第三方依赖**：Python ≥ 3.11 标准库（`sqlite3` + `http.server`），WAL 模式。
- **三类资源锁**（`resource_locks` 表，同一主键即冲突）：
  - `institution:{quarter}:{id}` —— 机构季度唯一占用，杜绝跨小组重复占用；
  - `inspector:{quarter}:{id}` 的槽位 0..容量-1 —— 检查员季度容量；
  - `region:{quarter}:{region}` 的槽位 0..容量-1 —— 区域季度容量。
- **两阶段候选**：先逐机构粗筛（停业/快照过期/已占用/规则未命中），再按规则优先级与风险分排序做贪心分配，依次扣减规则名额、区域容量、检查员槽位。
- **断点续跑**：粗筛阶段每批独立事务提交，以机构 id 排序 + `generation_done` 游标记录进度；主数据任何变化通过数据指纹（`generation_token`）使旧候选自动作废重算。
- **版本与历史**：机构、检查员、规则、区域容量、回避关系、计划、计划项全部 `version` 乐观锁 + `*_history` 表；所有状态迁移写 `plan_events` 审计日志。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/sampling_planner/`：服务端实现。
  - `config.py`：状态机、原因码与中文模板；
  - `database.py`：SQLite schema、连接、可重入事务；
  - `repositories.py`：版本化仓储（历史归档、乐观版本检查）；
  - `engine.py`：候选生成引擎（规则匹配、贪心分配、原因码）；
  - `service.py`：计划生命周期、原子确认锁定、改期/换人/停业级联、并发发布；
  - `api.py`：JSON HTTP API（标准库）；
  - `seed.py`：14 家机构 / 5 名检查员 / 3 条规则的演示数据。
- `tools/check_contract.py`：契约摘要检查。
- `tests/`：契约回归 + 服务端 18 个用例（含真实 HTTP 与跨连接模拟崩溃）。

## 快速开始

```bash
# 1. 灌演示数据
PYTHONPATH=src python3 -m sampling_planner.seed --db data/sampling.db

# 2. 启动服务
PYTHONPATH=src python3 -m sampling_planner.api --db data/sampling.db --port 8000
```

## API 总览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST/GET | `/institutions` | 机构建档 / 列表 |
| GET | `/institutions/{id}` `/history` | 机构详情 / 快照历史版本 |
| PUT | `/institutions/{id}/snapshot` | 更新风险快照（**body 带 `version`**，冲突 409） |
| POST | `/institutions/{id}/suspend` | 机构停业，级联剔除已排计划项并释放锁 |
| POST/GET | `/inspectors`；`PUT /inspectors/{id}` | 检查员档案与季度容量 |
| POST | `/avoidances`；DELETE `/avoidances/{insp}/{inst}`；GET `/institutions/{id}/avoidances` | 回避关系（解除也留痕） |
| PUT | `/regions/{region}/capacity/{quarter}`；GET `/regions/capacity?quarter=` | 区域季度容量（改容量版本+1） |
| POST/GET | `/rules`；PUT `/rules/{id}` | 抽检规则（风险等级、门槛、行业、标签、资格、名额、优先级） |
| POST/GET | `/plans` | 同季度可有多份计划（不同小组），靠资源锁去重 |
| POST | `/plans/{id}/generate` | 推进一批候选；`{"until_done": true}` 直接跑完 |
| GET | `/plans/{id}/candidates` | 候选列表（含预分配检查员） |
| GET | `/plans/{id}/explanations` | **每个入选/未入选机构的理由**（原因码 + 中文） |
| POST | `/plans/{id}/confirm` | 负责人确认，单事务锁定全部资源（带 `version`，并发失败整体回滚 409） |
| POST | `/plans/{id}/publish` | 并发发布，乐观版本控制 409 |
| POST | `/plans/{id}/cancel` | 取消草稿/候选计划 |
| GET | `/plans/{id}/items` `/history` `/events` | 计划项 / 版本历史 / 审计事件 |
| POST | `/items/{id}/reschedule` | 改期（日期须在同一季度，版本+1） |
| POST | `/items/{id}/swap-inspector` | 换人（校验资格、回避、槽位，原子切锁） |
| GET | `/items/{id}` `/history` | 计划项详情 / 改期换人版本链 |

所有变更类请求必须在 JSON body 中携带所基于的当前 `version`；过期返回 `409 VERSION_CONFLICT`，由调用方重新读取后重试。

## 原因码（explanations）

| 代码 | 含义 |
|---|---|
| `SELECTED` | 按风险排序入选并预分配检查员 |
| `RULE_FILTERED` | 不满足任何启用规则 |
| `BELOW_CUTOFF` | 符合规则但名额已满、排序靠后 |
| `REGION_CAPACITY` | 区域季度容量已满或未配置 |
| `NO_QUALIFIED_INSPECTOR` | 无具备规则要求资格的检查员 |
| `AVOIDANCE` | 合格检查员全部存在回避关系 |
| `INSPECTOR_CAPACITY` | 无回避的合格检查员槽位均满 |
| `INSTITUTION_SUSPENDED` | 机构停业 |
| `ALREADY_PLANNED` | 本季度已被其他计划锁定 |
| `SNAPSHOT_STALE` | 风险快照超过 90 天 |

## 端到端示例

```bash
curl -X POST localhost:8000/plans -d '{"id":"Q4","quarter":"2026Q4"}'
curl -X POST localhost:8000/plans/Q4/generate -d '{"until_done":true}'
curl localhost:8000/plans/Q4/explanations          # 每个机构为何入选/落选
V=$(curl -s localhost:8000/plans/Q4 | python3 -c 'import json,sys;print(json.load(sys.stdin)["version"])')
curl -X POST localhost:8000/plans/Q4/confirm -d "{\"version\":$V,\"actor\":\"周处长\"}"
```

## 并发与故障语义

1. **跨小组重复占用**：两个小组可各自生成候选；先确认者获得机构唯一锁，后确认者在同一 `BEGIN IMMEDIATE` 事务内撞唯一约束，计划项与锁**全部回滚**，计划回到"候选就绪"，重新生成后被占机构标记为 `ALREADY_PLANNED`。
2. **临时回避打乱计划**：回避增删改变数据指纹，用旧候选确认会被拒（409），重新生成后预分配自动调整；回避的生效/解除均有历史版本。
3. **并发发布**：两人持同一版本发布，仅一人成功，另一人 409。
4. **机构停业**：主数据版本+1，所有已确认/已发布计划中的待办项标记"已剔除"（版本+1），三把锁原子释放；历史可查。
5. **进程崩溃**：生成进度按批持久化（游标+总数），重启后再次调用 `/generate` 即从断点继续；主数据已变则自动从头重算。

## 验证

```bash
# 全部测试（契约 + 服务端，含 HTTP 与崩溃续跑）
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tools tests

# 契约摘要
python3 tools/check_contract.py domain/contract.json
```
