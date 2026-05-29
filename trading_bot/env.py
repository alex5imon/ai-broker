"""Environment resolution for Alpaca credentials.

Local ``.env`` and GitHub Actions secrets share the same four canonical names:

- ``ALPACA_PAPER_KEY_ID``  / ``ALPACA_PAPER_SECRET``
- ``ALPACA_LIVE_KEY_ID``   / ``ALPACA_LIVE_SECRET``

``ALPACA_ENV`` (``paper`` | ``live``, default ``paper``) selects which pair the
process should use.  This module loads ``.env`` (without overriding existing
env vars, so CI secrets always win) and exports the chosen pair as the legacy
``ALPACA_API_KEY`` / ``ALPACA_SECRET_KEY`` names that the rest of the codebase
already reads — keeping a single runtime contract for both local and CI.

Authoritative-not-advisory contract (issue #147)
-------------------------------------------------
``ALPACA_ENV`` together with the canonical ``ALPACA_{PAPER,LIVE}_KEY_ID`` pair
is the source of truth.  Previously a stray ``ALPACA_API_KEY`` exported from a
shell or a local ``.env`` would silently short-circuit selection and displace
the canonical pair — making ``ALPACA_ENV`` merely advisory.  Disjoint
paper/live key spaces meant a wrong key just 401'd, so this was fail-safe at
the API layer; but with a live account in play, confused-deputy operator error
becomes a real risk vector.

Now: if ``ALPACA_API_KEY`` is set but does **not** match the canonical key for
the selected environment (it matches the *other* environment's key, or matches
neither while a canonical pair is configured), the resolver logs ``CRITICAL``
and refuses for the remainder of the process — it scrubs the legacy names from
``os.environ`` and hands back empty credentials.  ``GatewayConnection.connect()``
reads ``os.environ`` directly and returns ``False`` on empty credentials, so the
tick aborts; CLI callers see the empty return value and exit non-zero.

A directly-set ``ALPACA_API_KEY`` is still honored when **no** canonical key id
is configured at all — the documented local-dev path (see ``CLAUDE.md``), where
there is no canonical pair to displace.
"""

from __future__ import annotations

import logging
import os

logger: logging.Logger = logging.getLogger(__name__)

# Process-wide refuse latch.  A credential mismatch is a fatal misconfiguration:
# once tripped we stay refused for the rest of the process so that a single
# import-time ``resolve_alpaca_env()`` (see ``trading_bot/__init__.py``) keeps
# *both* the live tick (``GatewayConnection`` reads ``os.environ``) and any CLI
# re-invocation (reads the return value) from proceeding on bad credentials.
_GUARD_TRIPPED: bool = False


def _reset_env_guard() -> None:
    """Test hook: clear the process-wide refuse latch."""
    global _GUARD_TRIPPED
    _GUARD_TRIPPED = False


def _scrub_legacy_env() -> None:
    """Remove the legacy credential names so the refusal reaches every reader."""
    os.environ.pop("ALPACA_API_KEY", None)
    os.environ.pop("ALPACA_SECRET_KEY", None)


def _refuse(env: str, detail: str, is_paper: bool) -> tuple[str, str, bool]:
    """Trip the guard, scrub the legacy names, and return the empty sentinel."""
    global _GUARD_TRIPPED
    _GUARD_TRIPPED = True
    _scrub_legacy_env()
    logger.critical(
        "ALPACA_API_KEY does not match the ALPACA_ENV=%s credential pair (%s). "
        "Refusing to start: a stray ALPACA_API_KEY must not displace the "
        "canonical ALPACA_PAPER_KEY_ID / ALPACA_LIVE_KEY_ID pair. Unset "
        "ALPACA_API_KEY / ALPACA_SECRET_KEY or correct ALPACA_ENV before retry.",
        env, detail,
    )
    return "", "", is_paper


def resolve_alpaca_env() -> tuple[str, str, bool]:
    """Resolve Alpaca credentials based on ``ALPACA_ENV``.

    Returns ``(api_key, secret_key, is_paper)``.  Sets ``ALPACA_API_KEY`` and
    ``ALPACA_SECRET_KEY`` in ``os.environ`` if they are not already populated.
    Missing keys are returned as empty strings — callers decide whether to
    error out (CI/live runs) or proceed (offline backtests).

    A credential mismatch (see module docstring) returns empty strings and
    latches the process into a refusing state.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(override=False)
    except ImportError:
        pass

    env: str = os.environ.get("ALPACA_ENV", "paper").strip().lower()
    is_paper: bool = env != "live"

    # Already refused earlier this process — stay refused.
    if _GUARD_TRIPPED:
        _scrub_legacy_env()
        return "", "", is_paper

    paper_key: str = os.environ.get("ALPACA_PAPER_KEY_ID", "")
    live_key: str = os.environ.get("ALPACA_LIVE_KEY_ID", "")
    expected_key: str = paper_key if is_paper else live_key
    other_key: str = live_key if is_paper else paper_key

    legacy_key: str = os.environ.get("ALPACA_API_KEY", "")
    legacy_secret: str = os.environ.get("ALPACA_SECRET_KEY", "")

    if legacy_key and legacy_secret:
        # A directly-set ALPACA_API_KEY is authoritative ONLY when it matches
        # the canonical key for the selected env.  This is also the steady
        # state on any second call within a process: the resolver re-exports
        # the chosen pair under these names below.
        if expected_key and legacy_key == expected_key:
            return legacy_key, legacy_secret, is_paper

        # Matches the *other* environment's key — classic confused deputy
        # (e.g. ALPACA_API_KEY == ALPACA_LIVE_KEY_ID while ALPACA_ENV=paper).
        if other_key and legacy_key == other_key:
            wrong_env: str = "live" if is_paper else "paper"
            return _refuse(
                env,
                f"it is the {wrong_env} key while ALPACA_ENV selects {env}",
                is_paper,
            )

        # A canonical pair is configured for some env but the legacy key
        # matches neither — a stray export displacing the canonical pair.
        if expected_key or other_key:
            return _refuse(env, "it matches neither configured key pair", is_paper)

        # No canonical key ids configured at all: honor the directly-set
        # ALPACA_API_KEY (documented local-dev path — nothing to displace).
        return legacy_key, legacy_secret, is_paper

    # No legacy override → select the canonical pair for ALPACA_ENV.
    if is_paper:
        key: str = paper_key
        secret: str = os.environ.get("ALPACA_PAPER_SECRET", "")
    else:
        key = live_key
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
