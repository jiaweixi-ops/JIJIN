from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.observability import install_observability


def _app() -> FastAPI:
    app = FastAPI()
    install_observability(app)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    return app


def test_request_id_is_preserved_and_metrics_are_exposed():
    client = TestClient(_app())

    response = client.get("/ping", headers={"X-Request-ID": "test-request-id"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "test-request-id"

    metrics = client.get("/metrics/")
    assert metrics.status_code == 200
    assert "jijin_http_requests_total" in metrics.text
    assert 'route="/ping"' in metrics.text
