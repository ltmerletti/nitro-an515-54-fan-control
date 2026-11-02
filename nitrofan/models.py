"""
models.py - Model catalog and exact DMI identification for NitroFan.

Loads profiles/catalog.json, enforces schema integrity and immutability,
and provides exact DMI matching against sysfs.

Guarantees:
- Strict JSON parsing rejecting duplicate keys.
- Frozen, immutable records across all catalog entries.
- Exact DMI vendor and product matching with zero fuzzy or prefix tolerance.
- Only Acer Nitro AN515-54 on verified BIOS V1.33 is eligible for guarded trial writes.
- Models AN515-55 and AN515-56 report needs_profile without aliasing.
- Production control capability remains false for all models.
- Pure read-only inspection; never writes to sysfs or EC.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG_PATH = REPO_ROOT / "profiles" / "catalog.json"
DEFAULT_DMI_ROOT = Path("/sys/class/dmi/id")

SUPPORT_STATUS_GUARDED_MAX_TRIAL_ELIGIBLE = "guarded_max_trial_eligible"
SUPPORT_STATUS_UPSTREAM_ONLY = "upstream_only"
SUPPORT_STATUS_NEEDS_PROFILE = "needs_profile"
SUPPORT_STATUS_UNSUPPORTED = "unsupported"

VALID_SUPPORT_STATUSES = (
    SUPPORT_STATUS_GUARDED_MAX_TRIAL_ELIGIBLE,
    SUPPORT_STATUS_UPSTREAM_ONLY,
    SUPPORT_STATUS_NEEDS_PROFILE,
    SUPPORT_STATUS_UNSUPPORTED,
)


class ModelCatalogError(Exception):
    """Raised when model catalogue format or metadata is invalid."""


@dataclass(frozen=True)
class Capabilities:
    """Per-capability support flags for a model."""

    read_only_monitoring: bool
    guarded_max_trial: bool
    production_control: bool


@dataclass(frozen=True)
class UpstreamProfile:
    """Provenance and SHA-256 for an upstream NBFC profile."""

    path: str
    sha256: str


@dataclass(frozen=True)
class LocalArtifacts:
    """Repository paths for guarded trial policy and profile artifacts."""

    policy_path: str
    guarded_profile_path: str


@dataclass(frozen=True)
class EvidenceRecord:
    """Recorded hardware evidence for guarded trial validation."""

    tested_bios: tuple[str, ...]
    tested_date: str | None
    trial_type: str | None
    fan_max_rpm: int | None
    baseline_restored: bool
    write_support_restored: bool
    notes: str | None
    source_paths: tuple[str, ...]


@dataclass(frozen=True)
class ModelEntry:
    """Validated catalog entry for one notebook model."""

    model_id: str
    vendor: str
    product_name: str
    aliases: tuple[str, ...]
    support_status: str
    upstream_profile: UpstreamProfile | None
    local_artifacts: LocalArtifacts | None
    evidence: EvidenceRecord | None
    capabilities: Capabilities
    next_steps: str


@dataclass(frozen=True)
class UpstreamProvenance:
    """Audited upstream NBFC repository commit metadata."""

    repository: str
    version: str
    commit: str


@dataclass(frozen=True)
class DmiIdentity:
    """Raw DMI identifiers read from sysfs."""

    sys_vendor: str | None
    product_name: str | None
    bios_version: str | None


@dataclass(frozen=True)
class MatchResult:
    """Outcome of DMI matching against the model catalog."""

    matched: bool
    model_id: str | None
    support_status: str
    capabilities: Capabilities
    model_entry: ModelEntry | None
    identity: DmiIdentity
    bios_matched: bool
    next_steps: str
    message: str


def _validate_path_safety(raw_path: str, context: str) -> None:
    """Ensure a relative path does not escape or contain directory traversal."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ModelCatalogError(f"Empty path in {context}")
    normalized = Path(raw_path)
    if normalized.is_absolute():
        raise ModelCatalogError(f"Absolute path not allowed in {context}: {raw_path}")
    if raw_path != raw_path.strip() or "\\" in raw_path or "\x00" in raw_path:
        raise ModelCatalogError(f"Invalid path in {context}: {raw_path!r}")
    for part in raw_path.split("/"):
        if part in ("..", ".", ""):
            raise ModelCatalogError(f"Directory traversal not allowed in {context}: {raw_path}")
    if not (REPO_ROOT / raw_path).resolve().is_relative_to(REPO_ROOT.resolve()):
        raise ModelCatalogError(f"Path escapes the repository in {context}: {raw_path}")


