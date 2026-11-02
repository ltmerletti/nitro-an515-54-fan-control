"""Regression checks for model authorization and profile selection."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from nitrofan import models
from tools import nbfc_guarded_trial as trial


class TestProfileValidationReview(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(models.DEFAULT_CATALOG_PATH.read_text())
        self.position = next(i for i, m in enumerate(self.data["models"]) if m["model_id"] == "AN515-54")

    def load(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps(data))
            return models.load_catalog(path)

    def test_capabilities_and_restoration_flags_are_boolean_not_truthy(self):
        for section, key in (("capabilities", "guarded_max_trial"),
                             ("capabilities", "read_only_monitoring"),
                             ("evidence", "baseline_restored"),
                             ("evidence", "write_support_restored")):
            for invalid in ("false", "true", 0, 1, [], None):
                with self.subTest(section=section, key=key, value=invalid):
                    data = copy.deepcopy(self.data)
                    data["models"][self.position][section][key] = invalid
                    with self.assertRaises(models.ModelCatalogError):
                        self.load(data)

    def test_unknown_or_boolean_catalogue_version_is_rejected(self):
        for version in (True, False, 0, 2, 1.0, "1"):
            with self.subTest(version=version):
                data = copy.deepcopy(self.data)
                data["version"] = version
                with self.assertRaises(models.ModelCatalogError):
                    self.load(data)

    def test_unknown_fields_and_non_finite_numbers_are_rejected(self):
        for value in ("typo", float("nan"), float("inf")):
            data = copy.deepcopy(self.data)
            data["unexpected"] = value
            with self.assertRaises(models.ModelCatalogError):
                self.load(data)
        data = copy.deepcopy(self.data)
        data["models"][self.position]["capabilities"]["guarded_max_trail"] = True
        with self.assertRaises(models.ModelCatalogError):
            self.load(data)

    def test_trial_status_requires_artifacts_provenance_and_restoration_evidence(self):
        for remove in ("local_artifacts", "evidence", "upstream_profile"):
            data = copy.deepcopy(self.data)
            del data["models"][self.position][remove]
            with self.subTest(remove=remove), self.assertRaises(models.ModelCatalogError):
                self.load(data)
        for key, invalid in (("tested_bios", []), ("baseline_restored", False),
                             ("write_support_restored", False), ("source_paths", []),
                             ("fan_max_rpm", True), ("tested_date", "2026-02-30"),
                             ("notes", [])):
            data = copy.deepcopy(self.data)
            data["models"][self.position]["evidence"][key] = invalid
            with self.subTest(key=key), self.assertRaises(models.ModelCatalogError):
                self.load(data)

    def test_non_file_paths_and_traversal_spellings_are_rejected(self):
        for path in (".", "x/./policy.json", "x//policy.json", "x/../policy.json",
                     "x\\policy.json", " x/policy.json", "x/policy.json\0"):
            data = copy.deepcopy(self.data)
            data["models"][self.position]["local_artifacts"]["policy_path"] = path
            with self.subTest(path=path), self.assertRaises(models.ModelCatalogError):
                self.load(data)

    def test_repository_symlink_cannot_escape_for_local_artifacts(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            (root / "escape").symlink_to(outside, target_is_directory=True)
            data = copy.deepcopy(self.data)
            data["models"][self.position]["local_artifacts"]["policy_path"] = "escape/policy.json"
            with patch.object(models, "REPO_ROOT", root):
                with self.assertRaises(models.ModelCatalogError):
                    self.load(data)

    def test_catalogue_provenance_matches_the_audited_git_tree(self):
        root = models.REPO_ROOT
        audit = json.loads((root / "docs/research/nbfc-models-0.5.4.json").read_text())
        catalog = models.load_catalog()
        self.assertEqual(catalog.upstream.commit, audit["commit"])
        for profile in audit["profiles"]:
            entry = catalog.get(profile["model"])
            self.assertEqual(entry.upstream_profile.path, profile["path"])
            self.assertEqual(entry.upstream_profile.sha256, profile["sha256"])
        for source in catalog.get("AN515-54").evidence.source_paths:
            self.assertTrue((root / source).is_file())

    def test_new_model_selects_its_own_artifacts_without_a_model_code_branch(self):
        data = copy.deepcopy(self.data)
        future = copy.deepcopy(data["models"][self.position])
        future.update(model_id="AN515-99", product_name="Nitro AN515-99")
        future["upstream_profile"]["path"] = "share/nbfc/configs/Acer Nitro AN515-99.json"
        future["local_artifacts"] = {"policy_path": "new/policy.json", "guarded_profile_path": "new/max.json"}
        data["models"].append(future)
        catalog = self.load(data)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "new").mkdir()
            for path in future["local_artifacts"].values():
                (root / path).write_text("{}")
            dmi = root / "dmi"
            dmi.mkdir()
            for name, value in (("sys_vendor", "Acer"), ("product_name", "Nitro AN515-99"), ("bios_version", "V1.33")):
                (dmi / name).write_text(value)
            supervisor = trial.GuardedTrialSupervisor(synthetic_root=root, dmi_root=dmi)
            with patch.object(models, "load_catalog", return_value=catalog), patch.object(trial, "REPO_ROOT", root):
                result = supervisor._verify_hardware_identity()
            self.assertEqual(result.model_id, "AN515-99")
            self.assertEqual(supervisor.policy_path, (root / "new/policy.json").resolve())
            self.assertEqual(supervisor.profile_path, (root / "new/max.json").resolve())
            self.assertIsNone(supervisor.guard)

    def test_real_preflight_cannot_use_an_injected_dmi_identity(self):
        supervisor = trial.GuardedTrialSupervisor(dmi_root="/tmp/fake-dmi")
        with patch.object(trial.os, "geteuid", return_value=0), patch.object(supervisor, "_attest_build") as attest:
            with self.assertRaisesRegex(ValueError, "system DMI"):
                supervisor.preflight_checks()
            attest.assert_not_called()
        self.assertIsNone(supervisor.guard)

    def test_explicit_profile_cannot_override_the_selected_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dmi = root / "dmi"
            dmi.mkdir()
            for name, value in (("sys_vendor", "Acer"), ("product_name", "Nitro AN515-54"), ("bios_version", "V1.33")):
                (dmi / name).write_text(value)
            supervisor = trial.GuardedTrialSupervisor(synthetic_root=root, dmi_root=dmi,
                                                       profile_path=root / "other.json")
            with self.assertRaisesRegex(ValueError, "differs from the selected model"):
                supervisor._verify_hardware_identity()

    def test_trial_imports_model_code_when_started_outside_repository(self):
        script = str(models.REPO_ROOT / "tools/nbfc_guarded_trial.py")
        code = f"import runpy; runpy.run_path({script!r}, run_name='review_import'); from nitrofan.models import load_catalog; print(len(load_catalog().models))"
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-c", code], cwd=directory,
                                    env=environment, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "11")


if __name__ == "__main__":
    unittest.main()
