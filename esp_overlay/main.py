"""
ESP Overlay for Rogue Lineage.

A transparent always-on-top window that renders player ESP markers
(dot + name + health bar + distance) over the Roblox game window.

Receives data from rogue_lite.lua via WebSocket (primary) or HTTP POST (fallback)
on port 27015. Performs 3D→2D projection from camera CFrame data and renders
at ~60fps with position interpolation between updates.

Usage:
    python main.py
    (or run esp_overlay.exe after building with PyInstaller)
"""

import asyncio
import json
import math
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

import pygame
import pygame.freetype

try:
    import win32gui
    import win32con
    import win32api
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False
    print("[ESP] Warning: pywin32 not found. Window transparency will not work.")

try:
    import websockets
    import websockets.asyncio.server
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False
    print("[ESP] Warning: websockets not found. Only HTTP fallback available.")

from projection import world_to_screen

# ============================================================
# CONFIGURATION
# ============================================================

ESP_PORT = 27015
OVERLAY_FPS = 60
TRANSPARENT_COLOR = (1, 1, 1)  # Color key for transparency (near-black, invisible)
DEFAULT_VIEWPORT = (1920, 1080)

# Proximity fade settings
FADE_START_DIST = 50.0   # Start fading at this distance (studs)
FADE_MIN_OPACITY = 0.20  # Minimum opacity at 0 distance

# Rendering settings
DOT_RADIUS = 5
HEALTH_BAR_WIDTH = 40
HEALTH_BAR_HEIGHT = 4
NAME_OFFSET_Y = -18       # Above the dot
HEALTH_BAR_OFFSET_Y = 12  # Below the dot
DISTANCE_OFFSET_Y = 20    # Below the health bar

# Colors
COLOR_DOT = (255, 255, 255)
COLOR_NAME = (255, 255, 255)
COLOR_DISTANCE = (200, 200, 200)
COLOR_HEALTH_FULL = (75, 200, 75)
COLOR_HEALTH_EMPTY = (200, 75, 75)
COLOR_HEALTH_BG = (40, 40, 40)

# ============================================================
# SHARED STATE
# ============================================================

class GameState:
    """Thread-safe container for the latest game data."""

    def __init__(self):
        self.lock = threading.Lock()
        self.camera = None       # {"cf": [...], "fov": float, "vp": [w, h]}
        self.players = []        # [{"name": str, "pos": [x,y,z], "hp": float, "dist": float}, ...]
        self.last_update = 0.0
        self.connected = False

        # Interpolation: previous positions for smooth movement
        self._prev_players = {}  # name -> {"pos": [x,y,z], "time": float}
        self._curr_players = {}  # name -> {"pos": [x,y,z], "time": float}

    def update(self, data):
        """Update game state from received JSON data."""
        now = time.time()
        with self.lock:
            self.camera = data.get("camera")
            new_players = data.get("players", [])

            # Shift current → previous for interpolation
            self._prev_players = dict(self._curr_players)
            self._curr_players = {}
            for p in new_players:
                self._curr_players[p["name"]] = {
                    "pos": list(p["pos"]),
                    "time": now,
                }

            self.players = new_players
            self.last_update = now
            self.connected = True

    def get_interpolated_pos(self, name, current_pos):
        """
        Get interpolated position for smooth rendering between updates.
        Returns the interpolated [x, y, z] position.
        """
        now = time.time()
        with self.lock:
            prev = self._prev_players.get(name)
            curr = self._curr_players.get(name)

        if not prev or not curr:
            return current_pos

        dt = curr["time"] - prev["time"]
        if dt <= 0:
            return current_pos

        # How far we are between the last two updates
        t = min((now - curr["time"]) / dt + 1.0, 2.0)
        t = max(t, 0.0)

        # Linear extrapolation from prev → curr
        return [
            prev["pos"][i] + (curr["pos"][i] - prev["pos"][i]) * t
            for i in range(3)
        ]

    def get_snapshot(self):
        """Get a thread-safe copy of current state."""
        with self.lock:
            return {
                "camera": dict(self.camera) if self.camera else None,
                "players": list(self.players),
                "connected": self.connected,
                "age": time.time() - self.last_update if self.last_update > 0 else float("inf"),
            }


game_state = GameState()

# ============================================================
# HTTP FALLBACK SERVER
# ============================================================

class ESPHTTPHandler(BaseHTTPRequestHandler):
    """Handles HTTP POST /update as fallback IPC."""

    def do_POST(self):
        if self.path == "/update":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                game_state.update(data)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok")
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"bad json")
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        """Health check endpoint."""
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        """Suppress default HTTP logging."""
        pass


def run_http_server():
    """Run the HTTP fallback server in a background thread."""
    server = HTTPServer(("127.0.0.1", ESP_PORT), ESPHTTPHandler)
    server.serve_forever()

# ============================================================
# WEBSOCKET SERVER
# ============================================================

async def ws_handler(websocket):
    """Handle an incoming WebSocket connection from the Lua script."""
    print("[ESP] WebSocket client connected")
    game_state.connected = True
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                game_state.update(data)
            except json.JSONDecodeError:
                pass
    except Exception:
        pass
    finally:
        print("[ESP] WebSocket client disconnected")
        game_state.connected = False


