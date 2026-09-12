from __future__ import annotations

import logging
import time
import uuid
from contextvars import ContextVar

from fastapi import FastAPI, Request
from prometheus_client import Counter, Histogram, make_asgi_app

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
log = logging.getLogger("jijin.http")

HTTP_REQUESTS = Counter(
    "jijin_http_requests_total",
    "HTTP requests handled by the JIJIN API",
    ["method", "route", "status"],
)
HTTP_REQUEST_DURATION = Histogram(
    "jijin_http_request_duration_seconds",
    "HTTP request latency for the JIJIN API",
    ["method", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)


def configure_logging(level: str) -> None:
    normalized = level.upper().strip()
    numeric = getattr(logging, normalized, logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric)
    for handler in root.handlers:
        handler.setLevel(numeric)


def install_observability(app: FastAPI) -> None:
    @app.middleware("http")
    async def observe_request(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500
        response = None
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        except Exception:
            route = _route_label(request)
            duration = time.perf_counter() - started
            HTTP_REQUESTS.labels(request.method, route, "500").inc()
            HTTP_REQUEST_DURATION.labels(request.method, route).observe(duration)
            log.exception(
                "http_request_failed request_id=%s method=%s route=%s duration_ms=%.2f",
                request_id,
                request.method,
                route,
                duration * 1000,
            )
            raise
        finally:
            if response is not None:
                route = _route_label(request)
                duration = time.perf_counter() - started
                HTTP_REQUESTS.labels(request.method, route, str(status_code)).inc()
                HTTP_REQUEST_DURATION.labels(request.method, route).observe(duration)
                log.info(
                    "http_request request_id=%s method=%s route=%s status=%s duration_ms=%.2f",
                    request_id,
                    request.method,
                    route,
                    status_code,
                    duration * 1000,
                )
            request_id_var.reset(token)

    app.mount("/metrics", make_asgi_app())


def _route_label(request: Request) -> str:
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    return route_path or "__unmatched__"
