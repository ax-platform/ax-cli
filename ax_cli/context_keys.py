"""Helpers for stable, non-colliding context keys."""

from __future__ import annotations

import re
import time
from pathlib import Path
from uuid import uuid4


def build_upload_context_key(filename: str, attachment_id: str | None = None) -> str:
    """Build the default context key for uploads.

    Using the raw filename as the key caused repeated uploads of image.png to
    overwrite earlier context entries. Keep explicit --key exact, but make the
    default safe for paste/upload-heavy workflows.
    """

    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(filename).name).strip("-._")
    if not safe_name:
        safe_name = "upload"
    suffix = (attachment_id or str(uuid4())).strip()[:36]
    return f"upload:{int(time.time() * 1000)}:{safe_name}:{suffix}"
