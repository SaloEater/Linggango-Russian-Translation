#!/usr/bin/env python3
"""Translate files in artifacts/to_translate/ using an OpenAI-compatible LLM proxy.

This is step 2 of the translation workflow:
  1. find_untranslated.py      — extract English strings -> artifacts/to_translate/
  2. translate_untranslated.py — translate them via LLM proxy (this script)
  3. pull_translations.py      — apply translations -> resourcepacks/

Files are translated in-place inside artifacts/to_translate/.
Only values that come back with Russian text are saved; if the LLM returns
English or garbled output the original value is left unchanged so you can retry.

Usage:
  python scripts/translate_untranslated.py [options]

Options:
  --url URL          Proxy endpoint  (env: PROXY_URL,   default: http://127.0.0.1:8000/v1/chat/completions)
  --model MODEL      Model string    (env: PROXY_MODEL, default: gemini/gemini-2.5-flash)
  --api-key KEY      Bearer token    (env: PROXY_API_KEY, default: none)
  --chunk-size N     Keys per LLM call for flat lang files (default: 50)
  --delay SECS       Seconds to sleep between API calls (default: 0)
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

RUSSIAN_RE = re.compile(r'[а-яА-ЯёЁ]')
#TO_TRANSLATE_DIR = os.path.join('artifacts', 'to_translate')
#TO_TRANSLATE_DIR = os.path.join('artifacts', 'to_translate_quests')
TO_TRANSLATE_DIR = os.path.join('artifacts', 'to_translate_patchouli')

SYSTEM_PROMPT = """\
You are a professional Minecraft mod translator specializing in English to Russian translations.
Rules:
- Translate all English string values to natural Russian.
- Preserve Minecraft format/color codes exactly as-is: §a §r §l §6 etc.
- Preserve format specifiers exactly as-is: %s %d %1$s %2$s etc.
- Preserve what seems to be a mod name or an author/user nickname.
- Preserve what has technical key like creativeTab, mod_name, category
- Keep null values as null — do not translate or remove them.
- Keep all JSON keys exactly unchanged.
- Return ONLY valid JSON. No markdown fences, no extra text, no explanation.\
"""


def has_russian(text):
    return bool(RUSSIAN_RE.search(text))


def is_flat(data):
    """Return True if every top-level value is a str or None (flat lang file)."""
    return isinstance(data, dict) and all(
        isinstance(v, (str, type(None))) for v in data.values()
    )


def count_strings(obj):
    """Count non-None string leaves."""
    if isinstance(obj, str):
        return 1
    if isinstance(obj, dict):
        return sum(count_strings(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(count_strings(v) for v in obj if v is not None)
    return 0


def count_russian(obj):
    """Count string leaves that contain Russian."""
    if isinstance(obj, str):
        return 1 if has_russian(obj) else 0
    if isinstance(obj, dict):
        return sum(count_russian(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(count_russian(v) for v in obj if v is not None)
    return 0


def call_llm(payload, url, api_key):
    """POST payload to the proxy and return the assistant message text."""
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    headers = {'Content-Type': 'application/json'}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode('utf-8'))
    content = result['choices'][0]['message']['content']
    if content is None:
        finish_reason = result['choices'][0].get('finish_reason', 'unknown')
        raise ValueError(f'LLM returned null content (finish_reason={finish_reason!r})')
    return content


def parse_json_response(text):
    """Strip optional markdown fences and parse JSON."""
    text = text.strip()
    # Strip ```json ... ``` or ``` ... ```
    text = re.sub(r'^```[a-z]*\s*', '', text)
    text = re.sub(r'\s*```$', '', text.strip())
    return json.loads(text.strip())


def merge_translations(original, translated):
    """Overlay translated values onto original, keeping only Russian results.

    - null stays null
    - translated string with Russian -> use it
    - translated string without Russian (untranslated) -> keep original
    - structure mismatch -> keep original
    """
    if original is None:
        return None

    if isinstance(original, str):
        if isinstance(translated, str) and has_russian(translated):
            return translated
        return original

    if isinstance(original, dict) and isinstance(translated, dict):
        result = {}
        for key, orig_val in original.items():
            trans_val = translated.get(key)
            result[key] = merge_translations(orig_val, trans_val)
        return result

    if isinstance(original, list) and isinstance(translated, list):
        result = []
        for i, orig_item in enumerate(original):
            trans_item = translated[i] if i < len(translated) else None
            result.append(merge_translations(orig_item, trans_item))
        return result

    return original


def translate_chunk(chunk, url, model, api_key):
    """Send a dict chunk to the LLM and return the translated dict."""
    payload = {
        'model': model,
        'temperature': 0.2,
        'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': 'Translate the following JSON from English to Russian:\n' +
             json.dumps(chunk, indent=2, ensure_ascii=False)},
        ],
    }
    raw = call_llm(payload, url, api_key)
    return parse_json_response(raw)


def translate_nested(data, url, model, api_key):
    """Send the full nested structure to the LLM in one call."""
    payload = {
        'model': model,
        'temperature': 0.2,
        'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content':
             'Translate all English string values in this JSON to Russian. '
             'Keep null values as null. Keep the exact structure:\n' +
             json.dumps(data, indent=2, ensure_ascii=False)},
        ],
    }
    raw = call_llm(payload, url, api_key)
    return parse_json_response(raw)


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
    parser = argparse.ArgumentParser(description='Translate artifacts/to_translate/ via LLM proxy.')
    parser.add_argument('--url', default=os.environ.get('PROXY_URL', 'http://127.0.0.1:8000/v1/chat/completions'))
    parser.add_argument('--model', default=os.environ.get('PROXY_MODEL', 'gemini_cli/gemini-2.5-flash'))
    parser.add_argument('--api-key', default=os.environ.get('PROXY_API_KEY', 'VerysecretKey'))
    parser.add_argument('--chunk-size', type=int, default=50)
    parser.add_argument('--delay', type=float, default=0.0)
    args = parser.parse_args()

    if not os.path.isdir(TO_TRANSLATE_DIR):
        print(f'Error: {TO_TRANSLATE_DIR} not found. Run find_untranslated.py first.', file=sys.stderr)
        sys.exit(1)

    json_files = collect_json_files(TO_TRANSLATE_DIR)
    total_files = len(json_files)
    if total_files == 0:
        print('No files in to_translate. Run find_untranslated.py first.')
        return

    total_keys = sum(
        count_strings(json.load(open(os.path.join(TO_TRANSLATE_DIR, f), encoding='utf-8')))
        for f in json_files
    )

    total_translated = 0
    total_attempted = 0

    for file_idx, rel_path in enumerate(json_files, start=1):
        full_path = os.path.join(TO_TRANSLATE_DIR, rel_path)
        display = os.path.join('assets', rel_path).replace('\\', '/')

        with open(full_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        n_strings = count_strings(data)

        if is_flat(data):
            # Split into chunks of non-null keys
            keys = [k for k, v in data.items() if v is not None]
            n_chunks = max(1, (len(keys) + args.chunk_size - 1) // args.chunk_size)
            print(f'[{file_idx}/{total_files}] {display} ({len(keys)} keys, {n_chunks} chunk{"s" if n_chunks > 1 else ""})')

            for chunk_idx in range(n_chunks):
                chunk_keys = keys[chunk_idx * args.chunk_size:(chunk_idx + 1) * args.chunk_size]
                chunk = {k: data[k] for k in chunk_keys}
                label = f'  chunk {chunk_idx + 1}/{n_chunks}'
                print(f'{label} ... ', end='', flush=True)
                total_attempted += len(chunk_keys)

                if args.delay and (file_idx > 1 or chunk_idx > 0):
                    time.sleep(args.delay)

                try:
                    translated_chunk = translate_chunk(chunk, args.url, args.model, args.api_key)
                    merged = merge_translations(chunk, translated_chunk)
                    applied = count_russian(merged)
                    total_translated += applied
                    data.update(merged)
                    print(f'ok ({applied}/{len(chunk_keys)} translated)')
                except (urllib.error.URLError, json.JSONDecodeError, KeyError, ValueError) as e:
                    print(f'FAILED ({e})')

        else:
            # Nested structure — one call for the whole file
            print(f'[{file_idx}/{total_files}] {display} (nested, {n_strings} strings)')
            total_attempted += n_strings

            if args.delay and file_idx > 1:
                time.sleep(args.delay)

            try:
                translated = translate_nested(data, args.url, args.model, args.api_key)
                data = merge_translations(data, translated)
                applied = count_russian(data)
                total_translated += applied
                print(f'  ok ({applied}/{n_strings} translated)')
            except (urllib.error.URLError, json.JSONDecodeError, KeyError, ValueError) as e:
                print(f'  FAILED ({e})')

        with open(full_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    print(f'\nDone. {total_translated}/{total_keys} keys translated across {total_files} files.')
    if total_translated < total_keys:
        remaining = total_keys - total_translated
        print(f'{remaining} keys still need translation — re-run or check failed chunks.')


if __name__ == '__main__':
    main()
