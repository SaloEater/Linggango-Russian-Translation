#!/usr/bin/env python3
"""Unified modpack translation pipeline.

Replaces the eight per-content-type scripts (find_untranslated*, translate_*,
pull_translations, sync_*) with one script, one config table, and three modes
selected by CLI flags instead of editing module constants.

Content types (--type):
  mods       artifacts/assets            -> resourcepacks/.../assets      (lang)
  patchouli  artifacts/assets_patchouli  -> resourcepacks/.../assets      (patchouli)
  kjs        artifacts/assets_kjs        -> kubejs/assets                 (lang)
             (kjs absorbs ftbquestlocalizer/lang, enchdesc/lang, etc.)
  all        every type above, in order: mods, patchouli, kjs

Modes:
  find       extract untranslated lines  artifacts -> to_translate
  translate  translate to_translate files in place (proxy or claude backend)
  sync       pull translated lines back into resourcepacks, THEN full sync
             artifacts -> resourcepacks (merge/copy + prune)

Pipeline:  find  ->  translate  ->  sync

Note: `mods` and `patchouli` share the resourcepacks/.../assets root but stay
disjoint — `mods` (lang) only touches */lang/*.json, `patchouli` only touches
non-lang files (is_lang_file). Directory pruning during a lang sync unions ALL
artifact roots mapping to the same resourcepacks root, so a mod folder present
in either artifacts/assets or artifacts/assets_patchouli is never deleted.

WARNING: `sync` is destructive. merge_recursive drops resourcepack keys absent
from artifacts, prune_dirs rmtree's folders absent from artifacts, and patchouli
sync os.remove's files absent from artifacts. Use --dry-run first; git is the
safety net. Patchouli sync never merges CONTENT of files present in both sides —
changed English source text is refreshed only through find -> translate -> sync.

Examples:
  python scripts/translate_modpack.py find --type kjs --dry-run
  python scripts/translate_modpack.py translate --type kjs --backend claude --limit 1
  python scripts/translate_modpack.py sync --type mods --dry-run
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

RUSSIAN_RE = re.compile(r'[а-яА-ЯёЁ]')
# Matches dotted identifier values like "guide.animus.entry.foo" or "mod:cat.sub"
TRANSLATION_KEY_RE = re.compile(r'^[\w:]+(?:\.[\w:]+)+$')
# Patchouli: only string leaves under these keys are translatable text.
TARGET_KEYS = frozenset({'name', 'description', 'title', 'text', 'landing_text'})

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

CONTENT_TYPES = {
    'mods': dict(
        kind='lang',
        artifacts=('artifacts', 'assets'),
        resourcepacks=('resourcepacks', 'Community Russian Translations', 'assets'),
        to_translate=('artifacts', 'to_translate'),
    ),
    'patchouli': dict(
        kind='patchouli',
        artifacts=('artifacts', 'assets_patchouli'),
        resourcepacks=('resourcepacks', 'Community Russian Translations', 'assets'),
        to_translate=('artifacts', 'to_translate_patchouli'),
    ),
    'kjs': dict(
        kind='lang',
        artifacts=('artifacts', 'assets_kjs'),
        resourcepacks=('kubejs', 'assets'),
        to_translate=('artifacts', 'to_translate_kjs'),
    ),
}
# Deterministic order for --type all (patchouli adjacent to mods so their
# shared-resourcepacks interaction is exercised together).
TYPE_ORDER = ['mods', 'patchouli', 'kjs']

# Resourcepack folders (relative to a resourcepacks root) that exist ONLY in the
# resourcepack and have no artifact source — hand-maintained assets that sync
# must never delete during pruning. Extend this as new such assets are added.
PRUNE_EXCLUDE = {
    'brutality/font',                 # gamer.ttf — the Russian font
    'immersiveengineering/manual',    # translated IE manual (not sourced from artifacts)
    # TEMPORARY: quest source still lives in artifacts/assets_quests, not yet
    # migrated under artifacts/assets_kjs. Protects the 1.2MB quest translation
    # from a kjs sync until that migration happens; remove this line afterward.
    'ftbquestlocalizer',
}

DEFAULT_MODEL = {'proxy': 'gemini_cli/gemini-2.5-flash', 'claude': 'claude-haiku-4-5'}


def d(cfg, key):
    """Join a config path tuple into an OS path string."""
    return os.path.join(*cfg[key])


def artifact_roots_for_resource(resource_dir):
    """All artifact roots (as path strings) whose resourcepacks root == resource_dir.

    Used so that pruning a shared resourcepacks tree (mods + patchouli both map
    to resourcepacks/.../assets) never deletes a folder that belongs to the
    other content type.
    """
    roots = []
    for spec in CONTENT_TYPES.values():
        if os.path.join(*spec['resourcepacks']) == resource_dir:
            roots.append(os.path.join(*spec['artifacts']))
    return roots


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def has_russian(text):
    return bool(RUSSIAN_RE.search(text))


def is_translation_key(text):
    return bool(TRANSLATION_KEY_RE.match(text))


def is_lang_file(rel_path):
    return 'lang' in rel_path.replace('\\', '/').split('/')


def is_flat(data):
    """True if every top-level value is a str or None (flat lang file)."""
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


def count_applied(obj):
    """Count Russian string leaves (what pull would apply). Lists skip None."""
    return count_russian(obj)


def load_json(path):
    """Load JSON, tolerating missing / empty / corrupt files (-> None).

    A 0-byte or whitespace-only resourcepack file is treated as "not translated"
    instead of crashing; a non-empty invalid file prints a warning.
    """
    if not os.path.isfile(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f'  warning: skipping unreadable JSON {path} ({e})', file=sys.stderr)
        return None


def save_json(path, data):
    """Atomically write data to path (temp file + os.replace).

    open('w') truncates to 0 bytes immediately, so a process killed mid-write
    leaves an empty/partial file. Temp-file + rename means the destination is
    only ever the previous content or the complete new content.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def collect_json_files(base_dir, patchouli_only=False):
    """Sorted relative .json paths under base_dir. patchouli_only skips lang files."""
    files = []
    for root, _dirs, filenames in os.walk(base_dir):
        for filename in filenames:
            if not filename.endswith('.json'):
                continue
            rel_path = os.path.relpath(os.path.join(root, filename), base_dir)
            if patchouli_only and is_lang_file(rel_path):
                continue
            files.append(rel_path)
    files.sort()
    return files


