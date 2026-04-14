from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any


def _build_logger(name: str, file_path: Path) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(file_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)
    logger.propagate = False
    return logger


def init_trade_loggers(base_dir: str = "logs") -> dict[str, logging.Logger]:
    root = Path(base_dir)
    return {
        "analysis": _build_logger("ai_polymarket_analysis", root / "analysis.log"),
        "orders": _build_logger("ai_polymarket_orders", root / "orders.log"),
        "runtime": _build_logger("ai_polymarket_runtime", root / "runtime.log"),
    }


def log_json(logger: logging.Logger, payload: dict[str, Any]) -> None:
    logger.info(json.dumps(payload, ensure_ascii=False, default=str))
