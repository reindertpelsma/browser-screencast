#!/usr/bin/env python3
"""Fullscreen X11 animation fixture for local capture tests."""
import math
import os
import random
import time

import numpy as np
from Xlib import X, display


def _rgb(r, g, b):
    return (int(r) << 16) | (int(g) << 8) | int(b)


def _put_noise_frame(win, gc, depth, rng, width, height, tile):
    # X11 PutImage requests are capped at 64k 4-byte units. Tile the frame so
    # high-entropy content stays fast without exceeding protocol limits.
    frame = rng.integers(0, 256, size=(height, width, 4), dtype=np.uint8)
    frame[:, :, 3] = 0
    for y in range(0, height, tile):
        y2 = min(y + tile, height)
        for x in range(0, width, tile):
            x2 = min(x + tile, width)
            chunk = np.ascontiguousarray(frame[y:y2, x:x2])
            win.put_image(gc, x, y, x2 - x, y2 - y, X.ZPixmap, depth, 0, chunk.tobytes())


def main():
    d = display.Display(os.environ.get("DISPLAY"))
    screen = d.screen()
    w = int(os.environ.get("ANIM_W", screen.width_in_pixels or 1280))
    h = int(os.environ.get("ANIM_H", screen.height_in_pixels or 720))
    win = screen.root.create_window(
        0, 0, w, h, 0,
        screen.root_depth,
        X.InputOutput,
        X.CopyFromParent,
        background_pixel=screen.black_pixel,
        event_mask=X.ExposureMask,
        override_redirect=True,
    )
    win.map()
    win.configure(stack_mode=X.Above)
    gc = win.create_gc()
    d.sync()

    random.seed(42)
    blobs = []
    for i in range(80):
        blobs.append([
            random.randrange(0, max(1, w - 80)),
            random.randrange(0, max(1, h - 80)),
            random.choice([-1, 1]) * random.uniform(3, 10),
            random.choice([-1, 1]) * random.uniform(2, 8),
            random.randrange(24, 90),
            random.randrange(256),
        ])

    frame = 0
    target = 1.0 / float(os.environ.get("ANIM_FPS", "60"))
    mode = os.environ.get("STRESS_MODE", "balls")
    cell = int(os.environ.get("SAT_CELL", "24"))
    noise_tile = int(os.environ.get("NOISE_TILE", "128"))
    noise_seed = int(os.environ.get("NOISE_SEED", "20260505"))
    noise_rng = np.random.default_rng(noise_seed)
    while True:
        t0 = time.monotonic()
        if mode == "noise":
            # Full-frame deterministic noise. This is intentionally hostile to
            # inter-frame compression and should force the congestion loop to
            # throttle before TCP/SSH backpressure shows up.
            _put_noise_frame(win, gc, screen.root_depth, noise_rng, w, h, noise_tile)
            d.flush()
            frame += 1
            elapsed = time.monotonic() - t0
            if elapsed < target:
                time.sleep(target - elapsed)
            continue

        if mode == "saturation":
            # Large, deterministic random-color cells. This is deliberately
            # hard for inter-frame compression and is used to make the 2 Mbps
            # proxy test exercise the server rate controller.
            rnd = random.Random(frame)
            for y in range(0, h, cell):
                for x in range(0, w, cell):
                    gc.change(foreground=_rgb(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
                    win.fill_rectangle(gc, x, y, cell, cell)
            d.flush()
            frame += 1
            elapsed = time.monotonic() - t0
            if elapsed < target:
                time.sleep(target - elapsed)
            continue

        phase = frame * 0.035
        bg = _rgb(
            30 + 25 * (math.sin(phase) + 1),
            25 + 20 * (math.sin(phase * 0.7 + 2) + 1),
            45 + 35 * (math.sin(phase * 1.3 + 4) + 1),
        )
        gc.change(foreground=bg)
        win.fill_rectangle(gc, 0, 0, w, h)

        for b in blobs:
            x, y, dx, dy, size, hue = b
            x += dx
            y += dy
            if x < 0 or x + size >= w:
                dx = -dx
                x = max(0, min(w - size - 1, x))
            if y < 0 or y + size >= h:
                dy = -dy
                y = max(0, min(h - size - 1, y))
            b[0], b[1], b[2], b[3] = x, y, dx, dy
            r = (hue + frame * 3) % 256
            g = (hue * 3 + frame * 5) % 256
            blue = (hue * 5 + frame * 7) % 256
            gc.change(foreground=_rgb(r, g, blue))
            win.fill_arc(gc, int(x), int(y), size, size, 0, 360 * 64)

        d.flush()
        frame += 1
        elapsed = time.monotonic() - t0
        if elapsed < target:
            time.sleep(target - elapsed)


if __name__ == "__main__":
    main()
