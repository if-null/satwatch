# -*- coding: utf-8 -*-
"""satmap - building positions / 50 m bins for satwatch (stdlib only).

satparse.py is NOT modified. Positions are captured by a clearly marked,
temporary monkeypatch of satparse.parse_object_headers (and a pass-through
wrapper of satparse.parse_level to see the per-level 'collected' lists).
Both are restored in a finally block. If satparse's header reader ever changes,
the source fingerprint check below disables map capture instead of guessing.
"""
import os, re, json, struct, hashlib, inspect, contextlib, collections, time

# ===================== MAP CALIBRATION (single source of truth) =====================
# World bounds of the official in-game map image, in UE centimetres (X east, Y south).
# Source: satisfactory.wiki.gg/wiki/World ("Coordinate range (x, y) is between
#   -3246, -3750 (North West) and 4253, 3750 (South East)" metres), same values used by
#   community tools (-324698.832 .. 425301.832, -375000 .. 375000). Image is square:
#   7500 m x 7500 m over 5000 px = 1.5 m/px. Verified against save data by calibrate().
MAP_X0, MAP_X1 = -324698.832, 425301.832
MAP_Y0, MAP_Y1 = -375000.0, 375000.0
MAP_IMAGE = 'map_placeholder.png'  # default; pick_image() prefers a user-supplied map in assets/ (5000x5000)
MAP_IMAGE_PX = 5000
MAP_IMAGE_CANDIDATES = ('map.jpg', 'map.jpeg', 'map.png', 'map.webp', 'map_placeholder.png')
MAP_SOURCE_URL = ''
MAP_ATTRIBUTION_PLACEHOLDER = ('Placeholder map (plain grid, no game art). To see your buildings on a real map, put your own '
                               '5000 x 5000 px map image at assets/map.jpg (see the README, "Using a better map"). '
                               'Satisfactory is (c) Coffee Stain Studios; unofficial fan tool, not endorsed.')
MAP_ATTRIBUTION_CUSTOM = ('Map image: supplied by the user (assets/%s), not distributed with satwatch. '
                          'Satisfactory is (c) Coffee Stain Studios; unofficial fan tool, not endorsed.')
MAP_ATTRIBUTION = MAP_ATTRIBUTION_PLACEHOLDER
BIN_CM = 5000                    # 50 m bins
# ===================================================================================

MAPS_VERSION = '1'
_EXPECTED_POH = '1acd1a7552d9275d'   # sha256[:16] of satparse.parse_object_headers source (v0.4: adds the network position hook; v0.2/0.3 were c58138293815467c)

CATS = ['production', 'extraction', 'power', 'logistics', 'storage_transport', 'structure', 'other',
        'collectibles']
COLORS = {'production': '#F0503C', 'extraction': '#3CC8FF', 'power': '#FFDC3C', 'logistics': '#FA9549',
          'storage_transport': '#B46EDC', 'structure': '#9AA0A6', 'other': '#E6E6E6', 'collectibles': '#50FF78'}

_RX = [
    ('extraction', r'Miner|OilPump|WaterPump|Fracking'),
    ('production', r'Constructor|Assembler|Smelter|Foundry|Manufacturer|OilRefinery|Packager|Blender|HadronCollider|Converter|QuantumEncoder'),
    ('power', r'Generator|Power|AlienPowerBuilding|Wire'),
    ('logistics', r'Conveyor|Pipe|Pump|Splitter|Merger|Valve|Junction|Hyper'),
    ('storage_transport', r'Storage|Container|IndustrialTank|Train|Railroad|Truck|Drone|Station|CentralStorage|Platform'),
    ('structure', r'Foundation|Wall|Ramp|Roof|Beam|Pillar|Stair|Walkway|Catwalk|Fence|Railing|Window|Door|Frame|Ladder|Light|Barrier|Sign|Quarter|Corner|Gate'),
]
_RX = [(k, re.compile(p)) for k, p in _RX]
_VEH = re.compile(r'BP_(Locomotive|FreightWagon|DroneTransport|Tractor|Truck|Explorer|Golfcart)')
_COL = re.compile(r'BP_(WAT1|WAT2|Crystal|DropPod)')
COLLECTIBLE_NAMES = {'BP_WAT1': 'Somersloop', 'BP_WAT2': 'Mercer Sphere', 'BP_Crystal_mk3': 'Power Slug (purple)',
                     'BP_Crystal_mk2': 'Power Slug (yellow)', 'BP_Crystal': 'Power Slug (blue)', 'BP_DropPod': 'Hard Drive (crash site)'}


