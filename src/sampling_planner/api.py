"""JSON HTTP API：基于标准库 http.server，线程安全（每请求独立连接 + SQLite 串行写）。"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from . import __version__
from .database import (
    NotFound,
    StateConflict,
    VersionConflict,
    connect,
    immediate_tx,
    init_db,
)
from .repositories import (
    AvoidanceRepository,
    InstitutionRepository,
    InspectorRepository,
    RegionCapacityRepository,
    SamplingRuleRepository,
)
from .service import PlanService

# (方法, 正则) -> 处理器函数(handler, conn, params, body)
Route = tuple[str, re.Pattern[str], Callable[..., Any]]


def _ok(value: Any) -> tuple[int, Any]:
    return 200, value


def make_routes() -> list[Route]:
    routes: list[Route] = []

    def route(method: str, pattern: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        rx = re.compile("^" + pattern + "$")

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            routes.append((method, rx, fn))
            return fn
        return deco

    # ---------- 健康检查 ----------
    @route("GET", r"/health")
    def health(h: "Handler", conn, p, b):
        return _ok({"status": "ok", "service": "sampling-planner", "version": __version__})

    # ---------- 机构 / 风险快照 ----------
    @route("POST", r"/institutions")
    def create_institution(h, conn, p, b):
        return 201, InstitutionRepository(conn).create(b)

    @route("GET", r"/institutions")
    def list_institutions(h, conn, p, b):
        return _ok(InstitutionRepository(conn).list())

    @route("GET", r"/institutions/(?P<id>[^/]+)")
    def get_institution(h, conn, p, b):
        return _ok(InstitutionRepository(conn).get(p["id"]))

    @route("GET", r"/institutions/(?P<id>[^/]+)/history")
    def institution_history(h, conn, p, b):
        return _ok(InstitutionRepository(conn).history(p["id"]))

    @route("PUT", r"/institutions/(?P<id>[^/]+)/snapshot")
    def update_snapshot(h, conn, p, b):
        version = _require_version(b)
        note = b.pop("_note", "风险快照更新")
        return _ok(InstitutionRepository(conn).update_snapshot(p["id"], version, b, note=note))

    @route("POST", r"/institutions/(?P<id>[^/]+)/suspend")
    def suspend(h, conn, p, b):
        version = _require_version(b)
        return _ok(PlanService(conn).suspend_institution(p["id"], version, actor=_actor(b)))

    # ---------- 检查员 ----------
    @route("POST", r"/inspectors")
    def create_inspector(h, conn, p, b):
        return 201, InspectorRepository(conn).create(b)

    @route("GET", r"/inspectors")
    def list_inspectors(h, conn, p, b):
        return _ok(InspectorRepository(conn).list())

    @route("GET", r"/inspectors/(?P<id>[^/]+)")
    def get_inspector(h, conn, p, b):
        return _ok(InspectorRepository(conn).get(p["id"]))

    @route("GET", r"/inspectors/(?P<id>[^/]+)/history")
    def inspector_history(h, conn, p, b):
        return _ok(InspectorRepository(conn).history(p["id"]))

    @route("PUT", r"/inspectors/(?P<id>[^/]+)")
    def update_inspector(h, conn, p, b):
        version = _require_version(b)
        return _ok(InspectorRepository(conn).update(p["id"], version, b))

    # ---------- 回避 ----------
    @route("POST", r"/avoidances")
    def add_avoidance(h, conn, p, b):
        return 201, AvoidanceRepository(conn).add(
            b["inspector_id"], b["institution_id"], b.get("reason", "")
        )

    @route("GET", r"/institutions/(?P<id>[^/]+)/avoidances")
    def list_avoidance(h, conn, p, b):
        return _ok(AvoidanceRepository(conn).list_for_institution(p["id"]))

    @route("DELETE", r"/avoidances/(?P<inspector>[^/]+)/(?P<institution>[^/]+)")
    def remove_avoidance(h, conn, p, b):
        AvoidanceRepository(conn).remove(p["inspector"], p["institution"])
        return _ok({"deleted": True})

    # ---------- 区域容量 ----------
    @route("PUT", r"/regions/(?P<region>[^/]+)/capacity/(?P<quarter>[^/]+)")
    def set_capacity(h, conn, p, b):
        return _ok(RegionCapacityRepository(conn).set(
            p["region"], p["quarter"], int(b["capacity"])
        ))

    @route("GET", r"/regions/capacity")
    def list_capacity(h, conn, p, b):
        q = h.query.get("quarter", [None])[0]
        return _ok(RegionCapacityRepository(conn).list(q))

    # ---------- 抽检规则 ----------
    @route("POST", r"/rules")
    def create_rule(h, conn, p, b):
        return 201, SamplingRuleRepository(conn).create(b)

    @route("GET", r"/rules")
    def list_rules(h, conn, p, b):
        return _ok(SamplingRuleRepository(conn).list())

    @route("GET", r"/rules/(?P<id>[^/]+)")
    def get_rule(h, conn, p, b):
        return _ok(SamplingRuleRepository(conn).get(p["id"]))

    @route("PUT", r"/rules/(?P<id>[^/]+)")
    def update_rule(h, conn, p, b):
        version = _require_version(b)
        return _ok(SamplingRuleRepository(conn).update(p["id"], version, b))

    # ---------- 计划 ----------
    @route("POST", r"/plans")
    def create_plan(h, conn, p, b):
        return 201, PlanService(conn).create_plan(
            b["id"], b["quarter"], int(b.get("target_count", 0))
        )

    @route("GET", r"/plans")
    def list_plans(h, conn, p, b):
        return _ok(PlanService(conn).list_plans())

    @route("GET", r"/plans/(?P<id>[^/]+)")
    def get_plan(h, conn, p, b):
        return _ok(PlanService(conn).get_plan(p["id"]))

    @route("POST", r"/plans/(?P<id>[^/]+)/generate")
    def generate(h, conn, p, b):
        svc = PlanService(conn)
        if b.get("until_done"):
            result = svc.resume_generation(
                p["id"], batch_size=int(b.get("batch_size", 100))
            )
        else:
            result = svc.generate(p["id"], batch_size=int(b.get("batch_size", 100)))
        return _ok(result)

    @route("GET", r"/plans/(?P<id>[^/]+)/candidates")
    def candidates(h, conn, p, b):
        return _ok(PlanService(conn).candidates(p["id"]))

    @route("GET", r"/plans/(?P<id>[^/]+)/explanations")
    def explanations(h, conn, p, b):
        return _ok(PlanService(conn).explanations(p["id"]))

    @route("POST", r"/plans/(?P<id>[^/]+)/confirm")
    def confirm(h, conn, p, b):
        return _ok(PlanService(conn).confirm(p["id"], _require_version(b), actor=_actor(b)))

    @route("POST", r"/plans/(?P<id>[^/]+)/publish")
    def publish(h, conn, p, b):
        return _ok(PlanService(conn).publish(p["id"], _require_version(b), actor=_actor(b)))

    @route("POST", r"/plans/(?P<id>[^/]+)/cancel")
    def cancel(h, conn, p, b):
        return _ok(PlanService(conn).cancel(p["id"], _require_version(b), actor=_actor(b)))

    @route("GET", r"/plans/(?P<id>[^/]+)/items")
    def items(h, conn, p, b):
        return _ok(PlanService(conn).list_items(p["id"]))

    @route("GET", r"/plans/(?P<id>[^/]+)/history")
    def plan_history(h, conn, p, b):
        return _ok(PlanService(conn).plan_history(p["id"]))

    @route("GET", r"/plans/(?P<id>[^/]+)/events")
    def events(h, conn, p, b):
        return _ok(PlanService(conn).events(p["id"]))

    # ---------- 计划项操作 ----------
    @route("GET", r"/items/(?P<id>[^/]+)")
    def get_item(h, conn, p, b):
        return _ok(PlanService(conn).get_item(p["id"]))

    @route("GET", r"/items/(?P<id>[^/]+)/history")
    def item_history(h, conn, p, b):
        return _ok(PlanService(conn).item_history(p["id"]))

    @route("POST", r"/items/(?P<id>[^/]+)/reschedule")
    def reschedule(h, conn, p, b):
        return _ok(PlanService(conn).reschedule_item(
            p["id"], b["scheduled_date"], _require_version(b), actor=_actor(b)
        ))

    @route("POST", r"/items/(?P<id>[^/]+)/swap-inspector")
    def swap(h, conn, p, b):
        return _ok(PlanService(conn).swap_inspector(
            p["id"], b["inspector_id"], _require_version(b), actor=_actor(b)
        ))

    return routes


def _require_version(body: dict[str, Any]) -> int:
    if "version" not in body:
        raise ValueError("请求体必须包含 version（乐观锁，取值为当前资源版本号）")
    return int(body["version"])


def _actor(body: dict[str, Any]) -> str:
    return str(body.get("actor", ""))


class Handler(BaseHTTPRequestHandler):
    routes: list[Route] = []
    db_path: str = ""
    write_lock = threading.Lock()
    query: dict[str, list[str]] = {}

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, code: str, message: str) -> None:
        self._send(status, {"error": code, "message": message})

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        from urllib.parse import parse_qs
        self.query = parse_qs(parsed.query)

        for route_method, rx, fn in self.routes:
            if route_method != method:
                continue
            m = rx.match(path)
            if not m:
                continue
            body: dict[str, Any] = {}
            if method in ("POST", "PUT"):
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    raw = self.rfile.read(length) if length else b"{}"
                    body = json.loads(raw or b"{}")
                    if not isinstance(body, dict):
                        raise ValueError("请求体必须是 JSON 对象")
                except (json.JSONDecodeError, ValueError) as exc:
                    self._error(400, "BAD_REQUEST", f"请求体解析失败：{exc}")
                    return
            # 写操作全局串行化；写请求统一事务包裹（服务层内嵌调用走 SAVEPOINT）
            with self.write_lock:
                conn = connect(self.db_path)
                try:
                    init_db(conn)
                    if method in ("POST", "PUT", "DELETE"):
                        with immediate_tx(conn):
                            status, payload = fn(self, conn, m.groupdict(), body)
                    else:
                        status, payload = fn(self, conn, m.groupdict(), body)
                    conn.close()
                except VersionConflict as exc:
                    conn.close()
                    self._error(409, "VERSION_CONFLICT", str(exc))
                    return
                except StateConflict as exc:
                    conn.close()
                    self._error(409, "STATE_CONFLICT", str(exc))
                    return
                except NotFound as exc:
                    conn.close()
                    self._error(404, "NOT_FOUND", str(exc))
                    return
                except (ValueError, KeyError) as exc:
                    conn.close()
                    self._error(400, "BAD_REQUEST", str(exc))
                    return
                except LookupError as exc:
                    conn.close()
                    self._error(404, "NOT_FOUND", f"计划不存在：{exc}")
                    return
            self._send(status, payload)
            return
        self._error(404, "NOT_FOUND", f"无此路由：{method} {path}")


def build_server(db_path: str, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    Handler.routes = make_routes()
    Handler.db_path = db_path
    # 确保库已初始化
    conn = connect(db_path)
    init_db(conn)
    conn.close()
    server = ThreadingHTTPServer((host, port), Handler)
    server.verbose = False
    return server


def main(argv: list[str] | None = None) -> None:
    import argparse
    parser = argparse.ArgumentParser(description="监管抽检计划服务端")
    parser.add_argument("--db", default="data/sampling.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    server = build_server(args.db, args.host, args.port)
    print(f"监管抽检计划服务已启动: http://{args.host}:{args.port}  (db={args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
