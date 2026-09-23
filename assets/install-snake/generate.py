#!/usr/bin/env python3
"""Build percentage-indexed console frames from Tom Ballard's source video.

Development dependencies: ffmpeg and Pillow. The live ISO only needs the
generated text bundle, base64 and gzip.
"""
import base64
import gzip
import io
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

SOURCE = Path(__file__).with_name("source.mp4")
OUTPUT = Path(__file__).resolve().parents[2] / "configs/airootfs/usr/share/omarchy-iso/install-snake.frames"
SIZE = 56
LARGE_OUTPUT = OUTPUT.with_name("install-snake-large.frames")
LARGE_SIZE = 80


def frames(directory: Path, crop: str) -> list[Path]:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(SOURCE),
         "-vf", f"fps=30,crop={crop}", "-frames:v", "570", str(directory / "%03d.png")],
        check=True,
    )
    return sorted(directory.glob("*.png"))


def progress_from_bar(path: Path) -> int | None:
    image = Image.open(path).convert("RGB")
    blue = [x for x in range(256) if (lambda c: c[2] > 35 and c[2] > c[0] * 1.25 and
             c[1] > c[0] * 1.4)(image.getpixel((x, 4)))]
    return round(max(blue) * 100 / 255) if blue else None


def color(rgb: tuple[int, int, int]) -> int:
    red, green, blue = rgb
    # One solid foreground colour: indexed 113 is the nearest xterm colour
    # to Tokyo Night's default green (#9ece6a). The video's cyan luminance
    # determines the mask, not the output hue. No antialiased border bands.
    if blue >= 85 and green >= 70 and blue > red * 1.25:
        return 113
    if blue > 12 and green > red * 1.3:
        return 233
    return 16


def ansi_frame(path: Path, size: int) -> bytes:
    image = Image.open(path).convert("RGB").resize((size, size), Image.Resampling.BOX)
    # Video compression produces isolated pixels; remove only those without
    # rounding the long, straight logo edges.
    mask = Image.new("L", (size, size))
    mask.putdata([0 if (shade := color(image.getpixel((x, y)))) == 16 else 128 if shade == 233 else 255
                  for y in range(size) for x in range(size)])
    original = mask.load()
    cleaned = mask.copy()
    pixels = cleaned.load()
    for y in range(1, size - 1):
        for x in range(1, size - 1):
            neighbors = [original[x + dx, y + dy]
                         for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dx or dy]
            if original[x, y] != 0 and neighbors.count(original[x, y]) <= 1:
                pixels[x, y] = max(set(neighbors), key=neighbors.count)
            elif original[x, y] == 0 and neighbors.count(255) >= 7:
                pixels[x, y] = 255
    out = io.StringIO()
    for y in range(0, size, 2):
        last = None
        for x in range(size):
            pair = (113 if pixels[x, y] == 255 else 233 if pixels[x, y] == 128 else 16,
                    113 if pixels[x, y + 1] == 255 else 233 if pixels[x, y + 1] == 128 else 16)
            if pair != last:
                out.write(f"\x1b[38;5;{pair[0]};48;5;{pair[1]}m")
                last = pair
            out.write("▀")
        out.write("\x1b[0m\n")
    return out.getvalue().encode()


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        art_dir, bar_dir = root / "art", root / "bar"
        art_dir.mkdir()
        bar_dir.mkdir()
        art = frames(art_dir, "300:300:305:40")
        bars = frames(bar_dir, "256:10:327:423")
        samples = [(p, i) for i, path in enumerate(bars)
                   if (p := progress_from_bar(path)) is not None]
        # The initial dark frame is 0%. The last source frame is the completed
        # Omarchy mark, shown after the dashboard reaches 100%.
        indices = [0] + [min(samples, key=lambda item: (abs(item[0] - p), item[1]))[1]
                         for p in range(1, 101)] + [len(art) - 8]
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        for size, output in ((SIZE, OUTPUT), (LARGE_SIZE, LARGE_OUTPUT)):
            output.write_bytes(b"".join(base64.b64encode(gzip.compress(ansi_frame(art[i], size), mtime=0)) + b"\n"
                                        for i in indices))
            print(f"Wrote {len(indices)} frames to {output} ({output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
