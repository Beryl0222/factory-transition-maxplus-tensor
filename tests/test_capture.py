"""核对合成采集的交换记录与原始尺寸。"""
import json
from pathlib import Path
import unittest

import numpy as np

from caseio import load_case


class CaptureTests(unittest.TestCase):
    def setUp(self):
        data = Path(__file__).resolve().parents[1] / "data"
        self.records, self.tensors = load_case(data / "capture.json")
        self.capacity = json.loads((data / "capacity.json").read_text(encoding="utf-8"))

    def test_capture_contract(self):
        r, a = self.records, self.tensors
        for segment in r["segments"].values():
            for edge in segment["edge"]:
                self.assertIn(edge[1], segment["input"])
                self.assertIn(edge[2], segment["output"])
        self.assertNotEqual(r["revisions"][1]["input"], r["segments"]["C"]["input"])
        self.assertEqual(r["queries"][0]["range"], ["A", "D"])
