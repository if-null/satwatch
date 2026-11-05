#!/usr/bin/env python3
"""volume.py - "Volume items: where each is stored and moved", computed from one satparse (>=0.4) snapshot.

Rule: a row is any item that is an ingredient of an unlocked machine recipe whose product is a part, fluid,
fuel or Space Elevator / project part. Buildings, decorations, equipment, ammo, nobelisks, rebar, filters,
medical items, power shards, alien and biomass items don't count, as rows or as consumers. Raw resources are
not rows. Fluids are not rows; their packaged forms are, when used in a volume recipe or shipped by drone or
train. Unpackage recipes are skipped, except as a note on packaged rows.

Stdlib only. Game data: assets/sf-data1.0-volume.json (trimmed SatisfactoryTools data, MIT, (c) 2019 greeny).
Usage: python volume.py <save.sav | snapshot.json> [--boxes factories_boxes.json] --out volume_items
"""
import argparse, collections, json, math, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / 'assets' / 'sf-data1.0-volume.json'
VERSION = '1'
RULE = ("A row is any item that is an ingredient of an unlocked machine recipe whose product is a part, fluid, fuel or "
        "Space Elevator / project part. Buildings, decorations, equipment, ammo, nobelisks, rebar, filters, medical items, "
        "power shards, alien and biomass items don't count, as rows or as consumers. Raw resources are not rows. Fluids are "
        "not rows; their packaged forms are, when used in a volume recipe or shipped by drone or train. Unpackage recipes "
        "are skipped, except as a note on packaged rows. Tier = lowest milestone tier unlocking a standard recipe for the item.")
EXC = re.compile(r'Cartridge|Nobelisk|Rebar|Filter|Medkit|Inhaler|Shroom|Berry|Nut_|Parachute|Crystal_mk|CrystalShard|Sloop|Coupon|'
                 r'Leaves|Wood|Mycelia|Remains|HogParts|SpitterParts|AlienProtein|AlienDNA|Biofuel|GenericBiomass|LiquidBiofuel|'
                 r'PackagedBiofuel|Fireworks|Snow|Candy|Gift|XmasBall|Wreath|Star|Hatcher|Stinger|SpaceGiraffe|Hog|Spitter|'
                 r'Chainsaw|Gun|Rifle|Shatter|Explosive|Stun|Mercer|Sam_|Turbo.*Ammo|Beacon|GolfCart|HazmatFilter|PortableMiner', re.I)
TIER_FIX = {'Plastic': 5, 'Rubber': 5, 'Empty Canister': 5, 'Empty Fluid Tank': 8}   # hand-set (same as the tracker Page)
SKIP_ROWS = {'Uranium Waste'}
LEVELS = ['Raw', 'Ingot / fluid', 'Basic part', 'Intermediate', 'Advanced']
GROUPS = [[t, 'Tier %d' % t] for t in range(10)] + [[-1, 'MAM / alternate-only'], [99, 'Unknown tier']]
NEAR_BOX_CM = 30000   # a machine/station outside every box joins the nearest box within 300 m, else "F?"
_DATA = None


def load_data(path=None):
    global _DATA
    if path is None and _DATA is not None:
        return _DATA
    d = json.loads(Path(path or DATA).read_text(encoding='utf-8'))
    if path is None:
        _DATA = d
    return d


def has_network(snap):
    return isinstance((snap or {}).get('network'), dict) and isinstance(snap.get('recipes'), list)


# ---------------- factories (boxes from factories_boxes.json) ----------------
def _box(f):
    b = f.get('bbox_cm') or {}
    try:
        return float(b['min_x']), float(b['min_y']), float(b['max_x']), float(b['max_y'])
    except Exception:
        return None


def factory_of(boxes, pos):
    """-> 'F#' of the box containing pos (nearest centroid if several), else nearest box within NEAR_BOX_CM, else 'F?'."""
    if not pos or not boxes:
        return 'F?'
    x, y = pos[0], pos[1]
    best = None
    for f in boxes:
        b = _box(f)
        if not b:
            continue
        dx = max(b[0] - x, 0, x - b[2]); dy = max(b[1] - y, 0, y - b[3])
        d = math.hypot(dx, dy)
        c = f.get('centroid_cm') or [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2]
        k = (d, math.hypot(c[0] - x, c[1] - y))   # overlapping boxes: nearest centroid wins
        if best is None or k < best[0]:
            best = (k, f)
    if best is None or best[0][0] > NEAR_BOX_CM:
        return 'F?'
    return best[1].get('label') or 'F%s' % best[1].get('id')


