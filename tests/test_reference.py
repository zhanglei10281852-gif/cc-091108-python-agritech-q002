import json
import unittest
from pathlib import Path


class ReferenceDataTest(unittest.TestCase):
    def test_equipment_references_are_valid(self):
        data = json.loads((Path(__file__).parents[1] / "reference" / "domain.json").read_text(encoding="utf-8"))
        self.assertEqual(data["domain"], "grain-aeration")
        self.assertTrue(data["sensors"])
        self.assertTrue(all(fan["interlock_group"] for fan in data["fans"]))
        self.assertTrue(all(item["starts_at"] < item["ends_at"] for item in data["restrictions"]))


if __name__ == "__main__":
    unittest.main()
