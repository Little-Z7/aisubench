"""把全部物体受到的重力累加起来，并输出一行摘要。"""
from physics import compute_force, momentum
from vector import Vector2D

GRAVITY = Vector2D(0.0, -10.0)
BODIES = [(2.0, Vector2D(1.0, 2.0)), (0.5, Vector2D(-3.0, 4.0))]


def total_weight():
    result = Vector2D(0.0, 0.0)
    for mass, _velocity in BODIES:
        result = result.plus(compute_force(mass, GRAVITY))
    return result


def reported():
    weight = total_weight()
    total_mass = sum(mass for mass, _velocity in BODIES)
    pile = momentum(total_mass, weight)
    return f"x={weight.x:.1f} y={weight.y:.1f} p={pile.length():.1f}"


if __name__ == "__main__":
    print(reported())
