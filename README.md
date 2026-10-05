# 监管抽检计划

在有限检查力量下安排季度抽检：维护机构风险快照、检查员资格、回避关系、区域容量与
抽检规则，先由系统**确定性生成可解释候选**，再由负责人确认并**原子锁定**机构、检查员
与区域名额；改期、替换检查员、机构停业与并发发布全部**保留版本**，未完成的候选生成
**可在进程重启后续跑**；API 对每个入选 / 未入选机构都给出结构化理由。

仅依赖 Python 标准库（>=3.11），持久化采用 SQLite（文件库默认 WAL，内存库为
共享缓存模式）。

## 目录

- `domain/contract.json`：领域角色、状态、约束（风险抽样规则、检查员回避、
  资源原子锁定、计划断点续跑）和样例。
- `src/domain_contract/`：契约读取与确定性校验（既有）。
- `src/inspection_planning/`：抽检计划服务端实现。
  - `models.py`：风险快照、检查员（含季度检查容量）、回避、容量、候选/任务/计划。
  - `store.py`：版本化持久化、`BEGIN IMMEDIATE` 串行写事务、断点游标。
  - `planner.py`：确定性、可解释的风险优先候选规则引擎（无状态，可重放）。
  - `service.py`：候选生成（断点续跑）、确认锁定、发布、改期、换人、停业联动。
  - `api.py`：基于 `http.server` 的 JSON API（含完整错误映射）。
- `tools/check_contract.py`：契约摘要检查（既有）。
- `tools/demo.py`：完整工作流演示，打印每家机构入选/未入选的理由与版本轨迹。
- `tests/`：契约测试 + 规则/锁定/版本/续跑/API 共 22 项回归测试。

## 核心规则与不变量

1. **风险抽样规则**：仅对在营且有风险快照的机构抽样；按 高>中>低、同级按风险分
   降序、再按机构编号升序确定排序，同一输入结果恒定。高风险先占用区域有限名额，
   低风险仅在容量有余时入选。
2. **检查员回避与有限力量**：检查员有资格、服务区域、季度任务容量（默认 2 项）；
   存在生效回避、资格/区域不符、停用或季度工作量占满时不可派，并记录具体原因。
   派位按「总工作量最少、编号最小」做负载均衡，避免把任务压给同一人。
3. **资源原子锁定**：确认在**单事务**内按最新数据重新校验（停业、跨小组重复占用
   机构、区域容量、检查员资格/区域/回避/工作量），任一冲突整批回滚，不留半个锁定。
4. **版本保留**：风险快照、检查员资格、回避（含解除）、容量、计划每一变化（候选、
   确认、改期、换人、停业联动、发布、归档）都追加版本，旧版本随时可读、可对比。
5. **断点续跑**：候选生成按固定顺序逐机构评估，每批提交一次持久化游标；中途停止或
   重启后再次调用即可续跑，结果与一次跑完完全一致。若生成期间主数据发生变化（有了
   新版本），旧运行自动作废并按最新数据重跑。
6. **并发发布**：发布带 `expected_version` 乐观校验，版本已推进则 409 拒绝；
   进程内写事务由锁串行化，跨进程由 SQLite 锁兜底。

计划状态机：`草稿 → 候选待确认 → 已锁定 → 已发布 → 已归档`
（与契约状态 登记/待核验/处置中/已决定/已归档 对应）。

## 验证

```bash
# 全部回归测试
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tools tests

# 契约摘要
python3 tools/check_contract.py domain/contract.json

# 端到端演示
python3 tools/demo.py
```

## HTTP API 启动

```bash
python3 -m inspection_planning.api --db data/planning.db --host 0.0.0.0 --port 8080
```

运行 `PYTHONPATH=src python3 -m inspection_planning.api --help` 查看参数。
注意模块运行需要 `src` 在模块搜索路径中（或把 `src` 加入 `PYTHONPATH`）。

## API 总览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/institutions` | 登记机构 / 追加风险快照版本 |
| GET | `/institutions` | 机构与最新快照列表 |
| GET | `/institutions/{id}/risk-versions` | 机构风险快照全部版本 |
| POST | `/institutions/{id}/close` | 机构停业（联动已锁定计划，释放资源） |
| POST | `/inspectors` | 登记检查员 / 追加资格版本（含季度容量） |
| GET | `/inspectors` | 检查员最新资格列表 |
| GET | `/inspectors/{id}/versions` | 检查员资格全部版本 |
| POST | `/recusals` | 新增临时回避（如亲属任职） |
| POST | `/recusals/lift` | 解除回避（同样留版本） |
| GET | `/recusals` | 回避关系版本流水 |
| POST | `/regions/capacity` | 设置区域季度容量（新版本） |
| GET | `/regions/capacity` | 当前容量 |
| POST | `/plans` | 创建季度计划（草稿） |
| GET | `/plans` | 计划列表 |
| GET | `/plans/{id}` | 计划当前版本明细（`?version=N` 读历史版本） |
| GET | `/plans/{id}/versions` | 计划版本轨迹 |
| POST | `/plans/{id}/candidates/generate` | 生成 / 续跑候选（支持 `batch_size`、`force_restart`） |
| GET | `/plans/{id}/candidates` | 候选及每家机构的理由、续跑状态 |
| POST | `/plans/{id}/confirm` | 负责人确认，单事务原子锁定（可带 `candidate_version`） |
| POST | `/plans/{id}/publish` | 发布（可带 `expected_version` 并发校验） |
| POST | `/plans/{id}/reschedule` | 改期（保留锁定，新版本留存） |
| POST | `/plans/{id}/replace-inspector` | 换人（同事务重新校验并锁定） |
| GET | `/events` | 全局版本事件流 |
| POST | `/plans/{id}/archive` | 发布后归档 |

错误码：`400` 请求错误、`404` 不存在、`409` 状态不符或资源冲突（含候选版本过期、
跨小组重复占用、容量/工作量不足、回避等），响应统一为 `{"error": "..."}`。

## 示例：生成候选与确认

```bash
curl -s -X POST localhost:8080/plans/P1/candidates/generate \
  -H 'Content-Type: application/json' -d '{"batch_size": 50}'
curl -s localhost:8080/plans/P1/candidates        # 每家机构入选/未入选的理由
curl -s -X POST localhost:8080/plans/P1/confirm \
  -H 'Content-Type: application/json' -d '{"candidate_version": 2}'
```
