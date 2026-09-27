from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "deploy" / "paddleocr" / "real_book_benchmark.py"
MODULE_DIR = str(MODULE_PATH.parent)
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)
SPEC = importlib.util.spec_from_file_location("real_book_benchmark", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TruthinessForbiddenVector:
    def __init__(self, values: list[object]) -> None:
        self.values = values

    def __iter__(self):
        return iter(self.values)

    def __bool__(self) -> bool:
        raise ValueError("array truth value is ambiguous")


class FlattenPredictionTests(unittest.TestCase):
    def test_numpy_like_vectors_are_not_boolean_coerced(self) -> None:
        result = MODULE.flatten_prediction(
            {
                "rec_texts": TruthinessForbiddenVector(["甲", "乙"]),
                "rec_scores": TruthinessForbiddenVector([0.9, 0.8]),
                "rec_boxes": TruthinessForbiddenVector([[0, 0, 1, 1], [1, 1, 2, 2]]),
            }
        )

        self.assertEqual(result["rec_texts"], ["甲", "乙"])
        self.assertEqual(result["rec_scores"], [0.9, 0.8])
        self.assertEqual(len(result["rec_boxes"]), 2)

    def test_selected_matrix_contains_only_baseline_and_winner(self) -> None:
        configs = MODULE.config_matrix("selected")

        self.assertEqual(
            [config["name"] for config in configs],
            [
                "baseline-server-det32-server-rec32-b16",
                "server-det32-server-rec16-b16",
            ],
        )


if __name__ == "__main__":
    unittest.main()