def category(cls):
    """Map a short class name to a map category, or None if not drawn."""
    if cls.startswith('Build_'):
        for k, rx in _RX:
            if rx.search(cls): return k
        return 'other'
    if _VEH.match(cls): return 'storage_transport'
    if _COL.match(cls): return 'collectibles'
    return None


def friendly(cls, satparse=None):
    if satparse is not None and cls in satparse.FRIENDLY: return satparse.FRIENDLY[cls]
    for p, n in COLLECTIBLE_NAMES.items():
        if cls.startswith(p): return n
    s = re.sub(r'^(Build_|BP_)|_C$', '', cls)
    return re.sub(r'(?<=[a-z])(?=[A-Z0-9])', ' ', s).replace('_', ' ')


def to_px(x, y, size=MAP_IMAGE_PX):
    return ((x - MAP_X0) / (MAP_X1 - MAP_X0) * size, (y - MAP_Y0) / (MAP_Y1 - MAP_Y0) * size)


def available(satparse):
    try:
        h = hashlib.sha256(inspect.getsource(satparse.parse_object_headers).encode()).hexdigest()[:16]
        return h == _EXPECTED_POH
    except Exception:
        return False


# ---------------------------------------------------------------------------------
# >>> MONKEYPATCH (satmap) <<<  Same byte layout as satparse.parse_object_headers v0.2,
# but instead of skipping the 40-byte transform it reads rot(4f) pos(3f) scale(3f).
# Installed only inside capture(); originals are restored afterwards.
# ---------------------------------------------------------------------------------
@contextlib.contextmanager
def capture(satparse):
    """with capture(satparse) as cap: satparse.parse_save(path)  -> cap['actors'], cap['collected']"""
    cap = {'actors': [], 'collected': set()}
    orig_poh, orig_pl = satparse.parse_object_headers, satparse.parse_level
    S = satparse

    def poh(r, n, sv=99):
        hdrs = []
        for _ in range(n):
            typ = r.i32()
            cls = r.s(); lvl = r.s(); path = r.s()
            if typ == 1:
                r.skip(4 if sv <= 46 else S._actor_pre(r))
                x, y, z = struct.unpack_from('<3f', r.d, r.o + 16)
                if getattr(S, '_POS', None) is not None: S.note_pos(cls, path, r.d, r.o)  # satparse 0.4 network positions
                r.skip(40); r.i32()
                cap['actors'].append((cls.rsplit('.', 1)[-1], path, x, y, z))
                hdrs.append((1, cls, path))
            elif typ == 0:
                n1 = struct.unpack_from('<i', r.d, r.o)[0] if r.o + 4 <= r.end else 0
                if sv > 46 and not (0 < n1 < 5000 and r.d[r.o + 4:r.o + 5].isalpha()): r.skip(4)
                r.s(); hdrs.append((0, cls, path))
            else:
                raise S.ParseError('unknown object header type %d at %d' % (typ, r.o))
        return hdrs

    def pl(*a, **k):
        res = orig_pl(*a, **k)
        try: cap['collected'].update(res[2])
        except Exception: pass
        return res

    S.parse_object_headers, S.parse_level = poh, pl
    try:
        yield cap
    finally:
        S.parse_object_headers, S.parse_level = orig_poh, orig_pl
# --------------------------- end MONKEYPATCH ------------------------------------


