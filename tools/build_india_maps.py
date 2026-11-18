#!/usr/bin/env python3
"""Detailed maps of India's states and union territories, one download each (docs/MAPS.md, "India").

  python3 -I -B tools/build_india_maps.py zones                 convert each Geofabrik India zone once
  python3 -I -B tools/build_india_maps.py states [IN-MH ...]    cut, check and zip each state (all when none named)
  python3 -I -B tools/build_india_maps.py pins BASE              write content/india-maps.json, each zip at BASE + its name

Uploading the zips to BASE is the publisher's step (packaging/state_maps.py's bucket, or any host setup's downloader allows:
engine/setup/manifest.py PINNED_HOSTS). Upload first, then run pins: setup checks each file against the SHA-256 pinned here.

Reads (in derived/india/src/, fetched by hand; see docs/MAPS.md):
  <zone>-latest-free.gpkg.zip      Geofabrik's India zone extracts, OpenStreetMap data (ODbL 1.0)
  ne_10m_admin_1_states_provinces.zip   Natural Earth admin-1 (public domain): only each state's bounding box is used
Writes derived/india/zones/<zone>.sqlite, derived/india/sets/<code>/LastbookData/..., derived/india/zips/Lastbook-Map-<code>.zip.

A state's set is what the app's state maps hold (engine/setup/install.py, map-set.json), cut the way US states are cut
(packaging/build_packs.py state_set): every OpenStreetMap feature and help point inside the state's box plus 0.15 degrees,
and a place finder of its named towns, villages and localities. There is no terrain, crop, water-inventory or building file:
India's equivalents of those US datasets are not in this pipeline yet, and the app draws a map without them.
"""
import hashlib
import json
import shutil
import sqlite3
import struct
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
from build_map import build                     # noqa: E402
from engine.setup import install                 # noqa: E402

WORK = ROOT / 'derived' / 'india'
SRC, ZONES, SETS, ZIPS = WORK / 'src', WORK / 'zones', WORK / 'sets', WORK / 'zips'
OUT_JSON = ROOT / 'content' / 'india-maps.json'
ZONE_NAMES = ['central-zone', 'eastern-zone', 'north-eastern-zone', 'northern-zone', 'southern-zone', 'western-zone']
MARGIN = 0.15                      # as for US states (packaging/build_packs.py state_box)
PART_LIMIT = 1_900_000_000         # GitHub Releases takes files under 2 GiB
SOURCE = 'OpenStreetMap contributors, via Geofabrik India zone extracts (ODbL 1.0)'
INCLUDES = 'Streets, towns and villages, plus hospitals, clinics, pharmacies, police, fire stations and drinking water'
PLACE_RANK = {'city': 1, 'town': 2, 'suburb': 3, 'village': 3, 'hamlet': 4, 'locality': 4, 'island': 3}
# Natural Earth carries some codes ISO has since changed; the app uses today's (ISO 3166-2:IN, Newsletter 2020-11-24 and
# later: IN-OR > IN-OD, IN-CT > IN-CG, IN-TG > IN-TS, IN-UT > IN-UK; IN-DD and IN-DN merged as IN-DH).
CODES = {'IN-OR': 'IN-OD', 'IN-CT': 'IN-CG', 'IN-TG': 'IN-TS', 'IN-UT': 'IN-UK', 'IN-DD': 'IN-DH', 'IN-DN': 'IN-DH'}
NAMES = {'IN-OD': 'Odisha', 'IN-CG': 'Chhattisgarh', 'IN-TS': 'Telangana', 'IN-UK': 'Uttarakhand', 'IN-DL': 'Delhi',
         'IN-DH': 'Dadra and Nagar Haveli and Daman and Diu', 'IN-AN': 'Andaman and Nicobar Islands',
         'IN-JK': 'Jammu and Kashmir', 'IN-LA': 'Ladakh', 'IN-PY': 'Puducherry'}
SCHEMA = '''CREATE TABLE feat(id INTEGER PRIMARY KEY, layer TEXT, cls TEXT, name TEXT, minz INTEGER, geom BLOB);
  CREATE VIRTUAL TABLE feat_idx USING rtree(id, minx, maxx, miny, maxy, minz, maxz);
  CREATE TABLE poi(id INTEGER PRIMARY KEY, cls TEXT, name TEXT, lon REAL, lat REAL);
  CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);'''        # tools/build_map.py's tables, which server.py reads


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