def parse_json_response(text):
    """Strip optional markdown fences and parse JSON."""
    text = text.strip()
    text = re.sub(r'^```[a-z]*\s*', '', text)
    text = re.sub(r'\s*```$', '', text.strip())
    return json.loads(text.strip())


# --------------------------------------------------------------------------- #
# FIND
# --------------------------------------------------------------------------- #

def build_to_translate(artifact, resource, kind, current_key=None):
    """Return the subset of artifact that still needs translation, or None.

    kind == 'lang'      : every non-Russian string leaf is a candidate.
    kind == 'patchouli' : only strings whose immediate dict key is in TARGET_KEYS,
                          skipping dotted-id references (is_translation_key).
    Lists keep None placeholders for already-done / non-target positions so
    pull_translations can realign by index.
    """
    if isinstance(artifact, str):
        if not artifact:
            return None
        if kind == 'patchouli':
            if current_key not in TARGET_KEYS:
                return None
            if is_translation_key(artifact):
                return None
        if has_russian(artifact):
            return None  # artifact already Russian (sync handles it)
        if isinstance(resource, str) and has_russian(resource):
            return None  # already translated in resourcepacks
        return artifact

    if isinstance(artifact, dict):
        result = {}
        for key, val in artifact.items():
            res_val = resource.get(key) if isinstance(resource, dict) else None
            child_key = key if kind == 'patchouli' else None
            filtered = build_to_translate(val, res_val, kind, current_key=child_key)
            if filtered is not None:
                result[key] = filtered
        return result if result else None

    if isinstance(artifact, list):
        result = []
        has_any = False
        for i, item in enumerate(artifact):
            res_item = resource[i] if isinstance(resource, list) and i < len(resource) else None
            filtered = build_to_translate(item, res_item, kind, current_key=None)
            if filtered is not None:
                result.append(filtered)
                has_any = True
            else:
                result.append(None)  # placeholder preserves index alignment
        return result if has_any else None

    return None  # non-string leaf


