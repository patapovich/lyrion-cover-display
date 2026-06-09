#!/usr/bin/env python3
"""Generate the unified splash assets for Lyrion Cover Display.

One design, two-zone layout that mirrors the now-playing screen: a neutral
vinyl disc sits in the top "cover zone" (the same region a cover fills), and the
"Lyrion" wordmark + status line sit in the bottom info band with a soft wash —
exactly where the now-playing title/artist sit. Everything is rendered with the
same DejaVu fonts the app uses so the boot splash and the live app screens read
as one design.

Outputs (portrait 1200x1600 unless noted):
  assets/splashbg.png        - disc + gradient + wash, NO text (app draws live
                               "Lyrion" + status over this in the band)
  assets/splash.png          - splashbg + baked "Lyrion" + "loading…" (boot)
  assets/splash.raw          - splash.png rotated to the 1600x1200 firmware fb
                               and packed BGRX (what initramfs/splash-fb paint)
  assets/splash_plymouth.png - landscape 1600x1200 (plymouth; kept consistent)

Run:  python3 tools/gen_splash.py
"""
import os
import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(HERE, "assets")

W, H = 1200, 1600
# The app's stacked portrait layout derives the band geometrically (not from the
# info_height fraction): the cover is a 1:1 square zone filling the width at the
# top, and the info band is whatever is left below it. Mirror that exactly so the
# baked splash text lands where the app's live status_screen draws it (else the
# text jumps at the boot->app handoff). See lms_cover_display.py _band_top().
BAND_TOP = min(W, H)               # 1200 = bottom of the 1:1 square cover zone
SS = 3                             # supersample factor for crisp anti-aliasing

