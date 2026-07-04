#!/usr/bin/env python3
"""Find untranslated patchouli keys and write them to artifacts/to_translate/.

Like find_untranslated.py but for non-lang JSON files (patchouli books, templates, etc.).
Only extracts these specific keys: name, description, title, text, landing_text.
All other keys (icon, category, type, recipe, sortnum, …) are ignored.

Scans artifacts/assets/ against resourcepacks/ and extracts values that are:
  - Under a target key, AND
  - Not Russian in artifacts (source text), AND
  - Not yet Russian in resourcepacks (not yet translated)

After translating, run pull_translations.py to apply results back into resourcepacks.
"""

import json
import os
import re
import sys

RUSSIAN_RE = re.compile(r'[а-яА-ЯёЁ]')
# Matches dotted identifier keys like "guide.animus.entry.tier6_altar" or "mod:category.sub"
TRANSLATION_KEY_RE = re.compile(r'^[\w:]+(?:\.[\w:]+)+$')

TARGET_KEYS = frozenset({'name', 'description', 'title', 'text', 'landing_text'})

ARTIFACTS_DIR = os.path.join('artifacts', 'assets_patchouli')
TO_TRANSLATE_DIR = os.path.join('artifacts', 'to_translate_patchouli')
RESOURCEPACKS_DIR = os.path.join('resourcepacks', 'Community Russian Translations', 'assets')


def has_russian(text):
    return bool(RUSSIAN_RE.search(text))


def is_translation_key(text):
    """Return True for dotted identifier values like 'guide.animus.entry.foo' or 'mod:cat.sub'."""
    return bool(TRANSLATION_KEY_RE.match(text))


def is_lang_file(rel_path):
    return 'lang' in rel_path.replace('\\', '/').split('/')


def build_to_translate(artifact, resource, current_key=None):
    """Return the subset of artifact (under target keys) that needs translation, or None.

    current_key: the dict key under which this value sits. Only string leaves
    whose current_key is in TARGET_KEYS are considered for translation.
    List items reset current_key to None so each item's own keys are evaluated.
    """
    if isinstance(artifact, str):
        if not artifact:
            return None
        if current_key not in TARGET_KEYS:
            return None  # Not a translatable field — skip entirely
        if is_translation_key(artifact):
            return None  # Looks like a translation key reference, not real text
        if has_russian(artifact):
            return None  # Artifact already Russian
        if isinstance(resource, str) and has_russian(resource):
            return None  # Already translated in resourcepacks
        return artifact  # Needs translation

    if isinstance(artifact, dict):
        result = {}
        for key, val in artifact.items():
            res_val = resource.get(key) if isinstance(resource, dict) else None
            filtered = build_to_translate(val, res_val, current_key=key)
            if filtered is not None:
                result[key] = filtered
        return result if result else None

    if isinstance(artifact, list):
        result = []
        has_any = False
        for i, item in enumerate(artifact):
            res_item = resource[i] if isinstance(resource, list) and i < len(resource) else None
            # Reset current_key for list items — each item is a fresh dict context
            filtered = build_to_translate(item, res_item, current_key=None)
            if filtered is not None:
                result.append(filtered)
                has_any = True
            else:
                result.append(None)  # Null placeholder preserves list index alignment
        return result if has_any else None

    return None  # Non-string leaf (int, bool, null) — not a translation target


def count_strings(obj):
    if isinstance(obj, str):
        return 1
    if isinstance(obj, dict):
        return sum(count_strings(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(count_strings(v) for v in obj if v is not None)
    return 0


def load_json(path):
    """Load JSON, tolerating missing / empty / corrupt files.

    Returns None if the file is absent, empty or whitespace-only, or fails to
    parse (a non-empty but invalid file prints a warning so it stays visible).
    This is what keeps an empty resourcepacks file — e.g. a 0-byte
    simulated_hydroponic_bed.json — from crashing the run; it's simply treated
    as "not translated yet".
    """
    if not os.path.isfile(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    if not text.strip():
        return None  # empty / whitespace-only
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f'  warning: skipping unreadable JSON {path} ({e})', file=sys.stderr)
        return None


def save_json(path, data):
    """Atomically write data to path (temp file + os.replace).

    open(path, 'w') truncates to 0 bytes at once, so a process killed mid-write
    leaves an empty/partial file. Writing to a temp file and renaming means the
    destination is only ever the previous content or the complete new content —
    never an empty file.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def collect_json_files(base_dir, patchouli_only=True):
    files = []
    for root, _dirs, filenames in os.walk(base_dir):
        for filename in filenames:
            if filename.endswith('.json'):
                full_path = os.path.join(root, filename)
                rel_path = os.path.relpath(full_path, base_dir)
                if patchouli_only and is_lang_file(rel_path):
                    continue
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
        print('No patchouli JSON files found in artifacts.')
        return

    total_keys = 0
    files_written = 0

    for idx, rel_path in enumerate(json_files, start=1):
        artifact_path = os.path.join(ARTIFACTS_DIR, rel_path)
        resource_path = os.path.join(RESOURCEPACKS_DIR, rel_path)
        out_path = os.path.join(TO_TRANSLATE_DIR, rel_path)
        display = os.path.join('assets', rel_path).replace('\\', '/')

        artifact_data = load_json(artifact_path)
        if artifact_data is None:
            print(f'[{idx}/{total}] {display} — skipped (empty/unreadable artifact)')
            continue

        resource_data = load_json(resource_path)

        to_translate = build_to_translate(artifact_data, resource_data)

        if to_translate:
            n = count_strings(to_translate)
            total_keys += n
            files_written += 1
            print(f'[{idx}/{total}] {display} — {n} untranslated')
            save_json(out_path, to_translate)  # atomic: never leaves an empty file
        else:
            print(f'[{idx}/{total}] {display}')

    print(f'Done. {total_keys} untranslated keys across {files_written} files -> {TO_TRANSLATE_DIR}')


if __name__ == '__main__':
    main()
