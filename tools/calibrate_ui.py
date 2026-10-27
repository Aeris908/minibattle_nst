"""Interactive UI calibration for the Goal 3 scoreboard and roster overlays.

Run from the project root:

    python tools/calibrate_ui.py --video data/temp/sample.mp4 --time 45
    python tools/calibrate_ui.py --capture-digit 3 --video sample.mp4 --time 610

The tool stores:
  * data/templates/scoreboard_anchor.png
  * data/templates/digits/<label>_<n>.png
  * data/calibration/ui.json  (ROIs, relative score positions, templates)

Only OpenCV windows are used.  No shell / PowerShell subprocesses are invoked.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import cv2

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config  # noqa: E402
from core.cv_tracker import preprocess_digit_image, save_anchor_template  # noqa: E402


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(config.ROOT_DIR.resolve()))
    except ValueError:
        return str(path.resolve())


def _read_frame(image_path: str | None, video_path: str | None, seconds: float) -> tuple[object, str]:
    if image_path:
        frame = cv2.imread(image_path)
        if frame is None:
            raise SystemExit(f"Cannot read image: {image_path}")
        return frame, image_path
    if not video_path:
        raise SystemExit("Provide --video or --image")
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise SystemExit(f"Cannot open video: {video_path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(max(0.0, seconds) * fps))
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise SystemExit(f"Cannot read frame at {seconds:.1f}s from {video_path}")
    return frame, video_path


def select_roi(frame, title: str, instruction: str):
    """Select a rectangle on a copy of the frame; supports Esc / cancel."""
    canvas = frame.copy()
    cv2.putText(
        canvas,
        instruction,
        (20, 35),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (0, 255, 255),
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        canvas,
        "Drag a rectangle, then press SPACE or ENTER. Esc cancels.",
        (20, 70),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )
    window = f"Calibrate: {title}"
    try:
        x, y, w, h = cv2.selectROI(window, canvas, showCrosshair=True, fromCenter=False)
    finally:
        cv2.destroyWindow(window)
        cv2.waitKey(1)
    if w <= 0 or h <= 0:
        return None
    return [int(x), int(y), int(w), int(h)]


def select_zoom_roi(frame, region: list[int], title: str, instruction: str, zoom: int = 4):
    """Select a small ROI inside a zoomed crop (used for score digits)."""
    x, y, w, h = [int(v) for v in region]
    pad_x = max(25, w // 6)
    pad_y = max(25, h // 3)
    x1 = max(0, x - pad_x)
    y1 = max(0, y - pad_y)
    x2 = min(frame.shape[1], x + w + pad_x)
    y2 = min(frame.shape[0], y + h + pad_y)
    crop = frame[y1:y2, x1:x2].copy()
    if crop.size == 0:
        return None
    displayed = cv2.resize(
        crop, (crop.shape[1] * zoom, crop.shape[0] * zoom), interpolation=cv2.INTER_NEAREST
    )
    banner = 50
    canvas = cv2.copyMakeBorder(
        displayed, banner, 0, 0, 0, cv2.BORDER_CONSTANT, value=(24, 24, 24)
    )
    cv2.putText(
        canvas,
        instruction,
        (20, 34),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 255, 255),
        2,
        cv2.LINE_AA,
    )
    window = f"Calibrate: {title}"
    try:
        rx, ry, rw, rh = cv2.selectROI(window, canvas, showCrosshair=True, fromCenter=False)
    finally:
        cv2.destroyWindow(window)
        cv2.waitKey(1)
    ry -= banner
    if rw <= 0 or rh <= 0 or ry < 0:
        return None
    display_h = displayed.shape[0]
    if ry + rh > display_h:
        rh = display_h - ry
    if rh <= 0:
        return None
    return [
        int(x1 + round(rx / zoom)),
        int(y1 + round(ry / zoom)),
        int(round(rw / zoom)),
        int(round(rh / zoom)),
    ]


def _load_existing() -> dict:
    if config.CALIBRATION_PATH.exists():
        try:
            return json.loads(config.CALIBRATION_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _save_metadata(metadata: dict) -> None:
    config.CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.CALIBRATION_PATH.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def save_digit_template(frame, bbox: list[int], label: int, metadata: dict) -> Path:
    x, y, w, h = [int(v) for v in bbox]
    crop = frame[y: y + h, x: x + w]
    if crop.size == 0:
        raise ValueError("Empty digit crop")
    processed = preprocess_digit_image(crop)
    config.DIGIT_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{label}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
    path = config.DIGIT_TEMPLATES_DIR / filename
    if not cv2.imwrite(str(path), processed):
        raise OSError(f"Could not save digit template {path}")
    templates = metadata.setdefault("digit_templates", {})
    templates.setdefault(str(label), [])
    relative = _relative(path)
    if relative not in templates[str(label)]:
        templates[str(label)].append(relative)
    return path


def _ask_digit(default: int | None = None) -> int | None:
    prompt = "Digit shown in this box (0-9, blank = skip)"
    if default is not None:
        prompt += f" [default {default}]"
    prompt += ": "
    raw = input(prompt).strip()
    if not raw and default is not None:
        return default
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        print("Not a digit, skipped.")
        return None
    if value < 0 or value > 9:
        print("Digit must be 0-9, skipped.")
        return None
    return value


def _print_digit_status(metadata: dict) -> None:
    templates = metadata.get("digit_templates", {})
    missing = [str(d) for d in range(10) if not templates.get(str(d))]
    print("\nSaved digit templates:")
    for d in range(10):
        count = len(templates.get(str(d), []))
        print(f"  {d}: {count}")
    if missing:
        print(
            "\nWARNING: missing digit templates for " + ", ".join(missing) + ".\n"
            "Capture them from frames where those score digits are visible:\n"
            "  python tools/calibrate_ui.py --capture-digit N --video <video> --time <sec>"
        )
    else:
        print("\nAll digits 0-9 have at least one template.")


def full_calibration(frame, source: str) -> dict:
    print("Calibration order: game field, scoreboard anchor, score digits, roster overlay, lineup screen.")
    game = select_roi(
        frame,
        "game field",
        "1/6 Select the emulator PLAYFIELD (green pitch), without the right roster overlay.",
    )
    if game is None:
        h, w = frame.shape[:2]
        game = [int(w * 0.05), int(h * 0.10), int(w * 0.70), int(h * 0.82)]
        print("Playfield selection cancelled; using central 70% fallback.")
    anchor = select_roi(
        frame,
        "scoreboard anchor",
        "2/6 Select the whole in-game SCOREBOARD incl. both score digits.",
    )
    if anchor is None:
        raise SystemExit("The scoreboard anchor is mandatory. Calibration aborted.")
    print("Now select each score digit in a zoomed view of the scoreboard.")
    left = select_zoom_roi(
        frame,
        anchor,
        "left score digit",
        "3/6 Select the LEFT score digit (inside the scoreboard).",
    )
    right = select_zoom_roi(
        frame,
        anchor,
        "right score digit",
        "4/6 Select the RIGHT score digit (inside the scoreboard).",
    )
    if left is None or right is None:
        raise SystemExit("Both score digit ROIs are mandatory. Calibration aborted.")
    overlay = select_roi(
        frame,
        "roster overlay",
        "5/6 Select the RIGHT-HAND player-list overlay (both teams).",
    )
    if overlay is None:
        print("WARNING: roster overlay ROI not selected; OCR roster extraction will be limited.")
    lineup = select_roi(
        frame,
        "lineup screen",
        "6/6 Select the FORMATION / lineup area on the pre-match screen.",
    )
    if lineup is None:
        print("WARNING: lineup ROI not selected; positions will use voice fallbacks.")

    ax, ay, aw, ah = anchor
    relative_digits = {}
    for side, box in (("left", left), ("right", right)):
        x, y, w, h = box
        relative_digits[side] = [
            round((x - ax) / aw, 6),
            round((y - ay) / ah, 6),
            round(w / aw, 6),
            round(h / ah, 6),
        ]

    metadata = _load_existing()
    metadata.update(
        {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "reference_size": [int(frame.shape[1]), int(frame.shape[0])],
            "game": game,
            "anchor": anchor,
            "digits": relative_digits,
            "overlay": overlay,
            "lineup": lineup,
        }
    )
    anchor_path = save_anchor_template(frame[ay: ay + ah, ax: ax + aw], config.ANCHOR_TEMPLATE_PATH)
    metadata["anchor_template"] = _relative(anchor_path)
    metadata.setdefault("digit_templates", {})

    for side, box in (("left", left), ("right", right)):
        label = _ask_digit()
        if label is not None:
            path = save_digit_template(frame, box, label, metadata)
            print(f"Saved {side} digit template {label}: {path}")
        else:
            print(f"{side} digit template skipped.")

    _save_metadata(metadata)
    print(f"\nCalibration saved: {config.CALIBRATION_PATH}")
    print(f"Anchor template: {anchor_path}")
    _print_digit_status(metadata)
    return metadata


def capture_single_digit(frame, label: int) -> None:
    metadata = _load_existing()
    if not metadata or "anchor" not in metadata:
        raise SystemExit("No base calibration. Run full calibration first.")
    anchor = metadata["anchor"]
    box = select_zoom_roi(
        frame,
        anchor,
        f"digit {label}",
        f"Select a visible '{label}' score digit, then press SPACE/ENTER.",
        zoom=5,
    )
    if box is None:
        raise SystemExit("Selection cancelled.")
    path = save_digit_template(frame, box, label, metadata)
    metadata["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _save_metadata(metadata)
    print(f"Saved digit {label}: {path}")
    _print_digit_status(metadata)


def reset_calibration(confirm: bool) -> None:
    if not confirm:
        raise SystemExit("Refusing to reset without --yes")
    for path in (config.CALIBRATION_PATH, config.ANCHOR_TEMPLATE_PATH):
        if path.exists():
            path.unlink()
            print(f"Deleted {path}")
    print("Digit templates were kept. Run full calibration again.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", help="Video used to grab a calibration frame")
    parser.add_argument("--image", help="Alternative still image source")
    parser.add_argument("--time", type=float, default=30.0, help="Frame timestamp in seconds")
    parser.add_argument("--capture-digit", type=int, choices=range(10), metavar="0-9",
                        help="Add one more template for this digit")
    parser.add_argument("--reset", action="store_true", help="Delete Calibration JSON and anchor")
    parser.add_argument("--yes", action="store_true", help="Confirm reset")
    args = parser.parse_args()

    config.ensure_directories()
    if args.reset:
        reset_calibration(args.yes)
        return
    if not args.video and not args.image:
        parser.error("--video or --image is required")
    frame, source = _read_frame(args.image, args.video, args.time)
    print(f"Loaded frame {frame.shape[1]}x{frame.shape[0]} from {source}")
    if args.capture_digit is not None:
        capture_single_digit(frame, args.capture_digit)
    else:
        full_calibration(frame, source)


if __name__ == "__main__":
    main()
