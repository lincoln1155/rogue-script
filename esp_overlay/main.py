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
FADE_START_DIST = 200.0   # Start fading down when closer than this (studs)
FADE_MIN_OPACITY = 0.50  # Minimum opacity at 0 distance
FADE_FAR_DIST = 3000.0   # Start fading out when farther than this

# Rendering settings
HEALTH_BAR_WIDTH = 40
HEALTH_BAR_HEIGHT = 4
NAME_OFFSET_Y = -5        # Above the health bar
HEALTH_BAR_OFFSET_Y = 0   # Base offset
DISTANCE_OFFSET_Y = 8     # Below the health bar

# Colors
COLOR_DOT = (255, 255, 255)
COLOR_NAME = (255, 255, 255)
COLOR_DISTANCE = (200, 200, 200)
COLOR_HEALTH_FULL = (75, 200, 75)
COLOR_HEALTH_EMPTY = (200, 75, 75)
COLOR_HEALTH_BG = (40, 40, 40)
COLOR_TRINKET = (255, 255, 255)

EVENT_TRINKETS = {
    "Ornament", "Present", "Candy", "Scary Mask", "Pumpkin Centerpiece", "Idol of War"
}

ARTIFACT_TRINKETS = {
    "Rift Gem", "Amulet of the White King", "Lannis Amulet", "Mysterious Artifact",
    "Phoenix Flower", "Azael Horn", "Phoenix Down", "Night Stone", "Howler Friend", "Ice Essence"
}

# ============================================================
# SHARED STATE
# ============================================================

