from __future__ import annotations


class MockQuotaProbe:
    continuous = True

    def __init__(self, snapshots: list[dict[str, float]] | None = None):
        self.snapshots = snapshots or [
            {"5h": 10.0, "week": 20.0},
            {"5h": 12.0, "week": 21.0},
        ]
        self.index = 0

    def snapshot(self) -> dict[str, float]:
        value = self.snapshots[min(self.index, len(self.snapshots) - 1)]
        self.index += 1
        return dict(value)
