from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from src import config
from src.data_access import read_table
from src.mock_service import ensure_mock_api
from src.vendor_client import fetch_vendor_risk

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "evals" / "fixtures" / "data"


class ReferenceDateTests(unittest.TestCase):
    def test_parsed_from_policy(self):
        self.assertEqual(config.reference_date(), date(2026, 9, 30))

    def test_follows_data_dir_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "procurement_policy.md").write_text(
                "**Data snapshot / evaluation reference date:** 2027-01-15\n", encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {"PROCUREMENT_DATA_DIR": tmp}):
                self.assertEqual(config.reference_date(), date(2027, 1, 15))

    def test_machine_clock_never_used(self):
        pattern = re.compile(r"\b(date\.today|datetime\.(now|today|utcnow))\s*\(")
        offenders = [
            str(p.relative_to(ROOT))
            for p in [*ROOT.joinpath("src").rglob("*.py"), ROOT / "app.py"]
            if pattern.search(p.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [], "use src.config.reference_date(), never the machine clock")


class DataAccessTests(unittest.TestCase):
    def test_records_are_json_safe_with_none_for_blanks(self):
        for name in ["employees.csv", "department_budgets.csv", "software_catalog.csv", "vendors.csv", "purchase_history.csv"]:
            json.dumps(read_table(name), allow_nan=False)
        nimbus = next(v for v in read_table("vendors.csv") if v["vendor_name"] == "NimbusAI")
        self.assertIsNone(nimbus["security_review_date"])
        marketing = next(b for b in read_table("department_budgets.csv") if b["department"] == "Marketing")
        self.assertIs(type(marketing["available_usd"]), int)


class FixtureTests(unittest.TestCase):
    """The fixture data dir must be a strict superset of data/: originals untouched."""

    def _csv(self, path: Path) -> list[dict]:
        with path.open(encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))

    def test_fixture_dir_is_superset(self):
        for name in ["employees.csv", "department_budgets.csv", "software_catalog.csv", "vendors.csv", "purchase_history.csv"]:
            original, fixture = self._csv(ROOT / "data" / name), self._csv(FIXTURES / name)
            self.assertEqual(fixture[: len(original)], original, name)
        for name in ["requests.json"]:
            original = json.loads((ROOT / "data" / name).read_text(encoding="utf-8"))
            fixture = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
            self.assertEqual(fixture[: len(original)], original, name)
        original = json.loads((ROOT / "data" / "vendor_risk.json").read_text(encoding="utf-8"))
        fixture = json.loads((FIXTURES / "vendor_risk.json").read_text(encoding="utf-8"))
        for key, value in original.items():
            self.assertEqual(fixture[key], value, key)
        self.assertEqual(
            (ROOT / "data" / "procurement_policy.md").read_text(encoding="utf-8"),
            (FIXTURES / "procurement_policy.md").read_text(encoding="utf-8"),
        )

    def test_gold_cases_are_consistent(self):
        gold = json.loads((ROOT / "evals" / "gold_cases.json").read_text(encoding="utf-8"))
        roles, labels = set(gold["_meta"]["approval_roles"]), set(gold["_meta"]["labels"])
        ids = [c["case_id"] for c in gold["cases"]]
        self.assertEqual(len(ids), len(set(ids)))
        for case in gold["cases"]:
            requests = json.loads((ROOT / case["data_dir"] / "requests.json").read_text(encoding="utf-8"))
            self.assertIn(case["request_id"], {r["request_id"] for r in requests}, case["case_id"])
            exp = case["expected"]
            self.assertLessEqual(set(exp["approvals_exact"]) | set(exp["approvals_optional"]), roles, case["case_id"])
            self.assertLessEqual(set(exp["labels_accepted"]), labels, case["case_id"])
            self.assertFalse(set(exp["flags_must"]) & set(exp["flags_must_not"]), case["case_id"])
            self.assertTrue(exp["human_review_required"])
        public = json.loads((ROOT / "evals" / "public_cases.json").read_text(encoding="utf-8"))
        self.assertLessEqual({c["request_id"] for c in public}, {c["request_id"] for c in gold["cases"]})


class VendorClientTests(unittest.TestCase):
    """Runs against a real uvicorn instance serving a temp data dir."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        shutil.copytree(ROOT / "data", cls.tmp, dirs_exist_ok=True)
        risk = json.loads(Path(cls.tmp, "vendor_risk.json").read_text(encoding="utf-8"))
        record = {"risk_level": "low", "security_review_status": "approved", "last_review_date": "2026-01-01",
                  "processes_personal_data": False, "stores_data_outside_region": False, "notes": "x"}
        for name in ["A/B Labs", "100%Cloud", "Space Name"]:
            risk[name] = record
        Path(cls.tmp, "vendor_risk.json").write_text(json.dumps(risk), encoding="utf-8")
        cls.ctx = ensure_mock_api(Path(cls.tmp))
        cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_ok(self):
        r = fetch_vendor_risk("BrandBoard")
        self.assertEqual((r.status, r.data["security_review_status"]), ("ok", "not_completed"))

    def test_outage_is_unavailable_and_retried(self):
        r = fetch_vendor_risk("NimbusAI", backoff_seconds=0)
        self.assertEqual((r.status, r.reason, r.attempts), ("unavailable", "http_503", 2))

    def test_unknown_vendor_is_not_found_not_outage(self):
        r = fetch_vendor_risk("NoSuchVendor")
        self.assertEqual((r.status, r.attempts), ("not_found", 1))

    def test_special_character_names_survive_double_decoding(self):
        for name in ["A/B Labs", "100%Cloud", "Space Name"]:
            r = fetch_vendor_risk(name)
            self.assertEqual((r.status, r.data["vendor_name"]), ("ok", name), name)

    def test_connection_refused_is_unavailable(self):
        with mock.patch.dict(os.environ, {"VENDOR_RISK_BASE_URL": "http://127.0.0.1:9"}):
            r = fetch_vendor_risk("BrandBoard", backoff_seconds=0)
        self.assertEqual((r.status, r.reason), ("unavailable", "connection_refused"))


class SecretsTests(unittest.TestCase):
    KEY_PATTERNS = re.compile(r"(AIza[0-9A-Za-z_\-]{30,}|gsk_[0-9A-Za-z]{20,}|sk-[0-9A-Za-z_\-]{20,})")

    def test_no_api_keys_in_tracked_or_untracked_files(self):
        files = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        ).stdout.split()
        leaks = []
        for rel in files:
            path = ROOT / rel
            if path.is_file() and path.suffix not in {".pdf", ".png"}:
                if self.KEY_PATTERNS.search(path.read_text(encoding="utf-8", errors="ignore")):
                    leaks.append(rel)
        self.assertEqual(leaks, [])


if __name__ == "__main__":
    unittest.main()