def run_find(cfg, dry_run=False, only_file=None):
    artifacts_dir = d(cfg, 'artifacts')
    resource_dir = d(cfg, 'resourcepacks')
    to_translate_dir = d(cfg, 'to_translate')
    kind = cfg['kind']

    if not os.path.isdir(artifacts_dir):
        print(f'Error: {artifacts_dir} not found.', file=sys.stderr)
        return

    json_files = collect_json_files(artifacts_dir, patchouli_only=(kind == 'patchouli'))
    total = len(json_files)
    if total == 0:
        print(f'No JSON files found in {artifacts_dir}.')
        return

    total_keys = 0
    files_written = 0
    for idx, rel_path in enumerate(json_files, start=1):
        if only_file and rel_path.replace('\\', '/') != only_file:
            continue
        artifact_data = load_json(os.path.join(artifacts_dir, rel_path))
        if artifact_data is None:
            print(f'[{idx}/{total}] {rel_path} — skipped (empty/unreadable artifact)')
            continue
        resource_data = load_json(os.path.join(resource_dir, rel_path))
        to_translate = build_to_translate(artifact_data, resource_data, kind)

        if to_translate:
            n = count_strings(to_translate)
            total_keys += n
            files_written += 1
            tag = ' [dry-run]' if dry_run else ''
            print(f'[{idx}/{total}] {rel_path} — {n} untranslated{tag}')
            if not dry_run:
                save_json(os.path.join(to_translate_dir, rel_path), to_translate)
        else:
            print(f'[{idx}/{total}] {rel_path}')

    verb = 'would extract' if dry_run else 'extracted'
    print(f'Done. {verb} {total_keys} untranslated keys across {files_written} files '
          f'-> {to_translate_dir}')


# --------------------------------------------------------------------------- #
# TRANSLATE
# --------------------------------------------------------------------------- #

def call_llm(user_content, model, url, api_key):
    """POST to an OpenAI-compatible proxy; return the assistant message text."""
    payload = {
        'model': model,
        'temperature': 0.2,
        'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': user_content},
        ],
    }
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    headers = {'Content-Type': 'application/json'}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode('utf-8'))
    content = result['choices'][0]['message']['content']
    if content is None:
        finish = result['choices'][0].get('finish_reason', 'unknown')
        raise ValueError(f'LLM returned null content (finish_reason={finish!r})')
    return content


def call_claude(user_content, model, timeout):
    """Run `claude -p` headless with a minimal system prompt; return stdout text.

    Token-minimising flags: --system-prompt replaces the large default agent
    prompt, --exclude-dynamic-system-prompt-sections drops env/git context,
    --disallowedTools '*' avoids tool schemas / file reads. Needs no API key.
    """
    cmd = [
        'claude', '-p',
        '--model', model,
        '--system-prompt', SYSTEM_PROMPT,
        '--exclude-dynamic-system-prompt-sections',
        '--disallowedTools', '*',
    ]
    proc = subprocess.run(cmd, input=user_content, capture_output=True,
                          text=True, timeout=timeout)
    if proc.returncode != 0:
        raise ValueError(f'claude exited {proc.returncode}: {proc.stderr.strip()[:200]}')
    out = proc.stdout.strip()
    if not out:
        raise ValueError(f'claude returned empty output (stderr: {proc.stderr.strip()[:200]})')
    return out


def llm_translate(user_content, backend, model, url, api_key, timeout):
    if backend == 'claude':
        return call_claude(user_content, model, timeout)
    return call_llm(user_content, model, url, api_key)


