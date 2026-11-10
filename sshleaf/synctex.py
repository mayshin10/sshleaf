"""Minimal SyncTeX reader: forward (file, line -> page, x, y) and inverse (page, x, y -> file, line).

Coordinates returned/accepted are PDF points (bp) from the top-left of the page.
"""

import gzip
import os
import re
from pathlib import Path

RECORD = re.compile(r"^([\[(hxkg$v])(\d+),(\d+):(-?\d+),(-?\d+)(?::(-?\d+),(-?\d+),(-?\d+))?")


class SyncTex:
    def __init__(self, path: Path, base_dir: str):
        self.inputs = {}  # tag -> absolute path
        self.records = []  # (page, tag, line, x, y, width, height, depth) in bp
        unit, magnification, x_offset, y_offset = 1.0, 1000.0, 0.0, 0.0
        page = 0
        lines = gzip.open(path, "rt", errors="replace").read().splitlines()
        for line in lines:
            if line.startswith("Input:"):
                tag, _, name = line[6:].partition(":")
                self.inputs[int(tag)] = os.path.realpath(os.path.join(base_dir, name))
            elif line.startswith("Unit:"):
                unit = float(line[5:])
            elif line.startswith("Magnification:"):
                magnification = float(line[14:])
            elif line.startswith("X Offset:"):
                x_offset = float(line[9:])
            elif line.startswith("Y Offset:"):
                y_offset = float(line[9:])
            elif line.startswith("{"):
                page = int(line[1:])
            else:
                match = RECORD.match(line)
                if match and page:
                    kind, tag, lineno, h, v, width, height, depth = match.groups()
                    scale = unit * magnification / 1000 / 65536 * 72 / 72.27
                    self.records.append((
                        page, int(tag), int(lineno),
                        (int(h) + x_offset) * scale, (int(v) + y_offset) * scale,
                        int(width or 0) * scale, int(height or 0) * scale, int(depth or 0) * scale,
                    ))

    def tag_for(self, file: str):
        file = os.path.realpath(file)
        for tag, name in self.inputs.items():
            if name == file:
                return tag
        return None

    def forward(self, file: str, line: int):
        tag = self.tag_for(file)
        if tag is None:
            return None
        candidates = [r for r in self.records if r[1] == tag and r[2] > 0]
        if not candidates:
            return None
        best_line = min({r[2] for r in candidates}, key=lambda value: (abs(value - line), value < line))
        hits = [r for r in candidates if r[2] == best_line]
        page = hits[0][0]
        on_page = [r for r in hits if r[0] == page]
        x = min(r[3] for r in on_page)
        top = min(r[4] - r[6] for r in on_page)
        bottom = max(r[4] + r[7] for r in on_page)
        return {"page": page, "x": x, "y": top, "height": max(bottom - top, 10)}

    def inverse(self, page: int, x: float, y: float):
        best, best_score = None, None
        for record in self.records:
            if record[0] != page or record[2] <= 0:
                continue
            _, tag, line, left, base, width, height, depth = record
            top, bottom, right = base - height, base + depth, left + width
            inside = left <= x <= right and top <= y <= bottom
            dx = 0 if left <= x <= right else min(abs(x - left), abs(x - right))
            dy = 0 if top <= y <= bottom else min(abs(y - top), abs(y - bottom))
            # prefer the smallest box containing the point, else the nearest record
            score = (0, width * (height + depth)) if inside else (1, dy * 4 + dx)
            if best_score is None or score < best_score:
                best, best_score = (tag, line), score
        if best is None:
            return None
        return {"file": self.inputs[best[0]], "line": best[1]}