class GameState:
    """Thread-safe container for the latest game data."""

    def __init__(self):
        self.lock = threading.Lock()
        self.camera = None       # {"cf": [...], "fov": float, "vp": [w, h]}
        self.players = []        # [{"name": str, "pos": [x,y,z], "hp": float, "dist": float}, ...]
        self.trinkets = []       # [{"name": str, "pos": [x,y,z], "dist": float}, ...]
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
            self.trinkets = data.get("trinkets", [])
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
                "trinkets": list(self.trinkets),
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
    At < FADE_START_DIST: fades down to FADE_MIN_OPACITY
    At > FADE_FAR_DIST: fades down to FADE_MIN_OPACITY
    Between: fully opaque (1.0)
    """
    if dist < FADE_START_DIST:
        t = dist / FADE_START_DIST
        return FADE_MIN_OPACITY + (1.0 - FADE_MIN_OPACITY) * t
    elif dist > FADE_FAR_DIST:
        t = (dist - FADE_FAR_DIST) / 1000.0 # Fade out over 1000 studs
        t = min(1.0, max(0.0, t))
        return 1.0 - (1.0 - FADE_MIN_OPACITY) * t
    
    return 1.0


def apply_opacity(color, opacity):
    """Apply opacity to an RGB color by blending with transparent color."""
    return (
        int(TRANSPARENT_COLOR[0] + (color[0] - TRANSPARENT_COLOR[0]) * opacity),
        int(TRANSPARENT_COLOR[1] + (color[1] - TRANSPARENT_COLOR[1]) * opacity),
        int(TRANSPARENT_COLOR[2] + (color[2] - TRANSPARENT_COLOR[2]) * opacity),
    )


def draw_esp_marker(surface, font, cx, top_sy, bot_sy, name, hp, dist, opacity, box_h):
    """Draw a single ESP marker (name above head, vertical health bar on the right)."""
    ix = int(cx)
    top_y = int(top_sy)
    bot_y = int(bot_sy)

    scale = 1.0
    if dist < FADE_START_DIST:
        scale = 0.7 + 0.3 * (dist / FADE_START_DIST)
    elif dist > 1000.0:
        scale = 0.6
    else:
        scale = max(0.6, 1.0 - 0.4 * ((dist - FADE_START_DIST) / (1000.0 - FADE_START_DIST)))

    font_size = max(10, int(12 * scale))
    
    # We want name and distance above the head
    name_color = apply_opacity(COLOR_NAME, opacity)
    name_surf, name_rect = font.render(name, name_color, size=font_size)
    
    dist_text = f"{dist:.0f}m"
    dist_color = apply_opacity(COLOR_DISTANCE, opacity)
    dist_surf, dist_rect = font.render(dist_text, dist_color, size=font_size)

    text_spacing = 2
    total_text_h = name_rect.height + dist_rect.height + text_spacing
    
    # --- Name ---
    name_x = ix - name_rect.width // 2
    name_y = top_y - total_text_h - int(5 * scale)
    surface.blit(name_surf, (name_x, name_y))

    # --- Distance ---
    dist_x = ix - dist_rect.width // 2
    dist_y = name_y + name_rect.height + text_spacing
    surface.blit(dist_surf, (dist_x, dist_y))

    # --- Vertical Health bar on the right ---
    bar_width = max(2, int(4 * scale))
    bar_height = max(10, int(box_h))
    
    box_w = box_h / 2.0
    bar_x = ix + int(box_w / 2) + int(5 * scale)
    bar_y = top_y

    # Background
    bg_color = apply_opacity(COLOR_HEALTH_BG, opacity)
    pygame.draw.rect(surface, bg_color, (bar_x, bar_y, bar_width, bar_height))

    # Filled portion (fill from bottom to top)
    hp_clamped = max(0.0, min(1.0, hp))
    fill_h = int(bar_height * hp_clamped)
    if fill_h > 0:
        hp_color = lerp_color(COLOR_HEALTH_EMPTY, COLOR_HEALTH_FULL, hp_clamped)
        hp_color = apply_opacity(hp_color, opacity)
        pygame.draw.rect(surface, hp_color, (bar_x, bar_y + (bar_height - fill_h), bar_width, fill_h))


def draw_trinket_marker(surface, font, x, y, name, dist, color):
    """Draw a single trinket marker (name + distance) with fixed size and opacity."""
    ix, iy = int(x), int(y)

    font_size = 14
    dist_off = 15

    # --- Name ---
    name_surf, name_rect = font.render(name, color, size=font_size)
    name_x = ix - name_rect.width // 2
    name_y = iy - name_rect.height // 2
    surface.blit(name_surf, (name_x, name_y))

    # --- Distance ---
    dist_text = f"{dist:.0f}m"
    dist_surf, dist_rect = font.render(dist_text, COLOR_DISTANCE, size=font_size)
    dist_x = ix - dist_rect.width // 2
    dist_y = iy + dist_off + name_rect.height // 2
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
                if player["dist"] > 1000.0:
                    continue

                # Get interpolated position for smooth rendering
                interp_pos = game_state.get_interpolated_pos(player["name"], player["pos"])

                # Calculate bounding box
                top_pos = [interp_pos[0], interp_pos[1] + 2.5, interp_pos[2]]
                bot_pos = [interp_pos[0], interp_pos[1] - 3.0, interp_pos[2]]

                top_sx, top_sy, top_vis = world_to_screen(top_pos, cf, fov, cam_vp[0], cam_vp[1])
                bot_sx, bot_sy, bot_vis = world_to_screen(bot_pos, cf, fov, cam_vp[0], cam_vp[1])

                if not top_vis and not bot_vis:
                    continue

                scale_x = vp_w / cam_vp[0]
                scale_y = vp_h / cam_vp[1]
                top_sx *= scale_x
                top_sy *= scale_y
                bot_sx *= scale_x
                bot_sy *= scale_y

                cx = (top_sx + bot_sx) / 2
                box_h = abs(bot_sy - top_sy)

                opacity = get_opacity(player["dist"])

                draw_esp_marker(
                    screen, font,
                    cx, top_sy, bot_sy,
                    player["name"],
                    player["hp"],
                    player["dist"],
                    opacity, box_h
                )

            # Draw trinkets
            for trinket in snapshot.get("trinkets", []):
                t_name = trinket["name"]
                t_dist = trinket["dist"]
                
                t_color = COLOR_TRINKET

                if t_name in ARTIFACT_TRINKETS:
                    t_color = (255, 50, 50)  # Red for artifacts/rares
                    if t_name == "Phoenix Down":
                        t_color = (255, 255, 0)  # Yellow for Phoenix Down
                    elif t_name == "Ice Essence":
                        t_color = (100, 200, 255) # Blue for Ice Essence
                elif t_name in EVENT_TRINKETS:
                    if t_dist >= 400.0:
                        continue
                else:
                    if t_dist >= 150.0:
                        continue

                sx, sy, visible = world_to_screen(
                    trinket["pos"], cf, fov, cam_vp[0], cam_vp[1]
                )

                if not visible:
                    continue

                scale_x = vp_w / cam_vp[0]
                scale_y = vp_h / cam_vp[1]
                sx *= scale_x
                sy *= scale_y

                draw_trinket_marker(
                    screen, font,
                    sx, sy,
                    t_name,
                    t_dist,
                    t_color
                )

        pygame.display.flip()
        clock.tick(OVERLAY_FPS)

    pygame.quit()
    print("[ESP] Overlay closed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
