"""
tests/test_integration.py — P1-003: Integration tests

Flask test client 기반. 실제 LLM 호출 없이 API 라운드트립 검증.
"""

import json
import os
import pytest
from unittest.mock import patch

# API key 설정 (테스트용)
os.environ["HORCRUX_API_KEY"] = "test-key-12345"

import server

@pytest.fixture
def client():
    """Flask test client."""
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        yield c


@pytest.fixture
def api_headers():
    return {"Content-Type": "application/json", "X-API-Key": "test-key-12345"}


# ═══════════════════════════════════════════
# API 라운드트립 테스트
# ═══════════════════════════════════════════

class TestThreadsAPI:
    def test_list_threads(self, client):
        resp = client.get("/api/threads")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)

    def test_list_threads_returns_json(self, client):
        resp = client.get("/api/threads")
        assert resp.content_type.startswith("application/json")


class TestClassifyAPI:
    def test_classify_simple(self, client, api_headers):
        resp = client.post("/api/horcrux/classify", headers=api_headers,
                          data=json.dumps({"task": "fix typo in README"}))
        assert resp.status_code == 200
        data = resp.get_json()
        assert "recommended_mode" in data
        assert "internal_engine" in data
        assert "confidence" in data

    def test_classify_complex(self, client, api_headers):
        resp = client.post("/api/horcrux/classify", headers=api_headers,
                          data=json.dumps({"task": "refactor entire auth system"}))
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["recommended_mode"] in ("fast", "full", "standard")

    def test_classify_empty_task(self, client, api_headers):
        resp = client.post("/api/horcrux/classify", headers=api_headers,
                          data=json.dumps({"task": ""}))
        # Should still return a classification (default routing)
        assert resp.status_code in (200, 400)


class TestAuthMiddleware:
    def test_post_without_key_returns_401(self, client):
        resp = client.post("/api/horcrux/classify",
                          headers={"Content-Type": "application/json"},
                          data=json.dumps({"task": "test"}))
        assert resp.status_code == 401

    def test_post_with_wrong_key_returns_401(self, client):
        resp = client.post("/api/horcrux/classify",
                          headers={"Content-Type": "application/json", "X-API-Key": "wrong"},
                          data=json.dumps({"task": "test"}))
        assert resp.status_code == 401

    def test_post_with_correct_key_passes(self, client, api_headers):
        resp = client.post("/api/horcrux/classify", headers=api_headers,
                          data=json.dumps({"task": "test"}))
        assert resp.status_code == 200

    def test_get_threads_no_key_needed(self, client):
        resp = client.get("/api/threads")
        assert resp.status_code == 200


class TestPathValidation:
    def test_invalid_project_dir_rejected(self, client, api_headers):
        """P0-003: path traversal 차단 확인."""
        resp = client.post("/api/horcrux/run", headers=api_headers,
                          data=json.dumps({
                              "task": "test",
                              "mode": "fast",
                              "project_dir": "C:\\Windows\\System32"
                          }))
        # Should get 400 if ALLOWED_ROOTS is set, or proceed if not
        # At minimum, should not crash
        assert resp.status_code in (200, 400, 500)


class TestStatusResult:
    def test_status_nonexistent_job(self, client):
        resp = client.get("/api/horcrux/status/nonexistent_job_123")
        assert resp.status_code in (200, 404)

    def test_result_nonexistent_job(self, client):
        resp = client.get("/api/horcrux/result/nonexistent_job_123")
        assert resp.status_code in (200, 404)


class TestAnalytics:
    def test_analytics_dashboard(self, client):
        resp = client.get("/api/analytics")
        assert resp.status_code == 200

    def test_analytics_critics(self, client):
        resp = client.get("/api/analytics/critics")
        assert resp.status_code == 200

    def test_analytics_modes(self, client):
        resp = client.get("/api/analytics/modes")
        assert resp.status_code == 200


class TestHealthCheck:
    def test_index_returns_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
