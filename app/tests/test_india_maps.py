"""tools/build_india_maps.py: a state's set is cut from the zones, de-duplicated, valid for setup, and pinned the way setup reads it."""
import json, shutil, sqlite3, sys, unittest, zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import build_india_maps as B  # noqa: E402
from build_map import pack  # noqa: E402
from engine.setup import install, manifest  # noqa: E402
from app.tests import shorttemp  # noqa: E402


def zone(path, feats, pois):
    db = sqlite3.connect(path)
    db.executescript(B.SCHEMA)
    for i, (layer, cls, name, pts) in enumerate(feats, 1):
        xs, ys = pts[0::2], pts[1::2]
        db.execute('INSERT INTO feat VALUES (?,?,?,?,?,?)', (i, layer, cls, name, 10, pack([pts])))
        db.execute('INSERT INTO feat_idx VALUES (?,?,?,?,?,?,?)', (i, min(xs), max(xs), min(ys), max(ys), 10, 30))
    for i, (cls, name, lon, lat) in enumerate(pois, 1):
        db.execute('INSERT INTO poi VALUES (?,?,?,?,?)', (i, cls, name, lon, lat))
    db.execute("INSERT INTO meta VALUES ('data_as_of', '2026-11-16')")
    db.commit()
    db.close()


class Cut(unittest.TestCase):
    def setUp(self):
        self.tmp = shorttemp.mkdtemp('lastbook-india-')
        zones = self.tmp / 'zones'
        zones.mkdir()
        border_road = ('road', 'primary', 'NH 48', [73.0, 19.0, 73.2, 19.1])          # in both zones' extracts
        zone(zones / 'a.sqlite', [border_road, ('place', 'city', 'Pune', [73.85, 18.52]), ('road', 'primary', 'Far', [80.0, 25.0, 80.1, 25.1])],
             [('hospital', 'Sassoon', 73.87, 18.52), ('police', 'Far away', 80.0, 25.0)])
        zone(zones / 'b.sqlite', [border_road, ('place', 'village', 'Margin', [72.05, 18.0])], [('hospital', 'Sassoon', 73.87, 18.52)])
        self.patch = mock.patch.multiple(B, ZONES=zones, SETS=self.tmp / 'sets', ZIPS=self.tmp / 'zips', WORK=self.tmp, ZONE_NAMES=['a', 'b'])
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_set(self):
        box = (72.0, 17.5, 76.0, 21.0)
        lib = B.cut('IN-MH', 'Maharashtra', box)
        db = sqlite3.connect(lib / 'derived/geo/national.sqlite')
        self.assertEqual(sorted(r[0] for r in db.execute('SELECT name FROM feat')), ['Margin', 'NH 48', 'Pune'])      # once, and not 'Far'
        self.assertEqual(db.execute('SELECT count(*) FROM feat_idx').fetchone()[0], 3)
        self.assertEqual(db.execute('SELECT cls, name FROM poi').fetchall(), [('hospital', 'Sassoon')])
        meta = dict(db.execute('SELECT key, value FROM meta'))
        self.assertEqual(meta['region'], 'Maharashtra, India')
        self.assertEqual(json.loads(meta['status'])['features'], 3)
        pl = sqlite3.connect(lib / 'derived/geo/places.sqlite')
        self.assertEqual(pl.execute("SELECT p.name, p.kind FROM place_fts f JOIN place p ON p.id=f.rowid WHERE place_fts MATCH 'pun*'").fetchall(), [('Pune', 'City')])
        self.assertEqual(pl.execute("SELECT count(*) FROM place WHERE name='Margin'").fetchone()[0], 0)   # in the margin, not the state
        self.assertEqual(install.check_map(lib)['name'], 'IN-MH')
        self.assertEqual(install.map_name(lib), 'IN-MH')

    def test_zip_and_parts(self):
        lib = B.cut('IN-MH', 'Maharashtra', (72.0, 17.5, 76.0, 21.0))
        (out,) = B.zip_set('IN-MH', lib)
        names = zipfile.ZipFile(out[0]).namelist()
        self.assertIn('LastbookData/map-set.json', names)
        self.assertTrue(all(install.map_member(n[len('LastbookData/'):]) for n in names))
        with mock.patch.object(B, 'PART_LIMIT', 1):              # every file in a part of its own
            parts = B.zip_set('IN-MH', lib)
        self.assertGreater(len(parts), 1)
        members = [n for p, _ in parts for n in zipfile.ZipFile(p).namelist()]
        self.assertEqual(sorted(members), sorted(names))           # each file in exactly one part
        self.assertFalse((B.ZIPS / 'Lastbook-Map-IN-MH.zip').exists())


class Pins(unittest.TestCase):
    def test_load(self):
        tmp = shorttemp.mkdtemp('lastbook-india-pins-')
        try:
            good = {'id': 'IN-MH', 'title': 'Maharashtra', 'available': True, 'installed_bytes': 10,
                    'files': [{'name': 'Lastbook-Map-IN-MH.zip', 'size': 5, 'sha256': 'a' * 64,
                               'url': 'https://downloads.lastbook.org/maps/Lastbook-Map-IN-MH.zip'}]}
            bad = [{**good, 'id': 'CA'},                                                              # not an Indian id
                   {**good, 'id': 'IN-KA', 'files': [{**good['files'][0], 'url': 'https://evil.example/x.zip'}]},
                   {**good, 'id': 'IN-GJ', 'files': [{**good['files'][0], 'sha256': 'no'}]},
                   good]                                                                              # the same id again
            path = tmp / 'm.json'
            path.write_text(json.dumps({'maps': [good] + bad}))
            maps = manifest.load_india_maps(path)
            self.assertEqual([(m['id'], m['country']) for m in maps], [('IN-MH', 'IN')])
            mf = manifest.Manifest(bundled={'version': 1, 'packs': [], 'models': [], 'maps': []}, remote=None, india_maps=maps)
            self.assertEqual(mf.map('IN-MH')['title'], 'Maharashtra')
            kept = manifest.merge(mf.current, {'version': 1, 'packs': [], 'models': [], 'maps': []})     # a fetched manifest never drops them
            self.assertEqual([m['id'] for m in kept['maps']], ['IN-MH'])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_shipped_list(self):
        path = ROOT / 'content' / 'india-maps.json'
        if not path.exists():
            self.skipTest('no Indian maps published yet')
        raw = json.loads(path.read_text(encoding='utf-8'))['maps']
        self.assertEqual(len(manifest.load_india_maps(path)), len(raw))


if __name__ == '__main__':
    unittest.main()
