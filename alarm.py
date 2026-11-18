"""
============================================================
Forklift AI Safety System — Alarm / Safety Evaluation (v3)
============================================================
CHANGE LOG vs the "final" repo version reviewed:

  Previous behaviour: a person was only flagged as a violation if
  the CENTER POINT of their bounding box fell inside the ROI. A
  person entering the hazard zone from the side, bending down, or
  standing at the edge of frame could have their bbox centroid sit
  outside the ROI while their body/feet were inside it — a
  false-negative on exactly the case the system exists to catch.

  v3 behaviour: a violation fires if ANY of the following is true —
    1. The bbox FOOT POINT (bottom-center — the actual ground
       contact position) is inside the ROI. This is the primary
       test; it is what "is this person standing in the hazard
       zone" actually means.
    2. The bbox overlaps the ROI by more than MIN_IOU_OVERLAP
       (fraction of the person's bbox area that intersects the
       ROI), to catch a person mostly inside the zone whose foot
       point is just outside it (e.g. leaning in).
    3. A minimum bbox area filter (MIN_BOX_AREA_FRACTION) is applied
       before either test — filters out tiny/far/noisy detections so
       they don't consume alarm bandwidth. This does NOT suppress
       real nearby people; it suppresses specks. Only active when
       the caller passes frame_w/frame_h (optional, backward
       compatible with any call site that omits them).

  Public API is UNCHANGED (evaluate_safety / check_boundaries take
  the same first two positional args) — existing call sites keep
  working with no edits. The only change needed in ai_gui_system.py
  is passing frame_w, frame_h as two extra optional args to also get
  the noise filter (already available in scope at the call site).
============================================================
"""

from PyQt5.QtCore import QPoint

# Fraction of the person's bbox area that must overlap the ROI to
# count as a violation, even when the foot point itself is not inside
# the ROI (catches "leaning into the zone" cases).
MIN_IOU_OVERLAP = 0.15

# Bounding boxes smaller than this fraction of the frame area are
# treated as noise/far-field and are not evaluated against the ROI.
# Tune per camera lens/mount height.
MIN_BOX_AREA_FRACTION = 0.004  # 0.4% of frame area


class AlarmSystem:
    """
    Manages safety status evaluation and alarm states. Determines if
    detected person bounding boxes violate the specified Region of
    Interest (ROI).
    """

    def __init__(self):
        self.warning_active = False

    # ==========================================
    # GEOMETRY HELPERS
    # ==========================================
    @staticmethod
    def _foot_point(box):
        """Bottom-center of the bbox — the person's approximate ground
        contact position. This is what should be tested against a
        floor-plane hazard zone, not the geometric center of the box."""
        x1, y1, x2, y2 = box
        return (int((x1 + x2) / 2), int(y2))

    @staticmethod
    def _box_area(box):
        x1, y1, x2, y2 = box
        return max(0, x2 - x1) * max(0, y2 - y1)

    @staticmethod
    def _rect_overlap_fraction(box, rect):
        """Fraction of `box`'s area that intersects the rectangle `rect`."""
        x1, y1, x2, y2 = box
        rx1, ry1 = rect.left(), rect.top()
        rx2, ry2 = rect.right(), rect.bottom()
        ix1, iy1 = max(x1, rx1), max(y1, ry1)
        ix2, iy2 = min(x2, rx2), min(y2, ry2)
        inter_w, inter_h = max(0, ix2 - ix1), max(0, iy2 - iy1)
        inter_area = inter_w * inter_h
        box_area = AlarmSystem._box_area(box)
        if box_area <= 0:
            return 0.0
        return inter_area / box_area

    # ==========================================
    # SAFETY EVALUATION
    # ==========================================
    def evaluate_safety(self, persons, roi_rect, frame_w=None, frame_h=None):
        """
        Evaluates whether any detected person violates the ROI.

        Args:
            persons: list of (x1, y1, x2, y2) tuples in frame pixel coords.
            roi_rect: QRect — the active region of interest. None/null -> no ROI set.
            frame_w, frame_h: optional frame dimensions, used only for the
                min-box-area-fraction noise filter. If omitted, the filter
                is skipped (legacy call sites remain safe without it).

        Returns:
            bool: True if there is a safety warning/violation.
        """
        if not persons:
            return False
        if roi_rect is None or roi_rect.isNull():
            return False

        frame_area = (frame_w * frame_h) if (frame_w and frame_h) else None

        for box in persons:
            if frame_area:
                box_area = self._box_area(box)
                if box_area / frame_area < MIN_BOX_AREA_FRACTION:
                    continue  # too small / far / noise — skip

            foot = self._foot_point(box)

            if roi_rect.contains(QPoint(*foot)):
                return True
            if self._rect_overlap_fraction(box, roi_rect) >= MIN_IOU_OVERLAP:
                return True

        return False

    def check_boundaries(self, persons, roi_rect, frame_w=None, frame_h=None):
        """Alias for evaluate_safety to maintain compatibility with
        existing call sites in ai_gui_system.py."""
        return self.evaluate_safety(persons, roi_rect, frame_w, frame_h)
