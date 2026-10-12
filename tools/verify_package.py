"""Check shipped file integrity without reading or uploading runtime state."""
import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "PACKAGE_MANIFEST.json"
IGNORED_PARTS = {".git", ".venv", ".pytest_cache", "__pycache__", "data", "output", "tmp", "build", "dist"}


def shipped_files():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path == MANIFEST:
            continue
        relative = path.relative_to(ROOT)
        if any(part in IGNORED_PARTS or part.endswith(".egg-info") for part in relative.parts):
            continue
        if path.name.startswith(".env") or path.suffix in {".log", ".db", ".sqlite3", ".pyc"}:
            continue
        if relative.parts[0] == "config" and path.suffix == ".json" and not path.name.endswith(".example.json"):
            continue
        yield relative.as_posix(), path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    actual = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in shipped_files()}
    if args.refresh:
        MANIFEST.write_text(json.dumps({"schema_version": "client_search.package.v1", "files": actual}, indent=2) + "\n")
        print(f"Recorded {len(actual)} shipped files")
        return
    expected = json.loads(MANIFEST.read_text())["files"]
    errors = sorted(name for name in set(expected) | set(actual) if expected.get(name) != actual.get(name))
    if errors:
        raise SystemExit("Package differs from its manifest: " + ", ".join(errors))
    print(f"Verified {len(actual)} shipped files")


if __name__ == "__main__":
    main()
