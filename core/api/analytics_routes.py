"""
core/api/analytics_routes.py — P2-002: Analytics routes extracted from server.py
"""

from flask import Blueprint, jsonify

analytics_bp = Blueprint("analytics", __name__)


@analytics_bp.route("/api/analytics")
def analytics_dashboard():
    try:
        from core.adaptive.analytics import build_analytics_dashboard
        dashboard = build_analytics_dashboard()
        return jsonify(dashboard.to_dict())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@analytics_bp.route("/api/analytics/timeouts")
def analytics_timeouts():
    try:
        from core.adaptive.analytics import compute_latency_percentiles, auto_tune_timeouts
        stats = {k: v.to_dict() for k, v in compute_latency_percentiles().items()}
        recommendations = auto_tune_timeouts(dry_run=True)
        return jsonify({"stats": stats, "recommendations": recommendations})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@analytics_bp.route("/api/analytics/timeouts/apply", methods=["POST"])
def analytics_apply_timeouts():
    try:
        from core.adaptive.analytics import auto_tune_timeouts
        applied = auto_tune_timeouts(dry_run=False)
        return jsonify({"applied": applied, "status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@analytics_bp.route("/api/analytics/critics")
def analytics_critics():
    try:
        from core.adaptive.analytics import compute_critic_reliability
        data = {k: v.to_dict() for k, v in compute_critic_reliability().items()}
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@analytics_bp.route("/api/analytics/modes")
def analytics_modes():
    try:
        from core.adaptive.analytics import compute_mode_usage_stats
        data = {k: v.to_dict() for k, v in compute_mode_usage_stats().items()}
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@analytics_bp.route("/api/analytics/heuristic")
def analytics_heuristic():
    try:
        from core.adaptive.analytics import suggest_heuristic_refinements
        data = suggest_heuristic_refinements()
        return jsonify(data.to_dict())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@analytics_bp.route("/api/analytics/scoring")
def analytics_scoring():
    try:
        from core.adaptive.analytics import auto_tune_scoring_weights
        return jsonify(auto_tune_scoring_weights(dry_run=True))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@analytics_bp.route("/api/analytics/scoring/apply", methods=["POST"])
def analytics_scoring_apply():
    try:
        from core.adaptive.analytics import auto_tune_scoring_weights
        result = auto_tune_scoring_weights(dry_run=False)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
