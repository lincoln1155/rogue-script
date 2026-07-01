# External ESP Overlay — Design & Implementation Plan

> This file documents the design decisions and implementation details for the external ESP overlay feature.
> It is intended for future reference by developers and AI agents working on this project.

## Overview

An external ESP system that renders player positions, names, health bars, and distances on a transparent overlay window that sits on top of Roblox. The Lua script inside Roblox collects game data and streams it to a local Python overlay app via WebSocket (with HTTP fallback).

## Architecture

```
rogue_lite.lua (Roblox) --[WebSocket/HTTP, ~10 updates/sec]--> esp_overlay.exe (Python/Pygame overlay)
```

### Data Flow
1. Lua script collects every ~100ms:
   - Camera CFrame (12 floats), FieldOfView, ViewportSize
   - Per player: world position, health %, rogue character name, distance
2. Sends via WebSocket to ws://127.0.0.1:27015 (or HTTP POST fallback to same port)
3. Python overlay projects 3D→2D, renders transparent always-on-top window

### JSON Payload Format
```json
{
  "camera": {
    "cf": [x, y, z, r00, r01, r02, r10, r11, r12, r20, r21, r22],
    "fov": 70,
    "vp": [1920, 1080]
  },
  "players": [
    {
      "name": "RogueCharacterName",
      "pos": [x, y, z],
      "hp": 0.85,
      "dist": 42.5
    }
  ]
}
```

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| IPC method | WebSocket-first, HTTP POST fallback | Best latency with WS, HTTP fallback covers all executors |
| Language | Python + Pygame | Fast dev, easy overlay rendering, PyInstaller for portable .exe |
| Update interval | ~100ms (10/sec) | Balance of smoothness and low overhead; Python interpolates |
| Projection math | Done in Python | Lua sends raw world coords + camera; Python does matrix math |
| Player names | Rogue in-game name (leaderboard text) | NOT Roblox username/displayname; rogue names are what matters |
| Health | Humanoid.Health / MaxHealth ratio | Standard, reliable |
| Self | Excluded | Don't show own character |
| Menu players | Not shown | No character = no position; leaderboard dots already cover this |
| Proximity fade | 100%→20% at ≤50 studs | Prevents ESP from obscuring PvP at close range |
| Toggle | Always-on (for now) | Can add keybind later |
| Port | 27015 | Unlikely to conflict |
| Auto-launch | From %LOCALAPPDATA%/RogueLiteESP/ | Lua tries shell exec; fallback = manual start |
| Binary | Single .exe via PyInstaller | Zero-config for end user |

## Rogue Name Extraction

Rogue Lineage has its own in-game name system separate from Roblox. The character's rogue name appears on the leaderboard (TextLabel.Text in ScrollingFrame). The existing `labelPlayerMap` in rogue_lite.lua maps `label → {player, dot}`. We build a reverse lookup `Player → label.Text` to resolve each player's rogue name for the ESP.

## File Structure

```
hidroxide/
├── rogue_lite.lua          # Modified: new ESP DATA SENDER section
├── esp_overlay/
│   ├── main.py             # Entry point: WS/HTTP server + Pygame overlay
│   ├── projection.py       # 3D→2D projection math
│   ├── requirements.txt    # pygame, websockets, pywin32
│   └── build.bat           # PyInstaller build script
└── ESP_PLAN.md             # This file
```

## Changelog

| Date | Change |
|------|--------|
| 2026-07-01 | Initial plan created. All design decisions resolved via interview. |
| 2026-07-01 | Changed player name from Roblox DisplayName to Rogue in-game character name. |
