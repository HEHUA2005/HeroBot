from __future__ import annotations

import logging


def configure_logging(level_name: str = "INFO") -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=getattr(logging, level_name.upper(), logging.INFO),
    )
