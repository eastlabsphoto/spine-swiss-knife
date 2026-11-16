"""Rectangle-mask analysis (pure logic, no Qt)."""

from __future__ import annotations

from .spine_json import is_weighted


def analyze_clipping(att_data: dict, tolerance: float = 5.0) -> dict:
    """Analyse a clipping attachment; ``skip_code`` explains non-candidates.

    skip_code: "weighted", "not4", "duplicate_corners", "too_large".
    """
    vc = att_data.get("vertexCount", 0)
    verts = att_data.get("vertices", [])
    points = list(zip(verts[::2], verts[1::2]))
    info = {"vertex_count": vc, "vertices": verts, "points": points,
            "is_rect_candidate": False}

    if is_weighted(att_data):
        info["skip_code"] = "weighted"
        info["points"] = []
        return info
    if vc != 4 or len(verts) != 8:
        info["skip_code"] = "not4"
        return info

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    snapped_xs = [x_min if abs(x - x_min) < abs(x - x_max) else x_max for x in xs]
    snapped_ys = [y_min if abs(y - y_min) < abs(y - y_max) else y_max for y in ys]
    max_dev = max(
        max(abs(x - sx) for x, sx in zip(xs, snapped_xs)),
        max(abs(y - sy) for y, sy in zip(ys, snapped_ys)),
    )
    if max_dev > tolerance or len(set(snapped_xs)) != 2 or len(set(snapped_ys)) != 2:
        info["skip_code"] = "too_large"
        return info
    snapped_verts = []
    for sx, sy in zip(snapped_xs, snapped_ys):
        snapped_verts.append(round(sx))
        snapped_verts.append(round(sy))
    snapped_points = list(zip(snapped_verts[::2], snapped_verts[1::2]))
    if len(set(snapped_points)) != 4:
        info["skip_code"] = "duplicate_corners"
        return info
    info["is_rect_candidate"] = True
    info["max_deviation"] = round(max_dev, 2)
    info["snapped_vertices"] = snapped_verts
    info["snapped_points"] = snapped_points
    return info