async def run_ws_server():
    """Run the WebSocket server."""
    async with websockets.asyncio.server.serve(ws_handler, "127.0.0.1", ESP_PORT + 1):
        print(f"[ESP] WebSocket server listening on ws://127.0.0.1:{ESP_PORT + 1}")
        await asyncio.Future()  # Run forever

# ============================================================
# WINDOW MANAGEMENT
# ============================================================

def find_roblox_window():
    """Find the Roblox game window and return its rect (x, y, w, h)."""
    if not HAS_WIN32:
        return None

    hwnd = win32gui.FindWindow("WINDOWSCLIENT", None)  # Roblox window class
    if not hwnd:
        # Fallback: search by title
        def callback(h, results):
            title = win32gui.GetWindowText(h)
            if "Roblox" in title:
                results.append(h)
            return True

        results = []
        win32gui.EnumWindows(callback, results)
        if results:
            hwnd = results[0]
        else:
            return None

    try:
        rect = win32gui.GetClientRect(hwnd)
        point = win32gui.ClientToScreen(hwnd, (rect[0], rect[1]))
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        return (point[0], point[1], w, h)
    except Exception:
        return None


def setup_transparent_window(hwnd):
    """Make a pygame window transparent and click-through."""
    if not HAS_WIN32:
        return

    # Set layered window style
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    style |= win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT | win32con.WS_EX_TOPMOST
    win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, style)

    # Set the transparent color key
    r, g, b = TRANSPARENT_COLOR
    win32gui.SetLayeredWindowAttributes(
        hwnd,
        win32api.RGB(r, g, b),
        0,
        win32con.LWA_COLORKEY
    )

    # Make always-on-top
    win32gui.SetWindowPos(
        hwnd,
        win32con.HWND_TOPMOST,
        0, 0, 0, 0,
        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
    )

# ============================================================
# RENDERING
# ============================================================

def lerp_color(c1, c2, t):
    """Linear interpolate between two RGB colors."""
    t = max(0.0, min(1.0, t))
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def get_opacity(dist):
    """
    Calculate opacity based on distance.
    At > FADE_START_DIST: fully opaque (1.0)
    At 0: FADE_MIN_OPACITY
    Linear interpolation between.
    """
    if dist >= FADE_START_DIST:
        return 1.0
    t = dist / FADE_START_DIST
    return FADE_MIN_OPACITY + (1.0 - FADE_MIN_OPACITY) * t


def apply_opacity(color, opacity):
    """Apply opacity to an RGB color by blending with transparent color."""
    return (
        int(TRANSPARENT_COLOR[0] + (color[0] - TRANSPARENT_COLOR[0]) * opacity),
        int(TRANSPARENT_COLOR[1] + (color[1] - TRANSPARENT_COLOR[1]) * opacity),
        int(TRANSPARENT_COLOR[2] + (color[2] - TRANSPARENT_COLOR[2]) * opacity),
    )


def draw_esp_marker(surface, font, x, y, name, hp, dist, opacity):
    """Draw a single ESP marker (dot + name + health bar + distance)."""
    ix, iy = int(x), int(y)

    # --- Dot ---
    dot_color = apply_opacity(COLOR_DOT, opacity)
    pygame.draw.circle(surface, dot_color, (ix, iy), DOT_RADIUS)

    # --- Name ---
    name_color = apply_opacity(COLOR_NAME, opacity)
    name_surf, name_rect = font.render(name, name_color)
    name_x = ix - name_rect.width // 2
    name_y = iy + NAME_OFFSET_Y - name_rect.height
    surface.blit(name_surf, (name_x, name_y))

    # --- Health bar ---
    bar_x = ix - HEALTH_BAR_WIDTH // 2
    bar_y = iy + HEALTH_BAR_OFFSET_Y

    # Background
    bg_color = apply_opacity(COLOR_HEALTH_BG, opacity)
    pygame.draw.rect(surface, bg_color, (bar_x, bar_y, HEALTH_BAR_WIDTH, HEALTH_BAR_HEIGHT))

    # Filled portion
    hp_clamped = max(0.0, min(1.0, hp))
    fill_w = int(HEALTH_BAR_WIDTH * hp_clamped)
    if fill_w > 0:
        hp_color = lerp_color(COLOR_HEALTH_EMPTY, COLOR_HEALTH_FULL, hp_clamped)
        hp_color = apply_opacity(hp_color, opacity)
        pygame.draw.rect(surface, hp_color, (bar_x, bar_y, fill_w, HEALTH_BAR_HEIGHT))

    # --- Distance ---
    dist_text = f"{dist:.0f}m"
    dist_color = apply_opacity(COLOR_DISTANCE, opacity)
    dist_surf, dist_rect = font.render(dist_text, dist_color)
    dist_x = ix - dist_rect.width // 2
    dist_y = iy + DISTANCE_OFFSET_Y
    surface.blit(dist_surf, (dist_x, dist_y))


