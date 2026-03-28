from __future__ import annotations

import logging
import os


os.environ.setdefault("PREFECT_LOGGING_INTERNAL_LEVEL", "ERROR")
logging.raiseExceptions = False
