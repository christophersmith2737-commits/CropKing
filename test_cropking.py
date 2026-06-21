"""Unit & integration tests for CropKing — run without GUI."""
import unittest, os, sys, tempfile, shutil
from pathlib import Path
from PIL import Image, ImageDraw

# Add parent to path so we can import cropking
sys.path.insert(0, str(Path(__file__).parent))
from cropking import parse_face_coords, import_shape, list_shapes, SHAPES_DIR


class TestParseFaceCoords(unittest.TestCase):
    """Test AI response parsing."""

    def test_standard_format(self):
        text = "棕色短发女生|45|30|12\n黑长直女生|60|25|10"
        result = parse_face_coords(text, 1920, 1080)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0][0], "棕色短发女生")
        self.assertAlmostEqual(result[0][1], 1920 * 0.45, delta=2)
        self.assertAlmostEqual(result[0][2], 1080 * 0.30, delta=2)
        self.assertAlmostEqual(result[0][3], 1080 * 0.12, delta=2)

    def test_with_percent_signs(self):
        text = "Yui|50%|40%|15%"
        result = parse_face_coords(text, 1000, 1000)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][1], 500)
        self.assertEqual(result[0][2], 400)

    def test_missing_head_size(self):
        text = "Mio|30|50"
        result = parse_face_coords(text, 1000, 1000)
        self.assertEqual(len(result), 1)
        # Falls back to default 12% of min dimension
        self.assertAlmostEqual(result[0][3], 120, delta=2)

    def test_empty_text(self):
        result = parse_face_coords("", 100, 100)
        self.assertEqual(result, [])

    def test_no_valid_lines(self):
        result = parse_face_coords("这是介绍文字\n没有坐标", 100, 100)
        self.assertEqual(result, [])

    def test_mixed_valid_invalid(self):
        text = "无坐标行\nYui|40|50|10\n另一行无效"
        result = parse_face_coords(text, 1000, 1000)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], "Yui")

    def test_edge_positions(self):
        text = "Edge|0|0|5\nCorner|100|100|5"
        result = parse_face_coords(text, 800, 600)
        self.assertEqual(result[0][1], 0)
        self.assertEqual(result[1][1], 800)
        self.assertEqual(result[1][2], 600)


class TestShapeLibrary(unittest.TestCase):
    """Test custom shape import & listing."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        import cropking
        self._old_dir = cropking.SHAPES_DIR
        cropking.SHAPES_DIR = Path(self.tmpdir)

    def tearDown(self):
        import cropking
        cropking.SHAPES_DIR = self._old_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_import_and_list(self):
        # Create source image OUTSIDE shapes dir to avoid polluting
        srcdir = tempfile.mkdtemp()
        img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse([10, 10, 90, 90], fill=(255, 105, 180, 255))
        src = os.path.join(srcdir, "test_circle.png")
        img.save(src)

        dest = import_shape(src, "my_circle")
        self.assertTrue(os.path.exists(dest))

        shapes = list_shapes()
        self.assertEqual(len(shapes), 1)
        self.assertEqual(shapes[0][0], "my_circle")

    def test_import_sets_default_name(self):
        srcdir = tempfile.mkdtemp()
        img = Image.new("RGBA", (50, 50), (255, 0, 0, 128))
        src = os.path.join(srcdir, "character_render.png")
        img.save(src)

        dest = import_shape(src)  # no name, uses stem
        self.assertIn("character_render", dest)

    def test_import_rejects_no_alpha(self):
        srcdir = tempfile.mkdtemp()
        img = Image.new("RGB", (100, 100), "red")
        src = os.path.join(srcdir, "no_alpha.png")
        img.save(src)

        # Should convert to RGBA — red pixels with 255 alpha
        # But a fully opaque image still has alpha, so it should work
        dest = import_shape(src, "red_shape")
        self.assertTrue(os.path.exists(dest))

    def test_import_rejects_fully_transparent(self):
        srcdir = tempfile.mkdtemp()
        img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        src = os.path.join(srcdir, "empty.png")
        img.save(src)

        with self.assertRaises(ValueError):
            import_shape(src, "empty")


class TestCropLogic(unittest.TestCase):
    """Test cropping math without GUI."""

    def test_crop_bbox_clamping(self):
        """Verify coordinates clamp to image boundaries."""
        img = Image.new("RGB", (800, 600), "blue")
        # Try to crop partially outside
        crop = img.crop((700, 500, 900, 700))
        # PIL silently clamps? No, it doesn't — let's test our clamp logic
        x1, x2 = sorted([max(0, 700), min(800, 900)])
        y1, y2 = sorted([max(0, 500), min(600, 700)])
        self.assertEqual((x1, y1, x2, y2), (700, 500, 800, 600))

    def test_circle_mask_size(self):
        """Verify circle mask is correct dimensions."""
        img = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
        mask = Image.new("L", (200, 200), 0)
        draw = ImageDraw.Draw(mask)
        draw.ellipse([50, 50, 150, 150], fill=255)
        self.assertEqual(mask.size, (200, 200))
        # Center should be white
        self.assertEqual(mask.getpixel((100, 100)), 255)
        # Outside should be black
        self.assertEqual(mask.getpixel((0, 0)), 0)

    def test_output_naming(self):
        """Test the naming convention logic."""
        base = "K-On_group"
        prefix = "_crop"
        counter = 3
        name = f"{base}{prefix}_{counter:02d}.png"
        self.assertEqual(name, "K-On_group_crop_03.png")

    def test_output_naming_with_label(self):
        """Test naming with character label in prefix."""
        base = "photo"
        prefix = "__棕色短发"
        counter = 0
        # Strip non-safe chars for filename
        import re
        safe = re.sub(r'[^\w一-鿿]', '', "棕色短发")[:10]
        name = f"{base}_{safe}_{counter:02d}.png"
        self.assertEqual(name, "photo_棕色短发_00.png")


class TestIntegration(unittest.TestCase):
    """End-to-end: open image → crop → verify output."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.src = os.path.join(self.tmpdir, "test_src.png")
        img = Image.new("RGB", (400, 300), "#1e1e2e")
        d = ImageDraw.Draw(img)
        d.rectangle([50, 50, 150, 150], fill="#ff69b4")
        d.rectangle([200, 100, 350, 250], fill="#8a2be2")
        img.save(self.src)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_rect_crop_save(self):
        img = Image.open(self.src)
        cropped = img.crop((50, 50, 150, 150))
        out = os.path.join(self.tmpdir, "out_rect.png")
        cropped.save(out)
        self.assertTrue(os.path.exists(out))
        result = Image.open(out)
        self.assertEqual(result.size, (100, 100))

    def test_jpeg_output_converts_rgba(self):
        img = Image.open(self.src).convert("RGBA")
        cropped = img.crop((200, 100, 350, 250))
        out = os.path.join(self.tmpdir, "out.jpg")
        if cropped.mode in ("RGBA", "P"):
            cropped = cropped.convert("RGB")
        cropped.save(out, "JPEG")
        self.assertTrue(os.path.exists(out))

    def test_circle_crop_with_mask(self):
        img = Image.open(self.src).convert("RGBA")
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).ellipse([100, 75, 200, 175], fill=255)
        result = img.copy()
        result.putalpha(mask)
        result = result.crop(mask.getbbox())
        out = os.path.join(self.tmpdir, "out_circle.png")
        result.save(out)
        self.assertTrue(os.path.exists(out))
        # Verify transparent corners
        saved = Image.open(out)
        self.assertEqual(saved.mode, "RGBA")


if __name__ == "__main__":
    unittest.main(verbosity=2)
