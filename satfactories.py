"""satfactories.py - detect factories automatically from a parsed Satisfactory save.

stdlib only. Input: the satwatch snapshot of the newest save (its 'network' block:
production machines with recipe + position, drone ports, train stations, platforms)
and, optionally, the map points file of the same save (extractor positions).
Output: the same JSON shape as factories_boxes.json (see factories_boxes.example.json),
marked "source": "auto", so the dashboard map overlay, the Volume 'Made at' column
and plan.md work without a hand-made file.

Detection (all distances in metres on the map plane, height ignored):
  * production buildings (Constructor, Assembler, Smelter, Foundry, Manufacturer,
    Refinery, Packager, Blender, Particle Accelerator, Converter, Quantum Encoder)
    are grouped by single linkage: two machines closer than EPS (default 150 m)
    are in the same factory;
  * groups with fewer than MIN_SIZE production buildings (default 3) are dropped;
  * extractors (miners, oil/water/well extractors) join the factory whose nearest
    machine is within 2*EPS; drone ports and train stations the same way;
  * factories are numbered F1, F2, ... by production building count (largest first);
  * the name is the factory's main end products (outputs not used up inside it,
    weighted by machine count), e.g. 'Motor / Rotor'; fallback 'F3';
  * the box is the bounding box of all members plus MARGIN (default 100 m).

  python satfactories.py <snapshot.json> [--points x.points.json] [--eps-m 150] [--min-size 3] [--out f.json]
"""
import collections, datetime, json, math, os, re, sys

VERSION = '1.0'
DEFAULT_EPS_M = 150.0
DEFAULT_MIN_SIZE = 3
DEFAULT_MARGIN_M = 100.0
PROD = re.compile(r'Constructor|Assembler|Smelter|Foundry|Manufacturer|Refinery|Packager|Blender|HadronCollider|Converter|QuantumEnc')
EXT_NAME = re.compile(r'Miner|Extractor|Pressurizer|OilPump|WaterPump|Fracking', re.I)
# map centre of the satmap bounds (cm), for the 'region' text
DRONE_FUEL = re.compile(r'Fuel|Battery|Turbo')  # drone ports burn these; not a recipe input
MAP_CX, MAP_CY = (-324698.832 + 425301.832) / 2, 0.0


