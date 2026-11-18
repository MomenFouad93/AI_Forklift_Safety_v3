"""
============================================================
Forklift AI Safety System — Detector (v3)
============================================================
CHANGE LOG: YoloDetector itself is UNCHANGED — its confidence
threshold is a safety-sensitivity tradeoff (raising it cuts false
positives but can also cost real detections at range/occlusion),
so it is intentionally left as the field-tuned value in config.json
rather than silently overridden here. The noise-suppression gap this
review flagged is instead handled in alarm.py's MIN_BOX_AREA_FRACTION
filter, which filters by bbox SIZE (specks/far-field), not by model
confidence — it does not cost sensitivity for real close-range people.

New in v3: PersonTracker — an OPT-IN multi-frame consensus filter.
Not wired into ai_gui_system.py by default (it adds a few frames of
detection latency, which is a decision for whoever owns the ROI/
timing validation to make deliberately, not something to flip on
silently). See UPGRADE_REPORT.md for the one-line integration if you
want it: instantiate one PersonTracker per camera station and call
tracker.update(persons, frame_w, frame_h) instead of using the raw
detect_persons() output directly.
============================================================
"""

import os
from ultralytics import YOLO

class YoloDetector:
    """
    Wraps the Ultralytics YOLO model and filters detections for target object classes (specifically persons).
    """
    def __init__(self, model_name="yolov8n.pt", confidence=0.25, model_path=None, **kwargs):
        if model_path is not None:
            model_name = model_path
        self.confidence = confidence
        self.model_name = model_name
        
        # Resolve the model path checking local directory and parent directory
        base_dir = os.path.dirname(os.path.abspath(__file__))
        parent_dir = os.path.dirname(base_dir)
        
        path_in_parent = os.path.join(parent_dir, model_name)
        path_in_base = os.path.join(base_dir, model_name)
        
        if os.path.exists(path_in_parent):
            model_path = path_in_parent
        elif os.path.exists(path_in_base):
            model_path = path_in_base
        else:
            model_path = model_name  # Will download automatically via ultralytics
            
        self.model = YOLO(model_path)

    def detect_persons(self, frame, conf=None, imgsz=320):
        """
        Runs YOLO model inference on the given frame and filters for class 0 (person).
        Args:
            frame (numpy.ndarray): The input OpenCV frame.
            conf (float, optional): Custom confidence threshold override.
            imgsz (int): YOLO inference input size.
        Returns:
            list of tuples: Bounding boxes of detected persons, formatted as [(x1, y1, x2, y2), ...]
        """
        c = conf if conf is not None else self.confidence
        results = self.model(frame, conf=c, imgsz=imgsz, verbose=False)
        persons = []
        for result in results:
            for box in result.boxes:
                cls = int(box.cls[0])
                if cls == 0:  # Class 0 is Person in YOLO COCO model
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    persons.append((x1, y1, x2, y2))
        return persons


class PersonTracker:
    """
    Optional multi-frame consensus filter (opt-in — see module docstring).
    Confirms a detection only once it has appeared, at roughly the same
    position, across CONSENSUS_FRAMES of the last WINDOW_FRAMES calls to
    update(). Reduces one-off spurious detections (reflections, motion
    blur artifacts) without raising the model confidence threshold.
    """

    WINDOW_FRAMES       = 5
    CONSENSUS_FRAMES    = 2
    MATCH_DISTANCE_FRAC = 0.08  # fraction of frame diagonal

    def __init__(self):
        self._history = []  # list of lists of (cx, cy, box) per recent frame

    def reset(self):
        self._history = []

    @staticmethod
    def _centroid(box):
        x1, y1, x2, y2 = box
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    def update(self, persons, frame_w, frame_h):
        """
        Args:
            persons: current frame's list of (x1,y1,x2,y2) boxes.
            frame_w, frame_h: current frame dimensions (for match distance).
        Returns:
            list of (x1,y1,x2,y2) boxes from `persons` that have been seen
            consistently across the rolling window.
        """
        diag = (frame_w ** 2 + frame_h ** 2) ** 0.5
        max_dist = diag * self.MATCH_DISTANCE_FRAC

        current = [(self._centroid(b), b) for b in persons]
        self._history.append(current)
        if len(self._history) > self.WINDOW_FRAMES:
            self._history.pop(0)

        confirmed = []
        for (cx, cy), box in current:
            hits = 0
            for frame_entries in self._history:
                for (hx, hy), _ in frame_entries:
                    if ((hx - cx) ** 2 + (hy - cy) ** 2) ** 0.5 <= max_dist:
                        hits += 1
                        break
            if hits >= self.CONSENSUS_FRAMES:
                confirmed.append(box)
        return confirmed

