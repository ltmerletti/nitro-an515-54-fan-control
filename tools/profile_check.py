#!/usr/bin/env python3
"""
profile_check.py - Read-only developer diagnostic tool for model profiles.

Inspects the model catalogue and performs DMI matching against sysfs.

Guarantees:
- Strictly read-only: never opens EC, never loads modules, never modifies state.
- Accepts injectable --dmi-root for synthetic sandbox testing.
- Outputs machine-readable JSON with --json flag.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add project root to sys.path so nitrofan package is importable without pip install.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nitrofan.models import (
    DEFAULT_CATALOG_PATH,
    DEFAULT_DMI_ROOT,
    ModelCatalogError,
    detect_model,
    load_catalog,
    read_dmi_identity,
)


def _format_model_summary(model) -> dict:
    return {
        "model_id": model.model_id,
        "vendor": model.vendor,
        "product_name": model.product_name,
        "aliases": list(model.aliases),
        "support_status": model.support_status,
        "upstream_profile": (
            {
                "path": model.upstream_profile.path,
                "sha256": model.upstream_profile.sha256,
            }
            if model.upstream_profile
            else None
        ),
        "capabilities": {
            "read_only_monitoring": model.capabilities.read_only_monitoring,
            "guarded_max_trial": model.capabilities.guarded_max_trial,
            "production_control": model.capabilities.production_control,
        },
        "tested_bios": list(model.evidence.tested_bios) if model.evidence else [],
        "evidence_sources": list(model.evidence.source_paths) if model.evidence else [],
        "local_artifacts": {
            "policy_path": model.local_artifacts.policy_path,
            "guarded_profile_path": model.local_artifacts.guarded_profile_path,
        } if model.local_artifacts else None,
        "next_steps": model.next_steps,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect NitroFan model catalogue and local DMI identifiers (read-only diagnostic).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all notebook models in the catalogue.",
    )
    parser.add_argument(
        "--detect",
        action="store_true",
        help="Detect local DMI identity and match against catalogue (default behavior).",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Display details for a specific model ID (e.g. AN515-54).",
    )
    parser.add_argument(
        "--dmi-root",
        type=Path,
        default=DEFAULT_DMI_ROOT,
        help=f"Root sysfs DMI path (default: {DEFAULT_DMI_ROOT}).",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=DEFAULT_CATALOG_PATH,
        help=f"Path to catalog.json (default: {DEFAULT_CATALOG_PATH}).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit output as machine-readable JSON.",
    )

    args = parser.parse_args()

    try:
        catalog = load_catalog(args.catalog)
    except ModelCatalogError as error:
        if args.json:
            print(json.dumps({"error": str(error)}, indent=2))
        else:
            print(f"Error loading catalogue: {error}", file=sys.stderr)
        return 1

    # Default action is --detect if neither --list nor --model is specified.
    do_list = args.list
    do_model = args.model is not None
    do_detect = args.detect or (not do_list and not do_model)

    if do_list:
        models_data = [_format_model_summary(m) for m in catalog.list_models()]
        if args.json:
            print(json.dumps({
                "upstream": {
                    "repository": catalog.upstream.repository,
                    "version": catalog.upstream.version,
                    "commit": catalog.upstream.commit,
                },
                "models": models_data,
            }, indent=2))
        else:
            print(f"NitroFan Model Catalogue (Upstream {catalog.upstream.version} @ {catalog.upstream.commit[:7]}):")
            for m in catalog.list_models():
                sha_short = m.upstream_profile.sha256[:8] if m.upstream_profile else "none"
                print(f"  {m.model_id:<10} {m.product_name:<20} {m.support_status:<28} (SHA: {sha_short})")
        return 0

    if do_model:
        entry = catalog.get(args.model)
        if entry is None:
            if args.json:
                print(json.dumps({"error": f"Model {args.model} not found in catalogue"}, indent=2))
            else:
                print(f"Model '{args.model}' not found in catalogue.", file=sys.stderr)
            return 1
        summary = _format_model_summary(entry)
        if args.json:
            print(json.dumps(summary, indent=2))
        else:
            print(f"Model ID:        {entry.model_id}")
            print(f"Product:         {entry.product_name}")
            print(f"Vendor:          {entry.vendor}")
            print(f"Support status:  {entry.support_status}")
            print(f"Upstream SHA256: {entry.upstream_profile.sha256 if entry.upstream_profile else 'None'}")
            print(f"Guarded trial:   {entry.capabilities.guarded_max_trial} (recorded BIOS only)")
            print(f"Tested BIOS:     {', '.join(entry.evidence.tested_bios) if entry.evidence else 'None'}")
            print(f"Production ctrl: {entry.capabilities.production_control}")
            print(f"Next steps:      {entry.next_steps}")
        return 0

    if do_detect:
        identity = read_dmi_identity(args.dmi_root)
        match = catalog.match_dmi(
            sys_vendor=identity.sys_vendor,
            product_name=identity.product_name,
            bios_version=identity.bios_version,
        )

        result_payload = {
            "dmi": {
                "sys_vendor": identity.sys_vendor,
                "product_name": identity.product_name,
                "bios_version": identity.bios_version,
            },
            "matched": match.matched,
            "model_id": match.model_id,
            "support_status": match.support_status,
            "capabilities": {
                "read_only_monitoring": match.capabilities.read_only_monitoring,
                "guarded_max_trial": match.capabilities.guarded_max_trial,
                "production_control": match.capabilities.production_control,
            },
            "bios_matched": match.bios_matched,
            "message": match.message,
            "next_steps": match.next_steps,
        }

        if args.json:
            print(json.dumps(result_payload, indent=2))
        else:
            print("DMI Detection Result:")
            print(f"  Vendor:          {identity.sys_vendor or '<missing>'}")
            print(f"  Product:         {identity.product_name or '<missing>'}")
            print(f"  BIOS:            {identity.bios_version or '<missing>'}")
            print(f"  Matched:         {match.matched}")
            print(f"  Model ID:        {match.model_id or 'None'}")
            print(f"  Support status:  {match.support_status}")
            print(f"  Guarded trial:   {match.capabilities.guarded_max_trial}")
            print(f"  Production ctrl: {match.capabilities.production_control}")
            print(f"  Message:         {match.message}")
            print(f"  Next steps:      {match.next_steps}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
