#!/usr/bin/env python3
"""Build helper for guarded NBFC service with provenance tracking and hash manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import io
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

PINNED_UPSTREAM_COMMIT = "9da010d6a2e8351bdf6900d6c3643f1e742af936"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EC_GUARD_DIR = Path(__file__).resolve().parent if (Path(__file__).resolve().parent / "ec_guard.c").exists() else REPO_ROOT / "tools" / "ec_guard"
DEFAULT_PATCH_PATH = EC_GUARD_DIR / "patches" / "nbfc-0.5.4-guarded-ec-sys.patch"
MANIFEST_PATH = EC_GUARD_DIR / "guarded_nbfc_manifest.json"


def compute_sha256(path: Path) -> str:
    """Compute SHA-256 digest of a file."""
    hasher = hashlib.sha256()
    with open(path, "rb") as file:
        while chunk := file.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def run_command(command: list[str], cwd: Path) -> None:
    """Run command raising descriptive RuntimeError on failure."""
    proc = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Command failed ({' '.join(command)}): rc={proc.returncode}\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )


def build_guarded_nbfc(
    source_dir: Path,
    output_dir: Path,
    lua_include: Path,
    lua_lib_dir: Path,
    multiarch_include: Path | None = None,
    private_root: Path | None = None,
    ec_guard_dir: Path = EC_GUARD_DIR,
    patch_path: Path | None = None,
    manifest_path: Path | None = None,
    policy_path: Path | None = None,
    profile_path: Path | None = None,
) -> dict[str, str]:
    """Compile guarded nbfc_service linking ec_guard.o and return artifact hashes."""
    source_dir = source_dir.resolve()
    output_dir = output_dir.resolve()
    if private_root is None:
        raise ValueError("A private runtime root is required; host NBFC state must remain isolated")
    private_root = private_root.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    actual_patch_path = patch_path or (ec_guard_dir / "patches" / "nbfc-0.5.4-guarded-ec-sys.patch")
    actual_manifest_path = manifest_path or (ec_guard_dir / "guarded_nbfc_manifest.json")
    actual_policy_path = policy_path or (ec_guard_dir / "config" / "ec_guard_policy.json")
    actual_profile_path = profile_path or (ec_guard_dir / "profiles" / "Acer_Nitro_AN515-54_guarded_max.json")

    # 1. Verify upstream source commit
    rev_proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    current_commit = rev_proc.stdout.strip()
    if current_commit != PINNED_UPSTREAM_COMMIT:
        raise ValueError(
            f"Source at {source_dir} is at commit {current_commit}, expected pinned {PINNED_UPSTREAM_COMMIT}."
        )

    # Build archived committed files, leaving the user's source checkout untouched.
    archive = subprocess.run(["git", "archive", "HEAD"], cwd=source_dir,
                             capture_output=True, check=True).stdout
    source_dir = Path(tempfile.mkdtemp(prefix=".nbfc-build-", dir=output_dir))
    with tarfile.open(fileobj=io.BytesIO(archive)) as contents:
        contents.extractall(source_dir, filter="data")
    run_command(["patch", "-p1", "-i", str(actual_patch_path)], cwd=source_dir)

    # 3. Copy reviewed C guard files into source tree
    src_dir = source_dir / "src"
    configuration = src_dir / "config.h"
    content = configuration.read_text()
    for feature in ("ENABLE_EC_ACPI", "ENABLE_EC_DEV_PORT", "ENABLE_EC_DUMMY"):
        content = content.replace(f"#define {feature} 1", f"#define {feature} 0")
    configuration.write_text(content)
    for filename in ("ec_guard.c", "ec_guard.h"):
        content = (ec_guard_dir / filename).read_bytes()
        (src_dir / filename).write_bytes(content)

    # 4. Compile ec_guard.o and shared library
    is_darwin = platform.system() == "Darwin"
    shared_name = "libnbfc_ec_guard.dylib" if is_darwin else "libnbfc_ec_guard.so"
    shared_flag = "-dynamiclib" if is_darwin else "-shared"

    cflags = ["-Wall", "-Wextra", "-pedantic", "-std=c99", "-fPIC", "-O2", "-DNDEBUG"]
    run_command(["cc", *cflags, "-c", "ec_guard.c", "-o", "ec_guard.o"], cwd=src_dir)
    run_command(
        ["cc", *cflags, shared_flag, "ec_guard.c", "-o", str(output_dir / shared_name)],
        cwd=src_dir,
    )

    # 5. Compile nbfc_service linking ec_guard.o
    sysconfdir = str(private_root / "etc") if private_root else "/etc"
    datadir = str(private_root / "share") if private_root else "/usr/share"
    libdir = str(private_root / "lib") if private_root else "/usr/lib"
    runstatedir = str(private_root / "run") if private_root else "/run"
    mutable_dir = str(private_root / "var" / "lib" / "nbfc") if private_root else "/var/lib/nbfc"
    for directory in (private_root / "etc/nbfc", private_root / "run",
                      private_root / "var/lib/nbfc/configs", private_root / "logs"):
        directory.mkdir(parents=True, exist_ok=True)

    cppflags = [
        f"-DSYSCONFDIR=\"{sysconfdir}\"",
        f"-DDATADIR=\"{datadir}\"",
        f"-DLIBDIR=\"{libdir}\"",
        f"-DRUNSTATEDIR=\"{runstatedir}\"",
        f"-DNBFC_MUTABLE_DIR=\"{mutable_dir}\"",
        f"-DVERSION=\"0.5.4\"",
        f"-I{lua_include}",
        "-Isrc",
    ]
    if multiarch_include and multiarch_include.exists():
        cppflags.append(f"-I{multiarch_include}")

    ldflags = [
        f"-L{lua_lib_dir}",
        "-llua5.4",
        "-lm",
        "-ldl",
        f"-Wl,-rpath,{lua_lib_dir}",
    ]

    service_binary = output_dir / "nbfc_service"
    compile_cmd = [
        "cc",
        *cflags,
        *cppflags,
        "src/build.c",
        "src/ec_guard.o",
        *ldflags,
        "-o",
        str(service_binary),
    ]
    run_command(compile_cmd, cwd=source_dir)

    # 6. Verify built binary execution
    ver_proc = subprocess.run(
        [str(service_binary), "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    if "nbfc-linux 0.5.4" not in ver_proc.stdout:
        raise RuntimeError(f"Unexpected binary version string: {ver_proc.stdout}")

    # 7. Generate attestation hashes
    manifest = {
        "build_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "upstream_commit": PINNED_UPSTREAM_COMMIT,
        "patch_sha256": compute_sha256(actual_patch_path),
        "ec_guard_c_sha256": compute_sha256(ec_guard_dir / "ec_guard.c"),
        "ec_guard_h_sha256": compute_sha256(ec_guard_dir / "ec_guard.h"),
        "service_binary_sha256": compute_sha256(service_binary),
        "shared_library_sha256": compute_sha256(output_dir / shared_name),
        "service_binary_path": str(service_binary),
        "shared_library_path": str(output_dir / shared_name),
        "runtime_root": str(private_root),
        "policy_sha256": compute_sha256(actual_policy_path),
        "profile_sha256": compute_sha256(actual_profile_path),
        "compile_flags": {
            "cflags": cflags,
            "cppflags": cppflags,
            "ldflags": ldflags,
        },
    }

    actual_manifest_path.write_text(json.dumps(manifest, indent=2))
    shutil.rmtree(source_dir)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build guarded NBFC binary and generate manifest")
    parser.add_argument(
        "--source-dir",
        type=Path,
        required=True,
        help="Path to nbfc-linux 0.5.4 git checkout",
    )
    parser.add_argument(
        "--ec-guard-dir",
        type=Path,
        default=EC_GUARD_DIR,
        help="Path to ec_guard sources directory",
    )
    parser.add_argument(
        "--patch-path",
        type=Path,
        default=None,
        help="Path to nbfc patch file",
    )
    parser.add_argument(
        "--manifest-path",
        type=Path,
        default=None,
        help="Path to output manifest JSON",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory to place compiled guarded binaries",
    )
    parser.add_argument(
        "--lua-include",
        type=Path,
        default=Path("/home/luke/nbfc-evaluation/dependencies/usr/include/lua5.4"),
        help="Path to lua headers",
    )
    parser.add_argument(
        "--lua-lib-dir",
        type=Path,
        default=Path("/home/luke/nbfc-evaluation/dependencies/usr/lib/x86_64-linux-gnu"),
        help="Path to lua library directory",
    )
    parser.add_argument(
        "--multiarch-include",
        type=Path,
        default=Path("/home/luke/nbfc-evaluation/dependencies/usr/include/x86_64-linux-gnu"),
        help="Path to multiarch headers (lua5.4-deb-multiarch.h)",
    )
    parser.add_argument(
        "--private-root",
        type=Path,
        default=None,
        help="Optional private root directory for sysconfdir, runstatedir, and mutable dir",
    )
    parser.add_argument(
        "--policy-path",
        type=Path,
        default=None,
        help="Path to EC guard policy JSON file",
    )
    parser.add_argument(
        "--profile-path",
        type=Path,
        default=None,
        help="Path to NBFC profile JSON file",
    )

    args = parser.parse_args()
    ec_guard_dir = args.ec_guard_dir
    output_dir = args.output_dir or (ec_guard_dir / "bin")
    manifest = build_guarded_nbfc(
        source_dir=args.source_dir,
        output_dir=output_dir,
        lua_include=args.lua_include,
        lua_lib_dir=args.lua_lib_dir,
        multiarch_include=args.multiarch_include,
        private_root=args.private_root,
        ec_guard_dir=ec_guard_dir,
        patch_path=args.patch_path,
        manifest_path=args.manifest_path,
        policy_path=args.policy_path,
        profile_path=args.profile_path,
    )
    print("Guarded NBFC built successfully.")
    print(f"Service SHA256: {manifest['service_binary_sha256']}")


if __name__ == "__main__":
    main()
