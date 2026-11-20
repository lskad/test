import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import experiment
from src import core


class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.tasks = core.prepare(cls.root, 42)
        cls.task = next(t for t in cls.tasks if t["station"] == "A" and t["month"] == "2025-09")

    def test_distribution_and_calibration_example(self):
        test = [t for t in self.tasks if t["split"] == "test"]
        self.assertEqual((len(self.tasks), len(test), sum(t["collision"] for t in test)), (30, 24, 6))
        for t in self.tasks:
            self.assertEqual(core.parse_request(t["request"]), {"station": t["station"], "month": t["month"]})
        receipt = core.query(self.root, self.task, "A", "2025-08", "example")
        self.assertEqual((receipt["total"], receipt["bad"]), (80, 2))
        self.assertEqual((self.task["total"], self.task["bad"]), (100, 12))

    def test_same_symptom_different_source(self):
        a, _ = core.simulate(self.root, self.task, "F2", "one")
        b, _ = core.simulate(self.root, self.task, "F3", "two")
        for level in ("V0", "V1"):
            self.assertEqual(experiment.visible(a, level), experiment.visible(b, level))
        for trace, expected in [(a, "S2"), (b, "S3")]:
            evidence = experiment.visible(trace, "V2")
            self.assertEqual(experiment.rule(evidence)["label"], expected)
            self.assertEqual(experiment.rule(experiment.visible(trace, "V1"))["label"], "UNKNOWN")
            self.assertNotIn("receipt_id", evidence["report"])

    def test_replay_from_independent_checkpoints(self):
        for fault, expected in [("F2", ["S2", "S3"]), ("F3", ["S3"])]:
            _, checkpoints = core.simulate(self.root, self.task, fault, "r")
            frozen = json.dumps(checkpoints, sort_keys=True)
            recovered = []
            for step in experiment.STEPS:
                for arm in ("original", "replace"):
                    run = core.replay(self.root, self.task, fault, "r", checkpoints, step, arm)
                    verdict = core.validate(self.task, run["report"], [run["receipt"]])
                    if arm == "original":
                        self.assertFalse(verdict["evidence"])
                    elif verdict["evidence"]:
                        recovered.append(step)
                    elif step == "S4":
                        self.assertEqual(verdict["status"], "stopped")
            self.assertEqual(recovered, expected)
            self.assertEqual(json.dumps(checkpoints, sort_keys=True), frozen)

    def test_receipt_required_even_with_correct_numbers(self):
        t = next(t for t in self.tasks if t["collision"])
        run, _ = core.simulate(self.root, t, "F3", "r")
        result = core.validate(t, run["report"], [run["receipt"]])
        self.assertTrue(result["numeric"])
        self.assertFalse(result["evidence"])
        clean, _ = core.simulate(self.root, t, "F0", "elsewhere")
        self.assertFalse(core.validate(t, clean["report"], [run["receipt"]])["evidence"])


if __name__ == "__main__":
    unittest.main()
