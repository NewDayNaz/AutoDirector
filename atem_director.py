#!/usr/bin/env python3
"""
ATEM Multiview Person Detection Script

Ingests the ATEM switcher multiview (from USB capture card or file), determines
the layout, segments each input, and analyzes each as if it were a direct
per-camera capture.

Modes:
- Live capture: --capture 0 (or path to video file) uses MultiviewIngest for
  layout (profile or auto line-based grid) and per-input segments.
- Single image: pass path/to/multiview.png for one-shot file-based processing.

Requirements:
pip install opencv-python pillow ultralytics numpy

Usage:
  python atem_director.py path/to/multiview.png
  python atem_director.py --capture 0 [--profile multiview_profiles/default.json] [--oneshot]
"""

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO

try:
    from multiview_ingest import MultiviewIngest
except ImportError:
    MultiviewIngest = None

class ATEMMultiviewDetector:
    def __init__(self, model_path=None):
        """
        Initialize the detector with a YOLO model for fast inference.
        
        Args:
            model_path: Path to custom YOLO model, defaults to YOLOv8n for speed
        """
        # Load YOLOv8 nano model for fastest inference
        self.model = YOLO(model_path or 'yolov8n.pt')
        
        # ATEM multiview layouts - adjust based on your switcher model
        self.multiview_layouts = {
            'atem_mini': {
                'grid': (2, 2),  # 2x2 grid
                'inputs': 4,
                'layout': [(0, 0), (0, 1), (1, 0), (1, 1)]
            },
            'atem_mini_pro': {
                'grid': (2, 4),  # 2x4 grid
                'inputs': 8,
                'layout': [(0, 0), (0, 1), (0, 2), (0, 3),
                          (1, 0), (1, 1), (1, 2), (1, 3)]
            },
            'atem_1me': {
                'grid': (2, 5),  # 2x5 grid with preview/program
                'inputs': 8,
                'layout': [(0, 1), (0, 2), (0, 3), (0, 4),
                          (1, 1), (1, 2), (1, 3), (1, 4)]
            },
            # 4x4 multiview (e.g. Constellation): 8 camera cells in left two columns;
            # columns 2–3 are Preview/Program, SuperSource/Audio, Media, REC/ON AIR
            'atem_4x4': {
                'grid': (4, 4),
                'inputs': 8,
                'layout': [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (2, 1), (3, 0), (3, 1)]
            },
        }
        
    def detect_layout(self, image):
        """
        Auto-detect ATEM multiview layout based on image aspect ratio.
        Used only for file-based processing when no profile/ingest is used.
        
        Args:
            image: numpy array (H, W, C) or PIL Image
            
        Returns:
            dict: Layout configuration
        """
        if isinstance(image, np.ndarray):
            h, w = image.shape[:2]
        else:
            w, h = image.size  # PIL: (width, height)
            
        aspect_ratio = w / h
        
        # Detect layout based on aspect ratio (and size for 4x4 vs 2x2)
        if 1.8 < aspect_ratio < 2.2:  # ~2:1 for 2x4
            return self.multiview_layouts['atem_mini_pro']
        elif 2.4 < aspect_ratio < 2.6:  # ~2.5:1 for 2x5
            return self.multiview_layouts['atem_1me']
        elif 1.6 <= aspect_ratio <= 1.95 and w >= 1400:
            # 16:9-ish and wide: likely 4x4 multiview (e.g. Constellation)
            return self.multiview_layouts['atem_4x4']
        else:  # Default to 2x2 (e.g. small or square-ish)
            return self.multiview_layouts['atem_mini']
    
    def segment_multiview(self, image_path, layout_type='auto'):
        """
        Segment the multiview image into individual camera inputs.
        
        Args:
            image_path: Path to multiview PNG image
            layout_type: 'auto', 'atem_mini', 'atem_mini_pro', or 'atem_1me'
            
        Returns:
            list: List of (input_number, cropped_image) tuples
        """
        # Load image
        image = cv2.imread(str(image_path))
        if image is None:
            raise ValueError(f"Could not load image: {image_path}")
            
        h, w = image.shape[:2]
        
        # Get layout configuration
        if layout_type == 'auto':
            layout = self.detect_layout(image)
        else:
            layout = self.multiview_layouts.get(layout_type)
            
        if not layout:
            raise ValueError(f"Unknown layout type: {layout_type}")
            
        grid_rows, grid_cols = layout['grid']
        
        # Calculate segment dimensions
        segment_w = w // grid_cols
        segment_h = h // grid_rows
        
        segments = []
        
        for i, (row, col) in enumerate(layout['layout']):
            if i >= layout['inputs']:
                break
                
            # Calculate crop coordinates
            x1 = col * segment_w
            y1 = row * segment_h
            x2 = x1 + segment_w
            y2 = y1 + segment_h
            
            # Extract segment
            segment = image[y1:y2, x1:x2]
            
            # Input numbering starts from 1
            input_num = i + 1
            segments.append((input_num, segment))
            
        return segments
    
    def detect_person_in_segment(self, segment):
        """
        Detect if a person is present in the image segment.
        
        Args:
            segment: OpenCV image array
            
        Returns:
            tuple: (has_person: bool, confidence: float, bbox_count: int)
        """
        # Run YOLO inference
        results = self.model(segment, verbose=False)
        
        person_detections = []
        
        for result in results:
            boxes = result.boxes
            if boxes is not None:
                # Filter for person class (class 0 in COCO dataset)
                person_mask = boxes.cls == 0
                if person_mask.any():
                    person_boxes = boxes[person_mask]
                    confidences = person_boxes.conf.cpu().numpy()
                    person_detections.extend(confidences)
        
        if person_detections:
            max_confidence = max(person_detections)
            return True, max_confidence, len(person_detections)
        else:
            return False, 0.0, 0
    
    def save_segments(self, segments, output_dir, image_name):
        """
        Save segmented camera inputs to individual files.
        
        Args:
            segments: List of (input_number, cropped_image) tuples
            output_dir: Directory to save segmented images
            image_name: Base name of the original multiview image
            
        Returns:
            dict: Mapping of input numbers to saved file paths
        """
        output_path = Path(output_dir)
        output_path.mkdir(exist_ok=True)
        
        saved_files = {}
        base_name = Path(image_name).stem
        
        for input_num, segment in segments:
            filename = f"{base_name}_input_{input_num:02d}.png"
            file_path = output_path / filename
            
            # Save the segment
            success = cv2.imwrite(str(file_path), segment)
            if success:
                saved_files[input_num] = str(file_path)
                print(f"Saved Camera Input {input_num:2d}: {file_path}")
            else:
                print(f"Failed to save Camera Input {input_num:2d}: {file_path}")
        
        return saved_files

    def process_multiview(self, image_path, layout_type='auto', confidence_threshold=0.5, save_segments=True, output_dir='segments'):
        """
        Process the entire multiview image and return camera inputs with people.
        
        Args:
            image_path: Path to multiview PNG image
            layout_type: ATEM layout type
            confidence_threshold: Minimum confidence for person detection
            save_segments: Whether to save individual segment images
            output_dir: Directory to save segmented images
            
        Returns:
            dict: Results with camera inputs that have people detected
        """
        print(f"Processing multiview image: {image_path}")
        print(f"Layout type: {layout_type}")
        print(f"Confidence threshold: {confidence_threshold}")
        if save_segments:
            print(f"Output directory: {output_dir}")
        print("-" * 50)
        
        # Segment the multiview
        segments = self.segment_multiview(image_path, layout_type)
        
        results = {
            'inputs_with_people': [],
            'all_results': [],
            'summary': {},
            'saved_files': {}
        }
        
        # Save segments to files if requested
        if save_segments:
            results['saved_files'] = self.save_segments(segments, output_dir, Path(image_path).name)
            print("-" * 50)
        
        # Process each segment
        for input_num, segment in segments:
            has_person, confidence, bbox_count = self.detect_person_in_segment(segment)
            
            result = {
                'input': input_num,
                'has_person': has_person and confidence >= confidence_threshold,
                'confidence': confidence,
                'person_count': bbox_count,
                'segment_shape': segment.shape,
                'saved_file': results['saved_files'].get(input_num) if save_segments else None
            }
            
            results['all_results'].append(result)
            
            # Print detailed results
            status = "✓ PERSON DETECTED" if result['has_person'] else "✗ No person"
            print(f"Camera Input {input_num:2d}: {status} "
                  f"(conf: {confidence:.3f}, count: {bbox_count})")
            
            if result['has_person']:
                results['inputs_with_people'].append(input_num)
        
        # Generate summary
        total_inputs = len(segments)
        results['summary'] = {
            'total_inputs': total_inputs,
            'inputs_with_people': len(results['inputs_with_people']),
            'inputs_without_people': total_inputs - len(results['inputs_with_people']),
            'detection_rate': len(results['inputs_with_people']) / total_inputs if total_inputs > 0 else 0
        }
        print("-" * 50)
        print("SUMMARY:")
        print(f"Total camera inputs: {total_inputs}")
        print(f"Inputs with people: {len(results['inputs_with_people'])}")
        print(f"Camera inputs with people: {results['inputs_with_people']}")
        if save_segments:
            print(f"Segmented images saved to: {output_dir}/")
        return results

    def process_segments(
        self,
        segments: list,
        confidence_threshold: float = 0.5,
        save_segments: bool = False,
        output_dir: str = 'segments',
        base_name: str = 'frame',
    ):
        """
        Process a list of (input_id, crop) segments (e.g. from MultiviewIngest.get_segments()).
        Returns same result structure as process_multiview for compatibility.
        """
        results = {
            'inputs_with_people': [],
            'all_results': [],
            'summary': {},
            'saved_files': {}
        }
        if save_segments:
            results['saved_files'] = self.save_segments(segments, output_dir, base_name)
        for input_num, segment in segments:
            has_person, confidence, bbox_count = self.detect_person_in_segment(segment)
            result = {
                'input': input_num,
                'has_person': has_person and confidence >= confidence_threshold,
                'confidence': confidence,
                'person_count': bbox_count,
                'segment_shape': segment.shape,
                'saved_file': results['saved_files'].get(input_num) if save_segments else None
            }
            results['all_results'].append(result)
            if result['has_person']:
                results['inputs_with_people'].append(input_num)
        n = len(segments)
        results['summary'] = {
            'total_inputs': n,
            'inputs_with_people': len(results['inputs_with_people']),
            'inputs_without_people': n - len(results['inputs_with_people']),
            'detection_rate': len(results['inputs_with_people']) / n if n else 0,
        }
        return results


