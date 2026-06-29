import unittest

from spine_swiss_knife.spine_json import (
    mesh_is_weighted,
    resolve_weighted_vertices,
)


class _BT:
    """Minimal stand-in for BoneTransform (a, b, c, d, worldX, worldY)."""

    def __init__(self, a=1.0, b=0.0, c=0.0, d=1.0, world_x=0.0, world_y=0.0):
        self.a, self.b, self.c, self.d = a, b, c, d
        self.worldX, self.worldY = world_x, world_y


class MeshIsWeightedTests(unittest.TestCase):
    def test_flat_pairs_are_not_weighted(self):
        # 3 vertices -> 6 values, uvs give vertex_count = 3
        self.assertFalse(mesh_is_weighted([0, 0, 1, 0, 1, 1], vertex_count=3))

    def test_weighted_array_is_detected(self):
        # 1 vertex, 1 bone -> [boneCount, boneIdx, x, y, weight] = 5 values
        self.assertTrue(mesh_is_weighted([1, 0, 5.0, 7.0, 1.0], vertex_count=1))

    def test_zero_vertex_count_is_not_weighted(self):
        self.assertFalse(mesh_is_weighted([], vertex_count=0))


class ResolveWeightedVerticesTests(unittest.TestCase):
    def test_single_identity_bone_returns_local_position(self):
        verts = [1, 0, 5.0, 7.0, 1.0]
        out = resolve_weighted_vertices(verts, [_BT()])
        self.assertEqual(out, [5.0, 7.0])

    def test_single_translated_bone(self):
        verts = [1, 0, 5.0, 7.0, 1.0]
        out = resolve_weighted_vertices(verts, [_BT(world_x=10.0, world_y=20.0)])
        self.assertEqual(out, [15.0, 27.0])

    def test_two_bone_blend(self):
        # vertex influenced 50/50 by bone0 (identity) and bone1 (+10 on x)
        verts = [2, 0, 4.0, 0.0, 0.5, 1, 4.0, 0.0, 0.5]
        out = resolve_weighted_vertices(
            verts, [_BT(), _BT(world_x=10.0)])
        self.assertEqual(out, [9.0, 0.0])

    def test_odd_length_array_does_not_raise(self):
        # The old renderer crashed with IndexError on odd-length weighted arrays.
        verts = [1, 0, 1.0, 2.0, 1.0, 1, 1, 3.0, 4.0, 1.0]  # 10 values, 2 verts
        out = resolve_weighted_vertices(verts, [_BT(), _BT()])
        self.assertEqual(len(out), 4)  # 2 world-space (x, y) pairs

    def test_missing_bone_index_is_skipped(self):
        verts = [1, 99, 5.0, 7.0, 1.0]  # bone index out of range
        out = resolve_weighted_vertices(verts, [_BT()])
        self.assertEqual(out, [0.0, 0.0])


if __name__ == "__main__":
    unittest.main()
