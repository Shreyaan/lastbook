#!/usr/bin/env python3
"""Pins the encyclopedias setup offers one by one (content/encyclopedias.json), from Kiwix's public catalog.

  python3 -I -B tools/kiwix_pins.py [--base URL]    rewrite content/encyclopedias.json with the newest file of each book below
  python3 -I -B tools/kiwix_pins.py --check         exit 1 if a pinned file no longer matches what Kiwix publishes

Each entry pins a file name, size and SHA-256 (from Kiwix's .meta4) and the address setup downloads it from: BASE with
{folder} (Kiwix's folder, such as wikipedia) and {file} filled in. The default is Lastbook's own download site, where the
publisher mirrors each file unchanged (its SHA-256 is Kiwix's); setup's downloader must allow the host BASE names
(engine/setup/manifest.py PINNED_HOSTS). Run it by hand when the books should move to newer editions.
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / 'content' / 'encyclopedias.json'
CATALOG = 'https://library.kiwix.org/catalog/v2/entries?count=-1&name={name}'
META4 = 'https://download.kiwix.org/zim/{folder}/{file}.meta4'
BASE = 'https://downloads.lastbook.org/encyclopedias/{file}'

ENGLISH = [
    # (book, title, summary, icon): the four the packs carry, so a smaller library can still have any of them
    ('mdwiki_en_all_maxi', 'Medical encyclopedia', 'MDWiki: plain-language articles on conditions, medicines and first aid.', 'health'),
    ('appropedia_en_all_maxi', 'Appropedia', 'Appropriate technology: water, sanitation, energy and building projects.', 'repair'),
    ('wikibooks_en_all_maxi', 'Wikibooks', 'Open textbooks and how-to manuals, from cooking to electronics.', 'book'),
    ('wikipedia_en_all_nopic', 'Wikipedia', 'The full English encyclopedia, text only (no pictures).', 'wiki'),
]
INDIAN = [   # (Kiwix language code, language name in English, in its own script)
    ('hi', 'Hindi', 'हिन्दी'), ('bn', 'Bengali', 'বাংলা'), ('te', 'Telugu', 'తెలుగు'), ('mr', 'Marathi', 'मराठी'),
    ('ta', 'Tamil', 'தமிழ்'), ('ur', 'Urdu', 'اردو'), ('gu', 'Gujarati', 'ગુજરાતી'), ('kn', 'Kannada', 'ಕನ್ನಡ'),
    ('ml', 'Malayalam', 'മലയാളം'), ('or', 'Odia', 'ଓଡ଼ିଆ'), ('pa', 'Punjabi', 'ਪੰਜਾਬੀ'), ('as', 'Assamese', 'অসমীয়া'),
]


def books():
    for book, title, summary, icon in ENGLISH:
        yield {'id': book, 'title': title, 'summary': summary, 'icon': icon, 'lang': 'en', 'group': 'English'}
    for code, name, native in INDIAN:
        yield {'id': f'wikipedia_{code}_medicine_nopic', 'title': f'Medical Wikipedia in {name} ({native})',
               'summary': f'Wikipedia’s health and medicine articles in {name}, text only.', 'icon': 'health', 'lang': code,
               'group': 'Indian languages'}
        yield {'id': f'wikipedia_{code}_all_nopic', 'title': f'Wikipedia in {name} ({native})',
               'summary': f'The whole {name} Wikipedia, text only (no pictures).', 'icon': 'wiki', 'lang': code,
               'group': 'Indian languages'}


def get(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'lastbook-pins/1'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode('utf-8')


def newest_file(book):
    """(folder, file name) of the newest edition of a book, from the catalog's acquisition link."""
    stem, flavour = book.rsplit('_', 1)
    xml = get(CATALOG.format(name=stem))
    files = []
    for entry in re.findall(r'<entry>(.*?)</entry>', xml, re.S):
        link = re.search(r'href="https://(?:lb\.)?download\.kiwix\.org/zim/([a-z_]+)/(' + re.escape(book) + r'_\d{4}-\d{2}[a-z]?\.zim)\.meta4"', entry)
        if link:
            files.append((link.group(2), link.group(1)))
    if not files:
        raise SystemExit(f'{book}: not in the Kiwix catalog')
    name, folder = max(files)          # names end in _YYYY-MM (a re-issue adds a letter), so the largest is the newest
    return folder, name


def pin(folder, name, base=BASE):
    meta = get(META4.format(folder=folder, file=name))
    size = re.search(r'<size>(\d+)</size>', meta)
    sha = re.search(r'<hash type="sha-256">([0-9a-f]{64})</hash>', meta)
    if not (size and sha):
        raise SystemExit(f'{name}: no size or SHA-256 in its .meta4')
    return {'name': name, 'url': base.format(folder=folder, file=name), 'size': int(size.group(1)), 'sha256': sha.group(1)}


def main():
    if '--check' in sys.argv:
        bad = []
        for e in json.loads(OUT.read_text(encoding='utf-8'))['encyclopedias']:
            f = e['file']
            got = pin(e['kiwix_folder'], f['name'])
            if (got['size'], got['sha256']) != (f['size'], f['sha256']):
                bad.append(f['name'])
            print(('ok   ' if f['name'] not in bad else 'BAD  ') + f['name'])
        sys.exit(1 if bad else 0)
    base = sys.argv[sys.argv.index('--base') + 1] if '--base' in sys.argv else BASE
    out = []
    for b in books():
        folder, name = newest_file(b['id'])
        b['kiwix_folder'] = folder
        b['file'] = pin(folder, name, base)
        out.append(b)
        print(f"{b['file']['size'] / 1e9:7.2f} GB  {name}")
    OUT.write_text(json.dumps({'about': 'Encyclopedias setup offers one by one. Written by tools/kiwix_pins.py; edit that, not this.',
                               'encyclopedias': out}, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print(f'wrote {OUT} ({len(out)} encyclopedias)')


if __name__ == '__main__':
    main()
