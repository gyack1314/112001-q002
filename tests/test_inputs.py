import json
from pathlib import Path
import unittest


class InputShapeTest(unittest.TestCase):
    def test_readings_keep_unit_and_window(self):
        rows = json.loads((Path(__file__).parents[1] / "data" / "readings.json").read_text(encoding="utf-8"))
        self.assertTrue(rows)
        self.assertTrue(all(row["unit"] and row["window_start"] and row["window_end"] for row in rows))


if __name__ == "__main__":
    unittest.main()
