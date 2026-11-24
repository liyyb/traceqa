"""Build a versioned Windows portable release and a verifiable file manifest."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "release")
    parser.add_argument("--zip-only", action="store_true", help="Keep only the ZIP and its checksum.")
    args = parser.parse_args()
    output = args.output.resolve()
    if sys.platform != "win32":
        parser.error("Use a Windows Python environment to create the Windows release.")
    if any((output / name).exists() for name in ("TraceQA", "TraceQA-Windows.zip", "TraceQA-Windows.zip.sha256")):
        parser.error("A release already exists in the output directory. Use a fresh --output.")
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        parser.error("Build the frontend with npm ci and npm run build first.")
    for folder in ("cmaps", "standard_fonts"):
        source = ROOT / "frontend" / "node_modules" / "pdfjs-dist" / folder
        target = ROOT / "frontend" / "dist" / "assets" / "pdfjs" / folder
        names = [item.name for item in source.glob("*") if item.is_file()]
        if not names or any(not (target / name).is_file() or
                            digest(source / name) != digest(target / name) for name in names):
            parser.error(f"Missing or stale PDF.js {folder}. Run npm run build in frontend.")

    with tempfile.TemporaryDirectory(prefix="traceqa-build-") as temporary:
        work = Path(temporary)
        dist_dir = work / "dist" if args.zip_only else output
        build(output, dist_dir, work)


def build(output: Path, dist_dir: Path, work: Path) -> None:
    bundle = dist_dir / "TraceQA"
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir",
        "--name", "TraceQA", "--distpath", str(dist_dir),
        "--workpath", str(work / "work"), "--specpath", str(work),
        "--paths", str(ROOT / "backend"), "--paths", str(ROOT / "parsers"),
        "--additional-hooks-dir", str(ROOT / "packaging" / "hooks"),
        "--exclude-module", "PIL", "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops.auto", "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan.on",
        "--add-data", f"{ROOT / 'frontend' / 'dist'};frontend/dist",
        str(ROOT / "packaging" / "launcher.py"),
    ], cwd=ROOT, check=True)
    for name in ("启动 TraceQA.cmd", "README.md"):
        shutil.copy2(ROOT / name, bundle / name)

    lock = []
    for dist in sorted(importlib.metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
        name = dist.metadata["Name"]
        lock.append(f"{name}=={dist.version}")
        for item in dist.files or ():
            if item.parts and any(p.lower() in {"licenses", "license", "copying", "notice"}
                                  or p.lower().startswith(("license.", "copying.", "notice."))
                                  for p in item.parts[1:]):
                source = Path(dist.locate_file(item))
                if source.is_file():
                    dest = bundle / "licenses" / name / Path(*item.parts[1:])
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, dest)
    (bundle / "build-environment.txt").write_text("\n".join(lock) + "\n", encoding="utf-8")
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        (bundle / "licenses").mkdir(exist_ok=True)
        shutil.copy2(python_license, bundle / "licenses" / "Python-LICENSE.txt")
    modules = ROOT / "frontend" / "node_modules"
    for name in ("react", "react-dom", "scheduler", "pdfjs-dist"):
        for license_file in (modules / name).glob("LICENSE*"):
            dest = bundle / "licenses" / name
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(license_file, dest)
    shutil.copy2(ROOT / "frontend" / "package-lock.json", bundle)
    files = sorted(path for path in bundle.rglob("*") if path.is_file())
    manifest = {path.relative_to(bundle).as_posix(): digest(path) for path in files}
    (bundle / "SHA256.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / "TraceQA-Windows.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as dest:
        for path in [*files, bundle / "SHA256.json"]:
            dest.write(path, path.relative_to(dist_dir))
    archive_hash = digest(archive)
    (output / f"{archive.name}.sha256").write_text(
        f"{archive_hash}  {archive.name}\n", encoding="ascii")
    print(f"{archive_hash}  {archive}")


if __name__ == "__main__":
    main()
