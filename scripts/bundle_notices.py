"""Collect the license texts for distributions actually used by a frozen public build."""
from __future__ import annotations

import json
from importlib import metadata
from pathlib import Path
import sys
import sysconfig


def bundle_notices(analysis, project_root, work_dir):
    root = Path(project_root)
    output = Path(work_dir) / "third-party-notices"
    output.mkdir(parents=True, exist_ok=True)
    owners = metadata.packages_distributions()
    # Public builds collect these explicitly, including certifi as replaceable
    # source files outside the PYZ, so it may be absent from analysis.pure.
    names = {"certifi", "pyinstaller", "pdfminer.six"}
    for module, _, _ in analysis.pure:
        names.update(owners.get(module.partition(".")[0], ()))
    # Binary-only extensions and frameworks can have different owners from the
    # Python wrapper. Match the actual hook inputs against the installed RECORDs.
    sources = {Path(source).resolve() for _, source, _ in [*analysis.binaries, *analysis.datas] if source}
    distributions = list(metadata.distributions())
    for dist in distributions:
        for file in dist.files or ():
            if file.suffix.lower() in (".so", ".pyd", ".dylib", ".dll") and Path(dist.locate_file(file)).resolve() in sources:
                names.add(dist.metadata["Name"])
                break
    table = {}
    result = []
    for dist in sorted((metadata.distribution(name) for name in names), key=lambda d: d.metadata["Name"].lower()):
        name = dist.metadata["Name"]
        if name in table or name.lower().replace("_", "-") == "tuomin-gateway":
            continue
        texts = []
        for file in dist.files or ():
            path = Path(dist.locate_file(file))
            if file.name.upper().startswith(("LICENSE", "LICENCE", "COPYING", "COPYRIGHT", "NOTICE")) and path.is_file() and path.suffix.lower() not in (".py", ".pyc", ".so", ".pyd"):
                relative = Path(*[part for part in file.parts if part not in (".", "..")])
                texts.append((path, relative.as_posix()))
        if not texts and name.lower().startswith("pyobjc-"):
            cocoa = metadata.distribution("pyobjc-framework-Cocoa")
            texts = [(Path(cocoa.locate_file(f)), "LICENSE.txt") for f in cocoa.files or () if f.name == "LICENSE.txt"]
        if not texts and name.lower().replace("_", "-") == "proxy-tools":
            texts = [(root / "packaging/notices/proxy-tools.txt", "LICENSE.txt")]
        normalized = name.lower().replace("_", "-")
        if not texts and normalized in {"clr-loader", "pythonnet"}:
            texts = [(root / "packaging/windows-public" / normalized / "LICENSE.txt", "LICENSE.txt")]
        if not texts:
            license_field = dist.metadata.get("License", "")
            if len(license_field) < 250:
                raise RuntimeError(f"Supply the upstream license text for {name} {dist.version}")
            dest = output / (name + "-LICENSE.txt")
            dest.write_text(license_field + "\n", encoding="utf-8")
            texts = [(dest, "LICENSE.txt")]
        for path, relative in texts:
            result.append((f"licenses/components/{name}/{relative}", str(path), "DATA"))
        table[name] = dist.version

    # CPython distributes the interpreter and bundled library notices here.
    python_license = next((p for p in (Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
                                       Path(sys.base_prefix) / "LICENSE.txt") if p.is_file()), None)
    if not python_license:
        raise RuntimeError("CPython license file is missing")
    result.append(("licenses/components/CPython/LICENSE.txt", str(python_license), "DATA"))
    table["CPython"] = sys.version.split()[0]
    for p in sorted((root / "packaging/notices").glob("*.txt")):
        result.append(("licenses/upstream/" + p.name, str(p), "DATA"))
    manifest = output / "VERSIONS.json"
    manifest.write_text(json.dumps(table, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    result.append(("licenses/VERSIONS.json", str(manifest), "DATA"))
    return result
