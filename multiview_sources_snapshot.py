#!/usr/bin/env python3
"""
Capture one frame from each available cv2 source and display them in a
multiview grid with index labels. Use this to identify which index is which camera.
"""
import sys
from pathlib import Path

try:
    import cv2
    import numpy as np
except ImportError:
    print("opencv-python and numpy are required: pip install opencv-python numpy")
    sys.exit(1)


def get_available_sources(max_index: int = 20) -> list[tuple[int, int, int]]:
    """Return list of (index, width, height) for sources that open and deliver a frame."""
    available = []
    for i in range(max_index):
        cap = cv2.VideoCapture(i)
        if not cap.isOpened():
            cap.release()
            continue
        ret, frame = cap.read()
        cap.release()
        if ret and frame is not None:
            h, w = frame.shape[:2]
            available.append((i, w, h))
    return available


def capture_frame(index: int) -> np.ndarray | None:
    """Grab a single frame from the given source index."""
    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        return None
    ret, frame = cap.read()
    cap.release()
    return frame if ret and frame is not None else None


def build_multiview_grid(
    frames: list[tuple[int, np.ndarray]],
    cell_height: int = 360,
    label_height: int = 32,
    gap: int = 4,
    font_scale: float = 1.2,
    thickness: int = 2,
) -> np.ndarray:
    """
    Arrange frames in a grid with consistent cell size and index labels.
    frames: list of (index, BGR image).
    """
    if not frames:
        return np.zeros((cell_height + label_height, 320, 3), dtype=np.uint8)

    n = len(frames)
    # Grid that fits n cells (prefer wider than tall)
    cols = int(np.ceil(np.sqrt(n)))
    rows = int(np.ceil(n / cols))

    cell_width = int(cell_height * 16 / 9)  # 16:9
    total_cell = cell_height + label_height
    w = cols * cell_width + (cols - 1) * gap
    h = rows * total_cell + (rows - 1) * gap
    out = np.full((h, w, 3), 40, dtype=np.uint8)

    for k, (index, img) in enumerate(frames):
        row, col = k // cols, k % cols
        # Resize to cell size (excluding label strip)
        resized = cv2.resize(img, (cell_width, cell_height), interpolation=cv2.INTER_AREA)
        y0 = row * (total_cell + gap)
        x0 = col * (cell_width + gap)
        out[y0 : y0 + cell_height, x0 : x0 + cell_width] = resized

        # Label strip below the frame
        label_y0 = y0 + cell_height
        label_rect = out[label_y0 : label_y0 + label_height, x0 : x0 + cell_width]
        label_rect[:] = (60, 60, 60)
        text = f"Index {index}"
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
        tx = (cell_width - tw) // 2
        ty = label_height - (label_height - th) // 2
        cv2.putText(
            out,
            text,
            (x0 + tx, label_y0 + ty),
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            (255, 255, 255),
            thickness,
            cv2.LINE_AA,
        )

    return out


def main() -> None:
    max_index = 20
    save_path: str | None = None
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        try:
            max_index = int(args[0])
        except ValueError:
            pass
    if "--save" in sys.argv:
        idx = sys.argv.index("--save")
        if idx + 1 < len(sys.argv):
            save_path = sys.argv[idx + 1]
        else:
            save_path = "debug/cv2_sources_multiview.png"

    print("Scanning for available cv2 sources...")
    sources = get_available_sources(max_index)
    if not sources:
        print("No video sources found.")
        sys.exit(1)

    print(f"Capturing one frame from each of {len(sources)} sources: {[s[0] for s in sources]}")

    frames: list[tuple[int, np.ndarray]] = []
    for index, w, h in sources:
        frame = capture_frame(index)
        if frame is not None:
            frames.append((index, frame))
        else:
            print(f"  Warning: could not read frame from index {index}")

    if not frames:
        print("No frames captured.")
        sys.exit(1)

    grid = build_multiview_grid(frames)
    title = f"CV2 sources (indices: {', '.join(str(i) for i, _ in frames)})"
    cv2.namedWindow(title, cv2.WINDOW_NORMAL)
    cv2.imshow(title, grid)
    print("Displaying multiview. Press any key to close.")

    if save_path:
        path = Path(save_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if cv2.imwrite(str(path), grid):
            print(f"Saved: {path}")

    cv2.waitKey(0)
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
