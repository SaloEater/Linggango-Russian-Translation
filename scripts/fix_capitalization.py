#!/usr/bin/env python3
"""Fix sentence-case capitalization in Russian translation values.

For each string value containing Russian text:
- Capitalize the first letter of each sentence
- Lowercase mid-sentence Russian letters that start Title-Case words
  (e.g. "Изготовь Зарядник" → "Изготовь зарядник")
- Preserve all-caps Russian abbreviations like МЭ, ЦП, ТЭС
  (detected by: if a word has no lowercase Russian letters, it stays untouched)
- Latin letters are never changed (preserves English mod names, %s, %d, etc.)
- Abbreviations like "т.е.", "т.д." are detected via look-back and do not
  trigger a new sentence
"""

import json
import os
import re
import sys

RUSSIAN_RE = re.compile(r'[а-яА-ЯёЁ]')
RUSSIAN_UPPER = frozenset('АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ')
RUSSIAN_LOWER = frozenset('абвгдеёжзийклмнопрстуфхцчшщъыьэюя')
RUSSIAN_ALL = RUSSIAN_UPPER | RUSSIAN_LOWER

RESOURCEPACKS_DIR = os.path.join('resourcepacks', 'Community Russian Translations', 'assets')


def fix_sentence_case(text):
    """Apply Russian sentence-case to a single string value."""
    if not RUSSIAN_RE.search(text):
        return text

    result = []
    at_sentence_start = True
    i = 0
    n = len(text)

    while i < n:
        c = text[i]

        # Minecraft §X color/format code — pass through unchanged, no state change
        if c == '§' and i + 1 < n:
            result.append(c)
            result.append(text[i + 1])
            i += 2
            continue

        # Newline → sentence boundary
        if c == '\n':
            result.append(c)
            at_sentence_start = True
            i += 1
            continue

        # Sentence-ending punctuation
        if c in '.!?':
            result.append(c)
            i += 1

            # Only a real sentence boundary if followed by at least one space
            j = i
            while j < n and text[j] in ' \t':
                j += 1

            if j > i:
                # Space follows — but check for abbreviations like "т.е.", "т.д."
                # If the character before this dot is a single Russian letter word
                # (preceded by space, dot, or start of string), treat as abbreviation.
                k = len(result) - 2  # index of the char before the dot we just appended
                if k >= 0 and result[k] in RUSSIAN_ALL:
                    before_k = result[k - 1] if k > 0 else None
                    if before_k is None or before_k in ' \t.!?':
                        pass  # Single-letter Russian word → abbreviation, no boundary
                    else:
                        at_sentence_start = True
                else:
                    at_sentence_start = True
            continue

        # Whitespace — preserve current sentence state
        if c in ' \t':
            result.append(c)
            i += 1
            continue

        # Russian letter
        if c in RUSSIAN_ALL:
            if at_sentence_start:
                # Capitalize first letter of the sentence
                result.append(c.upper())
                at_sentence_start = False
            elif c in RUSSIAN_UPPER:
                # Mid-sentence uppercase Russian letter.
                # Only lowercase it if this word contains at least one lowercase
                # Russian letter (i.e. it's Title-Case, not an ALL-CAPS abbreviation).
                j = i + 1
                word_has_lower_russian = False
                while j < n and text[j] not in ' \t\n':
                    if text[j] in RUSSIAN_LOWER:
                        word_has_lower_russian = True
                        break
                    j += 1
                result.append(c.lower() if word_has_lower_russian else c)
                at_sentence_start = False
            else:
                # Lowercase Russian — keep as-is
                result.append(c)
                at_sentence_start = False
            i += 1
            continue

        # Latin or other letter — pass through, clears sentence-start state
        if c.isalpha():
            result.append(c)
            at_sentence_start = False
            i += 1
            continue

        # Digits, symbols, punctuation — pass through, no state change
        result.append(c)
        i += 1

    return ''.join(result)


def fix_value(obj):
    """Recursively apply fix_sentence_case to all string values in a JSON structure."""
    if isinstance(obj, str):
        return fix_sentence_case(obj)
    if isinstance(obj, dict):
        return {k: fix_value(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [fix_value(item) for item in obj]
    return obj


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
    if not os.path.isdir(RESOURCEPACKS_DIR):
        print(f'Error: directory not found: {RESOURCEPACKS_DIR}', file=sys.stderr)
        sys.exit(1)

    json_files = collect_json_files(RESOURCEPACKS_DIR)
    total = len(json_files)

    if total == 0:
        print('No JSON files found.')
        return

    changed = 0
    for idx, rel_path in enumerate(json_files, start=1):
        full_path = os.path.join(RESOURCEPACKS_DIR, rel_path)
        display_path = os.path.join('assets', rel_path).replace('\\', '/')

        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            print(f'[{idx}/{total}] {display_path} — SKIPPED (JSON error: {e})')
            continue

        fixed = fix_value(data)

        if fixed != data:
            changed += 1
            print(f'[{idx}/{total}] {display_path} (modified)')
            with open(full_path, 'w', encoding='utf-8') as f:
                json.dump(fixed, f, indent=2, ensure_ascii=False)
        else:
            print(f'[{idx}/{total}] {display_path}')

    print(f'Done. {changed}/{total} files modified.')


if __name__ == '__main__':
    main()
