"""诊断 SMTP 邮件链路（命令行入口，逻辑在 paper_radar/diagnostics.py）。

用法：
    python scripts/diag_smtp.py
    python -m paper_radar diag-mail        # 等价
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paper_radar.config import get_config  # noqa: E402
from paper_radar.diagnostics import diagnose_smtp  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(diagnose_smtp(get_config()))
