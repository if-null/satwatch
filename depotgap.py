"""depotgap.py - Dimensional Depot gap check (satwatch module + CLI). stdlib only.

Rule: every item required to build anything unlocked must be in the depot;
any unlocked item not in the depot goes on the to-do list.

Based on Helm's /workspace/depotgap.py: same logic (build-gun recipes from the
unlocked recipe list -> their ingredients; GAP = stock 0, LOW = stock under
--low stacks), but it works from a satparse >= 0.3 snapshot (snap['depot'],
snap['recipes']) so satwatch can call it after every parse.

  python depotgap.py <save.sav | snapshot.json> [--data assets/sf-data1.0-depot.json] [--out depot_gap] [--low 1]
"""
import argparse, collections, json, os, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / 'assets' / 'sf-data1.0-depot.json'
RULE = ('Every item required to build anything unlocked must be in the depot; '
        'any unlocked item not in the depot goes on the to-do list.')
_DATA = None


def load_data(path=None):
    global _DATA
    if path is None and _DATA is not None:
        return _DATA
    d = json.loads(Path(path or DATA).read_text(encoding='utf-8'))
    if path is None:
        _DATA = d
    return d


def has_depot(snap):
    return bool(snap) and isinstance(snap.get('depot'), dict) and isinstance(snap.get('recipes'), list)


def compute(snap, data=None, low=1.0):
    """Return the gap report dict for one snapshot, or None if it has no depot/recipes data."""
    if not has_depot(snap):
        return None
    data = data or load_data()
    items, recipes = data['items'], data['recipes']
    nm = lambda k: (items.get(k) or {}).get('name') or k
    need = collections.defaultdict(set); unknown = []
    for rc in snap['recipes']:
        R = recipes.get(rc)
        if R is None:
            unknown.append(rc); continue
        if str(R.get('forBuilding')) not in ('True', 'true', '1') and R.get('forBuilding') is not True:
            continue
        for ing in R['ingredients']:
            need[ing['item']].add(R['name'])
    st = snap['depot'].get('stored') or {}
    gap, lowl, ok = [], [], []
    for it, blds in sorted(need.items(), key=lambda x: nm(x[0])):
        try:
            stack = int(float((items.get(it) or {}).get('stackSize') or 0))
        except ValueError:
            stack = 0
        row = {'item': it, 'name': nm(it), 'stock': st.get(it, 0), 'stack': stack, 'buildings': sorted(blds)}
        if row['stock'] <= 0:
            gap.append(row)
        elif stack and row['stock'] < low * stack:
            lowl.append(row)
        else:
            ok.append(row)
    sch = snap.get('schematics') or {}
    allsch = [x for v in sch.values() if isinstance(v, list) for x in v if isinstance(x, str)]
    h = snap.get('header') or {}
    return {
        'save': snap.get('file'), 'save_name': h.get('session_name') or h.get('save_name'),
        'play_hours': round(h['play_seconds'] / 3600, 2) if isinstance(h.get('play_seconds'), (int, float)) else None,
        'save_datetime': h.get('save_datetime_utc_guess') or snap.get('file_mtime'),
        'rule': RULE,
        'depot_buildings': snap['depot'].get('uploaders', 0),
        'depot_upgrade_schematics': sorted({x for x in allsch if re.search(r'CentralStorage|DimensionalDepot|Depot', x, re.I)}),
        'depot_recipe_unlocked': 'Recipe_CentralStorage_C' in snap['recipes'],
        'depot_stock': {nm(k): v for k, v in sorted(st.items(), key=lambda x: -x[1])},
        'depot_item_types': len(st), 'build_required_items': len(need), 'low_threshold_stacks': low,
        'gap': gap, 'low': lowl, 'ok_count': len(ok), 'unknown_recipes': unknown,
    }


def diff(cur, prev):
    """What changed in the gap/low lists since the previous report with depot data."""
    if not cur or not prev:
        return None
    g0 = {r['item']: r for r in prev['gap']}; g1 = {r['item']: r for r in cur['gap']}
    l0 = {r['item']: r for r in prev['low']}; l1 = {r['item']: r for r in cur['low']}
    return {'previous_save': prev['save'], 'previous_play_hours': prev.get('play_hours'),
            'new_gap': [g1[k] for k in g1 if k not in g0],
            'resolved_gap': [dict(g0[k], stock_now=(cur['depot_stock'].get(g0[k]['name'], 0))) for k in g0 if k not in g1],
            'new_low': [l1[k] for k in l1 if k not in l0],
            'left_low': [l0[k] for k in l0 if k not in l1],
            'uploaders_delta': cur['depot_buildings'] - prev['depot_buildings']}


def to_md(res, df=None):
    L = ['# Dimensional Depot gap (%s)' % res['save'], '',
         '> ' + res['rule'], '',
         'Depots built: %s | depot item types: %s | build-required items: %s | gap: %d | low: %d' % (
             res['depot_buildings'], res['depot_item_types'], res['build_required_items'], len(res['gap']), len(res['low'])),
         '', '## Gap (0 in depot)']
    for r in res['gap']:
        b = r['buildings']
        L.append('- [ ] %s (needed by: %s%s)' % (r['name'], ', '.join(b[:8]), ' ...(+%d)' % (len(b) - 8) if len(b) > 8 else ''))
    if not res['gap']:
        L.append('- none')
    L += ['', '## Low (< %g stack)' % res['low_threshold_stacks']]
    L += ['- %s: %s / stack %s' % (r['name'], r['stock'], r['stack']) for r in res['low']] or ['- none']
    if df:
        L += ['', '## Since %s' % df['previous_save']]
        L += ['- NEW gap: %s' % r['name'] for r in df['new_gap']]
        L += ['- resolved: %s (now %s)' % (r['name'], r['stock_now']) for r in df['resolved_gap']]
        L += ['- new low: %s' % r['name'] for r in df['new_low']]
        L += ['- uploaders %+d' % df['uploaders_delta']] if df['uploaders_delta'] else []
        if len(L) and L[-1].startswith('## Since'):
            L.append('- no change')
    if res['unknown_recipes']:
        L += ['', '%d unlocked recipes are not in the game data file (ignored).' % len(res['unknown_recipes'])]
    return '\n'.join(L) + '\n'


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('src', help='.sav (parsed with satparse) or a satwatch snapshot .json')
    ap.add_argument('--data', default=None)
    ap.add_argument('--out', default='depot_gap')
    ap.add_argument('--low', type=float, default=1.0, help='low threshold in stacks')
    a = ap.parse_args(argv)
    if a.src.lower().endswith('.json'):
        snap = json.loads(Path(a.src).read_text(encoding='utf-8'))
    else:
        sys.path.insert(0, str(HERE)); import satparse
        snap = satparse.parse_save(a.src)
    res = compute(snap, load_data(a.data), a.low)
    if res is None:
        print('no depot data in this snapshot (needs satparse >= 0.3)'); return 1
    Path(a.out + '.json').write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding='utf-8')
    md = to_md(res)
    Path(a.out + '.md').write_text(md, encoding='utf-8')
    print(md.encode('ascii', 'replace').decode())
    return 0


if __name__ == '__main__':
    sys.exit(main())
