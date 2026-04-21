"""Package entry point: ``python -m trading_bot``.

Usage::

    python -m trading_bot              # run the live/paper trading bot
    python -m trading_bot backtest ... # run the backtesting engine
"""

from __future__ import annotations

import sys


def _main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "backtest":
        # Strip the 'backtest' sub-command so argparse in backtest.py
        # sees the remaining flags (--date, --equity, etc.) directly.
        sys.argv.pop(1)
        import asyncio
        from trading_bot.backtest import main
        asyncio.run(main())
    else:
        import asyncio
        from trading_bot.main import main  # type: ignore[import]
        asyncio.run(main())


_main()
