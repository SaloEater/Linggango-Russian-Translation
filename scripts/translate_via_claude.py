#!/usr/bin/env python3
"""Translate files in artifacts/to_translate/ using the local `claude` CLI.

Same role as translate_untranslated.py, but instead of POSTing to an
OpenAI-compatible proxy it shells out to Claude Code in headless mode
(`claude -p`). This needs no API key — it uses your existing Claude Code
login/plan.

Token-minimisation choices (see --help of `claude`):
  --system-prompt ...          REPLACES Claude Code's large default agent
                               prompt with one tight translation instruction.
                               This is the biggest per-call saving.
  --exclude-dynamic-system-prompt-sections
                               drops env/git/cwd context we don't need.
  --model claude-haiku-4-5     cheapest capable model for JSON EN->RU.
  --disallowedTools "*"        no tool schemas, no file-read round-trips;
                               the chunk is passed inline on stdin.
  large --chunk-size           fewer calls => less fixed overhead repeated.

Only values that come back with Russian text are saved; English/garbled
output leaves the original untouched so you can re-run. Identical I/O to
translate_untranslated.py, so pull_translations.py works unchanged.

Usage:
  python scripts/translate_via_claude.py [options]

Options:
  --model MODEL      claude model / alias   (default: claude-haiku-4-5)
  --chunk-size N     keys per call for flat lang files (default: 80)
  --delay SECS       seconds to sleep between calls (default: 0)
  --timeout SECS     per-call timeout       (default: 180)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

RUSSIAN_RE = re.compile(r'[а-яА-ЯёЁ]')
TO_TRANSLATE_DIR = os.path.join('artifacts', 'to_translate_quests')

SYSTEM_PROMPT = (
    "You are a professional Minecraft mod translator, English to Russian. "
    "Translate every English string value to natural Russian. "
    "Preserve Minecraft format/color codes exactly (§a §r §l §6 etc.). "
    "Preserve format specifiers exactly (%s %d %1$s %2$s etc.). "
    "Preserve mod names, author/user nicknames, and technical keys "
    "(creativeTab, mod_name, category). Keep null values as null. "
    "Keep all JSON keys exactly unchanged. "
    "Return ONLY valid JSON: no markdown fences, no commentary."
)

USER_PREFIX_FLAT = "Translate the following JSON from English to Russian:\n"
USER_PREFIX_NESTED = (
    "Translate all English string values in this JSON to Russian. "
    "Keep null values as null. Keep the exact structure:\n"
)


def has_russian(text):
    return bool(RUSSIAN_RE.search(text))


def is_flat(data):
    return isinstance(data, dict) and all(
        isinstance(v, (str, type(None))) for v in data.values()
    )


def count_strings(obj):
    if isinstance(obj, str):
        return 1
    if isinstance(obj, dict):
        return sum(count_strings(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(count_strings(v) for v in obj if v is not None)
    return 0


def count_russian(obj):
    if isinstance(obj, str):
        return 1 if has_russian(obj) else 0
    if isinstance(obj, dict):
        return sum(count_russian(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(count_russian(v) for v in obj if v is not None)
    return 0


def strip_translated(obj):
    """Return a copy of obj with already-Russian (and non-translatable) leaves
    removed, or None if nothing is left to translate.

    - String leaf containing Russian -> dropped (already translated).
    - Dict -> keys whose pruned value is empty are removed entirely.
    - List -> None placeholders keep index alignment for merge_translations.
    - Non-string leaves (num/bool/null) -> dropped (not a translation target).

    This is what lets a run skip every line that already has a Russian letter,
    so resumes cost no tokens for finished keys.
    """
    if isinstance(obj, str):
        return None if has_russian(obj) else obj
    if isinstance(obj, dict):
        result = {k: p for k, v in obj.items() if (p := strip_translated(v)) is not None}
        return result or None
    if isinstance(obj, list):
        pruned = [strip_translated(v) for v in obj]
        return pruned if any(p is not None for p in pruned) else None
    return None


def call_claude(user_content, model, timeout):
    """Run `claude -p` headless with a minimal system prompt; return stdout text.

    Content is passed on stdin so no tools/file reads are needed.
    """
    cmd = [
        'claude', '-p',
        '--model', model,
        '--system-prompt', SYSTEM_PROMPT,
        '--exclude-dynamic-system-prompt-sections',
        '--disallowedTools', '*',
    ]
    proc = subprocess.run(
        cmd,
        input=user_content,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise ValueError(f'claude exited {proc.returncode}: {proc.stderr.strip()[:200]}')
    out = proc.stdout.strip()
    if not out:
        raise ValueError(f'claude returned empty output (stderr: {proc.stderr.strip()[:200]})')
    return out


def parse_json_response(text):
    text = text.strip()
    text = re.sub(r'^```[a-z]*\s*', '', text)
    text = re.sub(r'\s*```$', '', text.strip())
    return json.loads(text.strip())


def merge_translations(original, translated):
    """Overlay translated values onto original, keeping only Russian results."""
    if original is None:
        return None
    if isinstance(original, str):
        if isinstance(translated, str) and has_russian(translated):
            return translated
        return original
    if isinstance(original, dict) and isinstance(translated, dict):
        return {k: merge_translations(v, translated.get(k)) for k, v in original.items()}
    if isinstance(original, list) and isinstance(translated, list):
        return [
            merge_translations(o, translated[i] if i < len(translated) else None)
            for i, o in enumerate(original)
        ]
    return original


def translate_chunk(chunk, model, timeout):
    content = USER_PREFIX_FLAT + json.dumps(chunk, indent=2, ensure_ascii=False)
    return parse_json_response(call_claude(content, model, timeout))


def translate_nested(data, model, timeout):
    content = USER_PREFIX_NESTED + json.dumps(data, indent=2, ensure_ascii=False)
    return parse_json_response(call_claude(content, model, timeout))


def save_json(path, data):
    """Atomically write data to path (temp file + rename).

    Using os.replace means an interrupt (Ctrl-C, kill) can never leave a
    half-written / corrupt JSON file — the old file stays intact until the
    new one is fully flushed.
    """
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def collect_json_files(base_dir):
    files = []
    for root, _dirs, filenames in os.walk(base_dir):
        for filename in filenames:
            if filename.endswith('.json'):
                rel_path = os.path.relpath(os.path.join(root, filename), base_dir)
                files.append(rel_path)
    files.sort()
    return files


def main():
    parser = argparse.ArgumentParser(description='Translate artifacts/to_translate/ via the claude CLI.')
    parser.add_argument('--model', default=os.environ.get('CLAUDE_MODEL', 'claude-haiku-4-5'))
    parser.add_argument('--chunk-size', type=int, default=80)
    parser.add_argument('--delay', type=float, default=0.0)
    parser.add_argument('--timeout', type=float, default=300.0)
    args = parser.parse_args()

    if not os.path.isdir(TO_TRANSLATE_DIR):
        print(f'Error: {TO_TRANSLATE_DIR} not found. Run find_untranslated.py first.', file=sys.stderr)
        sys.exit(1)

    json_files = collect_json_files(TO_TRANSLATE_DIR)
    total_files = len(json_files)
    if total_files == 0:
        print('No files in to_translate. Run find_untranslated.py first.')
        return

    total_keys = 0
    for f in json_files:
        d = json.load(open(os.path.join(TO_TRANSLATE_DIR, f), encoding='utf-8'))
        total_keys += count_strings(d) - count_russian(d)  # only strings still needing Russian

    total_translated = 0

    for file_idx, rel_path in enumerate(json_files, start=1):
        full_path = os.path.join(TO_TRANSLATE_DIR, rel_path)
        display = os.path.join('assets', rel_path).replace('\\', '/')

        with open(full_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if is_flat(data):
            # Skip keys whose value already contains any Russian letter.
            keys = [k for k, v in data.items() if v is not None and not has_russian(v)]
            if not keys:
                print(f'[{file_idx}/{total_files}] {display} — already translated, skipped')
                continue
            n_chunks = max(1, (len(keys) + args.chunk_size - 1) // args.chunk_size)
            print(f'[{file_idx}/{total_files}] {display} ({len(keys)} keys, {n_chunks} chunk{"s" if n_chunks > 1 else ""})')

            for chunk_idx in range(n_chunks):
                chunk_keys = keys[chunk_idx * args.chunk_size:(chunk_idx + 1) * args.chunk_size]
                chunk = {k: data[k] for k in chunk_keys}
                print(f'  chunk {chunk_idx + 1}/{n_chunks} ... ', end='', flush=True)

                if args.delay and (file_idx > 1 or chunk_idx > 0):
                    time.sleep(args.delay)

                try:
                    translated_chunk = translate_chunk(chunk, args.model, args.timeout)
                    merged = merge_translations(chunk, translated_chunk)
                    applied = count_russian(merged)
                    total_translated += applied
                    data.update(merged)
                    save_json(full_path, data)  # persist now so a stop mid-run loses nothing
                    print(f'ok ({applied}/{len(chunk_keys)} translated)')
                except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, ValueError) as e:
                    print(f'FAILED ({e})')
        else:
            # Send only the leaves that still need translating (drop Russian ones).
            subset = strip_translated(data)
            remaining = count_strings(subset) if subset is not None else 0
            if remaining == 0:
                print(f'[{file_idx}/{total_files}] {display} — already translated, skipped')
                continue
            print(f'[{file_idx}/{total_files}] {display} (nested, {remaining} strings)')
            if args.delay and file_idx > 1:
                time.sleep(args.delay)
            try:
                translated = translate_nested(subset, args.model, args.timeout)
                before = count_russian(data)
                data = merge_translations(data, translated)
                applied = count_russian(data) - before
                total_translated += applied
                save_json(full_path, data)  # persist now so a stop mid-run loses nothing
                print(f'  ok ({applied}/{remaining} translated)')
            except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, ValueError) as e:
                print(f'  FAILED ({e})')

    print(f'\nDone. {total_translated}/{total_keys} keys translated across {total_files} files.')
    if total_translated < total_keys:
        print(f'{total_keys - total_translated} keys still need translation — re-run or check failed chunks.')


if __name__ == '__main__':
    main()
