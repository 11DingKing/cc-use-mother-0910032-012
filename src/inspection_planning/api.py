"""基于标准库 http.server 的 JSON API。

启动：
    python -m inspection_planning.api --db data/planning.db --host 127.0.0.1 --port 8080

所有响应均为 JSON；业务错误返回 {"error": ...} 与对应状态码
（404 不存在 / 409 冲突或状态不符 / 400 请求错误）。
"""
from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .service import PlanningService
from .store import ConflictError, NotFoundError, StateError, Store


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (tuple, set, frozenset)):
        return list(obj)
    raise TypeError(f"不可序列化类型：{type(obj)!r}")


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "InspectionPlanning/1.0"

    # 注入属性
    store: Store
    service: PlanningService

    # ---- 工具 -------------------------------------------------------

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, default=_json_default).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _HttpError(400, f"请求体不是合法 JSON：{exc}")
        if not isinstance(value, dict):
            raise _HttpError(400, "请求体必须是 JSON 对象")
        return value

    def _require(self, data: dict[str, Any], key: str) -> Any:
        if key not in data or data[key] in (None, ""):
            raise _HttpError(400, f"缺少必填字段：{key}")
        return data[key]

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if getattr(self.server, "quiet", False):
            return
        super().log_message(fmt, *args)

    # ---- 路由 -------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            handler, groups = self.server.router.match(method, path)
            data = {} if method == "GET" else self._read_json()
            result = handler(self, groups, query, data)
            if result is None:
                result = {"ok": True}
            body, status = result if isinstance(result, tuple) else (result, 200)
            self._send_json(body, status)
        except _HttpError as exc:
            self._send_json({"error": exc.message}, exc.status)
        except (ConflictError, StateError) as exc:
            self._send_json({"error": str(exc)}, 409)
        except NotFoundError as exc:
            self._send_json({"error": str(exc)}, 404)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:  # pragma: no cover - 防御性
            self._send_json({"error": f"服务器内部错误：{exc}"}, 500)


class _HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# 极简模式路由：("METHOD", compiled_regex, 命名分组元组)
class Router:
    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern[str], tuple[str, ...], Callable]] = []

    def add(self, method: str, pattern: str, handler: Callable) -> None:
        names = tuple(re.findall(r"\{(\w+)\}", pattern))
        regex = "^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$"
        self._routes.append((method, re.compile(regex), names, handler))

    def match(self, method: str, path: str):
        for m, regex, _names, handler in self._routes:
            if m != method:
                continue
            found = regex.match(path)
            if found:
                return handler, found.groupdict()
        raise _HttpError(404, f"未找到接口：{method} {path}")


# ---- 处理函数 ----------------------------------------------------------------

def h_health(h: ApiHandler, groups, query, data) -> Any:
    return {"status": "ok", "service": "监管抽检计划服务"}


def h_upsert_institution(h: ApiHandler, groups, query, data) -> Any:
    inst_id = h._require(data, "institution_id")
    name = h._require(data, "name")
    region = h._require(data, "region")
    level = h._require(data, "risk_level")
    if level not in ("高", "中", "低"):
        raise _HttpError(400, "risk_level 必须为 高/中/低")
    score = float(h._require(data, "risk_score"))
    if not 0 <= score <= 100:
        raise _HttpError(400, "risk_score 需在 0~100 之间")
    factors = data.get("risk_factors") or []
    if not isinstance(factors, list):
        raise _HttpError(400, "risk_factors 必须是数组")
    res = h.store.upsert_institution(
        inst_id, name, region, level, score, factors,
        snapshot_note=data.get("snapshot_note", ""),
        active=bool(data.get("active", True)),
    )
    return res, 201


def h_list_institutions(h: ApiHandler, groups, query, data) -> Any:
    items = [i.to_dict() for i in h.store.list_institutions()]
    return {"count": len(items), "institutions": items}


def h_risk_versions(h: ApiHandler, groups, query, data) -> Any:
    inst_id = groups["institution_id"]
    items = [s.to_dict() for s in h.store.list_risk_versions(inst_id)]
    return {"institution_id": inst_id, "versions": items}


def h_close_institution(h: ApiHandler, groups, query, data) -> Any:
    inst_id = groups["institution_id"]
    reason = h._require(data, "reason")
    return h.service.close_institution(inst_id, reason), 200


