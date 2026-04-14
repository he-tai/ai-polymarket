from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_RUNTIME_CONFIG: dict[str, Any] = {
    "live_mode": False,
    "top_markets": 10,
    "max_orders": 1,
    "min_confidence": 0.8,
    "default_size": 5.0,
    "analysis_timeout_s": 45,
    "interval_seconds": 60,
    "signature_type": 1,
}


def load_runtime_config(path: str = "config/runtime_config.json") -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return dict(DEFAULT_RUNTIME_CONFIG)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return dict(DEFAULT_RUNTIME_CONFIG)
    merged = dict(DEFAULT_RUNTIME_CONFIG)
    merged.update({k: v for k, v in data.items() if k in DEFAULT_RUNTIME_CONFIG})
    return merged


def save_runtime_config(values: dict[str, Any], path: str = "config/runtime_config.json") -> dict[str, Any]:
    merged = dict(DEFAULT_RUNTIME_CONFIG)
    merged.update({k: v for k, v in values.items() if k in DEFAULT_RUNTIME_CONFIG})
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged
