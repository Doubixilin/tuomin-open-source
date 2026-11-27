#!/usr/bin/env python3
"""Build, sign and notarize a macOS app from an isolated source snapshot.

Run with .venv-packaging/bin/python. Stages run separately so the signed app
can undergo frozen acceptance before anything is submitted to Apple.
Existing dist/tuomin-workbench and the user's runtime data are never replaced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import runpy
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv-packaging/bin/python"
VERSION = runpy.run_path(str(ROOT / "src/tuomin_gateway/__init__.py"))["__version__"]
MACH_MAGICS = {bytes.fromhex(s) for s in ("feedface", "cefaedfe", "feedfacf", "cffaedfe", "cafebabe", "bebafeca", "cafebabf", "bfbafeca")}


def run(*args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def inventory(root):
    result = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            if not p.resolve().is_relative_to(root.resolve()):
                raise SystemExit(f"External symlink in app: {rel}")
            result[rel] = {"symlink": os.readlink(p)}
        elif p.is_file():
            result[rel] = {"sha256": sha256(p), "bytes": p.stat().st_size}
    return result


def native_checks(app):
    from PyInstaller.archive.readers import CArchiveReader

    config = runpy.run_path(str(ROOT / "scripts/build_native_modules.py"))
    names = {"tuomin_gateway." + p[:-3].replace("/", ".") for p in config["CROWN_MODULES"]}
    archive = CArchiveReader(str(app / "Contents/MacOS/tuomin-workbench"))
    pyz_name = next(n for n in archive.toc if n.endswith(".pyz"))
    pyz = archive.open_embedded_archive(pyz_name)
    leaked = names.intersection(pyz.toc)
    if leaked:
        raise SystemExit(f"Native modules leaked into PYZ: {sorted(leaked)}")
    native = []
    for rel in config["CROWN_MODULES"]:
        base = app / "Contents/Frameworks/tuomin_gateway" / rel[:-3]
        found = list(base.parent.glob(base.name + ".*.so"))
        if len(found) != 1:
            raise SystemExit(f"Missing/duplicate native module: {rel}")
        native.append(found[0])
    violations = config["check_strings_baseline"](native)
    if violations:
        raise SystemExit(str(violations))
    for p in app.rglob("*"):
        if p.name in {"benchmark-data.json", "key.pem", "admin-token", "license.json", ".env"} or p.name.endswith((".mapping.enc", ".tuominmap")) or p.name in {"local_private", "private_data"}:
            raise SystemExit(f"Private/internal artifact in package: {p}")
    return {"native_modules": sorted(names), "pyz_core_copies": 0, "strings_baseline": "passed", "private_artifact_scan": "passed"}


def required_macos(app):
    from macholib.MachO import MachO

    versions = [(13, 0, 0)]
    for p in app.rglob("*"):
        if not p.is_file() or p.is_symlink():
            continue
        with p.open("rb") as f:
            if f.read(4) not in MACH_MAGICS:
                continue
        for header in MachO(str(p)).headers:
            for lc, cmd, _ in header.commands:
                value = getattr(cmd, "minos", None) if lc.cmd == 50 else getattr(cmd, "version", None) if lc.cmd == 36 else None
                if value is not None:
                    versions.append((value >> 16, (value >> 8) & 255, value & 255))
    return ".".join(map(str, max(versions)))


def build(args, work, release, app):
    if work.exists() or release.exists():
        raise SystemExit("Build/output directory already exists; choose a new build number")
    work.mkdir(parents=True)
    release.mkdir(parents=True)
    snapshot = work / "source"
    snapshot.mkdir()
    for directory in ("src", "integrations/skills"):
        shutil.copytree(ROOT / directory, snapshot / directory, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "scripts/build_native_modules.py", "scripts/release_macos.py", "packaging/launcher.py", "packaging/tuomin_gateway.spec"):
        dest = snapshot / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, dest)
    for name in ("synthetic-apps.json", "synthetic-dictionary.json", "samples/synthetic-contract.docx", "samples/synthetic-register.xlsx"):
        dest = snapshot / "demo" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "demo" / name, dest)
    # App icon (.icns for the bundle, menu-bar glyphs and Dock PNG as data files).
    shutil.copytree(ROOT / "packaging/icon", snapshot / "packaging/icon", ignore=shutil.ignore_patterns("*.svg"))
    # Record actual working source, including uncommitted changes, without
    # copying private configs, keys, runtime state or repository history.
    source_files = {p.relative_to(snapshot).as_posix(): sha256(p) for p in sorted(snapshot.rglob("*")) if p.is_file()}
    provenance = {
        "product": "tuomin-workbench", "version": VERSION, "build": args.build_number,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": run("git", "rev-parse", "HEAD", cwd=ROOT, capture_output=True, text=True).stdout.strip(),
        "git_dirty": bool(run("git", "status", "--porcelain", cwd=ROOT, capture_output=True, text=True).stdout.strip()),
        "source_sha256": source_files,
        "dependencies": json.loads(run(PYTHON, "-m", "pip", "list", "--format=json", capture_output=True, text=True).stdout),
    }
    write_json(work / "provenance.json", provenance)
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "TUOMIN_MACOS_APP": "1", "TUOMIN_BUILD_NUMBER": args.build_number}
    run(PYTHON, snapshot / "scripts/build_native_modules.py", cwd=snapshot, env=env)
    run(PYTHON, "-m", "PyInstaller", "--clean", "--noconfirm", "--distpath", work / "frozen", "--workpath", work / "pyinstaller", snapshot / "packaging/tuomin_gateway.spec", cwd=snapshot, env=env)
    run("ditto", work / "frozen/Tuomin.app", app)
    plist_path = app / "Contents/Info.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    plist["LSMinimumSystemVersion"] = required_macos(app)
    plist_path.write_bytes(plistlib.dumps(plist))
    write_json(work / "hardening.json", native_checks(app))
    print(f"Built app: {app}", flush=True)


def sign(args, work, release, app):
    if not args.identity.startswith("Developer ID Application:"):
        raise SystemExit("A Developer ID Application identity is required")
    identities = run("security", "find-identity", "-v", "-p", "codesigning", capture_output=True, text=True).stdout
    if args.identity not in identities:
        raise SystemExit("Requested identity is not currently valid")
    files = []
    for p in app.rglob("*"):
        if p.is_file() and not p.is_symlink():
            with p.open("rb") as f:
                if f.read(4) in MACH_MAGICS:
                    files.append(p)
    if not files:
        raise SystemExit("No Mach-O files found")
    for i, p in enumerate(sorted(files, key=lambda p: (-len(p.parts), str(p))), 1):
        run("codesign", "--force", "--timestamp", "--options", "runtime", "--sign", args.identity, p, capture_output=True)
        print(f"Signed Mach-O {i}/{len(files)}: {p.relative_to(app)}", flush=True)
    for framework in sorted(app.rglob("*.framework"), key=lambda p: -len(p.parts)):
        if not framework.is_symlink():
            run("codesign", "--force", "--timestamp", "--options", "runtime", "--sign", args.identity, framework, capture_output=True)
    run("codesign", "--force", "--timestamp", "--options", "runtime", "--sign", args.identity, app)
    run("codesign", "--verify", "--deep", "--strict", "--verbose=2", app)
    for p in files:
        run("codesign", "--verify", "--strict", p, capture_output=True)
        details = run("codesign", "-d", "--verbose=4", p, capture_output=True, text=True).stderr
        if f"Authority={args.identity}" not in details or "runtime" not in details or "Timestamp=" not in details:
            raise SystemExit(f"Signature identity/runtime/timestamp incomplete: {p}")
    write_json(work / "signing.json", {"identity": args.identity, "mach_o_count": len(files), "hardened_runtime": True, "timestamp": True, "deep_strict_verification": "passed"})
    write_json(work / "hardening.json", native_checks(app))


def copy_documents(folder):
    """Install guide plus the illustrated HTML manual, built from source each time."""
    shutil.copy2(ROOT / "README.md", folder / "安装与使用说明.md")
    manual = runpy.run_path(str(ROOT / "scripts/build_manual.py"))["build"]
    manual(folder / "Tuomin使用手册.html")


def notarize(args, work, release, app):
    if not (work / "signing.json").is_file():
        raise SystemExit("Run the sign stage first")
    acceptance = json.loads((work / "acceptance.json").read_text())
    if not acceptance.get("full_matrix") or acceptance.get("exit_code") != 0 or acceptance.get("binary_sha256") != sha256(app / "Contents/MacOS/tuomin-workbench"):
        raise SystemExit("The exact signed executable must pass frozen acceptance before submission")
    run("codesign", "--verify", "--deep", "--strict", app)
    archive = work / "notarization.zip"
    if archive.exists():
        raise SystemExit("Submission archive exists; inspect existing submission before retrying")
    run("ditto", "-c", "-k", "--keepParent", app, archive)
    submit(args, archive, work / "notary-app.json")
    run("xcrun", "stapler", "staple", app)
    run("xcrun", "stapler", "validate", app)
    run("spctl", "--assess", "--type", "execute", "--verbose=2", app)
    image_root = work / "dmg-content"
    image_root.mkdir()
    run("ditto", app, image_root / "Tuomin.app")
    (image_root / "Applications").symlink_to("/Applications")
    copy_documents(image_root)
    dmg = release / f"Tuomin-{VERSION}-macos-arm64-{args.build_number}.dmg"
    run("hdiutil", "create", "-volname", "Tuomin", "-srcfolder", image_root, "-format", "UDZO", dmg)
    run("codesign", "--timestamp", "--sign", args.identity, dmg)
    submit(args, dmg, work / "notary-dmg.json")
    run("xcrun", "stapler", "staple", dmg)
    run("xcrun", "stapler", "validate", dmg)
    run("spctl", "--assess", "--type", "open", "--context", "context:primary-signature", "--verbose=2", dmg)


def submit(args, artifact, result_path):
    # notarytool's JSON output is saved before status validation, preserving
    # the submission ID for diagnostics; no automatic duplicate submissions.
    result = run("xcrun", "notarytool", "submit", artifact, "--keychain-profile", args.profile, "--wait", "--output-format", "json", capture_output=True, text=True)
    result_path.write_text(result.stdout)
    data = json.loads(result.stdout)
    print(f"Notarization: {artifact.name}: {data.get('status')} ({data.get('id')})", flush=True)
    if data.get("status") != "Accepted":
        raise SystemExit("Apple notarization did not accept the artifact; see saved result")


def package(args, work, release, app):
    """Prepare an inspectable signed candidate entirely before notarization."""
    acceptance = json.loads((work / "acceptance.json").read_text())
    if not acceptance.get("full_matrix") or acceptance.get("binary_sha256") != sha256(app / "Contents/MacOS/tuomin-workbench"):
        raise SystemExit("Frozen acceptance does not match signed executable")
    run("codesign", "--verify", "--deep", "--strict", app)
    candidate = release / "signed-candidate"
    candidate.mkdir()
    content = work / "signed-candidate-content"
    content.mkdir()
    run("ditto", app, content / "Tuomin.app")
    (content / "Applications").symlink_to("/Applications")
    copy_documents(content)
    dmg = candidate / f"Tuomin-{VERSION}-macos-arm64-{args.build_number}-signed.dmg"
    run("hdiutil", "create", "-volname", "Tuomin", "-srcfolder", content, "-format", "UDZO", dmg)
    run("codesign", "--timestamp", "--sign", args.identity, dmg)
    run("codesign", "--verify", "--strict", dmg)
    run("hdiutil", "verify", dmg)
    manifest = json.loads((work / "provenance.json").read_text())
    manifest.update(
        release_status="signed_candidate_not_notarized",
        notarization={"status": "not_submitted", "destination": "Apple notarization service", "reason": "Packaged before the notarize stage"},
        minimum_macos=required_macos(app),
        signing=json.loads((work / "signing.json").read_text()),
        hardening=native_checks(app),
        frozen_acceptance=acceptance,
        app_files=inventory(app),
        artifacts={dmg.name: {"sha256": sha256(dmg), "bytes": dmg.stat().st_size}},
    )
    write_json(candidate / "DELIVERY_MANIFEST.json", manifest)
    (candidate / "SHA256SUMS").write_text("\n".join(f"{sha256(p)}  {p.name}" for p in sorted(candidate.iterdir()) if p.is_file()) + "\n")
    print(f"Signed candidate (not notarized): {candidate}", flush=True)


def finalize(args, work, release, app):
    manifest = json.loads((work / "provenance.json").read_text())
    for field, filename in (("signing", "signing.json"), ("hardening", "hardening.json"), ("notarization_app", "notary-app.json"), ("notarization_dmg", "notary-dmg.json")):
        manifest[field] = json.loads((work / filename).read_text())
    for kind in ("notarization_app", "notarization_dmg"):
        if manifest[kind].get("status") != "Accepted":
            raise SystemExit("Cannot finalize an unaccepted release")
    run("codesign", "--verify", "--deep", "--strict", app)
    run("xcrun", "stapler", "validate", app)
    manifest["minimum_macos"] = required_macos(app)
    plist = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if plist["LSMinimumSystemVersion"] != manifest["minimum_macos"]:
        raise SystemExit("Info.plist does not match actual dependency minimum OS")
    manifest["frozen_acceptance"] = json.loads((work / "acceptance.json").read_text())
    if not manifest["frozen_acceptance"].get("full_matrix") or manifest["frozen_acceptance"].get("binary_sha256") != sha256(app / "Contents/MacOS/tuomin-workbench"):
        raise SystemExit("Acceptance receipt does not match final binary")
    manifest["app_files"] = inventory(app)
    manifest["artifacts"] = {p.name: {"sha256": sha256(p), "bytes": p.stat().st_size} for p in release.glob("*.dmg")}
    if len(manifest["artifacts"]) != 1:
        raise SystemExit("Expected exactly one final DMG")
    write_json(release / "DELIVERY_MANIFEST.json", manifest)
    checksums = [f"{sha256(p)}  {p.name}" for p in sorted(release.iterdir()) if p.is_file() and p.name != "SHA256SUMS"]
    (release / "SHA256SUMS").write_text("\n".join(checksums) + "\n")
    print(f"Final release: {release}")


def public_dmg(acceptance_path: Path, build_number: str):
    app = ROOT / "dist/lite/Tuomin Lite.app"
    binary = app / "Contents/MacOS/tuomin-workbench"
    plist = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if not (app / "Contents/Resources/public-lite.txt").is_file():
        raise SystemExit("Expected a public Lite build")
    if plist.get("CFBundleShortVersionString") != VERSION or plist.get("CFBundleVersion") != build_number:
        raise SystemExit("Unexpected application version or build number")
    acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
    if (acceptance.get("variant") != "lite" or not acceptance.get("full_matrix")
            or acceptance.get("exit_code") != 0 or acceptance.get("binary_sha256") != sha256(binary)):
        raise SystemExit("Run --public-lite frozen acceptance on this exact app first")
    run("codesign", "--verify", "--deep", "--strict", app)
    release = ROOT / "dist/releases"
    release.mkdir(parents=True, exist_ok=True)
    dmg = release / f"Tuomin-{VERSION}-macOS-arm64-Lite.dmg"
    if dmg.exists():
        raise SystemExit(f"Output already exists: {dmg}")
    with tempfile.TemporaryDirectory(dir=release) as tmp:
        tmp = Path(tmp)
        content = tmp / "content"
        content.mkdir()
        run("ditto", app, content / app.name)
        (content / "Applications").symlink_to("/Applications")
        manual = runpy.run_path(str(ROOT / "scripts/build_manual.py"))["build"]
        manual(content / "Tuomin使用手册.html")
        shutil.copy2(ROOT / "LICENSE", content / "LICENSE")
        run("hdiutil", "create", "-volname", "Tuomin Lite", "-srcfolder", content,
            "-format", "UDZO", tmp / dmg.name)
        run("hdiutil", "verify", tmp / dmg.name)
        (tmp / dmg.name).rename(dmg)
    print(f"{sha256(dmg)}  {dmg.name} ({dmg.stat().st_size} bytes)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("build", "sign", "package", "notarize", "finalize", "public-dmg"))
    parser.add_argument("--build-number", required=True, help="Numeric CFBundleVersion, e.g. 20260915.1")
    parser.add_argument("--identity", default=os.environ.get("TUOMIN_SIGNING_IDENTITY", ""), help="Developer ID Application identity for sign, package and notarize")
    parser.add_argument("--profile", default=os.environ.get("TUOMIN_NOTARY_PROFILE"), help="Keychain profile for the notarize stage")
    parser.add_argument("--acceptance", type=Path, help="Frozen acceptance JSON for public-dmg")
    args = parser.parse_args()
    if args.stage in {"sign", "package", "notarize"} and not args.identity:
        parser.error("Provide --identity or TUOMIN_SIGNING_IDENTITY for this stage")
    if args.stage == "notarize" and not args.profile:
        parser.error("Provide --profile or TUOMIN_NOTARY_PROFILE to submit to Apple")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("This release workflow requires macOS arm64")
    if not all(part.isdigit() for part in args.build_number.split(".")) or len(args.build_number.split(".")) > 3:
        raise SystemExit("Invalid build number")
    if args.stage == "public-dmg":
        if not args.acceptance:
            parser.error("Provide --acceptance for the public-dmg stage")
        public_dmg(args.acceptance, args.build_number)
        return
    work = ROOT / "build" / ("macos-release-" + args.build_number)
    release = ROOT / "dist/releases" / ("tuomin-macos-arm64-" + args.build_number)
    globals()[args.stage](args, work, release, release / "Tuomin.app")


if __name__ == "__main__":
    main()