def h_upsert_inspector(h: ApiHandler, groups, query, data) -> Any:
    insp_id = h._require(data, "inspector_id")
    name = h._require(data, "name")
    quals = h._require(data, "qualifications")
    regions = h._require(data, "regions")
    if not isinstance(quals, list) or not isinstance(regions, list):
        raise _HttpError(400, "qualifications / regions 必须是数组")
    capacity = int(data.get("quarterly_capacity", 2))
    res = h.store.upsert_inspector(
        insp_id, name, quals, regions,
        active=bool(data.get("active", True)),
        quarterly_capacity=capacity,
    )
    return res, 201


def h_list_inspectors(h: ApiHandler, groups, query, data) -> Any:
    items = [i.to_dict() for i in h.store.list_inspectors()]
    return {"count": len(items), "inspectors": items}


def h_inspector_versions(h: ApiHandler, groups, query, data) -> Any:
    insp_id = groups["inspector_id"]
    versions = h.store.list_inspector_versions(insp_id)
    if not versions:
        raise NotFoundError(f"检查员不存在：{insp_id}")
    return {"inspector_id": insp_id, "versions": versions}


def h_add_recusal(h: ApiHandler, groups, query, data) -> Any:
    insp_id = h._require(data, "inspector_id")
    inst_id = h._require(data, "institution_id")
    reason = h._require(data, "reason")
    return h.store.add_recusal(insp_id, inst_id, reason), 201


def h_lift_recusal(h: ApiHandler, groups, query, data) -> Any:
    insp_id = h._require(data, "inspector_id")
    inst_id = h._require(data, "institution_id")
    reason = h._require(data, "reason")
    return h.store.lift_recusal(insp_id, inst_id, reason)


def h_list_recusals(h: ApiHandler, groups, query, data) -> Any:
    return {"recusals": h.store.list_recusal_versions()}


def h_set_capacity(h: ApiHandler, groups, query, data) -> Any:
    region = h._require(data, "region")
    capacity = int(h._require(data, "capacity"))
    return h.store.set_capacity(region, capacity), 201


def h_capacities(h: ApiHandler, groups, query, data) -> Any:
    return {
        "regions": [
            {"region": r, "capacity": cap, "version": ver}
            for r, (cap, ver) in h.store.active_capacities().items()
        ]
    }


def h_create_plan(h: ApiHandler, groups, query, data) -> Any:
    plan_id = h._require(data, "plan_id")
    quarter = h._require(data, "quarter")
    inspection_type = h._require(data, "inspection_type")
    plan = h.store.create_plan(plan_id, quarter, inspection_type)
    return plan.to_dict(), 201


def h_list_plans(h: ApiHandler, groups, query, data) -> Any:
    plans = []
    for p in h.store.list_plans():
        d = p.to_dict()
        # 列表视图不展开任务明细，保持轻量
        d["assignment_count"] = len(d.pop("assignments"))
        plans.append(d)
    return {"count": len(plans), "plans": plans}


def h_get_plan(h: ApiHandler, groups, query, data) -> Any:
    version = int(query["version"]) if query.get("version") else None
    plan = h.store.get_plan(groups["plan_id"], version)
    if plan is None:
        raise NotFoundError(f"计划不存在：{groups['plan_id']}")
    return plan.to_dict()


def h_plan_versions(h: ApiHandler, groups, query, data) -> Any:
    plan_id = groups["plan_id"]
    if h.store.get_plan(plan_id) is None:
        raise NotFoundError(f"计划不存在：{plan_id}")
    return {"plan_id": plan_id,
            "versions": h.store.list_plan_versions(plan_id)}


def h_generate_candidates(h: ApiHandler, groups, query, data) -> Any:
    plan_id = groups["plan_id"]
    batch_size = int(data.get("batch_size") or query.get("batch_size") or 25)
    force = bool(data.get("force_restart"))
    result = h.service.generate_candidates(
        plan_id, resume=not force, batch_size=batch_size
    )
    status = 200
    if result.get("state") == "finalized":
        result["candidates"] = h.service.get_candidates(plan_id)["candidates"]
    return result, status


def h_get_candidates(h: ApiHandler, groups, query, data) -> Any:
    return h.service.get_candidates(groups["plan_id"])


