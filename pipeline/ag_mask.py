#!/usr/bin/env python3
"""AnomalyGuessr masked inpainting helper (ticket #1157, spike #1154).

Generates one scene edit via the Gemini direct Interactions API with an
explicit edit mask: the model is allowed to change only the masked region,
so the answer position is known BEFORE generation (mask bbox -> normalized
x/y/r) and the whole "pixel-diff hotspot on a re-encode artifact" failure
class of the unmasked pipeline disappears.

Mask format (spike #1154): grayscale PNG, same pixel dims as the source,
white = edit region, feathered edges help blending. Passed as a THIRD input
image (after the text prompt + source), NOT as image_config.mask (HTTP 400).

Pipeline view::

    ag_mask.py --source S.jpg --center 0.7,0.6 --radius 0.09 \\
               --prompt "Edit this historical photograph: ..." \\
               --out-dir /tmp/ag-gen/<id>/ --env /home/exedev/fuchs/.env

writes (all in --out-dir, basename = --out flag or "<stem>-masked"):

    <name>.jpg            edited image (JPEG re-encode from the API)
    <name>-mask.png       the mask that was sent (grayscale PNG)
    <name>-answer.json    {"x": .., "y": .., "r": ..} normalized answer

The answer is derived from the mask's geometry, NEVER from a pixel diff:
for a generated single-ellipse mask it is the ellipse center + a radius
from the bbox extent (capped into the game's accepted range). With --mask
the bbox of the mask's largest white region is used the same way.

CLI::

    ag_mask.py --source S.jpg --center CX,CY --radius R [--radius-y RY] \\
        --prompt "P" [--out-dir DIR] [--out NAME] [--mask MASK.png] \\
        [--size 1K|2K] [--model gemini-3.1-flash-image] [--env .env] \\
        [--max-px W,H] [--timeout N] [--dry-run]

Notes:

- ``--center/--radius`` are normalized (0..1) source-image coordinates; the
  mask is a white ellipse with that center and radii at the source's native
  pixel dims, feathered over ~8 % of the image width (FEATHER_FRAC).
- ``--mask`` reuses a saved mask PNG instead of generating one.
- ``--max-px`` downscales very large sources (e.g. 3936x2624) so the API's
  1K output keeps enough pixels per object; the answer is in the coordinate
  space of the image the game shows.
- ``--dry-run`` only builds + saves the mask and the answer file (no API
  call); it prints the answer so mask tuning is free.
- Exit code 0 = success (image + answer written); 1 = API/HTTP/argument
  error with a message on stderr.
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_MODEL = "gemini-3.1-flash-image"
DEFAULT_SIZE = "1K"
API_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
API_REVISION = "2026-05-20"
# Mask feathering as a fraction of the image width (spike #1154: feathered
# edges help blending).
FEATHER_FRAC = 0.3


def load_env(env_path) -> None:
    """Minimal .env loader (KEY=VALUE lines) for keys not already set."""
    env_path = Path(env_path)
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and v and os.environ.get(k) is None:
            os.environ[k] = v


def image_dims(path: Path) -> tuple:
    r = subprocess.run(
        ["identify", "-format", "%w %h", str(path)],
        capture_output=True, text=True, check=True,
    )
    w, h = r.stdout.split()
    return int(w), int(h)


def scale_box(center: tuple, radii: tuple, w: int, h: int) -> dict:
    """Normalized center/radii -> pixel ellipse params (clamped to frame).

    Returns {"cx", "cy", "rx", "ry"} in source pixels. The ellipse center is
    clamped so the whole ellipse stays inside the frame: an ellipse hanging
    off the edge would move the visible edit (and thus the answer the game
    needs) to the border and could clip the object (the foreground-edge
    failure class, #1114).
    """
    cx, cy = float(center[0]) * w, float(center[1]) * h
    rx = float(radii[0]) * w
    ry = float(radii[1]) * h if len(radii) > 1 else rx
    cx = min(max(rx, cx), w - rx)
    cy = min(max(ry, cy), h - ry)
    if rx <= 0 or ry <= 0 or rx > w / 2 or ry > h / 2:
        raise ValueError(
            f"mask radii too large for {w}x{h}: rx={rx:.0f} ry={ry:.0f} "
            "(each must be < half the frame)")
    return {"cx": round(cx, 3), "cy": round(cy, 3),
            "rx": round(rx, 3), "ry": round(ry, 3)}


def build_mask(source: Path, center: tuple, radii: tuple, out: Path) -> dict:
    """Feathered white ellipse mask at the source's native dims.

    The ellipse is drawn in black on white and gaussian-blurred, which
    feathers BOTH edges symmetrically (a blurred white-on-black keeps a hard
    inverted edge on one side). The blur radius is ~8 % of the image width
    (FEATHER_FRAC); half the blur spreads into the edit region, so the
    effective edit region is the ellipse shrunk by half the blur.
    """
    w, h = image_dims(source)
    p = scale_box(center, radii, w, h)
    # Feather proportional to the ellipse's MINOR radius, not the image
    # width: a blur sigma fixed at ~8 % of image width would exceed a thin
    # ellipse's vertical radius and erase the whole mask (measured: sigma
    # 64 on a ry=24 ellipse leaves nothing above the 50 % threshold).
    # ~30 % of the minor radius gives a soft edge while the core survives.
    blur = max(1, int(min(p["rx"], p["ry"]) * 0.3))
    subprocess.run(
        ["convert", "-size", f"{w}x{h}", "xc:black",
         "-fill", "white",
         "-draw", f"ellipse {p['cx']:.1f},{p['cy']:.1f} "
                  f"{p['rx']:.1f},{p['ry']:.1f} 0,360",
         "-blur", f"0x{blur}", str(out)],
        check=True, capture_output=True,
    )
    return p


def mask_bbox(mask_path: Path) -> dict:
    """Bounding box of the mask's white region (threshold + trim).

    Returns pixel {"cx", "cy", "rx", "ry"} of the bbox, like scale_box's
    shape so callers can reuse mask_answer.
    """
    r = subprocess.run(
        ["convert", str(mask_path), "-threshold", "50%", "-trim",
         "-format", "%wx%h%O", "info:-"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    m = re.match(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$", r)
    if not m:
        raise ValueError(f"could not parse mask bbox: {r!r}")
    bw, bh, bx, by = map(int, m.groups())
    return {"cx": bx + bw / 2, "cy": by + bh / 2, "rx": bw / 2, "ry": bh / 2}


def mask_answer(p: dict, w: int, h: int) -> dict:
    """Normalized answer for a single-ellipse mask.

    x/y = ellipse center; r = the smaller half-extent normed by the larger
    frame side (the visible object fills roughly the core of the ellipse,
    and the smaller axis is the conservative hit radius), capped into the
    game's accepted range (0, 0.5].
    """
    r = min(p["rx"], p["ry"]) / max(w, h)
    r = max(0.02, min(r, 0.15))
    return {"x": round(p["cx"] / w, 4), "y": round(p["cy"] / h, 4),
            "r": round(r, 4)}


def b64_of(path: Path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode()


def call_interactions(api_key: str, model: str, prompt: str,
                      source_b64: str, source_mime: str,
                      mask_b64: str, size: str, timeout: int) -> bytes:
    """POST one masked edit to the Gemini Interactions API; return bytes.

    Raises RuntimeError on HTTP/API errors with the server's error text.
    """
    body = {
        "model": model,
        "input": [
            {"type": "text", "text": prompt},
            {"type": "image", "data": source_b64, "mime_type": source_mime},
            {"type": "image", "data": mask_b64, "mime_type": "image/png"},
        ],
        "generation_config": {"image_config": {"image_size": size}},
        "stream": False,
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
            "Api-Revision": API_REVISION,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"interactions API HTTP {e.code}: "
                           f"{e.read()[:1000]!r}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"interactions API request failed: {e}") from e
    if data.get("error"):
        raise RuntimeError("interactions API error: "
                           + json.dumps(data["error"])[:1000])
    # Collect image blocks from steps (recursive, mirrors scratch poc.mjs).
    images = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "image" and node.get("data"):
                images.append(node)
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    if not images:
        raise RuntimeError("no image block in response; step types="
                           + json.dumps([s.get("type") for s in
                                         data.get("steps", [])])[:500])
    return base64.b64decode(images[0]["data"])


def maybe_downscale(source: Path, max_px: tuple, out_dir: Path) -> Path:
    """Downscale a source above ``max_px`` into ``out_dir`` (JPEG).

    Returns the original path when no downscale is needed. The pipeline must
    edit exactly the file the game shows (run-guard 1, #1122), so the
    edited result and the original always share the same pixel space.
    """
    w, h = image_dims(source)
    if w <= max_px[0] and h <= max_px[1]:
        return source
    out = out_dir / "source-downscaled.jpg"
    subprocess.run(
        ["convert", str(source), "-resize", f"{max_px[0]}x{max_px[1]}",
         "-quality", "92", str(out)],
        check=True, capture_output=True,
    )
    return out


def run(args: argparse.Namespace) -> int:
    if not args.source:
        raise ValueError("--source is required")
    if not args.prompt:
        raise ValueError("--prompt is required (the inpainting instruction)")
    if args.center is None and not args.mask:
        raise ValueError("--center CX,CY (normalized) is required when "
                         "--mask is not given")
    src = Path(args.source)
    if not src.exists():
        raise ValueError(f"source not found: {src}")
    out_dir = Path(args.out_dir or ".")
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = args.out or src.stem + "-masked"
    mask_path = Path(args.mask) if args.mask else None
    if mask_path is not None and not mask_path.exists():
        raise ValueError(f"--mask file not found: {mask_path}")

    # The image the game shows: downscale giant sources so 1K output keeps
    # enough pixels per object. The mask (and answer) are in ITS space.
    max_px = args.max_px
    if max_px:
        max_px = tuple(int(v) for v in max_px.split(","))
        if len(max_px) != 2:
            raise ValueError("--max-px must be W,H")
        src = maybe_downscale(src, max_px, out_dir)
    w, h = image_dims(src)

    if mask_path is None:
        parts = [float(v) for v in args.center.split(",")]
        if len(parts) != 2:
            raise ValueError("--center must be CX,CY (two normalized 0..1)")
        radii = [args.radius]
        if args.radius_y is not None:
            radii.append(args.radius_y)
        if not (0 <= parts[0] <= 1 and 0 <= parts[1] <= 1):
            raise ValueError("--center values must be in 0..1")
        if any(v <= 0 or v > 0.5 for v in radii):
            raise ValueError("--radius values must be in (0, 0.5]")
        mask_path = out_dir / f"{stem}-mask.png"
        p = build_mask(src, tuple(parts), tuple(radii), mask_path)
    else:
        p = mask_bbox(mask_path)

    answer = mask_answer(p, w, h)
    answer_path = out_dir / f"{stem}-answer.json"
    answer_path.write_text(json.dumps(answer, indent=2) + "\n")

    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True,
                          "mask": str(mask_path), "answer": answer,
                          "dims": [w, h]}))
        return 0

    if os.environ.get("GEMINI_API_KEY") is None and args.env:
        load_env(args.env)
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set (pass --env with the fuchs "
                         ".env or export it)")
    mime = "image/png" if src.suffix.lower() == ".png" else "image/jpeg"
    try:
        img_bytes = call_interactions(
            api_key, args.model, args.prompt,
            b64_of(src), mime, b64_of(mask_path), args.size,
            timeout=args.timeout,
        )
    except RuntimeError as e:
        raise
    edited = out_dir / f"{stem}.jpg"
    edited.write_bytes(img_bytes)
    print(json.dumps({"ok": True, "edited": str(edited),
                      "answer": answer, "mask": str(mask_path),
                      "bytes": len(img_bytes)}))
    return 0


def parse_args(argv) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="ag_mask.py")
    ap.add_argument("--source", help="source image file (what the game shows)")
    ap.add_argument("--center", help="normalized mask center CX,CY (0..1)")
    ap.add_argument("--radius", type=float, default=0.1,
                    help="normalized mask radius (fraction of width)")
    ap.add_argument("--radius-y", type=float, default=None,
                    help="optional separate vertical radius")
    ap.add_argument("--prompt", help="inpainting instruction (text input 1)")
    ap.add_argument("--mask", help="reuse a saved mask PNG instead of "
                                   "generating one")
    ap.add_argument("--out-dir", default=None,
                    help="output directory (default: cwd)")
    ap.add_argument("--out", default=None,
                    help="output basename (default: <source-stem>-masked)")
    ap.add_argument("--size", default=DEFAULT_SIZE, choices=["1K", "2K"],
                    help="API output size (default 1K)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--env", default=None,
                    help="path to .env for GEMINI_API_KEY")
    ap.add_argument("--max-px", default=None,
                    help="W,H; downscale sources above this before masking")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--dry-run", action="store_true",
                    help="build mask + answer only, no API call")
    return ap.parse_args(argv)


def main(argv) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except (subprocess.CalledProcessError, ValueError, RuntimeError,
            OSError) as e:
        print(f"ag_mask: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))