def _run_capture_mode(
    source,
    profile_path=None,
    confidence=0.5,
    save_segments=False,
    output_dir='segments',
    oneshot=False,
    model_path=None,
):
    """Run person detection on live capture using MultiviewIngest for layout and segments."""
    if MultiviewIngest is None:
        print("Error: multiview_ingest not available. Install from ATEM directory or add to path.")
        return None
    detector = ATEMMultiviewDetector(model_path)
    try:
        source_int = int(source) if str(source).strip().isdigit() else source
    except (ValueError, TypeError):
        source_int = source
    ingest = MultiviewIngest(
        source=source_int,
        profile_path=profile_path,
        inset_ratio=0.02,
    )
    last_with_people = []
    try:
        if oneshot:
            segments = ingest.get_segments()
            if not segments:
                print("No segments (layout not detected or no frame).")
                return []
            results = detector.process_segments(
                segments,
                confidence_threshold=confidence,
                save_segments=save_segments,
                output_dir=output_dir,
                base_name="frame",
            )
            for r in results['all_results']:
                status = "✓ PERSON" if r['has_person'] else "✗ No person"
                print(f"Input {r['input']:2d}: {status} (conf: {r['confidence']:.3f})")
            print(f"Inputs with people: {results['inputs_with_people']}")
            return results['inputs_with_people']
        # Continuous loop
        print("Live capture mode. Reading frames (Ctrl+C to stop)...")
        frame_count = 0
        last_with_people = []
        while True:
            segments = ingest.get_segments()
            if not segments:
                time.sleep(0.05)
                continue
            frame_count += 1
            results = detector.process_segments(segments, confidence_threshold=confidence)
            last_with_people = results['inputs_with_people']
            print(f"Frame {frame_count}: inputs with people = {last_with_people}")
            time.sleep(0.033)  # ~30 fps cap for readability
    except KeyboardInterrupt:
        print("\nStopped.")
        return last_with_people
    finally:
        ingest.release()
    return last_with_people


