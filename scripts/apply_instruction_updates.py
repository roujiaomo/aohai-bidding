#!/usr/bin/env python3
"""Apply version-pinned instruction text updates with backups; dry-run by default.

This is an explicit local maintenance command, not a startup hook or updater.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "tools/instruction-optimization/patches.json"
ALLOWED_ROOTS = (".codex/skills/", ".agents/skills/", ".codex/plugins/cache/")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def target_path(profile: Path, relative: str) -> Path:
    if not relative.startswith(ALLOWED_ROOTS) or ".." in Path(relative).parts or "\\" in relative:
        raise ValueError(f"Unsupported target: {relative}")
    candidate = profile / relative
    if candidate.is_symlink() or not candidate.resolve().is_relative_to(profile.resolve()):
        raise ValueError(f"Target escapes profile: {relative}")
    return candidate


def render(original: bytes, entry: dict) -> bytes:
    text = original.decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    text = text.replace("\r\n", "\n")
    if "content" in entry:
        text = entry["content"]
    else:
        for edit in entry.get("edits", []):
            if not edit["old"] or text.count(edit["old"]) != 1:
                raise ValueError(f"Original text must match exactly once: {entry['path']}")
            text = text.replace(edit["old"], edit["new"], 1)
    return text.replace("\n", newline).encode("utf-8")


def state_directory(profile: Path, manifest: dict) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9-]+", manifest["id"]):
        raise ValueError("Invalid patch id")
    directory = profile / ".codex/instruction-backups" / manifest["id"]
    if not directory.resolve().is_relative_to(profile.resolve()):
        raise ValueError("Backup directory escapes profile")
    return directory


def prepare(profile: Path, manifest: dict) -> list[dict]:
    state_dir = state_directory(profile, manifest)
    journal = state_dir / "state.json"
    state = json.loads(journal.read_text(encoding="utf-8")) if journal.exists() else {"records": []}
    previous = {r["path"]: r for r in state["records"]}
    planned, seen = [], set()
    for entry in manifest["entries"]:
        relative = entry["path"]
        if relative in seen:
            raise ValueError(f"Duplicate target: {relative}")
        seen.add(relative)
        target = target_path(profile, relative)
        disabled = target.with_name(target.name + ".disabled-agent-optimization")
        if entry.get("disable") and not target.exists():
            if disabled.exists() and digest(disabled.read_bytes()) == entry["sha256"].lower():
                continue
            raise ValueError(f"Missing original/disabled target: {relative}")
        original = target.read_bytes()
        current_hash = digest(original)
        if relative in previous and current_hash == previous[relative]["after_sha256"] and not entry.get("disable"):
            continue
        if current_hash != entry["sha256"].lower():
            raise ValueError(f"Version/content drift; no files changed: {relative}")
        if entry.get("disable") and disabled.exists():
            raise ValueError(f"Disabled destination already exists: {disabled}")
        output = original if entry.get("disable") else render(original, entry)
        planned.append({"entry": entry, "target": target, "disabled": disabled,
                        "original": original, "output": output})
    return planned


def apply(profile: Path, manifest: dict, planned: list[dict]) -> Path:
    directory = state_directory(profile, manifest)
    directory.mkdir(parents=True, exist_ok=True)
    journal = directory / "state.json"
    old_state = json.loads(journal.read_text(encoding="utf-8")) if journal.exists() else {"records": []}
    records = {record["path"]: record for record in old_state["records"]}
    # Backup all originals before the first mutation; refuse backup collisions.
    for item in planned:
        relative = item["entry"]["path"]
        backup = directory / (hashlib.sha256(relative.encode()).hexdigest() + ".original")
        if backup.exists() and backup.read_bytes() != item["original"]:
            raise ValueError(f"Backup collision: {relative}")
        if not backup.exists():
            backup.write_bytes(item["original"])
        records[relative] = {"path": relative, "backup": backup.name,
                             "before_sha256": digest(item["original"]),
                             "after_sha256": digest(item["output"]),
                             "disable": bool(item["entry"].get("disable"))}
    journal.write_text(json.dumps({"status": "prepared", "records": list(records.values())}, indent=2), encoding="utf-8")
    touched = []
    try:
        for item in planned:
            if item["target"].read_bytes() != item["original"]:
                raise ValueError(f"Target changed after preview: {item['target']}")
            touched.append(item)
            if item["entry"].get("disable"):
                item["target"].rename(item["disabled"])
                actual = item["disabled"].read_bytes()
            else:
                item["target"].write_bytes(item["output"])
                actual = item["target"].read_bytes()
            if actual != item["output"]:
                raise ValueError(f"Read-back failed: {item['target']}")
    except Exception:
        for item in reversed(touched):
            if item["entry"].get("disable"):
                if item["disabled"].exists() and not item["target"].exists():
                    item["disabled"].rename(item["target"])
            else:
                item["target"].write_bytes(item["original"])
        journal.write_text(json.dumps(old_state, indent=2), encoding="utf-8")
        raise
    journal.write_text(json.dumps({"status": "applied", "records": list(records.values())}, indent=2), encoding="utf-8")
    return journal


def rollback(profile: Path, manifest: dict) -> None:
    directory = state_directory(profile, manifest)
    journal = directory / "state.json"
    state = json.loads(journal.read_text(encoding="utf-8"))
    allowed = {entry["path"] for entry in manifest["entries"]}
    planned = []
    for record in state["records"]:
        if record["path"] not in allowed or Path(record["backup"]).name != record["backup"]:
            raise ValueError("Unexpected rollback target")
        target = target_path(profile, record["path"])
        actual = target.with_name(target.name + ".disabled-agent-optimization") if record["disable"] else target
        original = (directory / record["backup"]).read_bytes()
        if digest(original) != record["before_sha256"]:
            raise ValueError("Backup integrity check failed")
        if target.exists() and digest(target.read_bytes()) == record["before_sha256"]:
            continue
        if digest(actual.read_bytes()) != record["after_sha256"] or (record["disable"] and target.exists()):
            raise ValueError(f"Later edits detected; rollback refused: {target}")
        planned.append((target, actual, original, record["disable"]))
    for target, actual, original, disabled in planned:
        if disabled:
            actual.rename(target)
        else:
            target.write_bytes(original)
    state["status"] = "rolled_back"
    journal.write_text(json.dumps(state, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=Path.home())
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--validate-with", type=Path, help="Run the Skill Creator validator on temporary rendered previews; no installation")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    if args.validate_with and (args.apply or args.rollback):
        parser.error("--validate-with is a preview-only operation")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if args.rollback:
        rollback(args.profile, manifest)
        print("Original instruction files restored; backups retained.")
        return 0
    planned = prepare(args.profile, manifest)
    if args.validate_with:
        failures = []
        for item in planned:
            if item["entry"].get("disable") or item["target"].name != "SKILL.md":
                continue
            with tempfile.TemporaryDirectory() as directory:
                preview = Path(directory) / "SKILL.md"
                preview.write_bytes(item["output"])
                result = subprocess.run([sys.executable, "-X", "utf8", str(args.validate_with), directory], capture_output=True, text=True, encoding="utf-8")
                print(item["entry"]["path"] + ": " + (result.stdout + result.stderr).strip())
                if result.returncode:
                    # Surface pre-existing upstream extensions, never silently
                    # edit metadata merely to satisfy a different validator.
                    preview.write_bytes(item["original"])
                    baseline = subprocess.run([sys.executable, "-X", "utf8", str(args.validate_with), directory], capture_output=True, text=True, encoding="utf-8")
                    if baseline.returncode and baseline.stdout == result.stdout and baseline.stderr == result.stderr:
                        print("Same validator failure exists in the original; upstream metadata preserved.")
                    failures.append(item["entry"]["path"])
        return 1 if failures else 0
    print(json.dumps({"mode": "apply" if args.apply else "preview", "changes": [
        {"path": item["entry"]["path"], "action": "disable duplicate entrypoint" if item["entry"].get("disable") else "update instructions",
         "before_sha256": digest(item["original"]), "after_sha256": digest(item["output"])} for item in planned]}, indent=2))
    if args.apply and planned:
        print("Backup journal: " + str(apply(args.profile, manifest, planned)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