def _clusters(pts, eps):
    """Single linkage with a grid (cells of eps); -> list of index lists, largest first."""
    par = list(range(len(pts)))

    def f(i):
        while par[i] != i:
            par[i] = par[par[i]]; i = par[i]
        return i
    grid = collections.defaultdict(list)
    for i, p in enumerate(pts):
        grid[(int(p[0] // eps), int(p[1] // eps))].append(i)
    for (gx, gy), ids in grid.items():
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((gx + dx, gy + dy), ()):
                    for i in ids:
                        if i < j and math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1]) <= eps:
                            par[f(i)] = f(j)
    g = collections.defaultdict(list)
    for i in range(len(pts)):
        g[f(i)].append(i)
    return sorted(g.values(), key=lambda x: (-len(x), min(pts[i][0] for i in x)))


def _rname(r):
    return re.sub(r'^Recipe_|_C$', '', r or '')


def _region(cx, cy):
    dx, dy = cx - MAP_CX, cy - MAP_CY
    d = math.hypot(dx, dy) / 100000.0
    if d < 0.5:
        return 'centre (%.1f km from map centre)' % d
    ns = 'N' if dy < -abs(dx) / 2.4 else ('S' if dy > abs(dx) / 2.4 else '')
    ew = 'E' if dx > abs(dy) / 2.4 else ('W' if dx < -abs(dy) / 2.4 else '')
    return '%s%s (%.1f km from map centre)' % ('outer ' if d > 2.5 else '', ns + ew, d)


def _items_name(I, c):
    return (I.get(c) or [re.sub(r'^Desc_|_C$', '', c)])[0]


def detect(snap, points=None, data=None, eps_m=DEFAULT_EPS_M, min_size=DEFAULT_MIN_SIZE, margin_m=DEFAULT_MARGIN_M):
    """-> factories_boxes dict ('factories' may be empty). Never needs a hand-made file."""
    net = (snap or {}).get('network') or {}
    eps = float(eps_m) * 100; m = float(margin_m) * 100
    I = (data or {}).get('items') or {}; R = (data or {}).get('recipes') or {}
    prod = [(c, r, p) for c, r, p in (net.get('machines') or []) if p and PROD.search(c or '')]
    P = [p for _c, _r, p in prod]
    groups = [g for g in _clusters(P, eps) if len(g) >= max(1, int(min_size))] if P else []
    members = [[('production', prod[j][0], prod[j][1], P[j]) for j in g] for g in groups]

    def nearest(pos):
        best = (None, 1e18)
        for k, ms in enumerate(members):
            for x in ms:
                d = math.hypot(x[3][0] - pos[0], x[3][1] - pos[1])
                if d < best[1]:
                    best = (k, d)
        return best
    ext = [[] for _ in members]
    pts_ok = points and (not snap.get('file') or points.get('file') in (None, snap.get('file')))
    for q in ((points or {}).get('points') or []) if pts_ok else []:
        if len(q) >= 5 and q[4] == 'extraction' and EXT_NAME.search(str(q[3])):
            k, d = nearest(q)
            if k is not None and d <= 2 * eps:
                ext[k].append((q[3], q))
    # stations: drone ports + train stations (platforms give a train station its load/unload role)
    plats = net.get('platforms') or []
    sts = [[] for _ in members]
    dports = [s for s in net.get('drone_ports') or [] if s.get('pos')]
    paired_from = collections.Counter(s.get('paired_to') for s in dports if s.get('paired_to'))
    for s in dports:
        k, d = nearest(s['pos'])
        if k is None or d > 2 * eps:
            continue
        # a drone port's input buffer holds what it sends, its output buffer what it received
        snd, rcv = s.get('input') or {}, s.get('output') or {}
        role = 'output (source)' if snd and not rcv else ('input (destination)' if rcv and not snd else ('input + output' if snd else 'empty'))
        sts[k].append({'kind': 'drone', 'name': s.get('name') or '?', 'role': role, 'pos': s['pos'], 'conflict': False,
                       '_in': set(rcv), '_out': set(snd), '_paired': s.get('paired_to') or paired_from.get(s.get('name'))})
    tst = [s for s in net.get('train_stations') or [] if s.get('pos')]
    by_st = collections.defaultdict(list)
    for p in plats:   # each cargo platform belongs to its nearest station (within 150 m)
        if not p.get('pos') or not tst or 'Docking' not in (p.get('kind') or ''):
            continue
        j, d = min(((j, math.hypot(p['pos'][0] - t['pos'][0], p['pos'][1] - t['pos'][1])) for j, t in enumerate(tst)), key=lambda t: t[1])
        if d <= 15000:
            by_st[j].append(p)
    for j, s in enumerate(tst):
        k, d = nearest(s['pos'])
        if k is None or d > 2 * eps:
            continue
        near = by_st.get(j, [])
        lm = {bool(p.get('load_mode')) for p in near}
        role = 'output (load)' if lm == {True} else ('input (unload)' if lm == {False} else ('input + output' if lm else 'no cargo platform'))
        sts[k].append({'kind': 'train', 'name': s.get('name') or '?', 'role': role, 'pos': s['pos'], 'conflict': False,
                       '_in': {c for p in near if not p.get('load_mode') for c in (p.get('items') or {})},
                       '_out': {c for p in near if p.get('load_mode') for c in (p.get('items') or {})}, '_paired': True})
    out = []
    for k, ms in enumerate(members):
        n = k + 1
        recs = collections.Counter(x[2] for x in ms if x[2])
        made = collections.Counter(); used = set()
        for r, c in recs.items():
            rr = R.get(r)
            if not rr:
                continue
            for g, _a in rr[2]:
                used.add(g)
            for i_, (p_, _a) in enumerate(rr[3]):
                made[p_] += c if i_ == 0 else 0   # by-products do not name a factory
        finals = [c for c, v in made.most_common() if v and c not in used] or [c for c, v in made.most_common(1) if v]
        shipped = {c for st in sts[k] for c in st['_out']}
        # products this factory ships out by drone/train name it first
        finals = [c for c, v in made.most_common() if v and c in shipped] + [c for c in finals if c not in shipped]
        fnames = [_items_name(I, c) for c in finals]
        name = ' / '.join(fnames[:2]) if fnames else ''
        pts = [x[3] for x in ms] + [e[1] for e in ext[k]] + [s['pos'] for s in sts[k]]
        x0 = min(p[0] for p in pts) - m; x1 = max(p[0] for p in pts) + m
        y0 = min(p[1] for p in pts) - m; y1 = max(p[1] for p in pts) + m
        cx = sum(x[3][0] for x in ms) / len(ms); cy = sum(x[3][1] for x in ms) / len(ms)
        warn = []
        for s in sts[k]:
            if s['kind'] == 'drone' and not s['_paired']:
                warn.append('drone port "%s" is unpaired' % s['name'])
            nu = sorted(_items_name(I, c) for c in s['_in'] if c not in used and not (s['kind'] == 'drone' and DRONE_FUEL.search(c)))
            if nu:
                warn.append('input %s "%s" (%s) is not consumed by any recipe in this cluster' % (
                    'station' if s['kind'] == 'train' else 'drone port', s['name'], ', '.join(nu[:3])))
        if any(not x[2] for x in ms):
            warn.append('%d machine(s) without a recipe' % sum(1 for x in ms if not x[2]))
        out.append({
            'id': str(n), 'label': 'F%d' % n, 'name': name or 'F%d' % n, 'source': 'auto',
            'name_source': 'auto-detected (main products)' if name else 'auto-detected',
            'region': _region(cx, cy),
            'bbox_cm': {'min_x': x0, 'min_y': y0, 'max_x': x1, 'max_y': y1},
            'size_m': [round((x1 - x0) / 100), round((y1 - y0) / 100)],
            'centroid_cm': [cx, cy],
            'counts': {'production': len(ms), 'extraction': len(ext[k]), 'stations': len(sts[k])},
            'buildings': dict(collections.Counter(x[1] for x in ms).most_common()),
            'extractors': dict(collections.Counter(e[0] for e in ext[k]).most_common()),
            'top_recipes': [[_rname(r), c] for r, c in recs.most_common(5)],
            'final_outputs': fnames[:8],
            'stations': [{kk: v for kk, v in s.items() if not kk.startswith('_')} for s in sts[k]],
            'warnings': warn,
        })
    f = (snap or {}).get('file')
    dt = (snap or {}).get('file_mtime') or ((snap or {}).get('header') or {}).get('save_datetime_utc_guess')
    return {'source': 'auto', 'satfactories_version': VERSION,
            'derived_from_save': {'file': f, 'date': str(dt)[:16].replace('T', ' ') if dt else None, 'source': f},
            'generated': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
            'margin_m': float(margin_m), 'eps_cm': int(eps), 'min_size': int(min_size),
            'note': 'auto-detected by satwatch from the newest save on every run; put your own factories_boxes.json beside satwatch.py to override',
            'extractors_included': bool(pts_ok),
            'factories': out}


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description='Auto-detect factory boxes from a satwatch snapshot.')
    ap.add_argument('snapshot'); ap.add_argument('--points'); ap.add_argument('--out')
    ap.add_argument('--eps-m', type=float, default=DEFAULT_EPS_M); ap.add_argument('--min-size', type=int, default=DEFAULT_MIN_SIZE)
    ap.add_argument('--margin-m', type=float, default=DEFAULT_MARGIN_M)
    a = ap.parse_args(argv)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import volume
    rd = lambda p: json.load(open(p, encoding='utf-8'))
    res = detect(rd(a.snapshot), rd(a.points) if a.points else None, volume.load_data(), a.eps_m, a.min_size, a.margin_m)
    for f in res['factories']:
        print('%-4s %-45s %4d x %-4d m  %3d prod %2d ext %2d st' % (f['label'], f['name'][:45], f['size_m'][0], f['size_m'][1],
              f['counts']['production'], f['counts']['extraction'], f['counts']['stations']))
    if a.out:
        with open(a.out, 'w', encoding='utf-8') as fh:
            json.dump(res, fh, indent=1)
    return 0


if __name__ == '__main__':
    sys.exit(main())