def main():
    parser = argparse.ArgumentParser(
        description='Detect people in ATEM multiview (image file or live USB capture)'
    )
    parser.add_argument(
        'image_path',
        nargs='?',
        default=None,
        help='Path to multiview image (optional when using --capture)',
    )
    parser.add_argument(
        '--capture',
        metavar='SOURCE',
        default=None,
        help='Use live capture: device index (e.g. 0) or path to video file. Segments via MultiviewIngest.',
    )
    parser.add_argument(
        '--profile',
        default=None,
        help='Path to multiview layout JSON (for --capture). Same format as revamp crop mapper.',
    )
    parser.add_argument(
        '--oneshot',
        action='store_true',
        help='With --capture: process one frame and exit.',
    )
    parser.add_argument(
        '--layout',
        choices=['auto', 'atem_mini', 'atem_mini_pro', 'atem_1me', 'atem_4x4'],
        default='auto',
        help='ATEM layout for image file mode (ignored when --capture is set)',
    )
    parser.add_argument('--no-save', action='store_true', help='Skip saving segment images')
    parser.add_argument('--output-dir', default='segments', help='Directory to save segments')
    parser.add_argument('--confidence', type=float, default=0.5, help='Person detection threshold')
    parser.add_argument('--model', help='Path to custom YOLO model')
    args = parser.parse_args()

    if args.capture is not None:
        return _run_capture_mode(
            args.capture,
            profile_path=args.profile,
            confidence=args.confidence,
            save_segments=not args.no_save,
            output_dir=args.output_dir,
            oneshot=args.oneshot,
            model_path=args.model,
        )

    if not args.image_path:
        parser.error("Provide image_path or use --capture for live capture.")
    image_path = Path(args.image_path)
    if not image_path.exists():
        print(f"Error: Image file not found: {image_path}")
        sys.exit(1)
    if image_path.suffix.lower() not in ('.png', '.jpg', '.jpeg'):
        print(f"Warning: Expected image file, got {image_path.suffix}")

    try:
        detector = ATEMMultiviewDetector(args.model)
        results = detector.process_multiview(
            image_path,
            args.layout,
            args.confidence,
            save_segments=not args.no_save,
            output_dir=args.output_dir,
        )
        return results['inputs_with_people']
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    inputs_with_people = main()
    if inputs_with_people is not None:
        print(f"\nFinal result - Camera inputs with people: {inputs_with_people}")
