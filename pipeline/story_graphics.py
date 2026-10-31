"""Deterministic explanatory graphics, never synthetic documentary photographs."""

from functools import lru_cache

from config import settings


@lru_cache(maxsize=128)
def _font(size):
    from PIL import ImageFont
    for name in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def _text(draw, text, box, size, color):
    """Fit by measured pixels, including long words, without clipping or ellipsis."""
    x, y, right, bottom = box
    size = max(4, size)
    while size >= 4:
        font = _font(size)
        lines, current = [], ""
        for word in text.split():
            trial = f"{current} {word}".strip()
            if draw.textlength(trial, font=font) <= right-x:
                current = trial
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        step = round(size*1.3)
        if (len(lines)*step <= bottom-y
                and all(draw.textlength(line, font=font) <= right-x for line in lines)):
            for i, line in enumerate(lines):
                draw.text(((x+right)/2, y+(bottom-y-len(lines)*step)/2+i*step),
                          line, font=font, fill=color, anchor="mt")
            return
        size -= 1
    raise ValueError("Diagram text cannot fit; shorten the visual labels")


def render_graphic(beat, directory):
    """Every generated graphic is a timed motion clip, including fallbacks."""
    from pipeline.motion_graphics import render_animation
    if beat["visual"]["kind"] == "animation":
        return render_animation(beat, directory)
    from pipeline.editorial_motion import render_editorial
    return render_editorial(beat, directory)
