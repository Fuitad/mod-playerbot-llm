from __future__ import annotations

from pathlib import Path
import subprocess
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
README_OPENING = (
    "> **Work in progress**\n\n"
    "This project is not ready for installation or use. It provides no deployment or compatibility guarantee.\n"
)
FORBIDDEN_TRACKED_ROOTS = {"build", "graphify-out"}
FORBIDDEN_TRACKED_DIRECTORIES = {"docs/plans", "docs/prd"}
FORBIDDEN_TYPOGRAPHIC_CODEPOINTS = {
    0x2013, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2022, 0x2026, 0x2190, 0x2192, 0x2713, 0x2717
}


def has_source_decoration(text: str) -> bool:
    return any(
        ord(character) in FORBIDDEN_TYPOGRAPHIC_CODEPOINTS or unicodedata.category(character) == "So"
        for character in text
    )


def tracked_files(errors: list[str]) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        errors.append("git ls-files failed")
        return []
    return [ROOT / entry.decode("utf-8") for entry in result.stdout.split(b"\0") if entry]


def check_contract(files: list[Path]) -> list[str]:
    errors: list[str] = []
    readme = ROOT / "README.md"
    if not readme.is_file() or not readme.read_text(encoding="utf-8").startswith(README_OPENING):
        errors.append("README.md does not start with the required work in progress notice")
    if not (ROOT / "LICENSE").is_file():
        errors.append("LICENSE is missing")
    for path in files:
        relative = path.relative_to(ROOT)
        first = relative.parts[0]
        relative_name = relative.as_posix()
        in_private_directory = any(
            relative_name == directory or relative_name.startswith(f"{directory}/")
            for directory in FORBIDDEN_TRACKED_DIRECTORIES
        )
        if first in FORBIDDEN_TRACKED_ROOTS or first.startswith("build") or in_private_directory:
            errors.append(f"forbidden tracked path: {relative}")
    return errors


def check_text(files: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in files:
        relative = path.relative_to(ROOT)
        content = path.read_bytes()
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"tracked file is not UTF-8 text: {relative}")
            continue
        if "\r" in text:
            errors.append(f"carriage return found: {relative}")
        if text and not text.endswith("\n"):
            errors.append(f"missing final newline: {relative}")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if line.rstrip() != line:
                errors.append(f"trailing whitespace: {relative}:{line_number}")
            if "\t" in line:
                errors.append(f"tab found: {relative}:{line_number}")
            if len(line) > 120 and path.suffix != ".md" and path.name not in {"LICENSE", "uv.lock"}:
                errors.append(f"line exceeds 120 columns: {relative}:{line_number}")
        if path.suffix != ".md" and has_source_decoration(text):
            errors.append(f"non-ASCII source decoration: {relative}")
    return errors


def main() -> int:
    errors: list[str] = []
    files = tracked_files(errors)
    errors.extend(check_contract(files))
    errors.extend(check_text(files))
    if errors:
        for error in errors:
            print(error)
        return 1
    print("repository checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
