"""
core/adaptive/classifier.py — Heuristic-first Task Complexity Classification

v8.0: Adaptive 단일 진입점 리팩토링
  - detected_intent + internal_engine 필드 추가
  - intent detection heuristics (brainstorm, artifact, parallel, self_improve 등)
  - routing rules: intent → internal_engine 매핑

Routing 우선순위:
  1. user_mode_override (있으면 최우선)
  2. rule-based heuristic (confidence >= 0.6이면 확정)
  3. lightweight LLM fallback (confidence < 0.6일 때만)
  4. safe_default = standard (LLM fallback도 실패 시)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple

from .config import get_config


# ─── Mode 정의 ───

class HorcruxMode(str, Enum):
    FAST          = "fast"
    STANDARD      = "standard"
    FULL          = "full"           # R10: 정식 이름 (full_horcrux 통합)
    PARALLEL      = "parallel"       # pair2/3 전용


def normalize_mode(mode: str) -> str:
    """R10: mode 문자열 정규화 — full_horcrux → full, 알 수 없는 값은 그대로 반환."""
    _ALIASES = {"full_horcrux": "full", "fullhorcrux": "full"}
    return _ALIASES.get(mode.lower().strip(), mode.lower().strip())


class InternalEngine(str, Enum):
    ADAPTIVE_FAST     = "adaptive_fast"
    ADAPTIVE_STANDARD = "adaptive_standard"
    ADAPTIVE_FULL     = "adaptive_full"
    DEBATE_LOOP       = "debate_loop"
    PLANNING_PIPELINE = "planning_pipeline"
    PAIR_GENERATION   = "pair_generation"
    SELF_IMPROVE      = "self_improve"
    DEEP_REFACTOR     = "deep_refactor"


class DetectedIntent(str, Enum):
    CODE_FIX      = "code_fix"
    FEATURE_ADD   = "feature_add"
    REFACTOR      = "refactor"
    BRAINSTORM    = "brainstorm"
    DOCUMENT      = "document"
    ARTIFACT      = "artifact"
    PARALLEL_GEN  = "parallel_gen"
    SELF_IMPROVE  = "self_improve"
    DEEP_REFACTOR = "deep_refactor"


class RoutingSource(str, Enum):
    OVERRIDE      = "override"
    HEURISTIC     = "heuristic"
    LLM_FALLBACK  = "llm_fallback"
    SAFE_DEFAULT  = "safe_default"


@dataclass
class ClassificationResult:
    recommended_mode: HorcruxMode
    routing_source: RoutingSource
    reason: str
    confidence: float
    internal_engine: InternalEngine = InternalEngine.ADAPTIVE_STANDARD
    detected_intent: DetectedIntent = DetectedIntent.FEATURE_ADD

    def to_dict(self) -> dict:
        return {
            "recommended_mode": self.recommended_mode.value,
            "internal_engine": self.internal_engine.value,
            "routing_source": self.routing_source.value,
            "reason": self.reason,
            "confidence": self.confidence,
            "detected_intent": self.detected_intent.value,
        }


# ─── Intent Detection 키워드 사전 ───

_BRAINSTORM_KEYWORDS = {
    "brainstorm", "아이디어", "ideas", "생각", "방향", "전략",
    "strategy", "plan", "기획", "브레인스토밍", "토론",
}

_ARTIFACT_KEYWORDS = {
    "ppt", "pdf", "presentation", "문서", "보고서", "report",
    "슬라이드", "포트폴리오", "portfolio", "slide",
}

_PARALLEL_KEYWORDS = {
    "병렬", "parallel", "pair", "동시에", "나눠서", "분할",
    "2개 ai", "3개 ai", "2-ai", "3-ai",
}

_SELF_IMPROVE_KEYWORDS = {
    "개선", "improve", "반복", "iterate", "다듬어", "polish",
    "self-improve", "self_improve", "개선해줘", "다듬",
}

_REFACTOR_KEYWORDS = {
    "refactor", "리팩토링", "architecture", "아키텍처", "재설계",
    "redesign", "migration", "마이그레이션", "microservice",
    "마이크로서비스",
}

_CODE_FIX_KEYWORDS = {
    "fix", "bug", "typo", "수정", "오류", "에러", "rename",
    "hotfix", "patch", "오타",
}

# 기존 heuristic 키워드 (mode 결정용)

_FAST_KEYWORDS = {
    # 영어
    "fix", "typo", "rename", "tweak", "hotfix", "patch", "bump", "minor",
    "comment", "lint", "format", "cleanup", "spelling", "whitespace",
    "todo", "readme", "changelog", "simple", "trivial", "small",
    # 한국어
    "수정", "오타", "이름변경", "간단", "사소한", "정리", "포맷",
    "주석", "린트", "클린업", "소소한",
}

_FULL_KEYWORDS = {  # R10: renamed from _FULL_KEYWORDS
    # 영어
    "architecture", "refactor", "redesign", "migration", "portfolio",
    "presentation", "pitch", "proposal", "system design", "infrastructure",
    "security audit", "performance optimization", "critical", "production",
    "deploy strategy", "database schema", "api design", "microservice",
    # 한국어
    "아키텍처", "리팩토링", "재설계", "마이그레이션", "포트폴리오",
    "프레젠테이션", "발표", "제안서", "시스템설계", "인프라",
    "보안감사", "성능최적화", "핵심", "프로덕션", "배포전략",
    "데이터베이스설계", "API설계", "마이크로서비스",
}

_ARTIFACT_FULL_TYPES = {"ppt", "pdf"}  # ppt/pdf는 기본 full


# ─── Heuristic 로직 ───

def _keyword_match_score(text: str, keywords: set) -> int:
    """텍스트에서 키워드 매칭 수 반환."""
    text_lower = text.lower()
    count = 0
    for kw in keywords:
        if kw in text_lower:
            count += 1
    return count


def _detect_intent(task_description: str, artifact_type: str) -> Tuple[DetectedIntent, int]:
    """
    task에서 intent를 감지한다. heuristic-first, LLM 호출 없음.
    Returns: (detected_intent, match_count)
    """
    scores = {
        DetectedIntent.BRAINSTORM: _keyword_match_score(task_description, _BRAINSTORM_KEYWORDS),
        DetectedIntent.ARTIFACT: _keyword_match_score(task_description, _ARTIFACT_KEYWORDS),
        DetectedIntent.PARALLEL_GEN: _keyword_match_score(task_description, _PARALLEL_KEYWORDS),
        DetectedIntent.SELF_IMPROVE: _keyword_match_score(task_description, _SELF_IMPROVE_KEYWORDS),
        DetectedIntent.REFACTOR: _keyword_match_score(task_description, _REFACTOR_KEYWORDS),
        DetectedIntent.CODE_FIX: _keyword_match_score(task_description, _CODE_FIX_KEYWORDS),
    }

    # artifact_type 힌트 보너스
    if artifact_type in ("ppt", "pdf", "doc"):
        scores[DetectedIntent.ARTIFACT] += 2

    best_intent = max(scores, key=scores.get)
    best_score = scores[best_intent]

    if best_score == 0:
        # 기본: feature_add
        return (DetectedIntent.FEATURE_ADD, 0)

    # document는 artifact의 하위
    if best_intent == DetectedIntent.ARTIFACT and artifact_type in ("doc", "readme"):
        return (DetectedIntent.DOCUMENT, best_score)

    return (best_intent, best_score)


def _route_intent_to_engine(
    intent: DetectedIntent,
    mode: HorcruxMode,
    artifact_type: str,
    estimated_scope: str,
    risk_level: str,
) -> Tuple[HorcruxMode, InternalEngine]:
    """
    detected_intent + mode → (final_mode, internal_engine) 매핑.

    실험 데이터 기반 최적화 (2026-03-29):
      - standard(6.5) vs full(7.7) → full이 +1.2 우위
      - Sonnet+full(7.5) > Opus+standard(6.5) → 모드가 모델보다 중요
      - 정책: auto 모드에서 standard 대신 full을 기본으로 사용
      - fast는 간단한 code_fix에만 유지
    """
    # parallel intent → pair_generation
    if intent == DetectedIntent.PARALLEL_GEN:
        return (HorcruxMode.PARALLEL, InternalEngine.PAIR_GENERATION)

    # self_improve intent
    if intent == DetectedIntent.SELF_IMPROVE:
        return (HorcruxMode.STANDARD, InternalEngine.SELF_IMPROVE)

    # brainstorm intent → planning_pipeline
    if intent == DetectedIntent.BRAINSTORM:
        return (HorcruxMode.FULL, InternalEngine.PLANNING_PIPELINE)

    # artifact intent (ppt, pdf, doc) → planning_pipeline
    if intent in (DetectedIntent.ARTIFACT, DetectedIntent.DOCUMENT):
        return (HorcruxMode.FULL, InternalEngine.PLANNING_PIPELINE)

    # refactor/architecture → adaptive_full
    if intent == DetectedIntent.REFACTOR:
        return (HorcruxMode.FULL, InternalEngine.ADAPTIVE_FULL)

    # code_fix + small + low risk → adaptive_fast (간단한 수정만)
    if intent == DetectedIntent.CODE_FIX:
        if mode in (HorcruxMode.FAST,) or (estimated_scope == "small" and risk_level == "low"):
            return (HorcruxMode.FAST, InternalEngine.ADAPTIVE_FAST)
        # 복잡한 code_fix → full (실험: standard 5.5 vs full 7.5)
        return (HorcruxMode.FULL, InternalEngine.ADAPTIVE_FULL)

    # feature_add → adaptive_full (실험: standard 5.5 vs full 7.5)
    if intent == DetectedIntent.FEATURE_ADD:
        if mode == HorcruxMode.FAST:
            return (HorcruxMode.FAST, InternalEngine.ADAPTIVE_FAST)
        return (HorcruxMode.FULL, InternalEngine.ADAPTIVE_FULL)

    # fallback → full (실험 데이터: full이 항상 우위)
    return (HorcruxMode.FULL, InternalEngine.ADAPTIVE_FULL)


def _heuristic_classify(
    task_description: str,
    task_type: str,
    num_files_touched: int,
    estimated_scope: str,
    risk_level: str,
    artifact_type: str,
) -> Tuple[HorcruxMode, float, str]:
    """
    Rule-based heuristic classification.
    Returns: (mode, confidence, reason)
    """

    fast_hits = _keyword_match_score(task_description, _FAST_KEYWORDS)
    full_hits = _keyword_match_score(task_description, _FULL_KEYWORDS)

    # ── Rule 1: artifact type 강제 ──
    if artifact_type in _ARTIFACT_FULL_TYPES:
        return (
            HorcruxMode.FULL,
            0.90,
            f"artifact_type={artifact_type} → full 강제",
        )

    # ── Rule 2: high risk → full ──
    if risk_level == "high":
        conf = 0.90 if full_hits > 0 else 0.75
        return (
            HorcruxMode.FULL,
            conf,
            f"high risk + full_keywords={full_hits}",
        )

    # ── Rule 3: exact fast match ──
    if fast_hits >= 2 and full_hits == 0 and num_files_touched <= 1:
        return (
            HorcruxMode.FAST,
            0.95,
            f"fast_keywords={fast_hits}, files={num_files_touched}, no full signals",
        )

    if fast_hits >= 1 and full_hits == 0 and estimated_scope == "small":
        return (
            HorcruxMode.FAST,
            0.85,
            f"fast_keywords={fast_hits}, scope=small",
        )

    # ── Rule 4: exact full match ──
    if full_hits >= 2:
        return (
            HorcruxMode.FULL,
            0.95,
            f"full_keywords={full_hits}",
        )

    if full_hits >= 1 and estimated_scope == "large":
        return (
            HorcruxMode.FULL,
            0.85,
            f"full_keywords={full_hits}, scope=large",
        )

    # ── Rule 5: scope/file count signals ──
    if num_files_touched <= 1 and estimated_scope == "small" and risk_level == "low":
        return (
            HorcruxMode.FAST,
            0.75,
            f"files={num_files_touched}, scope=small, risk=low",
        )

    if estimated_scope == "large" or num_files_touched >= 6:
        return (
            HorcruxMode.FULL,
            0.75,
            f"files={num_files_touched}, scope={estimated_scope}",
        )

    # ── Rule 6: standard range ──
    if 2 <= num_files_touched <= 5 or estimated_scope == "medium":
        conf = 0.75 if risk_level == "medium" else 0.65
        return (
            HorcruxMode.STANDARD,
            conf,
            f"files={num_files_touched}, scope={estimated_scope}, risk={risk_level}",
        )

    # ── Rule 7: conflicting or no signals ──
    if fast_hits > 0 and full_hits > 0:
        return (
            HorcruxMode.STANDARD,
            0.55,
            f"conflicting signals: fast={fast_hits}, full={full_hits}",
        )

    return (
        HorcruxMode.STANDARD,
        0.40,
        "no clear signals → safe default",
    )


# ─── Lightweight LLM Fallback (placeholder) ───

def _llm_classify_fallback(
    task_description: str,
    task_type: str,
) -> Tuple[HorcruxMode, float, str]:
    """
    Heuristic confidence < threshold일 때 lightweight LLM 호출로 분류.
    """
    try:
        from core.provider import make_core_pair
        from .analytics import build_llm_classify_prompt, parse_llm_classify_response

        prompt = build_llm_classify_prompt(task_description, task_type)
        core = make_core_pair()
        claude = core.get("claude")
        if not claude:
            raise RuntimeError("claude provider not available")

        response = claude.invoke(prompt, timeout=10)  # 10초 timeout
        text = response.text if hasattr(response, "text") else str(response)
        mode_str, confidence = parse_llm_classify_response(text)
        mode = HorcruxMode(mode_str)

        return (mode, confidence, f"llm_classify → {mode.value} (response: {text[:50]})")

    except Exception as e:
        # LLM fallback 실패 → safe_default
        return (
            HorcruxMode.STANDARD,
            0.60,
            f"llm_fallback failed ({str(e)[:80]}) → safe_default=standard",
        )


# ─── 메인 분류 함수 ───

def classify_task_complexity(
    task_description: str,
    task_type: str = "code",
    num_files_touched: int = 0,
    estimated_scope: str = "medium",
    risk_level: str = "medium",
    artifact_type: str = "none",
    user_mode_override: Optional[str] = None,
    claude_model: str = "",
) -> ClassificationResult:
    """
    작업을 분류하고 최적 모드/엔진/intent를 결정한다.

    v8.0: recommended_mode + internal_engine + detected_intent 를 모두 반환.

    Args:
        task_description: 작업 설명 텍스트
        task_type: code|document|artifact|analysis
        num_files_touched: 수정 대상 파일 수
        estimated_scope: small|medium|large
        risk_level: low|medium|high
        artifact_type: none|ppt|pdf|doc
        user_mode_override: 사용자 수동 모드 지정 (null이면 자동)

    Returns:
        ClassificationResult (with internal_engine, detected_intent)
    """
    cfg = get_config()

    # intent 감지 (항상 수행)
    detected_intent, intent_score = _detect_intent(task_description, artifact_type)

    # ① Override — parallel / deep_refactor는 별도 처리
    if user_mode_override and cfg.routing.enable_mode_override:
        if user_mode_override == "parallel":
            return ClassificationResult(
                recommended_mode=HorcruxMode.PARALLEL,
                routing_source=RoutingSource.OVERRIDE,
                reason="user override → parallel",
                confidence=1.0,
                internal_engine=InternalEngine.PAIR_GENERATION,
                detected_intent=detected_intent,
            )
        if user_mode_override == "deep_refactor":
            return ClassificationResult(
                recommended_mode=HorcruxMode.FULL,
                routing_source=RoutingSource.OVERRIDE,
                reason="user override → deep_refactor",
                confidence=1.0,
                internal_engine=InternalEngine.DEEP_REFACTOR,
                detected_intent=DetectedIntent.DEEP_REFACTOR,
            )
        try:
            mode = HorcruxMode(user_mode_override)
            # override 시 사용자가 지정한 mode를 최우선 존중
            # intent가 mode를 덮어쓰지 않도록 mode → engine 직접 매핑
            _mode_to_engine = {
                HorcruxMode.FAST: InternalEngine.ADAPTIVE_FAST,
                HorcruxMode.STANDARD: InternalEngine.ADAPTIVE_STANDARD,
                HorcruxMode.FULL: InternalEngine.ADAPTIVE_FULL,
            }
            engine = _mode_to_engine.get(mode, InternalEngine.ADAPTIVE_STANDARD)

            # brainstorm/artifact intent는 standard/full일 때만 planning으로 라우팅
            # fast를 명시했으면 fast 존중
            if detected_intent in (DetectedIntent.BRAINSTORM, DetectedIntent.ARTIFACT, DetectedIntent.DOCUMENT):
                if mode not in (HorcruxMode.FAST,):
                    engine = InternalEngine.PLANNING_PIPELINE

            return ClassificationResult(
                recommended_mode=mode,
                routing_source=RoutingSource.OVERRIDE,
                reason=f"user override → {mode.value} (engine={engine.value}), intent={detected_intent.value}",
                confidence=1.0,
                internal_engine=engine,
                detected_intent=detected_intent,
            )
        except ValueError:
            pass  # invalid override value → fall through to heuristic

    # ② Heuristic
    if cfg.routing.enable_heuristic_routing:
        mode, confidence, reason = _heuristic_classify(
            task_description=task_description,
            task_type=task_type,
            num_files_touched=num_files_touched,
            estimated_scope=estimated_scope,
            risk_level=risk_level,
            artifact_type=artifact_type,
        )

        if confidence >= cfg.routing.llm_fallback_threshold:
            # intent → engine 매핑
            final_mode, engine = _route_intent_to_engine(
                detected_intent, mode, artifact_type, estimated_scope, risk_level,
            )
            # high risk escalation
            if risk_level == "high" and final_mode not in (HorcruxMode.FULL, HorcruxMode.FULL):
                final_mode = HorcruxMode.FULL
                if engine in (InternalEngine.ADAPTIVE_FAST, InternalEngine.ADAPTIVE_STANDARD):
                    engine = InternalEngine.ADAPTIVE_FULL

            return ClassificationResult(
                recommended_mode=final_mode,
                routing_source=RoutingSource.HEURISTIC,
                reason=f"{reason} | intent={detected_intent.value}",
                confidence=confidence,
                internal_engine=engine,
                detected_intent=detected_intent,
            )

        # ③ LLM fallback (confidence < threshold)
        if cfg.routing.enable_llm_fallback:
            llm_mode, llm_conf, llm_reason = _llm_classify_fallback(
                task_description, task_type
            )
            final_mode, engine = _route_intent_to_engine(
                detected_intent, llm_mode, artifact_type, estimated_scope, risk_level,
            )
            return ClassificationResult(
                recommended_mode=final_mode,
                routing_source=RoutingSource.LLM_FALLBACK,
                reason=f"heuristic low conf ({confidence:.2f}): {reason} → llm: {llm_reason}",
                confidence=llm_conf,
                internal_engine=engine,
                detected_intent=detected_intent,
            )

    # ④ Safe default
    safe_mode = HorcruxMode(cfg.routing.safe_default_mode)
    final_mode, engine = _route_intent_to_engine(
        detected_intent, safe_mode, artifact_type, estimated_scope, risk_level,
    )
    return ClassificationResult(
        recommended_mode=final_mode,
        routing_source=RoutingSource.SAFE_DEFAULT,
        reason="all routing disabled or failed → safe default",
        confidence=0.5,
        internal_engine=engine,
        detected_intent=detected_intent,
    )


def apply_sonnet_compensation(
    result: ClassificationResult,
    claude_model: str,
    task_description: str,
    estimated_scope: str = "medium",
    risk_level: str = "medium",
) -> ClassificationResult:
    """
    Sonnet 모델 사용 시 실험 데이터 기반 보정.

    실험 결과 (Opus vs Sonnet, standard 모드):
      easy:   Opus 7.5 vs Sonnet 4.0 (차이 3.5)
      medium: Opus 6.5 vs Sonnet 5.5 (차이 1.0)
      hard:   Opus 5.5 vs Sonnet 5.0 (차이 0.5) ← 오케스트레이션이 보완

    정책:
      - Sonnet + hard task → full 모드 승격 (오케스트레이션 깊이로 보완)
      - Sonnet + easy/medium → 경고 플래그 추가 (Opus 추천)
    """
    is_sonnet = claude_model.lower() in ("sonnet", "claude-sonnet-4-6")
    if not is_sonnet:
        return result

    # P1 fix: 입력 객체 변이 방지 — 복사본에서 수정
    from dataclasses import replace as _dc_replace
    try:
        result = _dc_replace(result)
    except TypeError:
        pass  # dataclass가 아니면 원본 수정 (기존 동작 유지)

    # 난이도 추정: intent + scope + risk 기반
    is_hard = (
        result.detected_intent in (DetectedIntent.REFACTOR, DetectedIntent.DEEP_REFACTOR)
        or estimated_scope == "large"
        or risk_level == "high"
        or any(kw in task_description.lower() for kw in (
            "architecture", "아키텍처", "security", "보안", "migration", "마이그레이션",
            "msa", "microservice", "production", "프로덕션",
        ))
    )

    if is_hard:
        # Sonnet + hard → full 승격 (차이 0.5 → 오케스트레이션으로 보완 가능)
        if result.internal_engine in (InternalEngine.ADAPTIVE_FAST, InternalEngine.ADAPTIVE_STANDARD):
            result.internal_engine = InternalEngine.ADAPTIVE_FULL
            result.recommended_mode = HorcruxMode.FULL
            result.reason += " | sonnet_compensation: hard task → full mode upgrade"
    else:
        # Sonnet + easy/medium → 경고 (차이 1.0~3.5)
        result.reason += " | sonnet_warning: Opus recommended for easy/medium tasks (+1.0~3.5 score)"

    return result