def h_confirm_plan(h: ApiHandler, groups, query, data) -> Any:
    plan_id = groups["plan_id"]
    cv = data.get("candidate_version")
    if cv is not None:
        cv = int(cv)
    selected = data.get("selected_institutions")
    confirm_all = not selected
    return h.service.confirm_plan(
        plan_id,
        candidate_version=cv,
        confirm_all=confirm_all,
        selected_institutions=selected,
    )


def h_publish_plan(h: ApiHandler, groups, query, data) -> Any:
    expected = data.get("expected_version")
    return h.service.publish_plan(
        groups["plan_id"],
        expected_version=int(expected) if expected is not None else None,
    )


def h_reschedule(h: ApiHandler, groups, query, data) -> Any:
    return h.service.reschedule_assignment(
        groups["plan_id"],
        h._require(data, "institution_id"),
        h._require(data, "new_date"),
        data.get("reason", ""),
    )


def h_replace_inspector(h: ApiHandler, groups, query, data) -> Any:
    return h.service.replace_inspector(
        groups["plan_id"],
        h._require(data, "institution_id"),
        h._require(data, "new_inspector_id"),
        data.get("reason", ""),
    )


def h_archive_plan(h: ApiHandler, groups, query, data) -> Any:
    return h.service.archive_plan(groups["plan_id"])


def h_events(h: ApiHandler, groups, query, data) -> Any:
    limit = int(query.get("limit") or 100)
    return {"events": h.store.list_events(limit=limit)}


def build_router() -> Router:
    r = Router()
    r.add("GET", "/health", h_health)

    r.add("POST", "/institutions", h_upsert_institution)
    r.add("GET", "/institutions", h_list_institutions)
    r.add("GET", "/institutions/{institution_id}/risk-versions", h_risk_versions)
    r.add("POST", "/institutions/{institution_id}/close", h_close_institution)

    r.add("POST", "/inspectors", h_upsert_inspector)
    r.add("GET", "/inspectors", h_list_inspectors)
    r.add("GET", "/inspectors/{inspector_id}/versions", h_inspector_versions)

    r.add("POST", "/recusals", h_add_recusal)
    r.add("POST", "/recusals/lift", h_lift_recusal)
    r.add("GET", "/recusals", h_list_recusals)

    r.add("POST", "/regions/capacity", h_set_capacity)
    r.add("GET", "/regions/capacity", h_capacities)

    r.add("POST", "/plans", h_create_plan)
    r.add("GET", "/plans", h_list_plans)
    r.add("GET", "/plans/{plan_id}", h_get_plan)
    r.add("GET", "/plans/{plan_id}/versions", h_plan_versions)
    r.add("POST", "/plans/{plan_id}/candidates/generate", h_generate_candidates)
    r.add("GET", "/plans/{plan_id}/candidates", h_get_candidates)
    r.add("POST", "/plans/{plan_id}/confirm", h_confirm_plan)
    r.add("POST", "/plans/{plan_id}/publish", h_publish_plan)
    r.add("POST", "/plans/{plan_id}/reschedule", h_reschedule)
    r.add("POST", "/plans/{plan_id}/replace-inspector", h_replace_inspector)
    r.add("POST", "/plans/{plan_id}/archive", h_archive_plan)

    r.add("GET", "/events", h_events)
    return r


def create_server(
    db_path: str, host: str = "127.0.0.1", port: int = 8080, quiet: bool = False
) -> ThreadingHTTPServer:
    store = Store(db_path)
    service = PlanningService(store)

    class Handler(ApiHandler):
        pass

    Handler.store = store
    Handler.service = service

    server = ThreadingHTTPServer((host, port), Handler)
    server.router = build_router()  # type: ignore[attr-defined]
    server.quiet = quiet  # type: ignore[attr-defined]
    server.store = store  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="监管抽检计划服务端")
    parser.add_argument("--db", default="data/planning.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)

    server = create_server(args.db, args.host, args.port)
    print(
        f"监管抽检计划服务已启动：http://{args.host}:{args.port} "
        f"（数据库 {args.db}）",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断，关闭服务……")
    finally:
        server.shutdown()
        server.server_close()
        server.store.close()


if __name__ == "__main__":
    main()
