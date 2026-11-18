#!/usr/bin/env python3
"""
Minecraft Resource Pack Dual-Version Converter
Creates ONE ZIP intended for Java Edition 1.21.1 and 26.2.

Automatic conversion currently supported:
- Keeps the original pack as the 1.21.1/base assets.
- Creates a 26.2 overlay.
- Converts simple legacy custom_model_data item overrides into modern
  assets/<namespace>/items/<item>.json item definitions.
- Converts legacy integer custom_model_data predicates to the modern
  minecraft:custom_model_data float-list property (index 0).
- Updates pack.mcmeta for resource-pack formats 34 (1.21.1) through
  88 (26.2).

Limitations:
- Arbitrary item predicates, special bow/crossbow/shield/compass models,
  shaders, fonts, GUI assets, and other schema changes are not guessed.
- Unsupported overrides are listed in CONVERSION-REPORT.txt.
- Always test the output in both game versions. "Compatible" metadata alone
  cannot guarantee every asset behaves identically.
"""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import zipfile
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

BASE_FORMAT = 34       # Java 1.21.1
TARGET_FORMAT = 88     # Java 26.2
OVERLAY_DIR = "overlay_26_2"


def find_pack_root(folder: Path) -> Path:
    if (folder / "pack.mcmeta").is_file():
        return folder
    found = list(folder.rglob("pack.mcmeta"))
    if len(found) == 1:
        return found[0].parent
    if not found:
        raise ValueError("No pack.mcmeta found. Select a valid resource-pack ZIP or folder.")
    raise ValueError("More than one pack.mcmeta was found. Select the folder containing the actual pack.")


def extract_input(source: Path, work: Path) -> Path:
    if source.is_dir():
        copied = work / "input"
        shutil.copytree(source, copied)
        return find_pack_root(copied)

    extracted = work / "extracted"
    extracted.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source, "r") as archive:
        for member in archive.infolist():
            destination = (extracted / member.filename).resolve()
            if not destination.is_relative_to(extracted.resolve()):
                raise ValueError(f"Unsafe path inside ZIP: {member.filename}")
        archive.extractall(extracted)
    return find_pack_root(extracted)


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise ValueError(f"Invalid JSON in {path.relative_to(path.parents[1])}: {exc}") from exc


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def model_ref(model_value):
    """Normalize an override's model field to a namespaced model identifier."""
    if not isinstance(model_value, str) or not model_value:
        return None
    if ":" in model_value:
        return model_value
    return "minecraft:" + model_value


def convert_override_file(source_file: Path, pack_root: Path, overlay_root: Path, report: list[str]):
    """
    Convert only straightforward custom_model_data-only override arrays.
    The legacy model file remains in the base pack for 1.21.1.
    """
    relative = source_file.relative_to(pack_root)
    parts = relative.parts
    # assets/<namespace>/models/item/<item>.json
    if len(parts) != 5 or parts[0] != "assets" or parts[2] != "models" or parts[3] != "item":
        return False

    namespace = parts[1]
    item_id = source_file.stem
    data = read_json(source_file)
    overrides = data.get("overrides")
    if not isinstance(overrides, list) or not overrides:
        return False

    base_model = f"{namespace}:item/{item_id}"
    converted = []
    skipped = []

    for number, override in enumerate(overrides, start=1):
        if not isinstance(override, dict):
            skipped.append(f"override #{number}: not an object")
            continue
        predicate = override.get("predicate")
        model = model_ref(override.get("model"))
        if not isinstance(predicate, dict) or model is None:
            skipped.append(f"override #{number}: missing predicate or model")
            continue

        # Safe automatic case: exactly one custom_model_data predicate.
        if set(predicate.keys()) != {"custom_model_data"}:
            skipped.append(
                f"override #{number}: uses predicates {', '.join(predicate.keys()) or '(none)'} "
                "in addition to / instead of custom_model_data"
            )
            continue
        value = predicate["custom_model_data"]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            skipped.append(f"override #{number}: custom_model_data is not numeric")
            continue

        converted.append({
            "threshold": float(value),
            "model": {"type": "minecraft:model", "model": model}
        })

    if not converted:
        if skipped:
            report.append(f"NOT CONVERTED {relative.as_posix()}: " + "; ".join(skipped))
        return False

    # Legacy override evaluation is ordered. range_dispatch thresholds need
    # ascending order so the highest qualifying threshold wins.
    converted.sort(key=lambda entry: entry["threshold"])

    definition = {
        "model": {
            "type": "minecraft:range_dispatch",
            "property": "minecraft:custom_model_data",
            "index": 0,
            "entries": converted,
            "fallback": {
                "type": "minecraft:model",
                "model": base_model
            }
        }
    }

    output_file = overlay_root / "assets" / namespace / "items" / f"{item_id}.json"
    write_json(output_file, definition)

    report.append(
        f"CONVERTED {relative.as_posix()} -> "
        f"{output_file.relative_to(overlay_root).as_posix()} "
        f"({len(converted)} custom_model_data override(s))"
    )
    for issue in skipped:
        report.append(f"PARTIAL {relative.as_posix()}: {issue}")
    return True


