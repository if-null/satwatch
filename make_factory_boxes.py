"""make_factory_boxes.py - build your own factories_boxes.json for the satwatch map overlay
(factories_boxes.example.json in the release is only an example from one world).

stdlib only. Reads a factories.json (+ raw_objects.json for member
building positions) and writes one bounding box per factory.

  python make_factory_boxes.py --in factories.json --raw raw_objects.json \
      --out factories_boxes.json [--margin-m 100] [--todo todo.md]

Members = production buildings re-clustered exactly as factoryid/analyze.py does
(single linkage, eps_cm from factories.json) plus extractors whose nearest
member is within 2*eps, plus the factory's attached stations. Every member is
asserted to lie inside its box.
"""
import argparse, collections, datetime, json, math, re, sys
from pathlib import Path

PROD = re.compile(r'Constructor|Assembler|Smelter|Foundry|Manufacturer|Refinery|Packager|Blender|HadronCollider|Converter|QuantumEnc')
EXT = re.compile(r'Miner|OilPump|WaterPump|Fracking')
DEFAULT_TOWN = re.compile(r'default/unnamed')


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def clusters(pts, eps):
    n = len(pts); par = list(range(n))
    def f(i):
        while par[i] != i:
            par[i] = par[par[i]]; i = par[i]
        return i
    for i in range(n):
        for j in range(i + 1, n):
            if dist(pts[i], pts[j]) <= eps:
                par[f(i)] = f(j)
    g = collections.defaultdict(list)
    for i in range(n):
        g[f(i)].append(i)
    return sorted(g.values(), key=lambda x: -len(x))