def _fsort(x):
    m = re.match(r'F(\d+)$', x)
    return (0, int(m.group(1))) if m else (1, x)


def _g(v):
    return '%g' % v


# ---------------- main computation ----------------
def compute(snap, boxes=None, data=None):
    """-> dict with rows/summary/groups, or None when the snapshot lacks network or recipe data."""
    if not has_network(snap):
        return None
    D = data or load_data()
    I, R = D['items'], D['recipes']
    net = snap['network']
    nm = lambda c: (I.get(c) or [c])[0]
    name2cls = {}
    for c, v in I.items():
        name2cls.setdefault(v[0], c)
    vol = lambda c: c in I and not EXC.search(c)
    U = set(snap.get('recipes') or [])
    boxes = (boxes or {}).get('factories') if isinstance(boxes, dict) else boxes
    res = set(D.get('resources') or [])
    # recipes in use / made at
    used = collections.Counter(); made = collections.defaultdict(set); unplaced = 0
    for cls, rec, pos in net.get('machines') or []:
        if not rec:
            continue
        used[rec] += 1
        f = factory_of(boxes, pos)
        if f == 'F?': unplaced += 1
        for p, _a in (R.get(rec) or ['', 0, [], []])[3]:
            made[nm(p)].add(f)
    allrec = {k: r for k, r in R.items() if k in U}
    rec = {k: r for k, r in allrec.items() if not r[0].startswith('Unpackage')}
    cons = collections.defaultdict(list); producers = collections.defaultdict(list)
    for k, r in rec.items():
        outs = [p for p in r[3] if vol(p[0])]
        if not outs:
            continue
        main = outs[0]
        for p in outs:
            producers[p[0]].append(r)
        for g, a in r[2]:
            if vol(g) and g != main[0]:
                cons[g].append((nm(main[0]), r[0], r[1], a, main[1], k))
    # depot
    dep = {}
    for c, n in ((snap.get('depot') or {}).get('stored') or {}).items():
        dep[nm(c)] = dep.get(nm(c), 0) + (n or 0)
    # drones
    ports = net.get('drone_ports') or []
    byname = {}
    for s in ports:
        byname.setdefault(s.get('name'), []).append(s)
    targeted = {s.get('paired_to') for s in ports if s.get('paired_to')}
    drone = collections.defaultdict(list); dport = collections.defaultdict(list)
    q = lambda s: '\u201c%s\u201d' % (s or '?')
    for s in ports:
        items = list(dict.fromkeys([nm(c) for c in list(s.get('input') or {}) + list(s.get('output') or {})]))
        f = factory_of(boxes, s.get('pos'))
        if s.get('paired_to'):
            src = (byname.get(s['paired_to']) or [{}])[0]
            for it in items:
                drone[it].append('%s %s \u2192 %s %s' % (factory_of(boxes, src.get('pos')), q(s['paired_to']), f, q(s.get('name'))))
        elif s.get('name') not in targeted:
            for it in items:
                dport[it].append('%s %s (unpaired)' % (f, q(s.get('name'))))
    # trains
    st = net.get('train_stations') or []
    stops = collections.defaultdict(list)
    for t in net.get('timetables') or []:
        for sp in t.get('stops') or []:
            stops[sp.get('station')].append(dict(sp, train=t.get('train') or '?'))
    train = collections.defaultdict(set)
    for s in st:
        f = factory_of(boxes, s.get('pos'))
        for t in stops.get(s.get('id'), []):
            for c in (t.get('load') or []) + (t.get('unload') or []):
                train[nm(c)].add('%s at %s %s (%s)' % (t['train'], f, q(s.get('name')), 'load' if c in (t.get('load') or []) else 'unload'))
    tpos = [s for s in st if s.get('pos')]
    for p in net.get('platforms') or []:
        if not p.get('items') or not p.get('pos') or not tpos:
            continue
        s = min(tpos, key=lambda s: math.hypot(s['pos'][0] - p['pos'][0], s['pos'][1] - p['pos'][1]))
        ts = stops.get(s.get('id'), []); trains = [t['train'] for t in ts]
        suf = (' (%s, no item rule)' % ', '.join(trains)) if trains and not any(t.get('load') or t.get('unload') for t in ts) else ('' if trains else ' (no timetable)')
        for c in p['items']:
            train[nm(c)].add('platform %s %s%s' % (factory_of(boxes, s.get('pos')), q(s.get('name')), suf))
    # depth (recipe level)
    memo = {}

    def depth(c, seen=()):
        if c in memo: return memo[c]
        if c in res or c in seen: return 0
        ps = [r for r in producers.get(c, []) if not r[1]] or producers.get(c, [])
        if not ps:
            memo[c] = 0; return 0
        d = 1 + max([depth(g, seen + (c,)) for g, _a in ps[0][2] if vol(g)] or [0])
        memo[c] = d; return d

    def tier(n):
        if n in TIER_FIX: return TIER_FIX[n]
        t = (I.get(name2cls.get(n)) or [None, 0, None])[2]
        return 99 if t is None else t

    def row(c, kind, uses):
        n = nm(c); t = tier(n)
        return {'item': n, 'class': c, 'kind': kind, 'tier': t,
                'tier_group': dict((g, l) for g, l in GROUPS).get(t, 'Unknown tier'),
                'made_at': sorted(made.get(n, ()), key=_fsort), 'depot': dep.get(n, 0),
                'drone_routes': drone.get(n, []), 'drone_ports_unpaired': dport.get(n, []),
                'train': sorted(train.get(n, ())), 'used_to_make': uses}

    def tag(rn, alt, a, b, k):
        return '%s%s: %s \u2192 %s' % (rn.replace('Alternate: ', ''), ' (alt)' if alt else '', _g(a), _g(b)) + (' \u2605in save' if used.get(k) else '')

    rows = []
    for c, L in cons.items():
        n = nm(c)
        if n in SKIP_ROWS or n.startswith('Packaged ') or I[c][1]:
            continue
        dl = depth(c)
        if dl == 0:
            continue   # raw resource (or no unlocked producer)
        g = collections.OrderedDict()
        for prod, rn, alt, a, b, k in sorted(L, key=lambda x: (x[0], x[2])):
            g.setdefault(prod, []).append(tag(rn, alt, a, b, k))
        rows.append((min(dl, 4), n, row(c, LEVELS[min(dl, 4)], [[k, v] for k, v in g.items()])))
    rows.sort(key=lambda x: (x[0], x[1]))
    out = [r for _l, _n, r in rows]
    pk = []
    for c, v in I.items():
        n = v[0]
        if not n.startswith('Packaged ') or not vol(c):
            continue
        uses = collections.OrderedDict(); note = []
        for k, r in allrec.items():
            if not any(g == c for g, _a in r[2]):
                continue
            p = r[3][0]; a = [a for g, a in r[2] if g == c][0]
            if r[0].startswith('Unpackage'):
                note.append(nm(p[0])); continue
            if not vol(p[0]):
                continue
            uses.setdefault(nm(p[0]), []).append(tag(r[0], r[1], a, p[1], k))
        if not uses and not (drone.get(n) or dport.get(n) or train.get(n)):
            continue
        u = [[k, x] for k, x in uses.items()]
        if note:
            u.append(['Unpackages into', [', '.join(sorted(set(note)))]])
        pk.append((n, row(c, 'Packaged', u)))
    out += [r for _n, r in sorted(pk, key=lambda x: x[0])]
    on = lambda r: r['depot'] > 0 or r['drone_routes'] or r['train']
    summ = {'rows': len(out), 'in_depot': sum(1 for r in out if r['depot'] > 0), 'on_drone': sum(1 for r in out if r['drone_routes']),
            'on_train': sum(1 for r in out if r['train']), 'on_none': sum(1 for r in out if not on(r))}
    groups = []
    for t, l in GROUPS:
        gr = [r for r in out if r['tier'] == t]
        if gr:
            groups.append({'tier': t, 'label': l, 'items': len(gr), 'depot': sum(1 for r in gr if r['depot'] > 0),
                           'drone': sum(1 for r in gr if r['drone_routes']), 'train': sum(1 for r in gr if r['train'])})
    notes = []
    if not net.get('positions'):
        notes.append('no positions captured: Made at / factory labels show F?')
    if unplaced:
        notes.append('%d machine(s) outside every factory box (shown as F?); re-derive factories_boxes.json after building a new site' % unplaced)
    if not boxes:
        notes.append('factories_boxes.json not found: factory labels show F?')
    return {'volume_version': VERSION, 'rule': RULE, 'save': snap.get('file'), 'play_hours': _hours(snap),
            'summary': summ, 'groups': groups, 'rows': out, 'notes': notes,
            'boxes_from': ((boxes and isinstance(boxes, list)) and 'factories_boxes.json') or None}


