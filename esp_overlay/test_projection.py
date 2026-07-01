"""
Unit tests for the 3D→2D projection math.

Tests verify that known camera positions + known world positions
produce expected screen coordinates.
"""

import math
import sys
import os

# Add parent dir to path so we can import projection
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from projection import world_to_screen, build_view_matrix, build_projection_matrix


def test_point_directly_ahead():
    """A point directly in front of the camera should project to screen center."""
    # Camera at origin, looking along -Z (Roblox default)
    # CFrame identity: pos=(0,0,0), rotation=identity
    # In Roblox, identity CFrame looks along -Z, so a point at (0,0,-10) is ahead.
    cf = [
        0, 0, 0,     # position
        1, 0, 0,     # RightVector column: r00, r01, r02
        0, 1, 0,     # UpVector column: r10, r11, r12
        0, 0, 1,     # -LookVector column: r20, r21, r22
    ]
    fov = 70
    vp_w, vp_h = 1920, 1080

    # Point at (0, 0, -10) — directly ahead
    sx, sy, visible = world_to_screen([0, 0, -10], cf, fov, vp_w, vp_h)

    assert visible, "Point directly ahead should be visible"
    assert abs(sx - vp_w / 2) < 2, f"X should be center (~960), got {sx}"
    assert abs(sy - vp_h / 2) < 2, f"Y should be center (~540), got {sy}"
    print(f"  PASS: center point -> ({sx:.1f}, {sy:.1f})")


def test_point_behind_camera():
    """A point behind the camera should not be visible."""
    cf = [
        0, 0, 0,
        1, 0, 0,
        0, 1, 0,
        0, 0, 1,
    ]
    fov = 70
    vp_w, vp_h = 1920, 1080

    # Point at (0, 0, 10) — behind camera (camera looks at -Z)
    sx, sy, visible = world_to_screen([0, 0, 10], cf, fov, vp_w, vp_h)

    assert not visible, "Point behind camera should not be visible"
    print(f"  PASS: behind camera -> not visible")


def test_point_to_the_right():
    """A point to the right should project to the right half of the screen."""
    cf = [
        0, 0, 0,
        1, 0, 0,
        0, 1, 0,
        0, 0, 1,
    ]
    fov = 70
    vp_w, vp_h = 1920, 1080

    # Point at (5, 0, -10) — to the right and ahead
    sx, sy, visible = world_to_screen([5, 0, -10], cf, fov, vp_w, vp_h)

    assert visible, "Point should be visible"
    assert sx > vp_w / 2, f"X should be right of center (>960), got {sx}"
    print(f"  PASS: right point -> ({sx:.1f}, {sy:.1f})")


def test_point_above():
    """A point above should project to the upper half of the screen."""
    cf = [
        0, 0, 0,
        1, 0, 0,
        0, 1, 0,
        0, 0, 1,
    ]
    fov = 70
    vp_w, vp_h = 1920, 1080

    # Point at (0, 5, -10) — above and ahead
    sx, sy, visible = world_to_screen([0, 5, -10], cf, fov, vp_w, vp_h)

    assert visible, "Point should be visible"
    assert sy < vp_h / 2, f"Y should be above center (<540), got {sy}"
    print(f"  PASS: above point -> ({sx:.1f}, {sy:.1f})")


def test_camera_offset():
    """Camera at a different position should still project correctly."""
    # Camera at (100, 50, 200), looking along -Z
    cf = [
        100, 50, 200,
        1, 0, 0,
        0, 1, 0,
        0, 0, 1,
    ]
    fov = 70
    vp_w, vp_h = 1920, 1080

    # Point directly ahead of this camera
    sx, sy, visible = world_to_screen([100, 50, 190], cf, fov, vp_w, vp_h)

    assert visible, "Point should be visible"
    assert abs(sx - vp_w / 2) < 2, f"X should be center, got {sx}"
    assert abs(sy - vp_h / 2) < 2, f"Y should be center, got {sy}"
    print(f"  PASS: offset camera center -> ({sx:.1f}, {sy:.1f})")


def test_rotated_camera():
    """Camera rotated 90° to the right (looking along +X)."""
    # Looking along +X means:
    # RightVector = (0, 0, 1)  -> into screen in world
    # UpVector = (0, 1, 0)     -> up stays up
    # -LookVector = (-1, 0, 0) -> but CFrame stores the -Z column
    # Wait, let me think about this more carefully.
    #
    # If camera looks along +X:
    #   LookVector = (1, 0, 0)  (the direction camera faces)
    #   RightVector = (0, 0, -1) (cross of Up x Look if Up=(0,1,0))
    #   Actually: Right = Up × (-Look) ... let me just use the CFrame convention.
    #
    # CFrame looking along +X (yaw = -90°):
    #   RightVector  = (0, 0, -1)
    #   UpVector     = (0, 1, 0)
    #   -LookVector  = (-1, 0, 0)  (stored in CFrame, which is -LookVector)
    #
    # CFrame columns: r00,r01,r02 = Right; r10,r11,r12 = Up; r20,r21,r22 = -Look
    # Wait, actually in our cf array layout:
    # cf = [x, y, z, r00, r01, r02, r10, r11, r12, r20, r21, r22]
    # And in build_view_matrix:
    #   rx, ry, rz = cf[3], cf[6], cf[9]    # RightVector
    #   ux, uy, uz = cf[4], cf[7], cf[10]   # UpVector
    #   fx, fy, fz = -cf[5], -cf[8], -cf[11]  # LookVector (negate the -Z column)
    #
    # So cf stores the rotation matrix row by row (r00,r01,r02,r10,...), and we
    # extract columns from it. In Roblox, CFrame components are:
    #   CFrame:GetComponents() -> x,y,z, r00,r01,r02, r10,r11,r12, r20,r21,r22
    # where columns are Right, Up, -Look.
    #
    # For looking along +X:
    #   Right = (0, 0, -1)  -> column 0: r00=0, r10=0, r20=-1
    #   Up    = (0, 1, 0)   -> column 1: r01=0, r11=1, r21=0
    #   -Look = (-1, 0, 0)  -> column 2: r02=-1, r12=0, r22=0
    #
    # So cf[3..11] = [0, 0, -1,  0, 1, 0,  -1, 0, 0]

    cf = [
        0, 0, 0,
        0, 0, -1,   # row 0: r00, r01, r02
        0, 1, 0,    # row 1: r10, r11, r12
        -1, 0, 0,   # row 2: r20, r21, r22
    ]
    fov = 70
    vp_w, vp_h = 1920, 1080

    # Point at (10, 0, 0) — directly ahead when looking along +X
    sx, sy, visible = world_to_screen([10, 0, 0], cf, fov, vp_w, vp_h)

    assert visible, "Point should be visible"
    assert abs(sx - vp_w / 2) < 2, f"X should be center, got {sx}"
    assert abs(sy - vp_h / 2) < 2, f"Y should be center, got {sy}"
    print(f"  PASS: rotated camera center -> ({sx:.1f}, {sy:.1f})")


if __name__ == "__main__":
    print("Running projection tests...\n")

    tests = [
        test_point_directly_ahead,
        test_point_behind_camera,
        test_point_to_the_right,
        test_point_above,
        test_camera_offset,
        test_rotated_camera,
    ]

    passed = 0
    failed = 0

    for test in tests:
        name = test.__name__
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  FAIL: {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {name}: {e}")
            failed += 1

    print(f"\n{passed}/{passed + failed} tests passed")
    if failed > 0:
        sys.exit(1)
