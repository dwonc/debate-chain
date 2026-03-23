"""
server_patch.py - core 모듈 연결 패치 + 서버 실행
포트: 5000 (기존 server.py와 동일)
"""

from __future__ import annotations
import sys, os

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# ─── core 모듈 (실패해도 서버는 뜨도록 개별 try/except) ───
try:
    from core.security     import run_cli_stdin, redact
    print("[patch] security OK")
except Exception as e:
    print(f"[patch][WARN] security: {e}")

try:
    from core.job_store    import get_store, JobStatus
    _store = get_store()
    print("[patch] job_store OK")
except Exception as e:
    _store = None
    print(f"[patch][WARN] job_store: {e}")

try:
    from core.sse          import get_bus, make_sse_response
    _bus = get_bus()
    print("[patch] sse OK")
except Exception as e:
    _bus = None
    print(f"[patch][WARN] sse: {e}")

try:
    from core.cost_tracker import get_tracker
    _tracker = get_tracker()
    print("[patch] cost_tracker OK")
except Exception as e:
    _tracker = None
    print(f"[patch][WARN] cost_tracker: {e}")

try:
    from core.router       import ProviderRouter
    _router = ProviderRouter(config_path=os.path.join(ROOT, "config.json"))
    print("[patch] router OK")
except Exception as e:
    _router = None
    print(f"[patch][WARN] router: {e}")

try:
    from core.async_worker import get_pool
    _pool = get_pool(max_workers=4)
    print("[patch] async_worker OK")
except Exception as e:
    _pool = None
    print(f"[patch][WARN] async_worker: {e}")

# ─── server.py 로드 ───
print("[patch] loading server.py...")
import server as _srv
print("[patch] server.py loaded OK")

# ─── 추가 라우트 등록 ───
from flask import jsonify, Response
import pathlib

# SSE 스트리밍
if _bus:
    @_srv.app.route("/stream/<job_id>")
    def sse_stream(job_id):
        return make_sse_response(job_id)
    print("[patch] /stream/<job_id> registered")

# Job 목록 API
if _store:
    @_srv.app.route("/api/jobs")
    def api_jobs():
        jobs = _store.list_jobs(limit=50)
        return jsonify({"jobs": [
            {"job_id": j.job_id, "job_type": j.job_type,
             "status": j.status, "phase": j.phase, "created_at": j.created_at}
            for j in jobs
        ]})

# status API (in-memory + DB 병합)
if _store:
    @_srv.app.route("/api/status/<job_id>")
    def api_status_v2(job_id):
        data = {}
        for store_name in ["debates", "pairs", "pipelines", "self_improves"]:
            d = getattr(_srv, store_name, {})
            if job_id in d:
                data = dict(d[job_id])
                break
        rec = _store.get(job_id)
        if rec:
            data.setdefault("status", rec.status)
            data.setdefault("phase",  rec.phase)
        return jsonify(data)

# 시스템 상태 API
@_srv.app.route("/api/system")
def api_system():
    info = {
        "active_workers": _pool.active_count() if _pool else 0,
        "queue_size":     _pool.queue_size()   if _pool else 0,
        "cost_usd":       0.0,
        "budget_usd":     5.0,
    }
    if _tracker:
        s = _tracker.summary()
        info["cost_usd"]   = s["session_cost_usd"]
        info["budget_usd"] = s["budget_usd"]
    if _router:
        info["provider_stats"] = _router.stats()
    return jsonify(info)

# 대시보드
@_srv.app.route("/dashboard")
def dashboard():
    ui = pathlib.Path(ROOT) / "web_ui" / "index.html"
    if ui.exists():
        return ui.read_text(encoding="utf-8")
    return "web_ui/index.html not found", 404

print("[patch] all routes registered")

# ─── 실행 ───
if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 5000))
    print(f"""
  ================================================
   Debate Chain v8  -  Patched
  ------------------------------------------------
   API      : http://localhost:{PORT}
   Dashboard: http://localhost:{PORT}/dashboard
   Original : http://localhost:{PORT}  (same port)
  ================================================
""")
    _srv.app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
