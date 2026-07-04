#!/usr/bin/env python3
"""Pull completed translations from artifacts/to_translate/ into resourcepacks/.

For each JSON file in artifacts/to_translate/:
  - Russian string values are written into the corresponding resourcepacks file
  - Non-Russian string values are ignored (not yet translated)
  - Null entries in lists are skipped (index-alignment placeholders from find_untranslated.py)
  - Keys not present in the to_translate file are left unchanged in resourcepacks

Run this after translating files in artifacts/to_translate/.
Run find_untranslated.py first if the to_translate directory does not exist yet.
"""

import json
import os
import re
import sys

RUSSIAN_RE = re.compile(r'[а-яА-ЯёЁ]')

#TO_TRANSLATE_DIR = os.path.join('artifacts', 'to_translate')
#RESOURCEPACKS_DIR = os.path.join('resourcepacks', 'Community Russian Translations', 'assets')
#TO_TRANSLATE_DIR = os.path.join('artifacts', 'to_translate_quests')
#RESOURCEPACKS_DIR = os.path.join('kubejs', 'assets', 'ftbquestlocalizer', 'lang')
#TO_TRANSLATE_DIR = os.path.join('artifacts', 'to_translate_kjs')
#RESOURCEPACKS_DIR = os.path.join('kubejs', 'assets')
TO_TRANSLATE_DIR = os.path.join('artifacts', 'to_translate_patchouli')
RESOURCEPACKS_DIR = os.path.join('resourcepacks', 'Community Russian Translations', 'assets')


def has_russian(text):
    return bool(RUSSIAN_RE.search(text))


def apply_translations(to_translate, resource):
    """Merge Russian values from to_translate into resource.

    Only Russian string values from to_translate overwrite resource.
    Everything else in resource is left unchanged.
    Null entries in to_translate lists are skipped (placeholders).
    """
    if to_translate is None:
        return resource

    if isinstance(to_translate, str):
        return to_translate if has_russian(to_translate) else resource

    if isinstance(to_translate, dict):
        result = dict(resource) if isinstance(resource, dict) else {}
        for key, val in to_translate.items():
            result[key] = apply_translations(val, result.get(key))
        return result

    if isinstance(to_translate, list):
        result = list(resource) if isinstance(resource, list) else []
        for i, item in enumerate(to_translate):
            if item is None:
                continue  # Null placeholder — leave resource entry as-is
            res_item = result[i] if i < len(result) else None
            merged = apply_translations(item, res_item)
            if i < len(result):
                result[i] = merged
            else:
                result.append(merged)
        return result

    return resource


def count_applied(obj):
    """Count Russian string values that will be applied."""
    if isinstance(obj, str):
        return 1 if has_russian(obj) else 0
    if isinstance(obj, dict):
        return sum(count_applied(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(count_applied(v) for v in obj if v is not None)
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
    if not os.path.isdir(TO_TRANSLATE_DIR):
        print(f'Error: {TO_TRANSLATE_DIR} not found. Run find_untranslated.py first.', file=sys.stderr)
        sys.exit(1)

    json_files = collect_json_files(TO_TRANSLATE_DIR)
    total = len(json_files)

    if total == 0:
        print(f'No files in {TO_TRANSLATE_DIR}. Run find_untranslated.py first.')
        return

    total_applied = 0
    files_modified = 0

    for idx, rel_path in enumerate(json_files, start=1):
        tt_path = os.path.join(TO_TRANSLATE_DIR, rel_path)
        resource_path = os.path.join(RESOURCEPACKS_DIR, rel_path)
        display = os.path.join('assets', rel_path).replace('\\', '/')

        with open(tt_path, 'r', encoding='utf-8') as f:
            tt_data = json.load(f)

        n_applied = count_applied(tt_data)
        if n_applied == 0:
            print(f'[{idx}/{total}] {display} — no translations yet')
            continue

        resource_data = {}
        if os.path.isfile(resource_path):
            with open(resource_path, 'r', encoding='utf-8') as f:
                resource_data = json.load(f)

        merged = apply_translations(tt_data, resource_data)
        total_applied += n_applied
        files_modified += 1
        print(f'[{idx}/{total}] {display} — {n_applied} keys applied')

        os.makedirs(os.path.dirname(resource_path), exist_ok=True)
        with open(resource_path, 'w', encoding='utf-8') as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)

    print(f'Done. {total_applied} keys applied across {files_modified} files.')


if __name__ == '__main__':
    main()
