"""Put the bot directory on ``sys.path`` so ``_config`` is importable in tests.

The community bot is not a workspace package (it deliberately avoids the zu deps and
ships discord.py separately), so there is no installed distribution to import from.
Adding the bot dir here lets the offline test import the dependency-free ``_config``
helper without pulling in discord.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))