def update_pack_metadata(pack_root: Path):
    meta_path = pack_root / "pack.mcmeta"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise ValueError(f"pack.mcmeta is missing or invalid JSON: {exc}")

    pack = meta.setdefault("pack", {})
    if not isinstance(pack, dict):
        raise ValueError("pack.mcmeta must contain a 'pack' object.")

    # 1.21.1 reads pack_format/supported_formats. 26.2 reads min/max_format.
    # Since this pack intentionally spans the old and new format systems,
    # both generations of metadata fields are retained.
    pack["pack_format"] = BASE_FORMAT
    pack["supported_formats"] = {
        "min_inclusive": BASE_FORMAT,
        "max_inclusive": TARGET_FORMAT
    }
    pack["min_format"] = BASE_FORMAT
    pack["max_format"] = TARGET_FORMAT

    entries = meta.setdefault("overlays", {}).setdefault("entries", [])
    if not isinstance(entries, list):
        raise ValueError("pack.mcmeta overlays.entries must be a list.")

    # Remove only a previous entry made by this tool; preserve other overlays.
    entries[:] = [
        entry for entry in entries
        if not (isinstance(entry, dict) and entry.get("directory") == OVERLAY_DIR)
    ]
    entries.append({
        "directory": OVERLAY_DIR,
        "min_format": TARGET_FORMAT,
        "max_format": TARGET_FORMAT
    })

    write_json(meta_path, meta)


def zip_pack(root: Path, output: Path):
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in root.rglob("*"):
            if file.is_file():
                archive.write(file, file.relative_to(root).as_posix())


def convert(source: Path, output_file: Path):
    report = [
        "CommieDucks Resource Pack Compatibility Converter",
        f"Input: {source}",
        "Target versions: Java 1.21.1 (format 34) and Java 26.2 (format 88)",
        "",
        "Conversion results:"
    ]

    with tempfile.TemporaryDirectory(prefix="mc_dual_pack_") as temp_name:
        temp = Path(temp_name)
        pack_root = extract_input(source, temp)
        # Work on a copy so the user's original folder is never modified.
        working = temp / "working_pack"
        shutil.copytree(pack_root, working)

        overlay_root = working / OVERLAY_DIR
        overlay_root.mkdir(parents=True, exist_ok=True)

        item_model_files = list((working / "assets").glob("*/models/item/*.json")) \
            if (working / "assets").exists() else []
        converted_count = 0
        for item_file in item_model_files:
            if convert_override_file(item_file, working, overlay_root, report):
                converted_count += 1

        update_pack_metadata(working)

        # Include a warning for common areas this converter does not rewrite.
        if (working / "assets").exists():
            if list(working.glob("assets/*/font/*.json")):
                report.append("REVIEW: Font definitions exist; font schema changes are not automatically converted.")
            if list(working.glob("assets/*/shaders/**/*")):
                report.append("REVIEW: Shader files exist; shader compatibility is not automatically converted.")

        if converted_count == 0:
            report.append(
                "WARNING: No simple custom_model_data item overrides were converted. "
                "The pack may only need metadata, or it may use unsupported model features."
            )

        report.extend([
            "",
            "Important notes:",
            "- Original/base files are retained for 1.21.1.",
            "- Converted modern item definitions are placed in the 26.2 overlay.",
            "- Overrides using other predicates are listed as PARTIAL/NOT CONVERTED.",
            "- Test the ZIP in both Minecraft versions and inspect latest.log for asset errors."
        ])
        (working / "CONVERSION-REPORT.txt").write_text(
            "\n".join(report) + "\n", encoding="utf-8"
        )
        output_file.parent.mkdir(parents=True, exist_ok=True)
        zip_pack(working, output_file)

    return report, converted_count


def main():
    root = tk.Tk()
    root.withdraw()

    selected = filedialog.askopenfilename(
        title="Choose a Minecraft resource pack",
        filetypes=[("Resource pack ZIP", "*.zip"), ("All files", "*.*")]
    )
    if not selected:
        folder = filedialog.askdirectory(title="Or choose an unpacked resource-pack folder")
        if not folder:
            return
        source = Path(folder)
    else:
        source = Path(selected)

    destination = filedialog.asksaveasfilename(
        title="Save the unified 1.21.1 + 26.2 resource pack",
        initialfile=f"{source.stem if source.is_file() else source.name}-1.21.1-and-26.2.zip",
        defaultextension=".zip",
        filetypes=[("ZIP archive", "*.zip")]
    )
    if not destination:
        return

    try:
        report, count = convert(source, Path(destination))
        messagebox.showinfo(
            "Conversion finished",
            f"Created one unified resource pack.\n\n"
            f"Converted {count} item-model file(s).\n"
            f"Output: {destination}\n\n"
            "A CONVERSION-REPORT.txt is included inside the ZIP. "
            "Check it for anything that needs manual conversion."
        )
    except Exception as exc:
        messagebox.showerror("Conversion failed", str(exc))


if __name__ == "__main__":
    main()