def build(cap, satparse=None):
    """-> (bins dict, points list, player [x,y,z] or None, totals)"""
    bins = {c: collections.Counter() for c in CATS}
    totals = collections.Counter(); points = []; player = None
    col = cap['collected']
    for cls, path, x, y, z in cap['actors']:
        if cls.startswith('Char_Player'):
            if player is None: player = [round(x), round(y), round(z)]
            continue
        k = category(cls)
        if k is None: continue
        if k == 'collectibles' and path in col: continue   # already picked up / opened
        if not (x == x and abs(x) < 2e6 and abs(y) < 2e6): continue  # NaN / junk
        g = '%d,%d' % ((x - MAP_X0) // BIN_CM, (y - MAP_Y0) // BIN_CM)
        bins[k][g] += 1; totals[k] += 1
        if k in ('production', 'extraction'):
            points.append([round(x), round(y), round(z), friendly(cls, satparse), k])
    return {k: dict(v) for k, v in bins.items() if v}, points, player, dict(totals)


def stem(save_name):
    return os.path.splitext(os.path.basename(save_name))[0]


def bins_path(work, save_name):
    return os.path.join(work, 'map_bins', stem(save_name) + '.json')


def has_bins(work, save_name, size):
    p = bins_path(work, save_name)
    try:
        with open(p, encoding='utf-8') as f: d = json.load(f)
        return d.get('file') == os.path.basename(save_name) and d.get('file_size') == size and d.get('maps_version') == MAPS_VERSION
    except Exception:
        return False


def _write(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f: json.dump(obj, f, separators=(',', ':'))
    os.replace(tmp, p)


def save_bins(work, save_path, snap, cap, satparse=None):
    """Write map_bins/<stem>.json (+ <stem>.points.json). Returns bins record."""
    b, pts, player, tot = build(cap, satparse)
    name = os.path.basename(save_path)
    h = snap.get('header') or {}
    ps = h.get('play_seconds')
    ph = round(ps / 3600.0, 2) if isinstance(ps, (int, float)) else snap.get('play_hours')
    sdt = h.get('save_datetime_utc_guess') or snap.get('file_mtime')
    rec = {'maps_version': MAPS_VERSION, 'file': name, 'file_size': snap.get('file_size'),
           'play_hours': ph, 'save_datetime': sdt,
           'bin_cm': BIN_CM, 'origin_cm': [MAP_X0, MAP_Y0], 'totals': tot, 'player': player, 'bins': b,
           'buildings_total_snapshot': snap.get('buildings_total')}
    _write(bins_path(work, name), rec)
    _write(os.path.join(work, 'map_bins', stem(name) + '.points.json'),
           {'file': name, 'file_size': snap.get('file_size'), 'player': player,
            'fields': ['x_cm', 'y_cm', 'z_cm', 'name', 'category'], 'points': pts})
    return rec


def list_bins(work):
    d = os.path.join(work, 'map_bins'); out = []
    if not os.path.isdir(d): return out
    for fn in os.listdir(d):
        if not fn.endswith('.json') or fn.endswith('.points.json') or fn in ('latest_points.json', 'map_summary.json'): continue
        try:
            with open(os.path.join(d, fn), encoding='utf-8') as f: r = json.load(f)
            if r.get('maps_version') != MAPS_VERSION: continue
            out.append({'file': r['file'], 'stem': stem(r['file']), 'play_hours': r.get('play_hours'),
                        'save_datetime': r.get('save_datetime'), 'totals': r.get('totals', {})})
        except Exception:
            continue
    out.sort(key=lambda r: (r.get('play_hours') or 0, r.get('save_datetime') or ''))
    return out


def load_bins(work, save_stem):
    p = os.path.join(work, 'map_bins', os.path.basename(save_stem) + '.json')
    with open(p, encoding='utf-8') as f: return json.load(f)


def pick_image(assets_dir=None):
    """Name of the map image to show: the first user-supplied candidate found in assets/, else the placeholder."""
    if assets_dir:
        for n in MAP_IMAGE_CANDIDATES:
            if os.path.isfile(os.path.join(str(assets_dir), n)): return n
    return MAP_IMAGE


def meta(work, n_snapshots=None, assets_dir=None):
    lst = list_bins(work)
    img = pick_image(assets_dir)
    attr = MAP_ATTRIBUTION_PLACEHOLDER if img == 'map_placeholder.png' else MAP_ATTRIBUTION_CUSTOM % img
    ver = ''
    try:  # cache-buster: a replaced map.jpg always shows up, even if the browser cached the old file
        if assets_dir:
            st = os.stat(os.path.join(str(assets_dir), img))
            ver = '?v=%d-%d' % (st.st_size, int(st.st_mtime))
    except OSError:
        pass
    return {'bounds_cm': {'x0': MAP_X0, 'x1': MAP_X1, 'y0': MAP_Y0, 'y1': MAP_Y1}, 'image': '/assets/' + img + ver, 'placeholder': img == 'map_placeholder.png',
            'image_px': MAP_IMAGE_PX, 'bin_cm': BIN_CM, 'categories': CATS, 'colors': COLORS,
            'attribution': attr, 'source_url': MAP_SOURCE_URL, 'snapshots': lst,
            'snapshots_total': n_snapshots, 'snapshots_with_bins': len(lst)}
