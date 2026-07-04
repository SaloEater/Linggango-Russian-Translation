#!/usr/bin/env python3
"""Sync translations from artifacts/assets/ into resourcepacks/.

This is step 3 of the translation workflow:
  1. find_untranslated.py  — extract keys needing translation → artifacts/to_translate/
  2. pull_translations.py  — apply translated files back → resourcepacks/
  3. sync_translations.py  — full sync of artifacts/assets/ → resourcepacks/ (this script)

Merge rules (per key-value pair):
  - Key missing from artifacts        → remove from resourcepacks
  - Artifact value contains Russian   → overwrite resourcepacks value
  - Artifact value has no Russian     → keep existing resourcepacks value unchanged
  - Key missing from resourcepacks    → add it

Directory rule:
  - Any folder under RESOURCEPACKS_DIR with no counterpart under ARTIFACTS_DIR
    is deleted (a mod removed from artifacts is removed from the resourcepack).
"""

import json
import os
import re
import shutil
import sys

RUSSIAN_RE = re.compile(r'[а-яА-ЯёЁ]')

#ARTIFACTS_DIR = os.path.join('artifacts', 'assets')
#RESOURCEPACKS_DIR = os.path.join('resourcepacks', 'Community Russian Translations', 'assets')
#ARTIFACTS_DIR = os.path.join('artifacts', 'assets_quests')
#RESOURCEPACKS_DIR = os.path.join('kubejs', 'assets', 'ftbquestlocalizer', 'lang')
ARTIFACTS_DIR = os.path.join('artifacts', 'assets_kjs')
RESOURCEPACKS_DIR = os.path.join('kubejs', 'assets')


def has_russian(text):
    return bool(RUSSIAN_RE.search(text))


def merge_recursive(artifact, resource):
    """Recursively merge artifact into resource following the merge rules.

    Works for nested dicts (patchouli) and flat dicts (lang files).
    """
    if isinstance(artifact, dict) and isinstance(resource, dict):
        result = {}
        for key in artifact:
            if key in resource:
                result[key] = merge_recursive(artifact[key], resource[key])
            else:
                # Missing from resourcepacks → add the key
                result[key] = artifact[key]
        # Keys in resource but not in artifact are dropped (removed)
        return result

    if isinstance(artifact, list) and isinstance(resource, list):
        # Merge lists element-by-element up to the shorter length,
        # then take remaining from artifact
        result = []
        for i in range(max(len(artifact), len(resource))):
            if i < len(artifact) and i < len(resource):
                result.append(merge_recursive(artifact[i], resource[i]))
            elif i < len(artifact):
                result.append(artifact[i])
            # If resource is longer than artifact, extra elements are dropped
        return result

    # Leaf string values: apply Russian detection rule
    if isinstance(artifact, str):
        if has_russian(artifact):
            return artifact
        # No Russian in artifact value → keep existing resourcepacks value
        if isinstance(resource, str):
            return resource
        return artifact

    # Non-string leaf (numbers, booleans, null) → take artifact value
    return artifact


def collect_json_files(base_dir):
    """Collect all JSON file paths under base_dir, relative to base_dir."""
    files = []
    for root, _dirs, filenames in os.walk(base_dir):
        for filename in filenames:
            if filename.endswith('.json'):
                full_path = os.path.join(root, filename)
                rel_path = os.path.relpath(full_path, base_dir)
                files.append(rel_path)
    files.sort()
    return files


def prune_dirs(artifacts_dir, resource_dir):
    """Remove directories under resource_dir with no counterpart in artifacts_dir.

    Walks top-down: when a directory has no matching artifacts folder it is
    deleted whole and not descended into. Returns the list of removed relative
    paths.
    """
    removed = []
    for root, dirs, _files in os.walk(resource_dir, topdown=True):
        rel_root = os.path.relpath(root, resource_dir)
        kept = []
        for d in dirs:
            rel = d if rel_root == '.' else os.path.join(rel_root, d)
            if os.path.isdir(os.path.join(artifacts_dir, rel)):
                kept.append(d)
            else:
                shutil.rmtree(os.path.join(root, d))
                removed.append(rel.replace('\\', '/'))
        dirs[:] = kept  # don't descend into directories we just removed
    return removed


def main():
    if not os.path.isdir(ARTIFACTS_DIR):
        print(f'Error: artifacts directory not found: {ARTIFACTS_DIR}', file=sys.stderr)
        sys.exit(1)

    json_files = collect_json_files(ARTIFACTS_DIR)
    total = len(json_files)

    if total == 0:
        print('No JSON files found in artifacts.')
        return

    for idx, rel_path in enumerate(json_files, start=1):
        artifact_path = os.path.join(ARTIFACTS_DIR, rel_path)
        resource_path = os.path.join(RESOURCEPACKS_DIR, rel_path)

        # Normalize path separators for display
        display_path = os.path.join('assets', rel_path).replace('\\', '/')
        print(f'[{idx}/{total}] {display_path}')

        with open(artifact_path, 'r', encoding='utf-8') as f:
            artifact_data = json.load(f)

        if os.path.isfile(resource_path):
            with open(resource_path, 'r', encoding='utf-8') as f:
                resource_data = json.load(f)
            merged = merge_recursive(artifact_data, resource_data)
        else:
            # New file — just copy artifact data
            merged = artifact_data

        os.makedirs(os.path.dirname(resource_path), exist_ok=True)
        with open(resource_path, 'w', encoding='utf-8') as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)

    # Remove folders in resourcepacks that no longer exist in artifacts.
    if os.path.isdir(RESOURCEPACKS_DIR):
        removed = prune_dirs(ARTIFACTS_DIR, RESOURCEPACKS_DIR)
        for rel in removed:
            print(f'  removed folder: {rel}')
        if removed:
            print(f'Removed {len(removed)} folder(s) not present in artifacts.')

    print(f'Done. Processed {total} files.')


if __name__ == '__main__':
    main()
