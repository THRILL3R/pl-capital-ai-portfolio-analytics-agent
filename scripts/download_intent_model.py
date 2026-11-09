"""
One-off download of the local intent-classification model (needs internet once).

    python scripts/download_intent_model.py

Downloads sentence-transformers/all-MiniLM-L6-v2 (ONNX, about 90 MB) into the model cache
(default: <project>/.model_cache/fastembed; override with PORTFOLIO_INTENT_CACHE_DIR), then verifies
that it loads offline. After this the application never needs the network for intent classification.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_agent.intent_classifier import (  # noqa: E402
    FastEmbedder,
    IntentClassifierConfig,
    IntentClassifierUnavailable,
)


def main() -> int:
    cfg = IntentClassifierConfig.from_env()
    print(f"Model : {cfg.model_name}")
    print(f"Cache : {cfg.cache_dir}")
    try:
        start = time.time()
        FastEmbedder(cfg.model_name, cfg.cache_dir, allow_download=True)(["warm-up"])
        print(f"Downloaded and loaded in {time.time() - start:.1f}s")
        # Prove it now works without the network path.
        FastEmbedder(cfg.model_name, cfg.cache_dir, allow_download=False)(["offline check"])
    except IntentClassifierUnavailable as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    print("OK: the model loads from the local cache (offline mode).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