def draw_status(surface, font, connected, age):
    """Draw connection status indicator in the top-left corner."""
    if connected and age < 2.0:
        status_text = "ESP Connected"
        status_color = (75, 200, 75)
    elif connected:
        status_text = f"ESP Stale ({age:.0f}s)"
        status_color = (200, 200, 75)
    else:
        status_text = "ESP Waiting..."
        status_color = (200, 75, 75)

    text_surf, text_rect = font.render(status_text, status_color)
    surface.blit(text_surf, (10, 10))

# ============================================================
# MAIN LOOP
# ============================================================

def main():
    print("[ESP] Starting ESP Overlay...")
    print(f"[ESP] HTTP server on http://127.0.0.1:{ESP_PORT}")

    # Start HTTP server in background thread
    http_thread = threading.Thread(target=run_http_server, daemon=True)
    http_thread.start()

    # Start WebSocket server in background thread
    if HAS_WEBSOCKETS:
        def ws_thread_func():
            asyncio.run(run_ws_server())
        ws_thread = threading.Thread(target=ws_thread_func, daemon=True)
        ws_thread.start()

    # Initialize Pygame
    pygame.init()
    pygame.freetype.init()

    # Find Roblox window to match size
    roblox_rect = find_roblox_window()
    if roblox_rect:
        vp_w, vp_h = roblox_rect[2], roblox_rect[3]
        window_x, window_y = roblox_rect[0], roblox_rect[1]
        print(f"[ESP] Found Roblox window at ({window_x}, {window_y}) size {vp_w}x{vp_h}")
    else:
        vp_w, vp_h = DEFAULT_VIEWPORT
        window_x, window_y = 0, 0
        print(f"[ESP] Roblox window not found, using default {vp_w}x{vp_h}")

    # Position the overlay window before creating the display
    import os
    os.environ["SDL_VIDEO_WINDOW_POS"] = f"{window_x},{window_y}"

    screen = pygame.display.set_mode((vp_w, vp_h), pygame.NOFRAME)
    pygame.display.set_caption("ESP Overlay")

    # Make the window transparent and click-through
    if HAS_WIN32:
        pygame.display.update()  # Force window creation
        # Find our own window
        esp_hwnd = pygame.display.get_wm_info()["window"]
        setup_transparent_window(esp_hwnd)

    # Load font
    try:
        font = pygame.freetype.SysFont("Consolas", 12)
    except Exception:
        font = pygame.freetype.SysFont(None, 12)

    status_font = pygame.freetype.SysFont("Consolas", 11)

    clock = pygame.time.Clock()
    last_roblox_check = 0

    print("[ESP] Overlay ready. Waiting for data from rogue_lite.lua...")

    running = True
    while running:
        # Event handling
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False

        # Periodically re-check Roblox window position
        now = time.time()
        if now - last_roblox_check > 2.0:
            last_roblox_check = now
            roblox_rect = find_roblox_window()
            if roblox_rect and HAS_WIN32:
                new_w, new_h = roblox_rect[2], roblox_rect[3]
                new_x, new_y = roblox_rect[0], roblox_rect[1]
                if (new_w != vp_w or new_h != vp_h):
                    vp_w, vp_h = new_w, new_h
                    screen = pygame.display.set_mode((vp_w, vp_h), pygame.NOFRAME)
                    esp_hwnd = pygame.display.get_wm_info()["window"]
                    setup_transparent_window(esp_hwnd)
                # Reposition overlay to match Roblox
                win32gui.SetWindowPos(
                    esp_hwnd,
                    win32con.HWND_TOPMOST,
                    new_x, new_y, 0, 0,
                    win32con.SWP_NOSIZE
                )

        # Clear screen with transparent color
        screen.fill(TRANSPARENT_COLOR)

        # Get current game state
        snapshot = game_state.get_snapshot()

        # Draw status indicator
        draw_status(screen, status_font, snapshot["connected"], snapshot["age"])

        # Draw ESP markers if we have camera data
        camera = snapshot["camera"]
        if camera and snapshot["age"] < 5.0:
            cf = camera["cf"]
            fov = camera["fov"]
            cam_vp = camera["vp"]

            for player in snapshot["players"]:
                # Get interpolated position for smooth rendering
                interp_pos = game_state.get_interpolated_pos(player["name"], player["pos"])

                # Project world position to screen
                sx, sy, visible = world_to_screen(
                    interp_pos, cf, fov, cam_vp[0], cam_vp[1]
                )

                if not visible:
                    continue

                # Scale screen coordinates if overlay size differs from game viewport
                # (e.g., game runs at 1920x1080 but overlay matches a different window size)
                scale_x = vp_w / cam_vp[0]
                scale_y = vp_h / cam_vp[1]
                sx *= scale_x
                sy *= scale_y

                # Calculate proximity fade opacity
                opacity = get_opacity(player["dist"])

                # Draw the marker
                draw_esp_marker(
                    screen, font,
                    sx, sy,
                    player["name"],
                    player["hp"],
                    player["dist"],
                    opacity
                )

        pygame.display.flip()
        clock.tick(OVERLAY_FPS)

    pygame.quit()
    print("[ESP] Overlay closed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
