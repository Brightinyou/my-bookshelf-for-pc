import struct
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ICON_PATH = PROJECT_ROOT / "MyBookshelf.ico"


def ico_sizes(path):
    data = path.read_bytes()
    reserved, image_type, count = struct.unpack_from("<HHH", data)
    if reserved != 0 or image_type != 1:
        raise ValueError("not a Windows icon file")

    sizes = set()
    for index in range(count):
        width, height = struct.unpack_from("<BB", data, 6 + index * 16)
        sizes.add((width or 256, height or 256))
    return sizes


class IconAssetTests(unittest.TestCase):
    def test_windows_icon_contains_desktop_and_scaled_sizes(self):
        self.assertEqual(
            ico_sizes(ICON_PATH),
            {
                (16, 16),
                (20, 20),
                (24, 24),
                (32, 32),
                (40, 40),
                (48, 48),
                (64, 64),
                (96, 96),
                (128, 128),
                (256, 256),
            },
        )

    def test_windows_icon_uses_dib_frames_for_explorer_compatibility(self):
        data = ICON_PATH.read_bytes()
        _, _, count = struct.unpack_from("<HHH", data)

        for index in range(count):
            entry_offset = 6 + index * 16
            image_size, image_offset = struct.unpack_from(
                "<II", data, entry_offset + 8
            )
            self.assertGreater(image_size, 0)
            self.assertIn(
                struct.unpack_from("<I", data, image_offset)[0],
                {40, 108, 124},
            )


if __name__ == "__main__":
    unittest.main()
