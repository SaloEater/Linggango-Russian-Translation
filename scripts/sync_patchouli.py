#!/usr/bin/env python3
"""Sync non-lang JSON files (patchouli books, templates, etc.) from artifacts/assets/ to resourcepacks/.

- Files in artifacts but not in resourcepacks  → copy  (add missing)
- Files in resourcepacks but not in artifacts  → delete (remove unexisting)
- Files present in both                        → left unchanged (no content merge)
- Empty directories left after deletions       → removed

Lang files (*/lang/*.json) are excluded — those are handled by sync_translations.py.
"""

import os
import shutil
import sys

ARTIFACTS_DIR = os.path.join('artifacts', 'assets')
RESOURCEPACKS_DIR = os.path.join('resourcepacks', 'Community Russian Translations', 'assets')


def is_lang_file(rel_path):
    parts = rel_path.replace('\\', '/').split('/')
    return 'lang' in parts


def collect_files(base_dir):
    files = set()
    for root, _dirs, filenames in os.walk(base_dir):
        for filename in filenames:
            if filename.endswith('.json'):
                full_path = os.path.join(root, filename)
                rel_path = os.path.relpath(full_path, base_dir)
                if not is_lang_file(rel_path):
                    files.add(rel_path)
    return files


def remove_empty_dirs(base_dir):
    """Walk bottom-up and remove any directories that became empty."""
    for root, dirs, files in os.walk(base_dir, topdown=False):
        if root == base_dir:
            continue
        try:
            os.rmdir(root)  # only succeeds if the directory is empty
        except OSError:
            pass


def main():
    if not os.path.isdir(ARTIFACTS_DIR):
        print(f'Error: {ARTIFACTS_DIR} not found.', file=sys.stderr)
        sys.exit(1)

    artifact_files = collect_files(ARTIFACTS_DIR)
    resource_files = collect_files(RESOURCEPACKS_DIR)

    to_add = sorted(artifact_files - resource_files)
    to_remove = sorted(resource_files - artifact_files)

    print(f'Files to add:    {len(to_add)}')
    print(f'Files to remove: {len(to_remove)}')

    for rel_path in to_add:
        src = os.path.join(ARTIFACTS_DIR, rel_path)
        dst = os.path.join(RESOURCEPACKS_DIR, rel_path)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        print(f'  + {rel_path.replace(chr(92), "/")}')

    for rel_path in to_remove:
        dst = os.path.join(RESOURCEPACKS_DIR, rel_path)
        os.remove(dst)
        print(f'  - {rel_path.replace(chr(92), "/")}')

    remove_empty_dirs(RESOURCEPACKS_DIR)

    print(f'Done. Added {len(to_add)}, removed {len(to_remove)}.')


if __name__ == '__main__':
    main()
