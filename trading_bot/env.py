"""Environment resolution for Alpaca credentials.

Local ``.env`` and GitHub Actions secrets share the same four canonical names:

- ``ALPACA_PAPER_KEY_ID``  / ``ALPACA_PAPER_SECRET``
- ``ALPACA_LIVE_KEY_ID``   / ``ALPACA_LIVE_SECRET``

``ALPACA_ENV`` (``paper`` | ``live``, default ``paper``) selects which pair the
process should use.  This module loads ``.env`` (without overriding existing
env vars, so CI secrets always win) and exports the chosen pair as the legacy
``ALPACA_API_KEY`` / ``ALPACA_SECRET_KEY`` names that the rest of the codebase
already reads — keeping a single runtime contract for both local and CI.
"""

from __future__ import annotations

import logging
import os

logger: logging.Logger = logging.getLogger(__name__)


def resolve_alpaca_env() -> tuple[str, str, bool]:
    """Resolve Alpaca credentials based on ``ALPACA_ENV``.

    Returns ``(api_key, secret_key, is_paper)``.  Sets ``ALPACA_API_KEY`` and
    ``ALPACA_SECRET_KEY`` in ``os.environ`` if they are not already populated.
    Missing keys are returned as empty strings — callers decide whether to
    error out (CI/live runs) or proceed (offline backtests).
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(override=False)
    except ImportError:
        pass

    env: str = os.environ.get("ALPACA_ENV", "paper").strip().lower()
    is_paper: bool = env != "live"

    if os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"):
        return (
            os.environ["ALPACA_API_KEY"],
            os.environ["ALPACA_SECRET_KEY"],
            is_paper,
        )

    if is_paper:
        key: str = os.environ.get("ALPACA_PAPER_KEY_ID", "")
        secret: str = os.environ.get("ALPACA_PAPER_SECRET", "")
    else:
        key = os.environ.get("ALPACA_LIVE_KEY_ID", "")
        secret = os.environ.get("ALPACA_LIVE_SECRET", "")

    if key and secret:
        os.environ["ALPACA_API_KEY"] = key
        os.environ["ALPACA_SECRET_KEY"] = secret
    else:
        # Surface the misconfiguration explicitly. The previous behavior
        # silently returned empty strings, making downstream Alpaca
        # connection failures hard to root-cause. CI runs and live trading
        # MUST have these set — only offline backtests can legitimately
        # proceed without them.
        suffix: str = "PAPER" if is_paper else "LIVE"
        logger.error(
            "Alpaca credentials missing for env=%s — bot will fail to connect. "
            "Set ALPACA_%s_KEY_ID + ALPACA_%s_SECRET (or ALPACA_API_KEY + "
            "ALPACA_SECRET_KEY directly).",
            env, suffix, suffix,
        )

    return key, secret, is_paper