# ---------------------------------------------------------------------------------------------------------------- zones --
def zones():
    ZONES.mkdir(parents=True, exist_ok=True)
    for z in ZONE_NAMES:
        out = ZONES / f'{z}.sqlite'
        if out.exists():
            continue
        archive = SRC / f'{z}-latest-free.gpkg.zip'
        with zipfile.ZipFile(archive) as zf:
            member = next(i for i in zf.infolist() if i.filename.endswith('.gpkg'))
            as_of = '%04d-%02d-%02d' % member.date_time[:3]
            gpkg = WORK / 'tmp' / member.filename
            if not gpkg.exists():
                log('unzip', z)
                zf.extract(member, WORK / 'tmp')
        log('convert', z)
        tmp = out.with_suffix('.building')
        tmp.unlink(missing_ok=True)
        build(str(gpkg), str(tmp), [('source', SOURCE), ('data_as_of', as_of)])
        tmp.replace(out)
        gpkg.unlink()


# --------------------------------------------------------------------------------------------------------- state boxes --
def _dbf(data):
    """Records of a dBASE III table as dicts of stripped strings."""
    n, header, size = struct.unpack_from('<IHH', data, 4)
    fields, o = [], 32
    while data[o] != 0x0D:
        fields.append((data[o:o + 11].split(b'\0')[0].decode('ascii'), data[o + 16]))
        o += 32
    for r in range(n):
        rec, pos, row = header + r * size, 1, {}
        for name, length in fields:
            row[name] = data[rec + pos:rec + pos + length].decode('utf-8', 'replace').strip(' \x00')   # padded with spaces or NULs
            pos += length
        yield row


def _shp_rings(data):
    """Each record's rings as bounding boxes [(west, south, east, north), ...], in file order (polygon records)."""
    o = 100
    while o < len(data):
        length = struct.unpack_from('>i', data, o + 4)[0] * 2
        rec = o + 8
        if struct.unpack_from('<i', data, rec)[0] == 0:
            yield []
        else:
            nparts, npoints = struct.unpack_from('<2i', data, rec + 36)
            starts = list(struct.unpack_from(f'<{nparts}i', data, rec + 44)) + [npoints]
            pts = rec + 44 + 4 * nparts
            rings = []
            for a, b in zip(starts, starts[1:]):
                xy = struct.unpack_from(f'<{2 * (b - a)}d', data, pts + 16 * a)
                rings.append((min(xy[0::2]), min(xy[1::2]), max(xy[0::2]), max(xy[1::2])))
            yield rings
        o += 8 + length


