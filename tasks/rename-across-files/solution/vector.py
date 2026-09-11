"""二维向量的最小实现，供 physics.py 与 sim.py 使用。"""
import math


class Vector2D:
    """平面上的一个向量。"""

    def __init__(self, x, y):
        self.x = x
        self.y = y

    def plus(self, other):
        return Vector2D(self.x + other.x, self.y + other.y)

    def scale(self, factor):
        return Vector2D(self.x * factor, self.y * factor)

    def length(self):
        return math.hypot(self.x, self.y)
