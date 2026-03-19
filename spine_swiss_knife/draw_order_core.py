"""Pure helpers for draw-order optimisation.

This module intentionally avoids Qt imports so that the core blend math
and search heuristics can be unit-tested in isolation.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:  # pragma: no cover - exercised in app fallback mode
    np = None
    HAS_NUMPY = False


DEFAULT_TOLERANCE = 6
DEFAULT_THRESHOLD_PERCENT = 1.0


def threshold_percent_to_ratio(percent: float) -> float:
    """Convert a UI percentage value into a 0..1 ratio."""
    return percent / 100.0


def resolve_baseline_paths(
    json_path: str,
    *,
    backup_exists: bool,
) -> tuple[str, str, bool]:
    """Resolve the frozen comparison baseline and backup target paths."""
    backup_path = json_path + ".backup"
    baseline_path = backup_path if backup_exists else json_path
    return baseline_path, backup_path, not backup_exists


def count_blend_groups(slots: list[dict]) -> int:
    """Count contiguous blend-mode groups (~ draw calls)."""
    if not slots:
        return 0
    groups = 1
    prev = slots[0].get("blend", "normal")
    for slot in slots[1:]:
        cur = slot.get("blend", "normal")
        if cur != prev:
            groups += 1
            prev = cur
    return groups


def compute_optimal_order(slots: list[dict]) -> list[dict]:
    """Stable-partition: normal slots first, then non-normal blends."""
    groups: dict[str, list[dict]] = {}
    for slot in slots:
        blend = slot.get("blend", "normal")
        groups.setdefault(blend, []).append(slot)
    result = groups.pop("normal", [])
    for blend in sorted(groups):
        result.extend(groups[blend])
    return result


def blend_rgba(dst, src, mode: str):
    """Blend ``src`` onto ``dst`` in premultiplied RGBA space."""
    if not HAS_NUMPY:
        raise RuntimeError("numpy is required for RGBA blending")

    sa = src[:, :, 3:4]
    s_rgb = src[:, :, :3]
    d_rgb = dst[:, :, :3]
    d_a = dst[:, :, 3:4]

    if mode == "normal":
        out_rgb = s_rgb + d_rgb * (1.0 - sa)
        out_a = sa + d_a * (1.0 - sa)
    elif mode == "additive":
        out_rgb = np.minimum(s_rgb + d_rgb, 1.0)
        out_a = np.minimum(sa + d_a, 1.0)
    elif mode == "multiply":
        d_a_safe = np.where(d_a > 0, d_a, 1.0)
        s_a_safe = np.where(sa > 0, sa, 1.0)
        d_straight = d_rgb / d_a_safe
        s_straight = s_rgb / s_a_safe
        blended = s_straight * d_straight
        out_rgb = blended * sa * d_a + d_rgb * (1.0 - sa)
        out_a = sa + d_a * (1.0 - sa)
    elif mode == "screen":
        d_a_safe = np.where(d_a > 0, d_a, 1.0)
        s_a_safe = np.where(sa > 0, sa, 1.0)
        d_straight = d_rgb / d_a_safe
        s_straight = s_rgb / s_a_safe
        blended = s_straight + d_straight - s_straight * d_straight
        out_rgb = blended * sa * d_a + d_rgb * (1.0 - sa)
        out_a = sa + d_a * (1.0 - sa)
    else:
        out_rgb = s_rgb + d_rgb * (1.0 - sa)
        out_a = sa + d_a * (1.0 - sa)

    result = dst.copy()
    result[:, :, :3] = np.clip(out_rgb, 0.0, 1.0)
    result[:, :, 3:4] = np.clip(out_a, 0.0, 1.0)
    return result


def compose_layers(
    layers: Iterable[tuple[str, "np.ndarray"]],
    shape: tuple[int, int, int] | None = None,
):
    """Compose premultiplied RGBA layers in draw order."""
    if not HAS_NUMPY:
        raise RuntimeError("numpy is required for layer composition")

    layer_list = list(layers)
    if not layer_list:
        if shape is None:
            raise ValueError("shape is required when composing zero layers")
        return np.zeros(shape, dtype=np.float32)

    if shape is None:
        shape = layer_list[0][1].shape

    canvas = np.zeros(shape, dtype=np.float32)
    for mode, layer in layer_list:
        canvas = blend_rgba(canvas, layer, mode)
    return canvas


def compare_rgba_arrays(
    arr_a,
    arr_b,
    tolerance: int = 5,
    threshold: float = 0.02,
):
    """Compare two RGBA arrays using visible pixels as the denominator."""
    if arr_a.shape != arr_b.shape:
        return False, {
            "max_diff": None,
            "bad_pixels": None,
            "visible_pixels": None,
            "pct_bad": None,
        }

    diff = np.abs(arr_a.astype(np.int16) - arr_b.astype(np.int16))
    pixel_max = diff.max(axis=2)
    max_val = int(pixel_max.max()) if pixel_max.size > 0 else 0

    visible_mask = (
        np.any(arr_a > 0, axis=2) |
        np.any(arr_b > 0, axis=2)
    )
    visible = int(np.count_nonzero(visible_mask))
    denom = visible if visible > 0 else arr_a.shape[0] * arr_a.shape[1]

    bad_mask = pixel_max > tolerance
    if visible > 0:
        bad = int(np.count_nonzero(bad_mask & visible_mask))
    else:
        bad = int(np.count_nonzero(bad_mask))

    pct_bad = bad / denom * 100 if denom > 0 else 0.0
    match = bad / denom < threshold if denom > 0 else True
    return match, {
        "max_diff": max_val,
        "bad_pixels": bad,
        "visible_pixels": visible,
        "pct_bad": pct_bad,
    }


def plan_composition_batches(draw_list: list[dict]) -> Iterator[tuple[str, list[dict]]]:
    """Yield render batches that preserve exact blend semantics.

    Consecutive normal items are batched together because source-over is
    associative. Non-normal items stay isolated so additive/multiply/screen
    layers are still composed strictly one-by-one in draw order.
    """
    pending: list[dict] = []
    current_normal: list[dict] = []

    for item in draw_list:
        item_type = item.get("type")
        if item_type in ("clip", "clip_end_marker"):
            if current_normal:
                current_normal.append(item)
            else:
                pending.append(item)
            continue

        blend = item.get("blend", "normal")
        if blend == "normal":
            if not current_normal:
                current_normal = pending
                pending = []
            current_normal.append(item)
            continue

        if current_normal:
            yield "normal", current_normal
            current_normal = []

        yield blend, pending + [item]
        pending = []

    if current_normal:
        yield "normal", current_normal


def iter_group_reducing_block_moves(slots: list[dict]) -> Iterator[list[dict]]:
    """Yield group-reducing candidates made by moving contiguous runs.

    Each candidate preserves internal order of the moved run. Results are
    yielded best-first by draw-group reduction, then by move distance.
    """
    if len(slots) <= 1:
        return

    base_groups = count_blend_groups(slots)
    seen: set[tuple[str, ...]] = set()
    scored: list[tuple[int, int, int, int, list[dict]]] = []

    i = 0
    while i < len(slots):
        blend = slots[i].get("blend", "normal")
        j = i + 1
        while j < len(slots) and slots[j].get("blend", "normal") == blend:
            j += 1

        block = slots[i:j]
        remainder = slots[:i] + slots[j:]
        for insert_pos in range(len(remainder) + 1):
            candidate = remainder[:insert_pos] + block + remainder[insert_pos:]
            if candidate == slots:
                continue
            new_groups = count_blend_groups(candidate)
            if new_groups >= base_groups:
                continue

            signature = tuple(slot.get("name", "") for slot in candidate)
            if signature in seen:
                continue
            seen.add(signature)

            saved = base_groups - new_groups
            move_distance = abs(insert_pos - i)
            scored.append((-saved, move_distance, i, insert_pos, candidate))

        i = j

    scored.sort(key=lambda item: item[:4])
    for _saved, _distance, _start, _insert, candidate in scored:
        yield candidate
