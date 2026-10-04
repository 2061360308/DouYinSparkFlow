"""让 ``python -m scheduler`` 可用。"""

from app.scheduler.cli import main

raise SystemExit(main())
