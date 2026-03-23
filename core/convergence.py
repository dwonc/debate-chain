"""
core/convergence.py — Improvement #6: 수렴 알고리즘 고도화

단순 평균 점수 threshold → 다차원 수렴 지표:
1. 의미론적 유사도 (sentence-transformers > sklearn TF-IDF > 순수 Python fallback)
2. 키포인트 합의율
3. 가짜 합의(superficial agreement) 탐지
4. score 안정성
5. 조기 수렴 방지 (최소 min_rounds 강제)
6. 라운드별 수렴 추이 추적
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field, asdict
from difflib import SequenceMatcher
from statistics import pstdev
from typing import Any, Dict, List, Optional, Sequence, Tuple


# ─── 외부 의존성 (3-tier fallback) ───

def _load_similarity_backend():
    try:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
        def _embed(texts):
            return _model.encode(texts, convert_to_numpy=True)
        def _cosine(a, b):
            dot = float(a @ b)
            norm = float((a @ a) ** 0.5 * (b @ b) ** 0.5)
            return dot / norm if norm else 0.0
        return "sentence-transformers", _embed, _cosine
    except ImportError:
        pass
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity as _sk_cos
        def _tfidf_sim(texts):
            vec = TfidfVectorizer(min_df=1).fit_transform(texts).toarray()
            return vec
        def _cosine(a, b):
            import numpy as np
            denom = np.linalg.norm(a) * np.linalg.norm(b)
            return float(a @ b / denom) if denom else 0.0
        return "sklearn-tfidf", _tfidf_sim, _cosine
    except ImportError:
        pass
    return "pure-python", None, None

_BACKEND, _EMBED_FN, _COSINE_FN = _load_similarity_backend()


STOPWORDS = {
    "a","an","and","are","as","at","be","because","been","being","but","by","can",
    "could","did","do","does","for","from","had","has","have","how","if","in","into",
    "is","it","its","may","might","must","no","not","of","on","or","our","should",
    "so","that","the","their","there","these","they","this","to","was","we","were",
    "what","when","where","which","who","why","will","with","would","you","your",
}

KEYPOINT_CUES = {
    "important","key","critical","must","should","need","requires","therefore",
    "because","however","first","second","finally","issue","problem","fix","solution",
    "approach","recommend","avoid","ensure","always","never","주요","핵심","반드시",
}


# ─── 데이터클래스 ───

@dataclass
class ConvergenceThresholds:
    semantic_similarity:   float = 0.82
    keypoint_consensus:    float = 0.68
    regression_free:       float = 0.75
    score_stability:       float = 0.70
    # 가짜 합의 탐지
    superficial_surface_sim: float = 0.88
    superficial_semantic_sim: float = 0.78
    superficial_kp_ceil:     float = 0.55
    minimum_score:           float = 8.0


@dataclass
class ConvergenceResult:
    converged:            bool
    round_num:            int
    reason:               str
    metrics:              Dict[str, float]
    thresholds:           Dict[str, float]
    flags:                List[str] = field(default_factory=list)
    superficial_agreement: bool = False
    trends:               List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─── 유틸 ───

def _tokenize(text: str) -> List[str]:
    tokens = re.findall(r"\b[a-z가-힣]{2,}\b", text.lower())
    return [t for t in tokens if t not in STOPWORDS]


def _tfidf_vector(tokens: List[str], vocab: Dict[str, int], idf: Dict[str, float]) -> List[float]:
    counts = Counter(tokens)
    vec = [0.0] * len(vocab)
    for word, idx in vocab.items():
        if word in counts:
            tf = counts[word] / len(tokens) if tokens else 0
            vec[idx] = tf * idf.get(word, 1.0)
    return vec


def _build_idf(corpus: List[List[str]]) -> Tuple[Dict[str, int], Dict[str, float]]:
    N = len(corpus)
    df: Counter = Counter()
    for doc in corpus:
        for w in set(doc):
            df[w] += 1
    vocab = {w: i for i, w in enumerate(sorted(df))}
    idf = {w: math.log((N + 1) / (c + 1)) + 1 for w, c in df.items()}
    return vocab, idf


def _cosine_pure(a: List[float], b: List[float]) -> float:
    dot  = sum(x * y for x, y in zip(a, b))
    na   = math.sqrt(sum(x * x for x in a))
    nb   = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na * nb else 0.0


def _semantic_sim_pair(text_a: str, text_b: str, all_texts: List[str]) -> float:
    """두 텍스트 간 의미론적 유사도 (0~1)"""
    if _BACKEND == "sentence-transformers":
        embs = _EMBED_FN([text_a, text_b])
        return _COSINE_FN(embs[0], embs[1])
    elif _BACKEND == "sklearn-tfidf":
        vecs = _EMBED_FN([text_a, text_b])
        return _COSINE_FN(vecs[0], vecs[1])
    else:
        # 순수 Python TF-IDF
        docs = [_tokenize(t) for t in [text_a, text_b]]
        vocab, idf = _build_idf(docs)
        va = _tfidf_vector(docs[0], vocab, idf)
        vb = _tfidf_vector(docs[1], vocab, idf)
        return _cosine_pure(va, vb)


def _surface_sim(text_a: str, text_b: str) -> float:
    """표면적 유사도 (SequenceMatcher)"""
    return SequenceMatcher(None, text_a[:2000], text_b[:2000]).ratio()


def _extract_keypoints(text: str, max_kp: int = 8) -> List[str]:
    """키포인트 문장 추출 (cue 단어 기준)"""
    sentences = re.split(r"[.!?\n]", text)
    scored: List[Tuple[float, str]] = []
    for sent in sentences:
        sent = sent.strip()
        if len(sent) < 20:
            continue
        tokens = set(sent.lower().split())
        cue_hits = len(tokens & KEYPOINT_CUES)
        # 짧고 핵심적인 문장 선호
        length_penalty = 1 / (1 + len(sent) / 200)
        scored.append((cue_hits + length_penalty, sent))
    scored.sort(reverse=True)
    return [s for _, s in scored[:max_kp]]


def _keypoint_consensus(kp_prev: List[str], kp_curr: List[str]) -> float:
    """이전 라운드 키포인트가 현재 라운드에 얼마나 유지됐는지 (0~1)"""
    if not kp_prev:
        return 1.0
    matched = 0
    for kp in kp_prev:
        for curr in kp_curr:
            if _surface_sim(kp, curr) > 0.55:
                matched += 1
                break
    return matched / len(kp_prev)


def _score_stability(scores: List[float]) -> float:
    """최근 scores의 안정성 (stddev 기반, 0~1)"""
    if len(scores) < 2:
        return 1.0
    std = pstdev(scores[-3:])
    return max(0.0, 1.0 - std / 5.0)


def _detect_superficial(
    text_a: str, text_b: str, th: ConvergenceThresholds
) -> bool:
    """
    가짜 합의 감지:
    표면 유사도 높음 + 의미 유사도는 임계값 미달 + 키포인트 합의 낮음
    """
    surf = _surface_sim(text_a, text_b)
    if surf < th.superficial_surface_sim:
        return False
    sem = _semantic_sim_pair(text_a, text_b, [text_a, text_b])
    if sem > th.superficial_semantic_sim:
        return False  # 진짜 합의
    kp_a = _extract_keypoints(text_a)
    kp_b = _extract_keypoints(text_b)
    kp_cons = _keypoint_consensus(kp_a, kp_b)
    return kp_cons < th.superficial_kp_ceil


# ─── 메인 분석기 ───

class ConvergenceAnalyzer:
    """
    다차원 수렴 분석기.

    사용법:
        analyzer = ConvergenceAnalyzer()
        result = analyzer.check_convergence(solutions, scores, round_num)
    """

    def __init__(
        self,
        min_rounds: int = 2,
        max_keypoints: int = 8,
        thresholds: Optional[ConvergenceThresholds] = None,
    ):
        self.min_rounds    = min_rounds
        self.max_keypoints = max_keypoints
        self.th            = thresholds or ConvergenceThresholds()
        self._history: List[Dict[str, Any]] = []

    def check_convergence(
        self,
        solutions: List[str],
        scores: List[float],
        round_num: int,
    ) -> ConvergenceResult:
        """
        수렴 여부 판정.

        Args:
            solutions: 이번 라운드까지의 솔루션 리스트 (최신이 마지막)
            scores:    라운드별 평균 점수 리스트
            round_num: 현재 라운드 번호 (1-based)

        Returns:
            ConvergenceResult
        """
        th_dict = asdict(self.th)
        flags: List[str] = []
        metrics: Dict[str, float] = {}

        # ① 조기 수렴 방지
        if round_num < self.min_rounds:
            self._history.append({"round": round_num, "converged": False, "reason": "min_rounds"})
            return ConvergenceResult(
                converged=False, round_num=round_num,
                reason=f"최소 {self.min_rounds}라운드 미달",
                metrics=metrics, thresholds=th_dict,
                flags=["early_prevention"],
            )

        curr_sol  = solutions[-1]
        prev_sol  = solutions[-2] if len(solutions) >= 2 else ""
        curr_score = scores[-1] if scores else 0.0

        # ② 최소 점수 확인
        if curr_score < self.th.minimum_score:
            flags.append("score_too_low")
            metrics["score"] = curr_score
            self._record(round_num, False, "score_too_low", metrics)
            return ConvergenceResult(
                converged=False, round_num=round_num,
                reason=f"점수 {curr_score:.1f} < {self.th.minimum_score}",
                metrics=metrics, thresholds=th_dict, flags=flags,
            )

        # ③ 의미론적 유사도
        sem_sim = _semantic_sim_pair(prev_sol, curr_sol, solutions) if prev_sol else 1.0
        metrics["semantic_similarity"] = round(sem_sim, 4)
        if sem_sim < self.th.semantic_similarity:
            flags.append("low_semantic_similarity")

        # ④ 키포인트 합의율
        kp_prev = _extract_keypoints(prev_sol, self.max_keypoints) if prev_sol else []
        kp_curr = _extract_keypoints(curr_sol, self.max_keypoints)
        kp_cons = _keypoint_consensus(kp_prev, kp_curr)
        metrics["keypoint_consensus"] = round(kp_cons, 4)
        if kp_cons < self.th.keypoint_consensus:
            flags.append("low_keypoint_consensus")

        # ⑤ score 안정성
        stab = _score_stability(scores)
        metrics["score_stability"] = round(stab, 4)
        if stab < self.th.score_stability:
            flags.append("unstable_scores")

        # ⑥ 가짜 합의 감지
        superficial = False
        if prev_sol:
            superficial = _detect_superficial(prev_sol, curr_sol, self.th)
        metrics["superficial"] = float(superficial)
        if superficial:
            flags.append("superficial_agreement")

        # ⑦ 종합 판정
        regression_free_score = min(1.0, curr_score / 10.0)
        metrics["regression_free"] = round(regression_free_score, 4)

        all_ok = (
            sem_sim  >= self.th.semantic_similarity and
            kp_cons  >= self.th.keypoint_consensus  and
            stab     >= self.th.score_stability      and
            regression_free_score >= self.th.regression_free and
            not superficial
        )

        reason = "모든 지표 충족" if all_ok else f"미충족 플래그: {', '.join(flags)}"
        self._record(round_num, all_ok, reason, metrics)

        return ConvergenceResult(
            converged=all_ok,
            round_num=round_num,
            reason=reason,
            metrics=metrics,
            thresholds=th_dict,
            flags=flags,
            superficial_agreement=superficial,
            trends=list(self._history),
        )

    def _record(self, round_num: int, converged: bool, reason: str, metrics: Dict):
        self._history.append({
            "round": round_num,
            "converged": converged,
            "reason": reason,
            **{k: v for k, v in metrics.items()},
        })

    def reset(self):
        self._history.clear()

    @property
    def backend(self) -> str:
        return _BACKEND
