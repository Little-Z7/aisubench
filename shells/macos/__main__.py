"""``python3 -m shells.macos`` 入口：转发给 ``shells.macos.app.main``。"""

import sys

from .app import main

if __name__ == "__main__":
    sys.exit(main())
