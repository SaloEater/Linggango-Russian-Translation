#!/usr/bin/env python3
"""Find untranslated keys and write them to artifacts/to_translate/.

Scans artifacts/assets/ against resourcepacks/ and extracts all string values that are:
  - Not Russian in artifacts (they are source/English text to translate), AND
  - Not yet Russian in resourcepacks (not yet translated)

The output files in artifacts/to_translate/ mirror the artifacts/ structure but contain
only the untranslated strings. For nested structures (patchouli), already-translated
positions are written as null to preserve index alignment for list entries.

After translating, run pull_translations.py to apply the results back into resourcepacks.
"""

import json
import os
import re
import sys

RUSSIAN_RE = re.compile(r'[а-яА-ЯёЁ]')

ARTIFACTS_DIR = os.path.join('artifacts', 'assets')
TO_TRANSLATE_DIR = os.path.join('artifacts', 'to_translate')
RESOURCEPACKS_DIR = os.path.join('resourcepacks', 'Community Russian Translations', 'assets')
#RESOURCEPACKS_DIR = os.path.join('kubejs', 'assets', 'ftbquestlocalizer', 'lang')


def has_russian(text):
    return bool(RUSSIAN_RE.search(text))


def build_to_translate(artifact, resource):
    """Return the subset of artifact that still needs translation, or None.

    Rules for string leaves:
      - Artifact is Russian → skip (sync_translations.py handles these)
      - Resource is already Russian → skip (already translated)
      - Otherwise → include (needs translation)

    For dicts: recurse, drop keys where nothing needs translation.
    For lists: recurse, use None as a placeholder for already-translated positions
               so that index alignment is preserved for pull_translations.py.
    """
    if isinstance(artifact, str):
        if not artifact:
            return None  # Empty string — nothing to translate
        if has_russian(artifact):
            return None  # Artifact already Russian — sync_translations handles it
        if isinstance(resource, str) and has_russian(resource):
            return None  # Already translated in resourcepacks
        return artifact  # Needs translation

    if isinstance(artifact, dict):
        result = {}
        for key, val in artifact.items():
            res_val = resource.get(key) if isinstance(resource, dict) else None
            filtered = build_to_translate(val, res_val)
            if filtered is not None:
                result[key] = filtered
        return result if result else None

    if isinstance(artifact, list):
        result = []
        has_any = False
        for i, item in enumerate(artifact):
            res_item = resource[i] if isinstance(resource, list) and i < len(resource) else None
            filtered = build_to_translate(item, res_item)
            if filtered is not None:
                result.append(filtered)
                has_any = True
            else:
                result.append(None)  # Null placeholder preserves list index alignment
        return result if has_any else None

    return None  # Non-string leaf (int, bool, null) — not a translation target


def count_strings(obj):
    """Count non-None leaf string values in a structure."""
    if isinstance(obj, str):
        return 1
    if isinstance(obj, dict):
        return sum(count_strings(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(count_strings(v) for v in obj if v is not None)
    return 0


def collect_json_files(base_dir):
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
        print(f'Error: {ARTIFACTS_DIR} not found.', file=sys.stderr)
        sys.exit(1)

    json_files = collect_json_files(ARTIFACTS_DIR)
    total = len(json_files)

    if total == 0:
        print('No JSON files found in artifacts.')
        return

    total_keys = 0
    files_written = 0

    for idx, rel_path in enumerate(json_files, start=1):
        artifact_path = os.path.join(ARTIFACTS_DIR, rel_path)
        resource_path = os.path.join(RESOURCEPACKS_DIR, rel_path)
        out_path = os.path.join(TO_TRANSLATE_DIR, rel_path)
        display = os.path.join('assets', rel_path).replace('\\', '/')

        with open(artifact_path, 'r', encoding='utf-8') as f:
            artifact_data = json.load(f)

        resource_data = None
        if os.path.isfile(resource_path):
            with open(resource_path, 'r', encoding='utf-8') as f:
                resource_data = json.load(f)

        to_translate = build_to_translate(artifact_data, resource_data)

        if to_translate:
            n = count_strings(to_translate)
            total_keys += n
            files_written += 1
            print(f'[{idx}/{total}] {display} — {n} untranslated')
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(to_translate, f, indent=2, ensure_ascii=False)
        else:
            print(f'[{idx}/{total}] {display}')

    print(f'Done. {total_keys} untranslated keys across {files_written} files -> {TO_TRANSLATE_DIR}')


if __name__ == '__main__':
    main()
