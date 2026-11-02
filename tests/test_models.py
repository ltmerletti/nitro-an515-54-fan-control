"""
test_models.py - Unit tests for nitrofan.models and profile catalogue validation.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from nitrofan.models import (
    DEFAULT_CATALOG_PATH,
    SUPPORT_STATUS_GUARDED_MAX_TRIAL_ELIGIBLE,
    SUPPORT_STATUS_NEEDS_PROFILE,
    SUPPORT_STATUS_UNSUPPORTED,
    SUPPORT_STATUS_UPSTREAM_ONLY,
    DmiIdentity,
    ModelCatalogError,
    detect_model,
    load_catalog,
    read_dmi_identity,
)


class TestModelCatalog(unittest.TestCase):
    """Test model catalogue loading, schema verification, and immutability."""

    def setUp(self) -> None:
        self.catalog = load_catalog(DEFAULT_CATALOG_PATH)

    def test_default_catalog_loads_all_audited_models(self) -> None:
        expected_models = {
            "AN515-42",
            "AN515-43",
            "AN515-44",
            "AN515-45",
            "AN515-47",
            "AN515-51",
            "AN515-54",
            "AN515-55",
            "AN515-56",
            "AN515-57",
            "AN515-58",
        }
        loaded_keys = set(self.catalog.models.keys())
        self.assertEqual(loaded_keys, expected_models)

    def test_upstream_provenance(self) -> None:
        self.assertEqual(self.catalog.upstream.version, "0.5.4")
        self.assertEqual(
            self.catalog.upstream.commit,
            "9da010d6a2e8351bdf6900d6c3643f1e742af936",
        )
        self.assertEqual(
            self.catalog.upstream.repository,
            "https://github.com/nbfc-linux/nbfc-linux",
        )

    def test_immutability(self) -> None:
        with self.assertRaises((TypeError, AttributeError)):
            self.catalog.models["AN515-99"] = None  # type: ignore[index]

        entry = self.catalog.get("AN515-54")
        self.assertIsNotNone(entry)
        with self.assertRaises((TypeError, AttributeError)):
            entry.support_status = "unsupported"  # type: ignore[misc]

    def test_production_control_is_false_for_all_models(self) -> None:
        for entry in self.catalog.list_models():
            self.assertFalse(
                entry.capabilities.production_control,
                f"Model {entry.model_id} must have production_control=False",
            )

    def test_an515_54_evidence_and_support_distinction(self) -> None:
        entry = self.catalog.get("AN515-54")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.support_status, SUPPORT_STATUS_GUARDED_MAX_TRIAL_ELIGIBLE)
        self.assertTrue(entry.capabilities.guarded_max_trial)
        self.assertIsNotNone(entry.evidence)
        self.assertEqual(entry.evidence.tested_bios, ("V1.33",))
        self.assertEqual(entry.evidence.fan_max_rpm, 6122)
        self.assertTrue(entry.evidence.baseline_restored)
        self.assertTrue(entry.evidence.write_support_restored)
        self.assertEqual(
            entry.upstream_profile.sha256,
            "b04e2edc839169f7342dbbe8114f8ca66dee03dbd02d5685380b448d3161a568",
        )

    def test_needs_profile_distinction_for_55_and_56(self) -> None:
        for model_id in ("AN515-55", "AN515-56"):
            entry = self.catalog.get(model_id)
            self.assertIsNotNone(entry)
            self.assertEqual(entry.support_status, SUPPORT_STATUS_NEEDS_PROFILE)
            self.assertIsNone(entry.upstream_profile)
            self.assertFalse(entry.capabilities.guarded_max_trial)
            self.assertFalse(entry.capabilities.production_control)
            self.assertIn("No profile exists in", entry.next_steps)
            self.assertIn("alias", entry.next_steps.lower())

    def test_upstream_only_models(self) -> None:
        upstream_models = (
            "AN515-42",
            "AN515-43",
            "AN515-44",
            "AN515-45",
            "AN515-47",
            "AN515-51",
            "AN515-57",
            "AN515-58",
        )
        for model_id in upstream_models:
            entry = self.catalog.get(model_id)
            self.assertIsNotNone(entry)
            self.assertEqual(entry.support_status, SUPPORT_STATUS_UPSTREAM_ONLY)
            self.assertIsNotNone(entry.upstream_profile)
            self.assertFalse(entry.capabilities.guarded_max_trial)
            self.assertFalse(entry.capabilities.production_control)


class TestCatalogValidationErrors(unittest.TestCase):
    """Test rejection of malformed or invalid catalogues."""

    def _load_raw(self, data: dict) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
            json.dump(data, f)
            f.flush()
            load_catalog(f.name)

    def test_missing_file_raises_error(self) -> None:
        with self.assertRaises(ModelCatalogError) as ctx:
            load_catalog("/path/that/definitely/does/not/exist.json")
        self.assertIn("not found", str(ctx.exception))

    def test_duplicate_json_keys_rejected(self) -> None:
        duplicate_json = '{"version": 1, "version": 2, "upstream": {}, "models": []}'
        with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
            f.write(duplicate_json)
            f.flush()
            with self.assertRaises(ModelCatalogError) as ctx:
                load_catalog(f.name)
            self.assertIn("Duplicate JSON key", str(ctx.exception))

    def test_production_control_true_rejected(self) -> None:
        base = json.loads(DEFAULT_CATALOG_PATH.read_text())
        base["models"][0]["capabilities"]["production_control"] = True
        with self.assertRaises(ModelCatalogError) as ctx:
            self._load_raw(base)
        self.assertIn("production control is not authorized", str(ctx.exception))

    def test_duplicate_model_id_rejected(self) -> None:
        base = json.loads(DEFAULT_CATALOG_PATH.read_text())
        dup = dict(base["models"][0])
        base["models"].append(dup)
        with self.assertRaises(ModelCatalogError) as ctx:
            self._load_raw(base)
        self.assertIn("Duplicate model_id", str(ctx.exception))

    def test_duplicate_product_name_rejected(self) -> None:
        base = json.loads(DEFAULT_CATALOG_PATH.read_text())
        dup = dict(base["models"][0])
        dup["model_id"] = "AN515-42-CUSTOM"
        base["models"].append(dup)
        with self.assertRaises(ModelCatalogError) as ctx:
            self._load_raw(base)
        self.assertIn("collides with", str(ctx.exception))

    def test_path_traversal_in_upstream_profile_rejected(self) -> None:
        base = json.loads(DEFAULT_CATALOG_PATH.read_text())
        base["models"][0]["upstream_profile"]["path"] = "../../share/nbfc/configs/Acer.json"
        with self.assertRaises(ModelCatalogError) as ctx:
            self._load_raw(base)
        self.assertIn("Directory traversal not allowed", str(ctx.exception))

    def test_absolute_path_in_local_artifacts_rejected(self) -> None:
        base = json.loads(DEFAULT_CATALOG_PATH.read_text())
        for model in base["models"]:
            if model["model_id"] == "AN515-54":
                model["local_artifacts"]["policy_path"] = "/etc/nitrofan/policy.json"
        with self.assertRaises(ModelCatalogError) as ctx:
            self._load_raw(base)
        self.assertIn("Absolute path not allowed", str(ctx.exception))


class TestExactDmiMatching(unittest.TestCase):
    """Test exact DMI matching, near-miss rejections, and BIOS authorization."""

    def setUp(self) -> None:
        self.catalog = load_catalog(DEFAULT_CATALOG_PATH)

    def test_an515_54_v133_exact_match_authorizes_guarded_trial(self) -> None:
        result = self.catalog.match_dmi("Acer", "Nitro AN515-54", "V1.33")
        self.assertTrue(result.matched)
        self.assertEqual(result.model_id, "AN515-54")
        self.assertEqual(result.support_status, SUPPORT_STATUS_GUARDED_MAX_TRIAL_ELIGIBLE)
        self.assertTrue(result.capabilities.guarded_max_trial)
        self.assertFalse(result.capabilities.production_control)
        self.assertTrue(result.capabilities.read_only_monitoring)
        self.assertTrue(result.bios_matched)

    def test_an515_54_unverified_bios_denies_trial_writes(self) -> None:
        # BIOS V1.30 or V1.34 has not been validated; must fail closed for writes.
        for unverified_bios in ("V1.30", "V1.34", "V1.00"):
            result = self.catalog.match_dmi("Acer", "Nitro AN515-54", unverified_bios)
            self.assertTrue(result.matched)
            self.assertEqual(result.model_id, "AN515-54")
            self.assertEqual(result.support_status, SUPPORT_STATUS_UPSTREAM_ONLY)
            self.assertFalse(result.capabilities.guarded_max_trial)
            self.assertFalse(result.capabilities.production_control)
            self.assertTrue(result.capabilities.read_only_monitoring)
            self.assertFalse(result.bios_matched)
            self.assertIn("lacks verified evidence", result.next_steps)

    def test_an515_54_missing_bios_denies_trial_writes(self) -> None:
        result = self.catalog.match_dmi("Acer", "Nitro AN515-54", None)
        self.assertTrue(result.matched)
        self.assertFalse(result.capabilities.guarded_max_trial)
        self.assertFalse(result.bios_matched)

    def test_near_miss_and_suffix_rejection(self) -> None:
        near_misses = (
            "Nitro AN515-540",
            "Nitro AN515-54A",
            "Nitro AN515-54-PRO",
            "Nitro AN515-5",
            "AN515-54",  # Missing "Nitro " prefix
            "Nitro AN515-541",
        )
        for product in near_misses:
            result = self.catalog.match_dmi("Acer", product, "V1.33")
            self.assertFalse(result.matched, f"Near-miss '{product}' must not match")
            self.assertIsNone(result.model_id)
            self.assertEqual(result.support_status, SUPPORT_STATUS_UNSUPPORTED)
            self.assertFalse(result.capabilities.guarded_max_trial)
            self.assertTrue(result.capabilities.read_only_monitoring)

    def test_wrong_vendor_rejection(self) -> None:
        for vendor in ("Asus", "Dell", "HP", "Lenovo", "ACER"):
            result = self.catalog.match_dmi(vendor, "Nitro AN515-54", "V1.33")
            self.assertFalse(result.matched, f"Vendor '{vendor}' must not match")
            self.assertFalse(result.capabilities.guarded_max_trial)

    def test_needs_profile_models_cannot_authorize_trial(self) -> None:
        for model in ("Nitro AN515-55", "Nitro AN515-56"):
            result = self.catalog.match_dmi("Acer", model, "V1.00")
            self.assertTrue(result.matched)
            self.assertEqual(result.support_status, SUPPORT_STATUS_NEEDS_PROFILE)
            self.assertFalse(result.capabilities.guarded_max_trial)
            self.assertFalse(result.capabilities.production_control)
            self.assertTrue(result.capabilities.read_only_monitoring)

    def test_upstream_only_models_cannot_authorize_trial(self) -> None:
        for model in ("Nitro AN515-42", "Nitro AN515-58"):
            result = self.catalog.match_dmi("Acer", model, "V1.00")
            self.assertTrue(result.matched)
            self.assertEqual(result.support_status, SUPPORT_STATUS_UPSTREAM_ONLY)
            self.assertFalse(result.capabilities.guarded_max_trial)
            self.assertFalse(result.capabilities.production_control)

    def test_missing_dmi_identifiers(self) -> None:
        r1 = self.catalog.match_dmi(None, "Nitro AN515-54", "V1.33")
        self.assertFalse(r1.matched)
        self.assertFalse(r1.capabilities.guarded_max_trial)

        r2 = self.catalog.match_dmi("Acer", None, "V1.33")
        self.assertFalse(r2.matched)
        self.assertFalse(r2.capabilities.guarded_max_trial)


class TestDmiSysfsDetection(unittest.TestCase):
    """Test read_dmi_identity and detect_model with synthetic sysfs directories."""

    def test_read_dmi_identity_from_synthetic_sysfs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dmi_dir = Path(td)
            (dmi_dir / "sys_vendor").write_text("Acer\n")
            (dmi_dir / "product_name").write_text("Nitro AN515-54\n")
            (dmi_dir / "bios_version").write_text("V1.33\n")

            identity = read_dmi_identity(dmi_dir)
            self.assertEqual(identity.sys_vendor, "Acer")
            self.assertEqual(identity.product_name, "Nitro AN515-54")
            self.assertEqual(identity.bios_version, "V1.33")

    def test_detect_model_synthetic_success(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dmi_dir = Path(td)
            (dmi_dir / "sys_vendor").write_text("Acer\n")
            (dmi_dir / "product_name").write_text("Nitro AN515-54\n")
            (dmi_dir / "bios_version").write_text("V1.33\n")

            match = detect_model(dmi_root=dmi_dir)
            self.assertTrue(match.matched)
            self.assertEqual(match.model_id, "AN515-54")
            self.assertTrue(match.capabilities.guarded_max_trial)

    def test_detect_model_missing_dmi_dir(self) -> None:
        non_existent = Path("/path/that/does/not/exist")
        match = detect_model(dmi_root=non_existent)
        self.assertFalse(match.matched)
        self.assertEqual(match.support_status, SUPPORT_STATUS_UNSUPPORTED)
        self.assertFalse(match.capabilities.guarded_max_trial)
        self.assertTrue(match.capabilities.read_only_monitoring)


if __name__ == "__main__":
    unittest.main()
