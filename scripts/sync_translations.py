#!/usr/bin/env python3
"""Sync translations from artifacts/new into resourcepacks/Community Russian Translations.

Merge rules (per key-value pair):
- Key missing from artifacts → remove from resourcepacks
- Artifact value contains Russian letters → overwrite resourcepacks value
- Artifact value has NO Russian letters → keep existing resourcepacks value
- Key missing from resourcepacks → add it
"""

import json
import os
import re
import sys

RUSSIAN_RE = re.compile(r'[а-яА-ЯёЁ]')

ARTIFACTS_DIR = os.path.join('artifacts', 'new', 'assets')
RESOURCEPACKS_DIR = os.path.join('resourcepacks', 'Community Russian Translations', 'assets')


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

    print(f'Done. Processed {total} files.')


if __name__ == '__main__':
    main()
