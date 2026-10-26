"""Make Class Enhancement class files translatable.

Every player-facing text field of each class file is replaced with a JSON text component
{"translate": <key>, "fallback": <original English>}: players with the Russian resource pack
see the ru_ru entry, everyone else the English fallback.

Usage:
    python3 scripts/convert_classes.py <path/to/config/class_enhancement/classes>

Writes
    config/class_enhancement/classes/*.json                 converted class files
    artifacts/class_enhancement/en_us.json                  every key with its English text
and merges artifacts/class_enhancement/ru_ru.json (if present) into
    resourcepacks/Community Russian Translations/assets/class_enhancement/lang/ru_ru.json
reporting keys that have no Russian text yet.

Converted fields: name, description, stats[i], skill.name, skill.description,
passives[i].name, passives[i].description. Tags are left as they are.
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_CLASSES = os.path.join(ROOT, 'config', 'class_enhancement', 'classes')
ARTIFACTS = os.path.join(ROOT, 'artifacts', 'class_enhancement')
PACK_LANG = os.path.join(ROOT, 'resourcepacks', 'Community Russian Translations', 'assets',
                         'class_enhancement', 'lang', 'ru_ru.json')

# The mod turns "&x" colour codes into "§x" for plain strings; text components need "§x" directly
LEGACY = re.compile(r'&([0-9a-fk-or])', re.IGNORECASE)


def legacy(text):
    return LEGACY.sub(r'§\1', text)


def component(key, value, english):
    """Replace a plain string with a translatable component; leave anything else alone."""
    if not isinstance(value, str):
        return value
    text = legacy(value)
    english[key] = text
    return {'translate': key, 'fallback': text}


def convert(class_id, data, english):
    prefix = f'class_enhancement.class.{class_id}'
    for field in ('name', 'description'):
        if field in data:
            data[field] = component(f'{prefix}.{field}', data[field], english)
    if isinstance(data.get('stats'), list):
        data['stats'] = [component(f'{prefix}.stat.{i}', s, english) for i, s in enumerate(data['stats'])]
    skill = data.get('skill')
    if isinstance(skill, dict):
        for field in ('name', 'description'):
            if field in skill:
                skill[field] = component(f'{prefix}.skill.{field}', skill[field], english)
    for i, passive in enumerate(data.get('passives') or []):
        if isinstance(passive, dict):
            for field in ('name', 'description'):
                if field in passive:
                    passive[field] = component(f'{prefix}.passive.{i}.{field}', passive[field], english)
    return data


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    source = sys.argv[1]
    os.makedirs(OUT_CLASSES, exist_ok=True)
    os.makedirs(ARTIFACTS, exist_ok=True)

    english = {}
    # os.listdir, not glob: instance paths like "Linggango - [V6.5 IS OUT]" contain glob characters
    for name in sorted(os.listdir(source)):
        if not name.endswith('.json'):
            continue
        with open(os.path.join(source, name), encoding='utf-8') as f:
            data = json.load(f)
        convert(name[:-5], data, english)
        with open(os.path.join(OUT_CLASSES, name), 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n')

    with open(os.path.join(ARTIFACTS, 'en_us.json'), 'w', encoding='utf-8') as f:
        json.dump(english, f, ensure_ascii=False, indent=2)
        f.write('\n')
    print(f'Converted {len(english)} strings into {OUT_CLASSES}')

    russian_path = os.path.join(ARTIFACTS, 'ru_ru.json')
    if not os.path.exists(russian_path):
        print('No artifacts/class_enhancement/ru_ru.json yet, nothing merged')
        return
    with open(russian_path, encoding='utf-8') as f:
        russian = json.load(f)
    missing = [k for k in english if k not in russian]
    stale = [k for k in russian if k not in english]

    pack = {}
    if os.path.exists(PACK_LANG):
        with open(PACK_LANG, encoding='utf-8') as f:
            pack = json.load(f)
    pack.update({k: v for k, v in russian.items() if k in english})
    with open(PACK_LANG, 'w', encoding='utf-8') as f:
        json.dump(dict(sorted(pack.items())), f, ensure_ascii=False, indent=2)
        f.write('\n')
    print(f'Merged {len(english) - len(missing)} Russian strings into {PACK_LANG}')
    if missing:
        print(f'{len(missing)} keys have no Russian text yet:')
        for k in missing:
            print('  ', k)
    if stale:
        print(f'{len(stale)} Russian keys no longer exist in the class files (not merged):')
        for k in stale:
            print('  ', k)


if __name__ == '__main__':
    main()
