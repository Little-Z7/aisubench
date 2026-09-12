"""``python3 -m shells.windows`` 入口：转发给 ``shells.windows.ball.main``。"""

import sys

from .ball import main

if __name__ == "__main__":
    sys.exit(main())