def _hours(snap):
    try:
        return round(snap['header']['play_duration_s'] / 3600, 1)
    except Exception:
        return None


def outpost(snap, boxes=None):
    """Player position + nearest factory box (edge distance). None when no position was captured."""
    pp = ((snap or {}).get('network') or {}).get('player_pos')
    if not pp:
        return None
    boxes = (boxes or {}).get('factories') if isinstance(boxes, dict) else boxes
    best = None
    for f in boxes or []:
        b = _box(f)
        if not b:
            continue
        d = math.hypot(max(b[0] - pp[0], 0, pp[0] - b[2]), max(b[1] - pp[1], 0, pp[1] - b[3]))
        if best is None or d < best[0]:
            best = (d, f)
    o = {'player_cm': pp, 'nearest_factory': None, 'distance_m': None}
    if best:
        o.update(nearest_factory={'label': best[1].get('label'), 'name': best[1].get('name')}, distance_m=round(best[0] / 100))
    return o


def to_md(res):
    if not res:
        return '# Volume items\n\nNo network data in the latest snapshot (needs satparse 0.4). Run satwatch with --reparse-latest.\n'
    s = res['summary']
    L = ['# Volume items: where each is stored and moved', '', 'Save: %s' % res.get('save'), '',
         '%d items | in depot %d | on drone %d | on train %d | on none %d' % (s['rows'], s['in_depot'], s['on_drone'], s['on_train'], s['on_none']), '',
         'Rule: ' + res['rule'], '']
    for g in res['groups']:
        L += ['## %s (%d items; depot %d, drone %d, train %d)' % (g['label'], g['items'], g['depot'], g['drone'], g['train']), '',
              '| Item | Level | Made at | Depot | Drone | Train |', '|---|---|---|---:|---|---|']
        for r in res['rows']:
            if r['tier'] != g['tier']:
                continue
            dr = '; '.join(r['drone_routes'] + ['port only: ' + x for x in r['drone_ports_unpaired']]) or '-'
            L.append('| %s | %s | %s | %s | %s | %s |' % (r['item'], r['kind'], ', '.join(r['made_at']) or 'not made yet', r['depot'] or '-',
                                                     dr.replace('|', '/'), ('; '.join(r['train']) or '-').replace('|', '/')))
        L.append('')
    for n in res.get('notes') or []:
        L.append('- note: ' + n)
    return '\n'.join(L) + '\n'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('src'); ap.add_argument('--out', default='volume_items'); ap.add_argument('--boxes', default=str(HERE / 'factories_boxes.json'))
    a = ap.parse_args(argv)
    if a.src.lower().endswith('.sav'):
        sys.path.insert(0, str(HERE)); import satparse
        snap = satparse.parse_save(a.src)
    else:
        snap = json.loads(Path(a.src).read_text(encoding='utf-8'))
    bp = Path(a.boxes)
    boxes = json.loads(bp.read_text(encoding='utf-8')) if bp.is_file() else None
    res = compute(snap, boxes)
    Path(a.out + '.json').write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding='utf-8')
    Path(a.out + '.md').write_text(to_md(res), encoding='utf-8')
    print(json.dumps(res['summary']) if res else 'no network data')
    return 0 if res else 1


if __name__ == '__main__':
    sys.exit(main())
