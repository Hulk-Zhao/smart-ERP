"""让 `erp_doc` 容器可以直接 `python -m apps.docparse` 起进程。"""

from __future__ import annotations

from .service import main

if __name__ == "__main__":
    raise SystemExit(main())
