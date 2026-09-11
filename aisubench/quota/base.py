from __future__ import annotations

from typing import Protocol


class QuotaProbe(Protocol):
    """额度探针协议。

    continuous 为 True 表示探针可以随时读取且读数是连续可靠的（mock/arkcli），
    标定循环可以每轮任务后采样；False 表示读数成本高或精度有限（人工读页面），
    标定只在首尾各快照一次。
    """

    continuous: bool

    def snapshot(self) -> dict[str, float]: ...