# --- palette (cool, neutral; matches the app's white/grey UI) --------------- #
BG_TOP = (20, 21, 28)              # dark slate, top
BG_BOT = (8, 8, 12)               # near-black, bottom
DISC_BASE = (13, 14, 19)           # vinyl body
GROOVE = (30, 32, 41)              # groove rings
RIM = (58, 62, 78)                 # outer rim highlight
LABEL = (92, 100, 119)             # center label — cool slate (neutralized)
LABEL_HI = (120, 129, 150)         # label top sheen
LABEL_EDGE = (130, 139, 162)       # label edge ring
HOLE = (10, 10, 14)               # spindle hole
BRAND = (224, 226, 232)            # "Lyrion" — cool off-white (not pure white)
STATUS = (170, 176, 190)           # status line — calm cool grey

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _gradient_bg():
    """Vertical dark gradient with ordered dither (kills 8-bit banding)."""
    ys = np.linspace(0.0, 1.0, H)[:, None]
    top = np.array(BG_TOP, np.float32)
    bot = np.array(BG_BOT, np.float32)
    grad = top[None, None, :] * (1 - ys[..., None]) + bot[None, None, :] * ys[..., None]
    grad = np.repeat(grad, W, axis=1)                      # (H, W, 3)
    bayer = (np.array([[0, 8, 2, 10], [12, 4, 14, 6],
                       [3, 11, 1, 9], [15, 7, 13, 5]], np.float32) / 16.0 - 0.5)
    d = np.tile(bayer, (H // 4 + 1, W // 4 + 1))[:H, :W]
    grad = np.clip(grad + d[..., None], 0, 255)
    return Image.fromarray(grad.astype(np.uint8), "RGB")


def _disc(img):
    """Draw the neutral vinyl disc centered in the top cover zone."""
    cx, cy = W // 2, BAND_TOP // 2          # center of the cover zone
    R = 326                                 # outer radius
    label_r, hole_r = 116, 9

    big = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)

    def circle(cxp, cyp, r, **kw):
        d.ellipse([(cxp - r) * SS, (cyp - r) * SS, (cxp + r) * SS, (cyp + r) * SS], **kw)

    circle(cx, cy, R, fill=DISC_BASE + (255,))
    # grooves: concentric rings from just outside the label to the rim
    for r in range(label_r + 14, R - 2, 13):
        circle(cx, cy, r, outline=GROOVE + (255,), width=max(1, SS - 1))
    circle(cx, cy, R, outline=RIM + (255,), width=SS)           # outer rim
    # center label with a soft top-to-bottom sheen
    lab = Image.new("RGBA", (label_r * 2 * SS, label_r * 2 * SS), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lab)
    ld.ellipse([0, 0, label_r * 2 * SS - 1, label_r * 2 * SS - 1], fill=LABEL + (255,))
    ys = np.linspace(0, 1, label_r * 2 * SS)[:, None]
    sheen = (np.array(LABEL_HI, np.float32)[None, None] * (1 - ys[..., None])
             + np.array(LABEL, np.float32)[None, None] * ys[..., None])
    arr = np.array(lab)
    mask = arr[..., 3] > 0
    arr[..., :3] = np.where(mask[..., None], sheen.astype(np.uint8), arr[..., :3])
    lab = Image.fromarray(arr, "RGBA")
    big.alpha_composite(lab, ((cx - label_r) * SS, (cy - label_r) * SS))
    d.ellipse([(cx - label_r) * SS, (cy - label_r) * SS,
               (cx + label_r) * SS, (cy + label_r) * SS],
              outline=LABEL_EDGE + (255,), width=SS)
    circle(cx, cy, hole_r, fill=HOLE + (255,))                 # spindle hole

    big = big.resize((W, H), Image.LANCZOS)                    # downsample = AA
    img.alpha_composite(big)


def _wash(img):
    """Soft shadow where the cover zone meets the band — mirrors the app's
    _wash_overlay so the resting band has the same signature as now-playing."""
    shadow_up = int(H * 0.07)
    trans = int(H * 0.14)
    start = BAND_TOP - shadow_up
    maxa = 150
    over = np.zeros((H, W, 4), np.uint8)
    for y in range(start, H):
        t = min(1.0, (y - start) / trans)
        a = int(maxa * t * t * (3 - 2 * t))                    # smoothstep
        over[y, :, 3] = a
    img.alpha_composite(Image.fromarray(over, "RGBA"))


def _base():
    img = _gradient_bg().convert("RGBA")
    _disc(img)
    _wash(img)
    return img


def _band_text(img, status):
    """Bake the brand headline + status into the info band, at the same
    positions the app draws them live (title over subtitle)."""
    draw = ImageDraw.Draw(img)
    base = max(20, H // 28)                                    # = app's base (57)
    # Match the app's fonts exactly: brand = title (bold @ base), status =
    # subtitle (regular @ 0.7*base), so boot and live render the same.
    f_brand = ImageFont.truetype(FONT_BOLD, base)
    f_status = ImageFont.truetype(FONT_REG, int(base * 0.7))
    lines = [(f_brand, "Lyrion", BRAND), (f_status, status, STATUS)]
    pad = max(16, H // 48)                                     # = app's overlay pad
    heights = [draw.textbbox((0, 0), t, font=f)[3] for f, t, _ in lines]
    total = sum(heights) + pad * (len(lines) - 1)
    y = BAND_TOP + (H - BAND_TOP - total) // 2
    for (f, t, col), h in zip(lines, heights):
        w = draw.textbbox((0, 0), t, font=f)[2]
        x = (W - w) // 2
        draw.text((x + 2, y + 2), t, font=f, fill=(0, 0, 0))   # shadow
        draw.text((x, y), t, font=f, fill=col)
        y += h + pad


def main():
    base = _base()

    # splashbg.png — no text; the app draws "Lyrion" + live status over this
    base.convert("RGB").save(os.path.join(ASSETS, "splashbg.png"))

    # splash.png — boot version with baked brand + "loading…"
    boot = base.copy()
    _band_text(boot, "loading…")
    boot_rgb = boot.convert("RGB")
    boot_rgb.save(os.path.join(ASSETS, "splash.png"))

    # splash_plymouth.png — landscape 1600x1200 (rotate; plymouth disabled but
    # kept consistent)
    boot_rgb.rotate(-90, expand=True).save(os.path.join(ASSETS, "splash_plymouth.png"))

    # splash.raw — what initramfs/splash-fb paint: the portrait image rotated to
    # the 1600x1200 firmware framebuffer, packed 32-bit BGRX. The app composes a
    # portrait canvas and rot90(k=3) onto the fb; reproduce that exact mapping so
    # the boot splash and the app's first frame align pixel-for-pixel.
    arr = np.asarray(boot_rgb)                                 # (1600,1200,3) RGB
    fb = np.rot90(arr, 3)                                      # -> (1200,1600,3)
    out = np.empty((1200, 1600, 4), np.uint8)
    out[..., 0] = fb[..., 2]                                   # B
    out[..., 1] = fb[..., 1]                                   # G
    out[..., 2] = fb[..., 0]                                   # R
    out[..., 3] = 255
    out.tofile(os.path.join(ASSETS, "splash.raw"))

    print("wrote splashbg.png, splash.png, splash_plymouth.png, splash.raw")


if __name__ == "__main__":
    main()
