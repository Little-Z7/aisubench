"""力学辅助函数。"""
from vector import Vector2D


def compute_force(mass, acceleration: Vector2D) -> Vector2D:
    """F = m * a。"""
    return acceleration.scale(mass)


def momentum(mass, velocity: Vector2D) -> Vector2D:
    """p = m * v。"""
    return velocity.scale(mass)