def strip_translated(obj):
    """Copy of obj with already-Russian / non-translatable leaves removed (-> None).

    Lets nested files skip lines that already contain Russian, so resumes cost
    no tokens for finished keys. Lists keep None placeholders for alignment.
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


def merge_translations(original, translated):
    """Overlay translated onto original, keeping only Russian results."""
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


def _debug_dump(label, text):
    bar = '─' * 12
    print(f'\n{bar} DEBUG {label} {bar}\n{text}\n{bar} end {label} {bar}', file=sys.stderr, flush=True)


def translate_chunk(chunk, backend, model, url, api_key, timeout, debug=False):
    content = USER_PREFIX_FLAT + json.dumps(chunk, indent=2, ensure_ascii=False)
    if debug:
        _debug_dump('sent', content)
    raw = llm_translate(content, backend, model, url, api_key, timeout)
    if debug:
        _debug_dump('received', raw)
    return parse_json_response(raw)


def translate_nested(data, backend, model, url, api_key, timeout, debug=False):
    content = USER_PREFIX_NESTED + json.dumps(data, indent=2, ensure_ascii=False)
    if debug:
        _debug_dump('sent', content)
    raw = llm_translate(content, backend, model, url, api_key, timeout)
    if debug:
        _debug_dump('received', raw)
    return parse_json_response(raw)


TRANSLATE_ERRORS = (urllib.error.URLError, subprocess.TimeoutExpired,
                    json.JSONDecodeError, KeyError, ValueError)


def run_translate(cfg, args):
    to_translate_dir = d(cfg, 'to_translate')
    if not os.path.isdir(to_translate_dir):
        print(f'Error: {to_translate_dir} not found. Run find first.', file=sys.stderr)
        return

    json_files = collect_json_files(to_translate_dir)
    if args.file:
        json_files = [f for f in json_files if f.replace('\\', '/') == args.file]
    if args.limit is not None:
        json_files = json_files[:args.limit]
    total_files = len(json_files)
    if total_files == 0:
        print(f'No files to translate in {to_translate_dir}.')
        return

    model = args.model or DEFAULT_MODEL[args.backend]
    total_keys = 0
    for f in json_files:
        d0 = load_json(os.path.join(to_translate_dir, f))
        if d0 is not None:
            total_keys += count_strings(d0) - count_russian(d0)

    total_translated = 0
    for file_idx, rel_path in enumerate(json_files, start=1):
        full_path = os.path.join(to_translate_dir, rel_path)
        data = load_json(full_path)
        if data is None:
            print(f'[{file_idx}/{total_files}] {rel_path} — skipped (empty/unreadable)')
            continue

        if is_flat(data):
            keys = [k for k, v in data.items() if v is not None and not has_russian(v)]
            if not keys:
                print(f'[{file_idx}/{total_files}] {rel_path} — already translated, skipped')
                continue
            n_chunks = max(1, (len(keys) + args.chunk_size - 1) // args.chunk_size)
            print(f'[{file_idx}/{total_files}] {rel_path} '
                  f'({len(keys)} keys, {n_chunks} chunk{"s" if n_chunks > 1 else ""})')
            for chunk_idx in range(n_chunks):
                chunk_keys = keys[chunk_idx * args.chunk_size:(chunk_idx + 1) * args.chunk_size]
                chunk = {k: data[k] for k in chunk_keys}
                print(f'  chunk {chunk_idx + 1}/{n_chunks} ... ', end='', flush=True)
                if args.delay and (file_idx > 1 or chunk_idx > 0):
                    time.sleep(args.delay)
                try:
                    translated_chunk = translate_chunk(chunk, args.backend, model,
                                                       args.url, args.api_key, args.timeout,
                                                       debug=args.debug)
                    merged = merge_translations(chunk, translated_chunk)
                    if args.debug:
                        applied_dbg = count_russian(merged)
                        if applied_dbg == 0:
                            _debug_dump('parsed', json.dumps(translated_chunk, ensure_ascii=False, indent=2))
                            print('  DEBUG: 0 applied — parsed response above had no Cyrillic '
                                  'values matching the sent keys.', file=sys.stderr, flush=True)
                    applied = count_russian(merged)
                    total_translated += applied
                    data.update(merged)
                    save_json(full_path, data)  # persist per chunk (crash-safe)
                    print(f'ok ({applied}/{len(chunk_keys)} translated)')
                except TRANSLATE_ERRORS as e:
                    print(f'FAILED ({e})')
        else:
            subset = strip_translated(data)
            remaining = count_strings(subset) if subset is not None else 0
            if remaining == 0:
                print(f'[{file_idx}/{total_files}] {rel_path} — already translated, skipped')
                continue
            print(f'[{file_idx}/{total_files}] {rel_path} (nested, {remaining} strings)')
            if args.delay and file_idx > 1:
                time.sleep(args.delay)
            try:
                translated = translate_nested(subset, args.backend, model,
                                              args.url, args.api_key, args.timeout,
                                              debug=args.debug)
                before = count_russian(data)
                data = merge_translations(data, translated)
                applied = count_russian(data) - before
                total_translated += applied
                save_json(full_path, data)
                print(f'  ok ({applied}/{remaining} translated)')
            except TRANSLATE_ERRORS as e:
                print(f'  FAILED ({e})')

    print(f'\nDone. {total_translated}/{total_keys} keys translated across {total_files} files.')
    if total_translated < total_keys:
        print(f'{total_keys - total_translated} keys still need translation — re-run.')


# --------------------------------------------------------------------------- #
# PULL (part of sync)
# --------------------------------------------------------------------------- #

def apply_translations(to_translate, resource):
    """Overlay Russian values from to_translate onto resource (never deletes)."""
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
                continue  # placeholder — leave resource entry as-is
            res_item = result[i] if i < len(result) else None
            merged = apply_translations(item, res_item)
            if i < len(result):
                result[i] = merged
            else:
                result.append(merged)
        return result
    return resource


def run_pull(cfg, dry_run=False):
    to_translate_dir = d(cfg, 'to_translate')
    resource_dir = d(cfg, 'resourcepacks')
    if not os.path.isdir(to_translate_dir):
        print(f'  pull: {to_translate_dir} not found — nothing to pull.')
        return

    json_files = collect_json_files(to_translate_dir)
    total = len(json_files)
    total_applied = 0
    files_modified = 0
    for idx, rel_path in enumerate(json_files, start=1):
        tt_data = load_json(os.path.join(to_translate_dir, rel_path))
        n_applied = count_applied(tt_data) if tt_data is not None else 0
        if n_applied == 0:
            continue
        resource_path = os.path.join(resource_dir, rel_path)
        resource_data = load_json(resource_path)
        if resource_data is None:
            resource_data = {}
        merged = apply_translations(tt_data, resource_data)
        total_applied += n_applied
        files_modified += 1
        tag = ' [dry-run]' if dry_run else ''
        print(f'  pull [{idx}/{total}] {rel_path} — {n_applied} keys{tag}')
        if not dry_run:
            save_json(resource_path, merged)
    verb = 'would apply' if dry_run else 'applied'
    print(f'  pull: {verb} {total_applied} keys across {files_modified} files.')


# --------------------------------------------------------------------------- #
# SYNC
# --------------------------------------------------------------------------- #

def merge_recursive(artifact, resource):
    """Merge artifact into resource (artifact-authoritative structure).

    - dict: recurse on artifact keys; resource-only keys are dropped.
    - list: index-align; extra resource elements dropped.
    - string leaf: artifact-Russian overwrites; artifact-not-Russian keeps the
      existing resource string (protects freshly-pulled translations).
    """
    if isinstance(artifact, dict) and isinstance(resource, dict):
        result = {}
        for key in artifact:
            if key in resource:
                result[key] = merge_recursive(artifact[key], resource[key])
            else:
                result[key] = artifact[key]
        return result
    if isinstance(artifact, list) and isinstance(resource, list):
        result = []
        for i in range(max(len(artifact), len(resource))):
            if i < len(artifact) and i < len(resource):
                result.append(merge_recursive(artifact[i], resource[i]))
            elif i < len(artifact):
                result.append(artifact[i])
        return result
    if isinstance(artifact, str):
        if has_russian(artifact):
            return artifact
        if isinstance(resource, str):
            return resource
        return artifact
    return artifact


def prune_status(rel):
    """Classify a resource-relative dir against PRUNE_EXCLUDE.

    'protected' — the dir is (or is inside) an excluded path: keep, don't descend.
    'ancestor'  — the dir contains an excluded path deeper down: keep AND descend
                  (so the protected child is reached even if this parent has no
                  artifact source).
    'normal'    — subject to the usual artifact-presence prune rule.
    """
    rel = rel.replace('\\', '/')
    for ex in PRUNE_EXCLUDE:
        if rel == ex or rel.startswith(ex + '/'):
            return 'protected'
    for ex in PRUNE_EXCLUDE:
        if ex.startswith(rel + '/'):
            return 'ancestor'
    return 'normal'


def prune_dirs(artifact_roots, resource_dir, dry_run=False):
    """Remove resource subdirs absent from EVERY artifact root. Returns removed rels.

    Unioning artifact roots keeps a folder that belongs to another content type
    sharing the same resourcepacks root (mods vs patchouli). PRUNE_EXCLUDE paths
    (and their ancestors) are never removed.
    """
    removed = []
    for root, dirs, _files in os.walk(resource_dir, topdown=True):
        kept = []
        for name in dirs:
            rel = os.path.relpath(os.path.join(root, name), resource_dir)
            status = prune_status(rel)
            if status == 'protected':
                continue  # keep on disk, do not descend into the protected subtree
            if status == 'ancestor':
                kept.append(name)  # keep + descend to reach the protected child
                continue
            if any(os.path.isdir(os.path.join(ar, rel)) for ar in artifact_roots):
                kept.append(name)
            else:
                if not dry_run:
                    shutil.rmtree(os.path.join(root, name))
                removed.append(rel.replace('\\', '/'))
        dirs[:] = kept  # don't descend into removed dirs
    return removed


def collect_files(base_dir):
    """Set of relative non-lang .json paths under base_dir (patchouli sync)."""
    files = set()
    for root, _dirs, filenames in os.walk(base_dir):
        for filename in filenames:
            if not filename.endswith('.json'):
                continue
            rel = os.path.relpath(os.path.join(root, filename), base_dir)
            if is_lang_file(rel):
                continue
            files.add(rel)
    return files


def remove_empty_dirs(base_dir):
    """Bottom-up rmdir of empty directories under base_dir. Returns removed rels."""
    removed = []
    for root, _dirs, _files in os.walk(base_dir, topdown=False):
        if os.path.abspath(root) == os.path.abspath(base_dir):
            continue
        try:
            os.rmdir(root)
            removed.append(os.path.relpath(root, base_dir).replace('\\', '/'))
        except OSError:
            pass  # not empty
    return removed


def sync_lang(cfg, dry_run=False):
    artifacts_dir = d(cfg, 'artifacts')
    resource_dir = d(cfg, 'resourcepacks')
    if not os.path.isdir(artifacts_dir):
        print(f'  sync: {artifacts_dir} not found — skipping.')
        return

    json_files = collect_json_files(artifacts_dir)
    total = len(json_files)
    changed = 0
    for idx, rel_path in enumerate(json_files, start=1):
        artifact_data = load_json(os.path.join(artifacts_dir, rel_path))
        if artifact_data is None:
            continue
        resource_path = os.path.join(resource_dir, rel_path)
        resource_data = load_json(resource_path)
        if resource_data is not None:
            merged = merge_recursive(artifact_data, resource_data)
        else:
            merged = artifact_data
        if merged != resource_data:
            changed += 1
            if not dry_run:
                save_json(resource_path, merged)
    tag = ' [dry-run]' if dry_run else ''
    print(f'  sync: {changed}/{total} files changed{tag}')

    roots = artifact_roots_for_resource(resource_dir)
    if os.path.isdir(resource_dir):
        removed = prune_dirs(roots, resource_dir, dry_run=dry_run)
        for rel in removed:
            print(f'  {"would remove" if dry_run else "removed"} folder: {rel}')
        if removed:
            print(f'  sync: {len(removed)} folder(s) not present in artifacts '
                  f'{"would be " if dry_run else ""}removed.')


def sync_patchouli(cfg, dry_run=False):
    artifacts_dir = d(cfg, 'artifacts')
    resource_dir = d(cfg, 'resourcepacks')
    if not os.path.isdir(artifacts_dir):
        print(f'  sync: {artifacts_dir} not found — skipping.')
        return

    artifact_files = collect_files(artifacts_dir)
    resource_files = collect_files(resource_dir) if os.path.isdir(resource_dir) else set()
    to_add = sorted(artifact_files - resource_files)
    to_remove = sorted(resource_files - artifact_files)

    for rel in to_add:
        tag = ' [dry-run]' if dry_run else ''
        print(f'  add: {rel}{tag}')
        if not dry_run:
            dst = os.path.join(resource_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(artifacts_dir, rel), dst)
    for rel in to_remove:
        tag = ' [dry-run]' if dry_run else ''
        print(f'  remove: {rel}{tag}')
        if not dry_run:
            os.remove(os.path.join(resource_dir, rel))
    if not dry_run and os.path.isdir(resource_dir):
        remove_empty_dirs(resource_dir)
    print(f'  sync: {len(to_add)} added, {len(to_remove)} removed '
          f'({len(artifact_files & resource_files)} unchanged){" [dry-run]" if dry_run else ""}')


def run_sync(cfg, dry_run=False):
    """Mode 3: pull translated lines, THEN full sync artifacts -> resourcepacks."""
    run_pull(cfg, dry_run=dry_run)
    if cfg['kind'] == 'patchouli':
        sync_patchouli(cfg, dry_run=dry_run)
    else:
        sync_lang(cfg, dry_run=dry_run)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser():
    parser = argparse.ArgumentParser(
        description='Unified modpack translation pipeline (find / translate / sync).')
    sub = parser.add_subparsers(dest='mode', required=True)
    type_choices = [*CONTENT_TYPES, 'all']

    p_find = sub.add_parser('find', help='extract untranslated lines -> to_translate')
    p_find.add_argument('--type', required=True, choices=type_choices)
    p_find.add_argument('--dry-run', action='store_true')
    p_find.add_argument('--file', help='limit to one relative json path')

    p_tr = sub.add_parser('translate', help='translate to_translate files in place')
    p_tr.add_argument('--type', required=True, choices=type_choices)
    p_tr.add_argument('--backend', choices=['proxy', 'claude'], default='proxy')
    p_tr.add_argument('--url', default=os.environ.get(
        'PROXY_URL', 'http://127.0.0.1:8000/v1/chat/completions'))
    p_tr.add_argument('--api-key', default=os.environ.get('PROXY_API_KEY', 'VerysecretKey'))
    p_tr.add_argument('--model', default=os.environ.get('PROXY_MODEL'),
                      help='default: gemini_cli/gemini-2.5-flash (proxy) or claude-haiku-4-5 (claude)')
    p_tr.add_argument('--chunk-size', type=int, default=80)
    p_tr.add_argument('--delay', type=float, default=0.0)
    p_tr.add_argument('--timeout', type=float, default=300.0)
    p_tr.add_argument('--limit', type=int, help='process only first N files')
    p_tr.add_argument('--file', help='limit to one relative json path')
    p_tr.add_argument('--debug', action='store_true',
                      help='print the exact content sent to and raw text received from the LLM '
                           '(to stderr); also dumps the parsed response when a chunk applies 0 keys')

    p_sync = sub.add_parser('sync', help='pull translations + full sync -> resourcepacks')
    p_sync.add_argument('--type', required=True, choices=type_choices)
    p_sync.add_argument('--dry-run', action='store_true')

    return parser


def resolve_types(type_arg):
    return list(TYPE_ORDER) if type_arg == 'all' else [type_arg]


def main():
    args = build_parser().parse_args()

    for type_name in resolve_types(args.type):
        cfg = CONTENT_TYPES[type_name]
        print(f'=== {type_name} ({cfg["kind"]}) ===')
        if args.mode == 'find':
            run_find(cfg, dry_run=args.dry_run, only_file=args.file)
        elif args.mode == 'translate':
            run_translate(cfg, args)
        elif args.mode == 'sync':
            run_sync(cfg, dry_run=args.dry_run)
        print()


if __name__ == '__main__':
    main()
