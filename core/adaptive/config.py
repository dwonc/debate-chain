"""
core/adaptive/config.py — Adaptive Horcrux 설정

timeout, feature flag, routing threshold 등을 환경변수/config 파일에서 읽는다.
하드코딩 금지 원칙: 모든 수치는 이 모듈을 통해서만 참조.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, Optional


_CONFIG_DIR = Path(__file__).parent.parent.parent  # horcrux root


@dataclass
class TimeoutConfig:
    """Stage별 timeout (ms). 환경변수 > config file > default 순으로 resolve."""
    generator_ms:     int = 120_000  # 2분 (Opus 한국어 응답 고려)
    synth_ms:         int = 60_000   # 1분
    core_critic_ms:   int = 60_000   # 1분
    aux_critic_ms:    int = 30_000   # 30초
    revision_ms:      int = 60_000   # 1분
    light_critic_ms:  int = 30_000   # 30초 (fast 모드용)

    @classmethod
    def from_env(cls) -> "TimeoutConfig":
        """환경변수에서 override. HORCRUX_TIMEOUT_GENERATOR_MS 형식."""
        kw = {}
        mapping = {
            "HORCRUX_TIMEOUT_GENERATOR_MS":    "generator_ms",
            "HORCRUX_TIMEOUT_SYNTH_MS":        "synth_ms",
            "HORCRUX_TIMEOUT_CORE_CRITIC_MS":  "core_critic_ms",
            "HORCRUX_TIMEOUT_AUX_CRITIC_MS":   "aux_critic_ms",
            "HORCRUX_TIMEOUT_REVISION_MS":     "revision_ms",
            "HORCRUX_TIMEOUT_LIGHT_CRITIC_MS": "light_critic_ms",
        }
        for env_key, field_name in mapping.items():
            val = os.environ.get(env_key)
            if val and val.isdigit():
                kw[field_name] = int(val)
        return cls(**kw)


@dataclass
class RoutingConfig:
    """Task routing 설정."""
    llm_fallback_threshold: float = 0.6
    safe_default_mode: str = "standard"

    # Feature flags
    enable_heuristic_routing: bool = True
    enable_llm_fallback: bool = True
    enable_mode_override: bool = True


@dataclass
class RevisionConfig:
    """Revision loop 설정."""
    hard_cap: int = 2
    min_progress_delta: float = 0.05


@dataclass
class LoggingConfig:
    """Latency log 설정."""
    log_dir: str = ""
    log_filename: str = "horcrux_stage_latency.jsonl"

    @property
    def log_path(self) -> Path:
        if self.log_dir:
            return Path(self.log_dir) / self.log_filename
        return _CONFIG_DIR / "logs" / self.log_filename


@dataclass
class AdaptiveHorcruxConfig:
    """Phase 1 전체 설정 묶음."""
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig.from_env)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    revision: RevisionConfig = field(default_factory=RevisionConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def load(cls, config_path: Optional[str | Path] = None) -> "AdaptiveHorcruxConfig":
        """
        config 파일 + 환경변수에서 설정 로드.
        환경변수가 config 파일보다 우선.
        """
        cfg = cls()

        # config 파일이 있으면 timeout default를 덮어쓰기
        path = Path(config_path) if config_path else _CONFIG_DIR / "config.json"
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                adaptive = data.get("adaptive_horcrux", {})
                if "timeouts" in adaptive:
                    for k, v in adaptive["timeouts"].items():
                        if hasattr(cfg.timeouts, k):
                            setattr(cfg.timeouts, k, v)
                if "routing" in adaptive:
                    for k, v in adaptive["routing"].items():
                        if hasattr(cfg.routing, k):
                            setattr(cfg.routing, k, v)
                if "revision" in adaptive:
                    for k, v in adaptive["revision"].items():
                        if hasattr(cfg.revision, k):
                            setattr(cfg.revision, k, v)
            except Exception as e:
                print(f"[AdaptiveConfig] config load warning: {e}")

        # 환경변수 override (timeout)
        cfg.timeouts = TimeoutConfig.from_env()

        return cfg


# 싱글턴 편의 함수
_instance: Optional[AdaptiveHorcruxConfig] = None


def get_config() -> AdaptiveHorcruxConfig:
    global _instance
    if _instance is None:
        _instance = AdaptiveHorcruxConfig.load()
    return _instance


def reload_config(config_path: Optional[str | Path] = None) -> AdaptiveHorcruxConfig:
    global _instance
    _instance = AdaptiveHorcruxConfig.load(config_path)
    return _instance
