"""Generate the Open Graph share image for the landing page.

Creates ``src/echo_v2/app/static/og.png`` (1200x630) — the preview image
shown when the landing page link is shared in WhatsApp/social.

Requires Pillow with Raqm (for RTL Hebrew shaping):

    .venv/bin/pip install pillow
    .venv/bin/python scripts/generate_og_image.py

Pillow is a dev-only tool here — the generated PNG is committed and
served as a static file; the app has no Pillow dependency.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, features

WIDTH, HEIGHT = 1200, 630
OUT = Path(__file__).parent.parent / "src" / "echo_v2" / "app" / "static" / "og.png"

# WhatsApp-adjacent brand colors (match the landing hero gradient).
TOP = (7, 94, 84)       # #075e54
MID = (18, 140, 126)    # #128c7e
BOTTOM = (37, 211, 102)  # #25d366

# Arial (regular + bold) has both Hebrew and Latin glyphs on macOS.
# ArialHB lacks Latin glyphs (renders .notdef tofu squares for Latin).
HFONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
BODY_FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"


def _lerp(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def main() -> None:
    assert features.check("raqm"), "Pillow must be built with Raqm for RTL Hebrew"

    img = Image.new("RGB", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(img)

    # Vertical gradient: dark green -> teal -> whatsapp green.
    for y in range(HEIGHT):
        t = y / HEIGHT
        color = _lerp(TOP, MID, t / 0.6) if t < 0.6 else _lerp(MID, BOTTOM, (t - 0.6) / 0.4)
        draw.line([(0, y), (WIDTH, y)], fill=color)

    logo_font = ImageFont.truetype(HFONT, 92)
    title_font = ImageFont.truetype(HFONT, 64)
    sub_font = ImageFont.truetype(BODY_FONT, 36)

    # Logo.
    draw.text((WIDTH / 2, 150), "ECHO", font=logo_font, fill="white", anchor="mm")

    # Headline (RTL via Raqm).
    draw.text(
        (WIDTH / 2, 300),
        "איזה לקוח מחכה לך עכשיו בוואטסאפ?",
        font=title_font,
        fill="white",
        anchor="mm",
        direction="rtl",
        language="he",
    )

    # Subtitle.
    draw.text(
        (WIDTH / 2, 390),
        "Echo מזהה את השיחות שמחכות לך — לפני שהן עולות לך בעסקה",
        font=sub_font,
        fill=(235, 250, 240),
        anchor="mm",
        direction="rtl",
        language="he",
    )

    # CTA pill.
    pill_w, pill_h = 380, 74
    pill_x, pill_y = (WIDTH - pill_w) / 2, 470
    draw.rounded_rectangle(
        [pill_x, pill_y, pill_x + pill_w, pill_y + pill_h],
        radius=37,
        fill="white",
    )
    draw.text(
        (WIDTH / 2, pill_y + pill_h / 2),
        "רשימת המתנה לגישה מוקדמת",
        font=ImageFont.truetype(BODY_FONT, 30),
        fill=TOP,
        anchor="mm",
        direction="rtl",
        language="he",
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, "PNG", optimize=True)
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