def todo_warnings(path):
    """Lines of section 1 ('Unmatched stations') of todo.md, keyed by factory number."""
    out = collections.defaultdict(list)
    if not path or not Path(path).exists():
        return out
    sec = False
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        if line.startswith('## '):
            sec = line.startswith('## 1.')
            continue
        if not sec or not line.lstrip().startswith('- ['):
            continue
        text = re.sub(r'^\s*- \[.\]\s*', '', line).replace('`', '').replace('**', '')
        text = re.sub(r'\s*\[(save|inferred|save \+ inferred|game knowledge)[^\]]*\]\s*$', '', text)
        ids = set(re.findall(r'\bF(\d+)\b', text))
        ids |= {b for a, b in re.findall(r'\bF(\d+)/F(\d+)\b', text)}
        for i in ids:
            out[i].append(text)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='inp', default='factories.json')
    ap.add_argument('--raw', default='raw_objects.json')
    ap.add_argument('--out', default='factories_boxes.json')
    ap.add_argument('--margin-m', type=float, default=100.0)
    ap.add_argument('--todo', default=None, help='optional todo.md to fold its "Unmatched stations" lines into warnings (read-only)')
    a = ap.parse_args(argv)
    D = json.loads(Path(a.inp).read_text(encoding='utf-8'))
    O = json.loads(Path(a.raw).read_text(encoding='utf-8'))
    eps = D['eps_cm']
    prod = [(c, pos) for c, p, pos, pr in O if PROD.search(c) and c.startswith('Build_')]
    P = [pos for c, pos in prod]
    cl = clusters(P, eps)
    F = D['factories']
    if len(cl) != len(F):
        sys.exit('cluster count %d != factories %d: raw_objects.json does not match factories.json' % (len(cl), len(F)))
    # match each cluster to its factory by centroid + size
    members = {}
    for idx in cl:
        cx = sum(P[j][0] for j in idx) / len(idx); cy = sum(P[j][1] for j in idx) / len(idx)
        f = min(F, key=lambda f: math.hypot(float(f['cx']) - cx, float(f['cy']) - cy))
        if int(f['n']) != len(idx) or math.hypot(float(f['cx']) - cx, float(f['cy']) - cy) > 1:
            sys.exit('cluster at (%d,%d) n=%d does not match F%s' % (cx, cy, len(idx), f['id']))
        members[str(f['id'])] = [('production', prod[j][0], P[j]) for j in idx]
    def nearest(pos):
        return min(((fid, min(dist(pos, m[2]) for m in ms)) for fid, ms in members.items()), key=lambda t: t[1])
    for c, p, pos, pr in O:
        if EXT.search(c) and c.startswith('Build_'):
            fid, d = nearest(pos)
            if d <= eps * 2:
                members[fid].append(('extraction', c, pos))
    todo = todo_warnings(a.todo)
    m = a.margin_m * 100
    out = []
    for f in F:
        fid = str(f['id'])
        pts = [x[2] for x in members[fid]] + [s['pos'] for s in f['stations']]
        x0 = min(p[0] for p in pts) - m; x1 = max(p[0] for p in pts) + m
        y0 = min(p[1] for p in pts) - m; y1 = max(p[1] for p in pts) + m
        for p in pts:
            assert x0 <= p[0] <= x1 and y0 <= p[1] <= y1, 'F%s member outside box' % fid
        warn = list(f.get('flags') or [])
        for s in f['stations']:
            if s.get('conflict'):
                warn.append('%s "%s": name/contents conflict' % (s['kind'], s['name']))
            if s['kind'] == 'drone' and not s.get('paired_to') and not s.get('paired_from'):
                warn.append('drone port "%s" is unpaired' % s['name'])
            elif s['kind'] == 'drone' and not s.get('has_drone') and s.get('paired_to'):
                warn.append('drone port "%s" has no drone' % s['name'])
            if not a.todo and s['kind'] == 'train' and not s.get('timetable_stops'):
                warn.append('train station "%s" has no timetable stop' % s['name'])
            if not a.todo and DEFAULT_TOWN.search(s.get('role') or ''):
                warn.append('%s "%s" still has a default name' % (s['kind'], s['name']))
        for t in todo.get(fid, []):
            if t not in warn:
                warn.append('todo: ' + t)
        nbt = collections.Counter(x[0] for x in members[fid])
        out.append({
            'id': fid, 'label': 'F' + fid, 'name': f['name'], 'name_source': f.get('name_source'),
            'region': f.get('region'),
            'bbox_cm': {'min_x': x0, 'min_y': y0, 'max_x': x1, 'max_y': y1},
            'size_m': [round((x1 - x0) / 100), round((y1 - y0) / 100)],
            'centroid_cm': [float(f['cx']), float(f['cy'])],
            'counts': {'production': nbt['production'], 'extraction': nbt['extraction'], 'stations': len(f['stations'])},
            'buildings': f['buildings'], 'extractors': f.get('extractors') or {},
            'top_recipes': list(f['recipes'].items())[:5],
            'final_outputs': f.get('final_outputs') or [],
            'stations': [{'kind': s['kind'], 'name': s['name'], 'role': s.get('role'), 'pos': s['pos'],
                          'conflict': bool(s.get('conflict'))} for s in f['stations']],
            'warnings': warn,
        })
    src = D.get('source', '')
    mt = re.search(r'([^\\/:\"]+?\.sav)', src); dt = re.search(r'(\d{4}-\d\d-\d\d(?: \d\d:\d\d)?(?: [A-Z]{3,4})?)', src)
    res = {
        'derived_from_save': {'file': mt.group(1) if mt else src, 'date': dt.group(1) if dt else None, 'source': src},
        'generated': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'margin_m': a.margin_m, 'eps_cm': eps,
        'note': 'Boxes reflect one save. To re-derive after new saves: re-run the factory analysis to get a new factories.json + raw_objects.json, then: python make_factory_boxes.py --in factories.json --raw raw_objects.json --out factories_boxes.json',
        'factories': out,
    }
    Path(a.out).write_text(json.dumps(res, indent=1, ensure_ascii=True), encoding='utf-8')
    print('wrote %s: %d factories, margin %g m, all members inside their boxes' % (a.out, len(out), a.margin_m))


if __name__ == '__main__':
    main()