def _check_fields(data: dict, required: set[str], context: str,
                  optional: set[str] = frozenset()) -> None:
    if missing := required - data.keys():
        raise ModelCatalogError(f"Missing fields in {context}: {', '.join(sorted(missing))}")
    if unknown := data.keys() - required - optional:
        raise ModelCatalogError(f"Unknown fields in {context}: {', '.join(sorted(unknown))}")


def _text(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ModelCatalogError(f"Expected a non-empty string without surrounding whitespace in {context}")
    return value


def _boolean(value: Any, context: str) -> bool:
    if type(value) is not bool:
        raise ModelCatalogError(f"Expected a JSON boolean in {context}")
    return value


def _strict_json_loads(text: str, source_description: str) -> dict[str, Any]:
    """Parse JSON while rejecting duplicate object keys."""

    def object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ModelCatalogError(f"Duplicate JSON key in {source_description}: {key}")
            result[key] = value
        return result

    try:
        def reject_constant(value):
            raise ModelCatalogError(f"Non-finite JSON number in {source_description}: {value}")
        data = json.loads(text, object_pairs_hook=object_pairs_hook,
                          parse_constant=reject_constant)
    except json.JSONDecodeError as error:
        raise ModelCatalogError(f"Malformed JSON in {source_description}: {error}") from error

    if not isinstance(data, dict):
        raise ModelCatalogError(f"Root of {source_description} must be a JSON object")
    return data


@dataclass(frozen=True)
class ModelCatalog:
    """Immutable model catalog with strict validation and DMI matching."""

    version: int
    upstream: UpstreamProvenance
    models: MappingProxyType[str, ModelEntry]

    def get(self, model_id: str) -> ModelEntry | None:
        return self.models.get(model_id)

    def list_models(self) -> tuple[ModelEntry, ...]:
        return tuple(self.models.values())

    def match_dmi(
        self,
        sys_vendor: str | None,
        product_name: str | None,
        bios_version: str | None = None,
    ) -> MatchResult:
        """Perform exact DMI matching.

        Zero prefix or fuzzy matching is allowed. Unknown models fail closed.
        """
        identity = DmiIdentity(
            sys_vendor=sys_vendor.strip() if sys_vendor else None,
            product_name=product_name.strip() if product_name else None,
            bios_version=bios_version.strip() if bios_version else None,
        )

        default_unmatched_caps = Capabilities(
            read_only_monitoring=True,
            guarded_max_trial=False,
            production_control=False,
        )

        if not identity.sys_vendor or not identity.product_name:
            return MatchResult(
                matched=False,
                model_id=None,
                support_status=SUPPORT_STATUS_UNSUPPORTED,
                capabilities=default_unmatched_caps,
                model_entry=None,
                identity=identity,
                bios_matched=False,
                next_steps="Incomplete DMI information. Read-only monitoring available.",
                message="DMI sys_vendor or product_name is missing or empty.",
            )

        matched_entry: ModelEntry | None = None
        for entry in self.models.values():
            if entry.vendor != identity.sys_vendor:
                continue
            if identity.product_name == entry.product_name or identity.product_name in entry.aliases:
                matched_entry = entry
                break

        if matched_entry is None:
            return MatchResult(
                matched=False,
                model_id=None,
                support_status=SUPPORT_STATUS_UNSUPPORTED,
                capabilities=default_unmatched_caps,
                model_entry=None,
                identity=identity,
                bios_matched=False,
                next_steps="Hardware model not in catalogue. Read-only inspection available; trial writes disabled.",
                message=(
                    f"No matching profile in catalog for {identity.sys_vendor} {identity.product_name}."
                ),
            )

        if matched_entry.support_status == SUPPORT_STATUS_NEEDS_PROFILE:
            return MatchResult(
                matched=True,
                model_id=matched_entry.model_id,
                support_status=SUPPORT_STATUS_NEEDS_PROFILE,
                capabilities=Capabilities(
                    read_only_monitoring=True,
                    guarded_max_trial=False,
                    production_control=False,
                ),
                model_entry=matched_entry,
                identity=identity,
                bios_matched=False,
                next_steps=matched_entry.next_steps,
                message=(
                    f"Model {matched_entry.model_id} recognized but requires an upstream profile. "
                    "Aliasing to other models is forbidden."
                ),
            )

        if matched_entry.support_status == SUPPORT_STATUS_UPSTREAM_ONLY:
            return MatchResult(
                matched=True,
                model_id=matched_entry.model_id,
                support_status=SUPPORT_STATUS_UPSTREAM_ONLY,
                capabilities=Capabilities(
                    read_only_monitoring=True,
                    guarded_max_trial=False,
                    production_control=False,
                ),
                model_entry=matched_entry,
                identity=identity,
                bios_matched=False,
                next_steps=matched_entry.next_steps,
                message=(
                    f"Model {matched_entry.model_id} has an upstream profile but lacks local hardware "
                    "validation. Trial writes disabled."
                ),
            )

        if matched_entry.support_status == SUPPORT_STATUS_GUARDED_MAX_TRIAL_ELIGIBLE:
            tested_bios_list = (
                matched_entry.evidence.tested_bios
                if matched_entry.evidence and matched_entry.evidence.tested_bios
                else ()
            )

            if identity.bios_version and identity.bios_version in tested_bios_list:
                return MatchResult(
                    matched=True,
                    model_id=matched_entry.model_id,
                    support_status=SUPPORT_STATUS_GUARDED_MAX_TRIAL_ELIGIBLE,
                    capabilities=matched_entry.capabilities,
                    model_entry=matched_entry,
                    identity=identity,
                    bios_matched=True,
                    next_steps=matched_entry.next_steps,
                    message=(
                        f"Model {matched_entry.model_id} on BIOS {identity.bios_version} is eligible "
                        "for guarded maximum trial."
                    ),
                )

            # BIOS mismatch or unknown BIOS: fail closed for trial writes.
            tested_str = ", ".join(tested_bios_list) if tested_bios_list else "none"
            next_steps = (
                f"BIOS {identity.bios_version or 'unknown'} lacks verified evidence. "
                f"Recorded evidence exists only for BIOS {tested_str}. Trial writes disabled."
            )
            return MatchResult(
                matched=True,
                model_id=matched_entry.model_id,
                support_status=SUPPORT_STATUS_UPSTREAM_ONLY,
                capabilities=Capabilities(
                    read_only_monitoring=True,
                    guarded_max_trial=False,
                    production_control=False,
                ),
                model_entry=matched_entry,
                identity=identity,
                bios_matched=False,
                next_steps=next_steps,
                message=(
                    f"Model {matched_entry.model_id} recognized, but BIOS {identity.bios_version or 'unknown'} "
                    "is unverified for trial writes."
                ),
            )

        return MatchResult(
            matched=True,
            model_id=matched_entry.model_id,
            support_status=SUPPORT_STATUS_UNSUPPORTED,
            capabilities=default_unmatched_caps,
            model_entry=matched_entry,
            identity=identity,
            bios_matched=False,
            next_steps=matched_entry.next_steps,
            message=f"Model {matched_entry.model_id} support status is {matched_entry.support_status}.",
        )


def load_catalog(path: str | Path | None = None) -> ModelCatalog:
    """Load and validate the model catalogue.

    Enforces unique model IDs, unique product names and aliases, path safety,
    and valid support statuses.
    """
    catalog_path = Path(path) if path is not None else DEFAULT_CATALOG_PATH
    if not catalog_path.is_file():
        raise ModelCatalogError(f"Model catalogue file not found: {catalog_path}")

    try:
        text = catalog_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ModelCatalogError(f"Cannot read catalogue {catalog_path}: {error}") from error
    data = _strict_json_loads(text, str(catalog_path))
    _check_fields(data, {"version", "upstream", "models"}, "catalogue", {"$schema"})
    if "$schema" in data:
        _text(data["$schema"], "$schema")

    version = data.get("version")
    if type(version) is not int or version != 1:
        raise ModelCatalogError("Catalogue version must be integer 1")

    upstream_data = data.get("upstream")
    if not isinstance(upstream_data, dict):
        raise ModelCatalogError("Catalogue missing 'upstream' provenance object")
    _check_fields(upstream_data, {"repository", "version", "commit"}, "upstream")
    for field in ("repository", "version", "commit"):
        _text(upstream_data[field], f"upstream.{field}")
    repository = urlparse(upstream_data["repository"])
    if repository.scheme != "https" or not repository.netloc:
        raise ModelCatalogError("Upstream repository must be an HTTPS URL")
    if not re.fullmatch(r"[0-9a-f]{40}", upstream_data["commit"]):
        raise ModelCatalogError("Upstream commit must be 40 lowercase hex characters")

    upstream = UpstreamProvenance(
        repository=upstream_data["repository"],
        version=upstream_data["version"],
        commit=upstream_data["commit"],
    )

    raw_models = data.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        raise ModelCatalogError("Catalogue 'models' must be a non-empty list")

    models: dict[str, ModelEntry] = {}
    known_products: dict[str, str] = {}

    for idx, raw_model in enumerate(raw_models):
        if not isinstance(raw_model, dict):
            raise ModelCatalogError(f"Model entry {idx} is not an object")
        _check_fields(raw_model, {"model_id", "vendor", "product_name", "aliases",
                                 "support_status", "capabilities", "next_steps"},
                      f"model entry {idx}", {"upstream_profile", "local_artifacts", "evidence"})

        model_id = raw_model.get("model_id")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ModelCatalogError(f"Model entry {idx} missing valid model_id")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", model_id):
            raise ModelCatalogError(f"Invalid model_id: {model_id!r}")
        if model_id in models:
            raise ModelCatalogError(f"Duplicate model_id in catalogue: {model_id}")

        vendor = raw_model.get("vendor")
        _text(vendor, f"model {model_id} vendor")

        product_name = raw_model.get("product_name")
        _text(product_name, f"model {model_id} product_name")

        raw_aliases = raw_model.get("aliases", [])
        if not isinstance(raw_aliases, list) or not all(isinstance(a, str) for a in raw_aliases):
            raise ModelCatalogError(f"Model {model_id} aliases must be a list of strings")
        aliases = tuple(raw_aliases)
        for alias in aliases:
            _text(alias, f"model {model_id} alias")

        all_names = [product_name] + list(aliases)
        for name in all_names:
            if name in known_products:
                raise ModelCatalogError(
                    f"Product identifier '{name}' in model {model_id} collides with {known_products[name]}"
                )
            known_products[name] = model_id

        support_status = raw_model.get("support_status")
        if support_status not in VALID_SUPPORT_STATUSES:
            raise ModelCatalogError(f"Model {model_id} has invalid support_status: {support_status}")

        caps_data = raw_model.get("capabilities")
        if not isinstance(caps_data, dict):
            raise ModelCatalogError(f"Model {model_id} missing capabilities")
        _check_fields(caps_data, {"read_only_monitoring", "guarded_max_trial", "production_control"},
                      f"model {model_id} capabilities")
        prod_ctrl = caps_data.get("production_control")
        if prod_ctrl is not False:
            raise ModelCatalogError(
                f"Model {model_id} has production_control={prod_ctrl}; production control is not authorized"
            )
        read_only = _boolean(caps_data["read_only_monitoring"], f"model {model_id} read_only_monitoring")
        guarded_max = _boolean(caps_data["guarded_max_trial"], f"model {model_id} guarded_max_trial")

        if guarded_max and support_status != SUPPORT_STATUS_GUARDED_MAX_TRIAL_ELIGIBLE:
            raise ModelCatalogError(
                f"Model {model_id} cannot set guarded_max_trial=True without status {SUPPORT_STATUS_GUARDED_MAX_TRIAL_ELIGIBLE}"
            )
        if support_status == SUPPORT_STATUS_GUARDED_MAX_TRIAL_ELIGIBLE and not guarded_max:
            raise ModelCatalogError(f"Model {model_id} trial status contradicts its capability")

        capabilities = Capabilities(
            read_only_monitoring=read_only,
            guarded_max_trial=guarded_max,
            production_control=False,
        )

        upstream_profile: UpstreamProfile | None = None
        up_data = raw_model.get("upstream_profile")
        if up_data is not None:
            if not isinstance(up_data, dict):
                raise ModelCatalogError(f"Model {model_id} upstream_profile must be an object or null")
            _check_fields(up_data, {"path", "sha256"}, f"model {model_id} upstream_profile")
            up_path = up_data.get("path")
            up_sha = up_data.get("sha256")
            if not isinstance(up_path, str) or not isinstance(up_sha, str):
                raise ModelCatalogError(f"Model {model_id} upstream_profile must have path and sha256")
            _validate_path_safety(up_path, f"model {model_id} upstream_profile.path")
            if len(up_sha) != 64 or not all(c in "0123456789abcdefABCDEF" for c in up_sha):
                raise ModelCatalogError(f"Model {model_id} upstream_profile.sha256 must be a 64-char hex string")
            upstream_profile = UpstreamProfile(path=up_path, sha256=up_sha.lower())

        local_artifacts: LocalArtifacts | None = None
        local_data = raw_model.get("local_artifacts")
        if local_data is not None:
            if not isinstance(local_data, dict):
                raise ModelCatalogError(f"Model {model_id} local_artifacts must be an object")
            _check_fields(local_data, {"policy_path", "guarded_profile_path"},
                          f"model {model_id} local_artifacts")
            pol_path = local_data.get("policy_path")
            guard_path = local_data.get("guarded_profile_path")
            if not isinstance(pol_path, str) or not isinstance(guard_path, str):
                raise ModelCatalogError(f"Model {model_id} local_artifacts missing required paths")
            _validate_path_safety(pol_path, f"model {model_id} local_artifacts.policy_path")
            _validate_path_safety(guard_path, f"model {model_id} local_artifacts.guarded_profile_path")
            local_artifacts = LocalArtifacts(policy_path=pol_path, guarded_profile_path=guard_path)

        evidence: EvidenceRecord | None = None
        evidence_data = raw_model.get("evidence")
        if evidence_data is not None:
            if not isinstance(evidence_data, dict):
                raise ModelCatalogError(f"Model {model_id} evidence must be an object")
            _check_fields(evidence_data, {"tested_bios", "tested_date", "trial_type", "fan_max_rpm",
                                          "baseline_restored", "write_support_restored", "notes",
                                          "source_paths"}, f"model {model_id} evidence")
            raw_bios = evidence_data.get("tested_bios", [])
            if not isinstance(raw_bios, list) or not all(isinstance(b, str) and b.strip() for b in raw_bios):
                raise ModelCatalogError(f"Model {model_id} tested_bios must be a list of non-empty strings")
            for bios in raw_bios:
                _text(bios, f"model {model_id} BIOS")
            if len(raw_bios) != len(set(raw_bios)):
                raise ModelCatalogError(f"Model {model_id} repeats a tested BIOS")
            for field in ("tested_date", "trial_type", "notes"):
                _text(evidence_data[field], f"model {model_id} evidence.{field}")
            try:
                if date.fromisoformat(evidence_data["tested_date"]).isoformat() != evidence_data["tested_date"]:
                    raise ValueError("Use YYYY-MM-DD")
            except ValueError as error:
                raise ModelCatalogError(f"Model {model_id} has an invalid evidence date") from error
            if type(evidence_data["fan_max_rpm"]) is not int or evidence_data["fan_max_rpm"] <= 0:
                raise ModelCatalogError(f"Model {model_id} requires a positive integer fan_max_rpm")
            sources = evidence_data["source_paths"]
            if not isinstance(sources, list) or not sources:
                raise ModelCatalogError(f"Model {model_id} requires evidence source_paths")
            for source in sources:
                _validate_path_safety(source, f"model {model_id} evidence source")
            evidence = EvidenceRecord(
                tested_bios=tuple(raw_bios),
                tested_date=evidence_data.get("tested_date"),
                trial_type=evidence_data.get("trial_type"),
                fan_max_rpm=evidence_data.get("fan_max_rpm"),
                baseline_restored=_boolean(evidence_data["baseline_restored"], f"model {model_id} baseline_restored"),
                write_support_restored=_boolean(evidence_data["write_support_restored"], f"model {model_id} write_support_restored"),
                notes=evidence_data.get("notes"),
                source_paths=tuple(sources),
            )

        if support_status in (SUPPORT_STATUS_UPSTREAM_ONLY, SUPPORT_STATUS_GUARDED_MAX_TRIAL_ELIGIBLE) and upstream_profile is None:
            raise ModelCatalogError(f"Model {model_id} requires upstream provenance for this status")
        if support_status == SUPPORT_STATUS_NEEDS_PROFILE and upstream_profile is not None:
            raise ModelCatalogError(f"Model {model_id} needs_profile contradicts its upstream profile")
        if guarded_max and (local_artifacts is None or evidence is None or not evidence.tested_bios
                            or not evidence.baseline_restored or not evidence.write_support_restored):
            raise ModelCatalogError(f"Model {model_id} lacks complete guarded trial artifacts or restoration evidence")

        next_steps = raw_model.get("next_steps")
        _text(next_steps, f"model {model_id} next_steps")

        models[model_id] = ModelEntry(
            model_id=model_id,
            vendor=vendor,
            product_name=product_name,
            aliases=aliases,
            support_status=support_status,
            upstream_profile=upstream_profile,
            local_artifacts=local_artifacts,
            evidence=evidence,
            capabilities=capabilities,
            next_steps=next_steps,
        )

    return ModelCatalog(
        version=version,
        upstream=upstream,
        models=MappingProxyType(models),
    )


def read_dmi_identity(dmi_root: str | Path = DEFAULT_DMI_ROOT) -> DmiIdentity:
    """Read sys_vendor, product_name, and bios_version from sysfs DMI path.

    Returns None for any unreadable or missing fields without throwing errors.
    """
    root = Path(dmi_root)

    def _read(filename: str) -> str | None:
        target = root / filename
        try:
            if target.is_file():
                text = target.read_text(encoding="utf-8", errors="replace").strip()
                return text if text else None
        except (OSError, PermissionError):
            pass
        return None

    return DmiIdentity(
        sys_vendor=_read("sys_vendor"),
        product_name=_read("product_name"),
        bios_version=_read("bios_version"),
    )


def detect_model(
    catalog: ModelCatalog | None = None,
    dmi_root: str | Path = DEFAULT_DMI_ROOT,
) -> MatchResult:
    """Read local DMI identity and match against the model catalogue."""
    if catalog is None:
        catalog = load_catalog()
    identity = read_dmi_identity(dmi_root=dmi_root)
    return catalog.match_dmi(
        sys_vendor=identity.sys_vendor,
        product_name=identity.product_name,
        bios_version=identity.bios_version,
    )
