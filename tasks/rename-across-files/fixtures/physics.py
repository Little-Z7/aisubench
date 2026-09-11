"""力学辅助函数。"""
from vector import Vec2


def calc_force(mass, acceleration: Vec2) -> Vec2:
    """F = m * a。"""
    return acceleration.scale(mass)


def momentum(mass, velocity: Vec2) -> Vec2:
    """p = m * v。"""
    return velocity.scale(mass)
