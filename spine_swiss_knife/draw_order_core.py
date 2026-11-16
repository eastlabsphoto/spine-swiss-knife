"""Pure helpers for draw-order optimisation.

This module intentionally avoids Qt imports so that the core blend math
and search heuristics can be unit-tested in isolation.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Iterator

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:  # pragma: no cover - exercised in app fallback mode
    np = None
    HAS_NUMPY = False


from .spine_json import get_draw_order_timeline
from .spine_refs import encode_draw_order_offsets, rewrite_draw_order_offsets

DEFAULT_TOLERANCE = 6
DEFAULT_THRESHOLD_PERCENT = 1.0

# Verification memory budget / sampling (see plan_baseline_storage).
BASELINE_MEMORY_BUDGET = 1024 * 1024 * 1024  # 1 GB
FPS_STEPS = (60, 30, 20, 15)
# Local (grid cell) comparison.
CELL_SIZE = 64
CELL_MAX_BAD_FRACTION = 0.5
CELL_MIN_VISIBLE = 16


def threshold_percent_to_ratio(percent: float) -> float:
    """Convert a UI percentage value into a 0..1 ratio."""
    return percent / 100.0


def _strip_draw_order(data: dict) -> dict:
    """Copy of *data* without slots and without draw-order timelines."""
    out = {k: v for k, v in data.items() if k != "slots"}
    anims = data.get("animations")
    if isinstance(anims, dict):
        new_anims = {}
        for name, anim in anims.items():
            if isinstance(anim, dict):
                anim = {k: v for k, v in anim.items()
                        if k not in ("drawOrder", "draworder")}
            new_anims[name] = anim
        out["animations"] = new_anims
    return out


def _slot_signature(slot) -> str:
    import json
    return json.dumps(slot, sort_keys=True, default=str)


def baseline_is_reorder_of(current: dict, baseline: dict) -> bool:
    """True iff *current* is a pure slot reorder of *baseline*.

    Both must hold the same slots as a multiset of slot dicts (order ignored)
    and be equal everywhere else once draw-order timelines are removed from
    all animations.
    """
    if not isinstance(current, dict) or not isinstance(baseline, dict):
        return False
    cs = current.get("slots") or []
    bs = baseline.get("slots") or []
    if len(cs) != len(bs):
        return False
    if sorted(_slot_signature(s) for s in cs) != sorted(_slot_signature(s) for s in bs):
        return False
    return _strip_draw_order(current) == _strip_draw_order(baseline)


def resolve_baseline_paths(
    json_path: str,
    *,
    backup_exists: bool,
    backup_is_reorder: bool = True,
) -> tuple[str, str, bool]:
    """Resolve the comparison baseline and backup target paths.

    The ``.backup`` file is only a valid baseline when it exists AND the
    current JSON is a pure slot reorder of it (*backup_is_reorder*, see
    :func:`baseline_is_reorder_of`); otherwise the current JSON is the baseline.
    The third value tells whether the backup has to be (re)created.
    """
    backup_path = json_path + ".backup"
    use_backup = backup_exists and backup_is_reorder
    baseline_path = backup_path if use_backup else json_path
    return baseline_path, backup_path, not use_backup


def build_reordered_data(spine_data: dict, new_slots: list, deep: bool = False) -> dict:
    """Copy of *spine_data* with slots in *new_slots* order and every
    draw-order offsets list re-encoded so absolute draw orders are unchanged.

    The offsets are re-encoded relative to ``spine_data["slots"]``. With
    ``deep=False`` only the structures that are modified are copied.
    """
    old_names = [s.get("name") for s in spine_data.get("slots") or []]
    new_names = [s.get("name") for s in new_slots]
    if deep:
        out = copy.deepcopy(spine_data)
    else:
        out = dict(spine_data)
        anims = spine_data.get("animations")
        if isinstance(anims, dict):
            new_anims = {}
            for name, anim in anims.items():
                if isinstance(anim, dict) and get_draw_order_timeline(anim):
                    anim = dict(anim)
                    for key in ("drawOrder", "draworder"):
                        if key in anim:
                            anim[key] = copy.deepcopy(anim[key])
                new_anims[name] = anim
            out["animations"] = new_anims
    out["slots"] = list(new_slots)
    if old_names != new_names:
        setup_keys = _setup_order_keys(out)
        rewrite_draw_order_offsets(out, old_names, new_names)
        _preserve_setup_order_keys(out, old_names, new_names, setup_keys)
    return out


def _setup_order_keys(data: dict) -> list:
    """Draw-order key dicts (by identity) that have no offsets."""
    keys = []
    for anim in (data.get("animations") or {}).values():
        if isinstance(anim, dict):
            keys.extend(k for k in get_draw_order_timeline(anim)
                        if isinstance(k, dict) and not k.get("offsets"))
    return keys


def _preserve_setup_order_keys(data: dict, old_names: list, new_names: list,
                               setup_keys: list):
    """Draw-order keys without offsets (and the time before the first key)
    mean *setup order*, which just changed. Re-encode them as explicit
    offsets of the OLD setup order so the absolute order stays the same."""
    try:
        old_as_offsets = encode_draw_order_offsets(new_names, old_names)
    except ValueError:
        return
    if not old_as_offsets:
        return
    for anim in (data.get("animations") or {}).values():
        if not isinstance(anim, dict):
            continue
        timeline = get_draw_order_timeline(anim)
        if not timeline:
            continue
        for key in timeline:
            if any(key is k for k in setup_keys):
                key["offsets"] = copy.deepcopy(old_as_offsets)
        first = timeline[0]
        if isinstance(first, dict) and first.get("time", 0) > 0:
            timeline.insert(0, {"time": 0,
                                "offsets": copy.deepcopy(old_as_offsets)})


def sample_times(duration: float, fps: float) -> list:
    """Sample times 0, 1/fps, ... below *duration*, always ending at *duration*."""
    if duration <= 0 or fps <= 0:
        return [0.0]
    times = []
    i = 0
    while True:
        t = i / fps
        if t >= duration - 1e-9:
            break
        times.append(t)
        i += 1
    times.append(float(duration))
    return times


def plan_baseline_storage(durations, skin_count: int, width: int, height: int,
                          fps: int = 60, budget: int = BASELINE_MEMORY_BUDGET):
    """Choose sampling rate and storage for the cached baseline frames.

    *durations* is the duration of every rendered animation (the setup pose
    is one extra frame). Returns ``(fps, mode, estimated_bytes)`` where mode is
    ``"memory"`` or ``"disk"`` (spill to a temp directory at the lowest rate).
    """
    frame_bytes = max(1, int(width)) * max(1, int(height)) * 4
    skins = max(1, int(skin_count))
    candidates = [f for f in FPS_STEPS if f <= fps] or [min(FPS_STEPS)]
    if fps not in candidates and fps < min(FPS_STEPS):
        candidates = [fps]
    elif fps not in candidates:
        candidates.insert(0, fps)
    est = 0
    for cand in candidates:
        frames = skins * (1 + sum(len(sample_times(d, cand)) for d in durations))
        est = frames * frame_bytes
        if est <= budget:
            return cand, "memory", est
    return candidates[-1], "disk", est


def sanitize_dir_name(name) -> str:
    """Filesystem-safe directory name for an animation label."""
    import re
    if name is None:
        return "setup_pose"
    text = str(name)
    text = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", text)
    text = text.rstrip(". ")
    return "anim_" + text


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
        bad_mask = bad_mask & visible_mask
    bad = int(np.count_nonzero(bad_mask))

    pct_bad = bad / denom * 100 if denom > 0 else 0.0
    global_ok = bad / denom < threshold if denom > 0 else True
    worst_cell = _worst_cell_fraction(bad_mask, visible_mask if visible > 0 else None)
    local_ok = worst_cell <= CELL_MAX_BAD_FRACTION
    match = global_ok and local_ok
    return match, {
        "max_diff": max_val,
        "bad_pixels": bad,
        "visible_pixels": visible,
        "pct_bad": pct_bad,
        "worst_cell_pct": worst_cell * 100,
    }


def _worst_cell_fraction(bad_mask, visible_mask) -> float:
    """Largest fraction of differing pixels among visible pixels of any
    CELL_SIZE x CELL_SIZE cell (two grid origins, so a small element cannot
    hide on a cell border). Cells with fewer than CELL_MIN_VISIBLE visible
    pixels are ignored."""
    if bad_mask.size == 0 or not bad_mask.any():
        return 0.0
    if visible_mask is None:
        visible_mask = np.ones_like(bad_mask)
    h, w = bad_mask.shape
    worst = 0.0
    c = CELL_SIZE
    for off in (0, c // 2):
        bm = bad_mask[off:, off:]
        vm = visible_mask[off:, off:]
        hh, ww = bm.shape
        if hh == 0 or ww == 0:
            continue
        ph, pw = (-hh) % c, (-ww) % c
        if ph or pw:
            bm = np.pad(bm, ((0, ph), (0, pw)))
            vm = np.pad(vm, ((0, ph), (0, pw)))
        gh, gw = bm.shape[0] // c, bm.shape[1] // c
        bad_c = bm.reshape(gh, c, gw, c).sum(axis=(1, 3))
        vis_c = vm.reshape(gh, c, gw, c).sum(axis=(1, 3))
        ok = vis_c >= CELL_MIN_VISIBLE
        if ok.any():
            frac = bad_c[ok] / vis_c[ok]
            worst = max(worst, float(frac.max()))
    return worst


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
