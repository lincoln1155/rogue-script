"""
3D-to-2D projection for ESP overlay.

Reconstructs a view-projection matrix from Roblox camera data
(CFrame components, FOV, viewport size) and projects world-space
positions to screen-space pixel coordinates.

CFrame layout from Lua (12 floats):
  [x, y, z, r00, r01, r02, r10, r11, r12, r20, r21, r22]

Roblox CFrame stores rotation as columns of the rotation matrix:
  RightVector  = (r00, r10, r20)
  UpVector     = (r01, r11, r21)
  -LookVector  = (r02, r12, r22)   # CFrame faces -Z, so this column IS the negated look direction

The view matrix transforms world coords into camera-local coords,
then a perspective projection matrix maps to clip space, and finally
we map to screen pixels.
"""

import math


def build_view_matrix(cf):
    """
    Build a 4x4 view matrix from a Roblox CFrame (12 floats).

    Args:
        cf: list of 12 floats [x, y, z, r00, r01, r02, r10, r11, r12, r20, r21, r22]

    Returns:
        4x4 view matrix as a list of 16 floats (row-major).
    """
    px, py, pz = cf[0], cf[1], cf[2]
    # Roblox CFrame columns: Right=(r00,r10,r20), Up=(r01,r11,r21), -Look=(r02,r12,r22)
    rx, ry, rz = cf[3], cf[6], cf[9]    # RightVector
    ux, uy, uz = cf[4], cf[7], cf[10]   # UpVector
    fx, fy, fz = cf[5], cf[8], cf[11]   # -LookVector (used directly as view Z axis)

    # View matrix: transpose of rotation * -translation
    # This is the standard OpenGL-style view matrix construction
    return [
        rx, ry, rz, -(rx * px + ry * py + rz * pz),
        ux, uy, uz, -(ux * px + uy * py + uz * pz),
        fx, fy, fz, -(fx * px + fy * py + fz * pz),
        0,  0,  0,  1
    ]


def build_projection_matrix(fov_deg, aspect, near=0.1, far=10000.0):
    """
    Build a perspective projection matrix.

    Args:
        fov_deg: vertical field of view in degrees (Roblox Camera.FieldOfView)
        aspect: viewport width / height
        near: near clipping plane
        far: far clipping plane

    Returns:
        4x4 projection matrix as a list of 16 floats (row-major).
    """
    fov_rad = math.radians(fov_deg)
    f = 1.0 / math.tan(fov_rad / 2.0)

    return [
        f / aspect, 0,  0,                              0,
        0,          f,  0,                              0,
        0,          0,  (far + near) / (near - far),    (2 * far * near) / (near - far),
        0,          0,  -1,                             0
    ]


def mat4_mul_vec4(m, v):
    """Multiply a 4x4 row-major matrix by a 4-component vector."""
    return [
        m[0] * v[0] + m[1] * v[1] + m[2] * v[2] + m[3] * v[3],
        m[4] * v[0] + m[5] * v[1] + m[6] * v[2] + m[7] * v[3],
        m[8] * v[0] + m[9] * v[1] + m[10] * v[2] + m[11] * v[3],
        m[12] * v[0] + m[13] * v[1] + m[14] * v[2] + m[15] * v[3],
    ]


def mat4_mul(a, b):
    """Multiply two 4x4 row-major matrices."""
    result = [0.0] * 16
    for row in range(4):
        for col in range(4):
            s = 0.0
            for k in range(4):
                s += a[row * 4 + k] * b[k * 4 + col]
            result[row * 4 + col] = s
    return result


def world_to_screen(world_pos, cf, fov_deg, viewport_w, viewport_h):
    """
    Project a 3D world position to 2D screen coordinates.

    Args:
        world_pos: [x, y, z] world position
        cf: list of 12 floats (Roblox CFrame components)
        fov_deg: camera vertical FOV in degrees
        viewport_w: viewport width in pixels
        viewport_h: viewport height in pixels

    Returns:
        (screen_x, screen_y, is_visible)
        screen_x, screen_y are pixel coordinates (0,0 = top-left).
        is_visible is False if the point is behind the camera.
    """
    view = build_view_matrix(cf)
    aspect = viewport_w / viewport_h
    proj = build_projection_matrix(fov_deg, aspect)
    vp = mat4_mul(proj, view)

    clip = mat4_mul_vec4(vp, [world_pos[0], world_pos[1], world_pos[2], 1.0])

    # Behind camera check
    if clip[3] <= 0:
        return 0, 0, False

    # Perspective divide → NDC (-1 to 1)
    ndc_x = clip[0] / clip[3]
    ndc_y = clip[1] / clip[3]

    # NDC to screen pixels (Y is flipped: NDC +1 = top, screen 0 = top)
    screen_x = (ndc_x + 1.0) * 0.5 * viewport_w
    screen_y = (1.0 - ndc_y) * 0.5 * viewport_h

    # Check if on screen (with small margin)
    margin = 50
    is_visible = (
        -margin <= screen_x <= viewport_w + margin and
        -margin <= screen_y <= viewport_h + margin
    )

    return screen_x, screen_y, is_visible