def merge_boxes(boxes):
    """Boxes that touch or overlap become one, until none do: a state is one box, an exclave its own."""
    boxes = [list(b) for b in boxes]
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]:
                    boxes[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break
    return sorted(tuple(b) for b in boxes)


def state_boxes():
    """{code: (name, [(west, south, east, north), ...])} for India's states and union territories: each outline ring's box
    plus MARGIN, those that touch merged. Puducherry's four enclaves stay apart, so its map is not most of the south."""
    with zipfile.ZipFile(SRC / 'ne_10m_admin_1_states_provinces.zip') as zf:
        shp = zf.read(next(n for n in zf.namelist() if n.endswith('.shp')))
        dbf = zf.read(next(n for n in zf.namelist() if n.endswith('.dbf')))
    names, rings = {}, {}
    for row, record in zip(_dbf(dbf), _shp_rings(shp)):
        if row.get('adm0_a3') != 'IND' or not record or not row.get('iso_3166_2', '').startswith('IN-'):
            continue
        code = CODES.get(row['iso_3166_2'], row['iso_3166_2'])
        names[code] = NAMES.get(code, row.get('name'))
        rings.setdefault(code, []).extend((w - MARGIN, s - MARGIN, e + MARGIN, n + MARGIN) for w, s, e, n in record)
    return {c: (names[c], merge_boxes(rings[c])) for c in sorted(names)}


# --------------------------------------------------------------------------------------------------------------- states --
def cut(code, name, boxes):
    """The state's map set in SETS/<code>/LastbookData, from every row inside any of its boxes. Returns its folder."""
    hit = ' OR '.join(['(i.maxx>=? AND i.minx<=? AND i.maxy>=? AND i.miny<=?)'] * len(boxes))
    hit_args = [v for w, s, e, n in boxes for v in (w, e, s, n)]
    pin = ' OR '.join(['(lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?)'] * len(boxes))

    def inside(x, y):        # the state itself, without the margin: for the place finder
        return any(w + MARGIN <= x <= e - MARGIN and s + MARGIN <= y <= n - MARGIN for w, s, e, n in boxes)
    lib = SETS / code / 'LastbookData'
    shutil.rmtree(SETS / code, ignore_errors=True)
    geo = lib / 'derived' / 'geo'
    geo.mkdir(parents=True)
    nat = sqlite3.connect(geo / 'national.sqlite')
    nat.executescript('PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;' + SCHEMA)
    seen_feat, seen_poi, places, as_of = set(), set(), [], []
    fid = pid = 0
    for zone in ZONE_NAMES:
        z = sqlite3.connect(f'file:{ZONES / zone}.sqlite?immutable=1', uri=True)
        as_of.append(dict(z.execute('SELECT key,value FROM meta')).get('data_as_of', ''))
        rows = z.execute('SELECT f.layer,f.cls,f.name,f.minz,f.geom,i.minx,i.maxx,i.miny,i.maxy,i.maxz FROM feat_idx i '
                         f'JOIN feat f ON f.id=i.id WHERE {hit}', hit_args)
        for layer, cls, nm, minz, geom, x0, x1, y0, y1, maxz in rows:
            key = hashlib.blake2b(geom, digest_size=12).digest() + layer.encode()
            if key in seen_feat:      # a way that crosses a zone border is in both zones' extracts
                continue
            seen_feat.add(key)
            fid += 1
            nat.execute('INSERT INTO feat VALUES (?,?,?,?,?,?)', (fid, layer, cls, nm, minz, geom))
            nat.execute('INSERT INTO feat_idx VALUES (?,?,?,?,?,?,?)', (fid, x0, x1, y0, y1, minz, maxz))
            if layer == 'place' and nm and inside(x0, y0):
                places.append((nm, cls, x0, y0))
        for cls, nm, lon, lat in z.execute(f'SELECT cls,name,lon,lat FROM poi WHERE {pin}', hit_args):
            key = (cls, round(lon, 6), round(lat, 6))
            if key in seen_poi:
                continue
            seen_poi.add(key)
            pid += 1
            nat.execute('INSERT INTO poi VALUES (?,?,?,?,?)', (pid, cls, nm, lon, lat))
        z.close()
    status = {'state': 'complete', 'stage': 'ready', 'features': fid}
    nat.execute('CREATE INDEX poi_cls ON poi(cls, lon, lat)')
    nat.executemany('INSERT INTO meta VALUES (?,?)', [('source', SOURCE), ('data_as_of', max(as_of)), ('region', f'{name}, India'),
                                                      ('bounds', ';'.join(','.join(f'{v:.2f}' for v in b) for b in boxes)), ('status', json.dumps(status)),
                                                      ('built', time.strftime('%Y-%m-%dT%H:%M:%S'))])
    nat.commit()
    nat.execute('VACUUM')
    nat.close()
    empty = sqlite3.connect(geo / 'map.sqlite')        # server.py draws tiles only when this file is there; it holds no rows
    empty.executescript(SCHEMA)
    empty.close()
    pl = sqlite3.connect(geo / 'places.sqlite')
    pl.executescript('''CREATE TABLE place(id INTEGER PRIMARY KEY, name TEXT, kind TEXT, detail TEXT, lat REAL, lon REAL, rank INTEGER, source TEXT);
      CREATE VIRTUAL TABLE place_fts USING fts5(name, detail, content='place', content_rowid='id', tokenize='unicode61 remove_diacritics 2');''')
    pl.executemany('INSERT INTO place(name,kind,detail,lat,lon,rank,source) VALUES (?,?,?,?,?,?,?)',
                   [(nm, cls.title(), f'{cls.title()} · {name}', lat, lon, PLACE_RANK.get(cls, 5), 'OpenStreetMap') for nm, cls, lon, lat in places])
    pl.execute("INSERT INTO place_fts(place_fts) VALUES('rebuild')")
    pl.commit()
    pl.execute('VACUUM')
    pl.close()
    (geo / 'national-status.json').write_text(json.dumps(status), encoding='utf-8')
    (geo / 'socal-cells.json').write_text('[]', encoding='utf-8')
    files = {p.relative_to(lib).as_posix(): p.stat().st_size for p in lib.rglob('*') if p.is_file()}
    (lib / 'map-set.json').write_text(json.dumps({'name': code, 'files': files, 'bytes': sum(files.values())}, indent=1), encoding='utf-8')
    install.check_map(lib)
    log(code, name, f'{fid:,} features, {pid:,} help points, {len(places):,} places, {sum(files.values()) / 1e6:,.0f} MB')
    return lib


def zip_set(code, lib):
    """One zip, or parts under PART_LIMIT each (setup unpacks every file of a map in turn). Returns [(path, installed bytes)]."""
    ZIPS.mkdir(parents=True, exist_ok=True)
    for old in ZIPS.glob(f'Lastbook-Map-{code}*.zip'):
        old.unlink()
    files = sorted((p for p in lib.rglob('*') if p.is_file()), key=lambda p: -p.stat().st_size)
    parts, part = [], []
    for p in files:                    # largest first, each into the first part with room for its full size
        part.append(p)
        if sum(x.stat().st_size for x in part) > PART_LIMIT and len(part) > 1:
            parts.append(part[:-1])
            part = [p]
    parts.append(part)
    out = []
    for i, members in enumerate(parts, 1):
        path = ZIPS / (f'Lastbook-Map-{code}.zip' if len(parts) == 1 else f'Lastbook-Map-{code}-part{i}.zip')
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for p in members:
                zf.write(p, 'LastbookData/' + p.relative_to(lib).as_posix())
        if path.stat().st_size >= 2 ** 31:
            raise SystemExit(f'{path.name} is {path.stat().st_size:,} bytes, over what GitHub takes: split the set further')
        install.check_zip(path, sum(p.stat().st_size for p in files), install.map_member)
        out.append((path, sum(p.stat().st_size for p in members)))
    return out


def states(codes):
    boxes = state_boxes()
    unknown = set(codes) - set(boxes)
    if unknown:
        raise SystemExit(f'not a state or union territory code here: {sorted(unknown)}; have {sorted(boxes)}')
    done = json.loads((WORK / 'states.json').read_text()) if (WORK / 'states.json').exists() else {}
    for code in codes or boxes:
        name, parts = boxes[code]
        lib = cut(code, name, parts)
        zips = zip_set(code, lib)
        done[code] = {'title': name, 'installed_bytes': json.loads((lib / 'map-set.json').read_text())['bytes'],
                      'files': [{'name': p.name, 'size': p.stat().st_size, 'sha256': sha256(p)} for p, _ in zips]}
        (WORK / 'states.json').write_text(json.dumps(done, indent=1, ensure_ascii=False))
        shutil.rmtree(SETS / code)
        log(code, 'zipped', ', '.join(f'{p.name} {p.stat().st_size / 1e6:,.0f} MB' for p, _ in zips))


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


# ----------------------------------------------------------------------------------------------------------------- pins --
def pins(base):
    """content/india-maps.json for every state in states.json, its zips at BASE + name, checked against what was built."""
    done = json.loads((WORK / 'states.json').read_text())
    maps = []
    for code, d in sorted(done.items(), key=lambda kv: kv[1]['title']):
        for f in d['files']:
            if sha256(ZIPS / f['name']) != f['sha256']:
                raise SystemExit(f'{f["name"]} changed since it was checked')
        maps.append({'id': code, 'title': d['title'], 'country': 'IN', 'kind': 'state', 'includes': INCLUDES,
                     'installed_bytes': d['installed_bytes'], 'available': True,
                     'files': [{'name': f['name'], 'url': base + f['name'], 'size': f['size'], 'sha256': f['sha256'], 'kind': 'zip'}
                               for f in d['files']]})
    OUT_JSON.write_text(json.dumps({'about': 'Indian state maps setup offers. Written by tools/build_india_maps.py pins; edit that, not this.',
                                    'maps': maps}, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    log(f'wrote {OUT_JSON} ({len(maps)} maps)')


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd == 'zones':
        zones()
    elif cmd == 'states':
        states(sys.argv[2:])
    elif cmd == 'pins' and len(sys.argv) == 3 and sys.argv[2].startswith('https://'):
        pins(sys.argv[2])
    elif cmd == 'boxes':
        for code, (name, parts) in state_boxes().items():
            print(f'{code:6} {name:45}', '  '.join(','.join(f'{v:.2f}' for v in b) for b in parts))
    else:
        raise SystemExit(__doc__)


if __name__ == '__main__':
    main()
