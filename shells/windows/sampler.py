"""采样实现与 macOS 壳共用，集中在 ``shells.shared.sampler``；此处仅做转发。

保留本模块只为与 ``shells/macos`` 的目录结构对齐（``from .sampler import
Sampler, build_session`` 的写法两壳一致）。
"""

from shells.shared.sampler import Sampler, build_session

__all__ = ["Sampler", "build_session"]
