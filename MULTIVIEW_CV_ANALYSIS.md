# ATEM Multiview Ingestion — Computer Vision Analysis

## Goal

Ingest the ATEM switcher multiview via a **single USB capture card**, infer the **layout**, **segment each input**, and expose each cell so downstream logic can treat them as **virtual per-camera streams** (as if each camera had its own capture device).

---

## Current State

| Component | Current approach | Limitation |
|-----------|------------------|------------|
| **Input** | PNG file only (`atem_director.py`) | No live USB; revamp uses `cv2.VideoCapture(0)` elsewhere. |
| **Layout** | Hardcoded presets (atem_mini 2×2, atem_mini_pro 2×4, atem_1me 2×5) + aspect-ratio guess | Fails when resolution/aspect doesn’t match; no borders/labels; 2×5 layout has non-input cells (preview/program). |
| **Segmentation** | Uniform grid: `segment_w = w // grid_cols`, no insets | Ignores bezels, on-screen labels, and variable padding. Crops include UI. |
| **Profile** | None in ATEM; revamp uses manual **crop mapper** (draw rectangles, save JSON) | One-time calibration works but no auto path for new setups. |

---

## Recommended Architecture

### 1. Single pipeline: Capture → Layout → Segment → Per-input analysis

```
[USB Capture Card] → Frame → [Layout Detection] → N cells
                                    ↓
                    [Segment] → { input_id: crop_ndarray }
                                    ↓
                    [Analysis] → person det / activity / quality (per input)
```

- **Capture**: `cv2.VideoCapture(source)` with `source` = device index (e.g. `0`) or file path. Same as revamp.
- **Layout**: Either **saved profile** (JSON with `inputs: { "1": [x,y,w,h], ... }`) or **automatic** grid detection.
- **Segment**: For each frame, crop by layout rects (with optional border inset to avoid labels/bezels).
- **Per-input streams**: API that yields `(input_id, crop)` per frame so revamp, atem_director, or any analyzer see “one feed per camera.”

### 2. Layout detection (determine layout automatically)

Avoid relying only on aspect ratio. Prefer structure-based detection:

| Method | Pros | Cons |
|--------|------|------|
| **Line-based grid** | No training; works for clean borders; fast | Fails if multiview has no visible lines (e.g. seamless grid). |
| **Contour / connected components** | Can find panel-like regions | Needs thresholding; may merge/split cells. |
| **Profile override** | Accurate; same format as revamp mapper | Requires one-time manual mapping. |
| **Hybrid** | Use profile if present; else run line-based (or contour) and optionally save as profile | Best of both. |

**Recommended**: Implement **line-based grid detection** (Canny + Hough or morphological line detection), cluster line positions to get row/column dividers, then build rectangles. Add **optional border inset** (e.g. 2–3% of cell size) to reduce label/bezel in the crop. Support **profile JSON** to override auto when available.

Not recommended for layout: SAM 3 / VLMs — overkill for a regular grid; YOLO for “panel” detection only if you need to support many arbitrary layouts and can collect training data.

### 3. Segmentation quality

- **Inset**: After resolving each cell (from profile or grid), crop with a small inset (e.g. 2–5 px or 2% of min(w,h)) so that thin borders and label bars are mostly excluded.
- **Ordering**: ATEM input order (1..N) should map to a stable cell order (e.g. left-to-right, top-to-bottom). Profile explicitly stores input_id → rect; auto layout should assign consistent indices (e.g. by position).
- **Masking labels**: If labels are always in the same region (e.g. bottom 10% of cell), you could mask that strip before analysis; for “analyze as if direct capture” the inset usually suffices.

### 4. Live capture and frame flow

- **Thread or async**: Run capture in a dedicated thread (like revamp’s `MultiviewAnalyzer`); main loop consumes latest frame and runs layout/segment once per frame (or reuse cached layout).
- **Layout caching**: Run layout detection on first frame (or when profile is missing); then reuse until resolution change or “recalibrate” trigger.
- **Output contract**: A single method that returns “current frame’s segments”: `list of (input_id: int, crop: np.ndarray)`. Downstream code (person detection, activity, quality) only sees per-input crops.

### 5. Downstream analysis (per-input “as if direct capture”)

Once segments are available:

- **Person detection**: Keep YOLO (or move to YOLOv10/v11 nms-free) on each crop — already in atem_director; can run in batch for speed.
- **Activity / quality**: Reuse revamp’s logic (frame diff, Laplacian focus, black/freeze) on each crop.
- **Optional**: If you need “segment the person” inside a cell (e.g. mask), SAM 3 text prompt (“person”) could refine; for “analyze as if direct capture,” cropping is enough.

### 6. Unify with revamp

- **Shared profile format**: Use the same JSON as revamp: `{"width": W, "height": H, "inputs": {"1": [x,y,w,h], "2": [...], ...}}`. Then the existing **crop mapper** (`--mapper`) can produce a profile that both revamp and ATEM use.
- **Single source of truth**: ATEM package can own “multiview capture + layout + segment”; revamp imports and uses it so layout detection and segmentation live in one place.

---

## Anti-patterns to avoid

- **Aspect-ratio-only layout**: Too brittle; different resolutions and overlays break it.
- **No inset**: Cropping to the pixel-perfect grid includes borders and labels and can hurt activity/focus metrics and person detection.
- **Manual NMS**: If you upgrade YOLO, prefer nms-free (e.g. YOLOv10+) for lower latency.
- **Layout detection per frame**: Run once (or on resolution change); cache layout.

---

## Implementation summary

1. **Live capture**: Add a small capture module (USB index or file path) that yields frames.
2. **Layout**:  
   - If profile path given and file exists → load JSON, use those rects.  
   - Else → run line-based grid detection on first frame; optionally save profile for next run.
3. **Segment**: For each frame, for each (input_id, rect), crop with optional inset; return `List[Tuple[int, np.ndarray]]`.
4. **API**: e.g. `MultiviewIngest(source, profile_path=None).get_segments() -> List[Tuple[int, np.ndarray]]` (and optionally `get_frame()` for debugging).
5. **atem_director**: Switch from “load PNG + aspect-ratio layout” to “live capture + layout (profile or auto) + segment”; run person detection on each segment; expose same summary (inputs with people, etc.) and optional save of segments.
6. **revamp**: Optionally import ATEM’s ingest; use `get_segments()` instead of raw profile crop math so layout and segment logic stay in one place.

This gives you a single USB capture, robust layout handling (profile or auto), and per-input segments that you can analyze as if each camera had its own capture card.

---

## Quick usage (this repo)

**One-time layout calibration (optional)**  
From the main Auto-Switcher repo, run the crop mapper so the same profile can be used by both revamp and ATEM:
```bash
python revamp.py --mapper --capture 0 --profile multiview_profiles/default.json
```
Draw rectangles over each multiview cell, press Enter to save.

**Live capture with profile (recommended)**  
```bash
cd ATEM
python atem_director.py --capture 0 --profile ../multiview_profiles/default.json --oneshot
```

**Live capture with auto layout**  
If no profile is provided, the first frame is used to detect a grid via line detection:
```bash
python atem_director.py --capture 0 --oneshot
```

**Single image (legacy)**  
```bash
python atem_director.py path/to/multiview.png
```
