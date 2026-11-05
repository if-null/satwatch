"""insights.py: the Page-only sections, computed from snapshots (stdlib only, read-only on snapshots).

milestone history, factory census, inventory & collectibles (with peaks across saves, cached),
header numbers and the "what the parser couldn't read" list.
"""
import json, re
from pathlib import Path

CENSUS = [
    ('Production', ['Constructor', 'Assembler', 'Manufacturer', 'Smelter', 'Foundry', 'Refinery', 'Blender', 'Packager',
                    'Particle Accelerator', 'Converter', 'Quantum Encoder']),
    ('Extraction', ['Miner Mk1', 'Miner Mk2', 'Miner Mk3', 'Oil Extractor', 'Water Extractor', 'Resource Well Extractor', 'Resource Well Pressurizer']),
    ('Power', ['Fuel Generator', 'Geothermal Generator', 'Coal Generator', 'Nuclear Power Plant', 'HUB Biomass Burner', 'Biomass Burner',
               'Alien Power Augmenter', 'Power Storage', 'Power Switch', 'Priority Power Switch']),
    ('Logistics', ['Belt Mk1', 'Belt Mk2', 'Belt Mk3', 'Belt Mk4', 'Belt Mk5', 'Belt Mk6', 'Railroad Track', 'Train Station', 'Freight Platform',
                   'Fluid Freight Platform', 'Empty Platform', 'Drone Port', 'Truck Station', 'Hypertube', 'Power Line']),
    ('Storage & special', ['Storage Container', 'Industrial Storage Container', 'Fluid Buffer', 'Industrial Fluid Buffer', 'Dimensional Depot',
                           'AWESOME Sink', 'AWESOME Shop', 'MAM', 'HUB', 'Space Elevator']),
]
SPECIAL = {'Desc_HardDrive_C': 'Hard drives', 'Desc_WAT1_C': 'Somersloops', 'Desc_WAT2_C': 'Mercer spheres',
           'Desc_Crystal_C': 'Blue power slugs', 'Desc_Crystal_mk2_C': 'Yellow power slugs', 'Desc_Crystal_mk3_C': 'Purple power slugs'}
CAVEATS = [
    'Storage containers are not scanned. Hard-drive and somersloop counts cover the player inventory only.',
    'Power is nominal only (nameplate x count, geothermal assumed 200 MW). Clock speeds, shards and fuel are not read.',
    'Space Elevator remaining costs and next goals come from game knowledge (default cost multiplier), not from the save.',
    'Which milestones remain in a tier cannot be named: the save stores milestone IDs only.',
    'The collected-object refs may double count, so they are not shown as totals.',
    'Milestone history is "first seen in a save": an event may have happened a little before the save that shows it.',
    'Save versions 47 to 50 are untested.',
]
INV_CACHE = 'inventory_peaks.json'
PEAK_KEYS = ('Desc_HardDrive_C', 'Desc_WAT1_C', 'Desc_WAT2_C')


def _read(p):
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def item_name(cls, names=None):
    if names and cls in names:
        return names[cls]
    if cls in SPECIAL:
        return SPECIAL[cls][:-1]
    s = re.sub(r'^(Desc_|BP_EquipmentDescriptor|BP_)', '', cls)
    s = re.sub(r'_C$', '', s).replace('_', ' ')
    return re.sub(r'(?<=[a-z])(?=[A-Z0-9])', ' ', s).strip()


def milestone_history(rows):
    """Events first seen in a save, from timeline rows (oldest first)."""
    ev, prev = [], None
    firsts = ['Refinery', 'Fuel Generator', 'Geothermal Generator', 'Manufacturer', 'Train Station', 'Miner Mk3',
              'Resource Well Extractor', 'Blender', 'Drone Port', 'Particle Accelerator', 'Coal Generator', 'Nuclear Power Plant',
              'Converter', 'Quantum Encoder']
    seen = set()
    best = {'max_tier': 0, 'phase': 0, 'bld': 0, 'alt': 0, 'ms': 0}
    last_t = None

    def add(r, text, major=False):
        ev.append({'date': (r.get('save_datetime') or '')[:10], 'hours': r.get('play_hours'), 'event': text, 'major': major, 'file': r.get('file')})

    for i, r in enumerate(rows):
        if i == 0:
            add(r, 'First save in the timeline: "%s" (build %s)' % (r.get('session'), r.get('build')), True)
        try:
            from datetime import datetime
            t = datetime.fromisoformat((r.get('save_datetime') or '')[:19])
            if last_t and (t - last_t).days >= 60:
                add(r, 'Back after a ~%d-day gap' % (t - last_t).days)
            last_t = t
        except Exception:
            pass
        if r.get('standin'):
            continue
        mt = r.get('max_tier') or 0
        if mt > best['max_tier']:
            add(r, 'Tier %s' % mt if mt - best['max_tier'] == 1 or best['max_tier'] == 0 and mt == 1 else 'Tiers %d to %d' % (best['max_tier'] + 1, mt), True)
            best['max_tier'] = mt
        try:
            ph = int(str(r.get('elevator_phase') or '').rsplit('_', 1)[-1])
        except ValueError:
            ph = 0
        if ph > best['phase']:
            add(r, 'Space Elevator phase %d delivered' % ph, True)
            best['phase'] = ph
        b = r.get('buildings_total') or 0
        for th in (1000, 5000, 10000, 15000, 20000, 25000, 30000, 40000, 50000):
            if best['bld'] < th <= b:
                add(r, '{:,} buildings'.format(th))
        best['bld'] = max(best['bld'], b)
        a = r.get('alt_recipes') or 0
        for th in (50, 100, 150):
            if best['alt'] < th <= a:
                add(r, '%d alternate recipes' % th)
        best['alt'] = max(best['alt'], a)
        m = r.get('milestones') or 0
        if m >= 44 > best['ms']:
            add(r, 'All 44 tier 1-8 milestones in the save', True)
        best['ms'] = max(best['ms'], m)
        for k in firsts:
            if k not in seen and (r.get(k) or 0) > 0:
                seen.add(k)
                if i > 0:
                    add(r, 'First %s' % k)
    if rows:
        r = rows[-1]
        add(r, 'Latest save: {:,} buildings'.format(r.get('buildings_total') or 0) if r.get('buildings_total') else 'Latest save')
    return ev


def census(snap):
    bf = (snap or {}).get('buildings_friendly') or {}
    total = (snap or {}).get('buildings_total') or 0
    cats, used = [], set()
    for c, names in CENSUS:
        xs = sorted([[k, bf[k]] for k in names if bf.get(k)], key=lambda x: -x[1])
        used.update(names)
        cats.append({'category': c, 'items': xs, 'total': sum(x[1] for x in xs)})
    extra = sorted([[k, v] for k, v in bf.items() if k not in used and v], key=lambda x: -x[1])
    if extra:
        cats.append({'category': 'Other named', 'items': extra, 'total': sum(x[1] for x in extra)})
    item = sum(c['total'] for c in cats)
    veh = (snap or {}).get('vehicles') or {}
    return {'categories': cats, 'itemized': item, 'buildings_total': total, 'unitemized': max(0, total - item),
            'vehicles': {'Locomotives': veh.get('BP_Locomotive_C', 0), 'Freight wagons': veh.get('BP_FreightWagon_C', 0),
                         'Drones': veh.get('BP_DroneTransport_C', 0), 'Other': sum(v for k, v in veh.items() if k not in ('BP_Locomotive_C', 'BP_FreightWagon_C', 'BP_DroneTransport_C'))}}


def _inv(snap):
    items = {}
    for p in (snap or {}).get('players') or []:
        for k, v in (p.get('items') or {}).items():
            items[k] = items.get(k, 0) + (v or 0)
    return items


def inventory(latest, rows, snaps_dir, work, names=None):
    """Player inventory of the latest save + peaks of special items across all snapshots (cached per file+size)."""
    cp = Path(work) / INV_CACHE
    cache = _read(cp) or {}
    changed = False
    peaks = {k: {'count': 0, 'date': None, 'file': None} for k in PEAK_KEYS}
    for r in rows or []:
        f = r.get('file')
        if not f or r.get('standin'):
            continue
        sp = Path(snaps_dir) / (Path(f).stem + '.json')
        try:
            sz = sp.stat().st_size
        except OSError:
            continue
        c = cache.get(f)
        if not c or c.get('size') != sz:
            inv = _inv(_read(sp))
            c = {'size': sz, 'd': {k: inv.get(k, 0) for k in PEAK_KEYS}}
            cache[f] = c
            changed = True
        for k in PEAK_KEYS:
            n = c['d'].get(k, 0)
            if n > peaks[k]['count']:
                peaks[k] = {'count': n, 'date': (r.get('save_datetime') or '')[:10], 'file': f}
    if changed:
        try:
            tmp = cp.with_name(cp.name + '.tmp')
            tmp.write_text(json.dumps(cache), encoding='utf-8')
            tmp.replace(cp)
        except OSError:
            pass
    inv = _inv(latest)
    top = sorted(inv.items(), key=lambda x: -x[1])
    s = latest or {}
    dp = s.get('drop_pods') or {}
    unc = s.get('uncollected_in_world') or {}
    return {
        'special_now': [[SPECIAL[k], inv.get(k, 0)] for k in SPECIAL],
        'peaks': [[SPECIAL[k], peaks[k]['count'], peaks[k]['date']] for k in PEAK_KEYS],
        'top_items': [[item_name(k, names), v] for k, v in top[:24]],
        'stacks': len(inv), 'units': sum(inv.values()),
        'drop_pods': {'opened': dp.get('opened'), 'total': dp.get('actors')},
        'left_in_world': [[n, unc.get(k)] for k, n in (('BP_WAT1_C', 'Somersloops'), ('BP_WAT2_C', 'Mercer spheres'),
                          ('BP_Crystal_C', 'Blue power slugs'), ('BP_Crystal_mk2_C', 'Yellow power slugs'), ('BP_Crystal_mk3_C', 'Purple power slugs')) if unc.get(k) is not None],
        'players': len(s.get('players') or []),
    }


def header(latest, rows):
    rows = rows or []
    ok = sum(1 for r in rows if r.get('status') == 'ok')
    first = next((r for r in rows if r.get('save_datetime')), None)
    h = (latest or {}).get('header') or {}
    return {'session': h.get('session_name'), 'play_hours': round(h['play_seconds'] / 3600.0, 1) if h.get('play_seconds') else None,
            'last_save_local': (latest or {}).get('file_mtime'), 'last_save_utc': h.get('save_datetime_utc_guess'),
            'started': (first or {}).get('save_datetime'), 'buildings': (latest or {}).get('buildings_total'),
            'saves_ok': ok, 'saves_total': len(rows),
            'saves_partial': sum(1 for r in rows if r.get('status') == 'partial'),
            'saves_other': sum(1 for r in rows if r.get('status') not in ('ok', 'partial'))}


def parser_notes(latest, rows, run_warnings, snaps_dir):
    """Auto list (non-ok saves, latest-snapshot warnings, run warnings) + fixed caveats."""
    auto = []
    bad = [r for r in rows or [] if r.get('status') != 'ok']
    if bad:
        by = {}
        for r in bad:
            by.setdefault(r.get('status'), []).append(r)
        for st, rs in by.items():
            hs = [r.get('play_hours') for r in rs if r.get('play_hours') is not None]
            auto.append('%d save(s) with status "%s" (%s to %s%s)%s' % (
                len(rs), st, (rs[0].get('save_datetime') or '?')[:16], (rs[-1].get('save_datetime') or '?')[:16],
                ('; %.1f to %.1f h' % (min(hs), max(hs))) if hs else '',
                '; counts treated as unknown, so "first seen" dates near them may be late' if any(r.get('standin') for r in rs) else ''))
    for w in ((latest or {}).get('warnings') or [])[:15]:
        auto.append('latest snapshot: ' + str(w)[:300])
    for w in (run_warnings or [])[:15]:
        auto.append('this run: ' + str(w).replace('\n', ' ')[:300])
    if latest and not (latest.get('network')):
        auto.append('latest snapshot has no network data (volume table/transport plan cannot show what exists)')
    return {'auto': auto, 'caveats': CAVEATS}


def read_todo(vault_out):
    """games/satisfactory/todo.md = parent of the vault data folder. Read-only. None when missing."""
    try:
        p = Path(vault_out).parent / 'todo.md'
        if p.is_file() and p.stat().st_size < 512 * 1024:
            return {'path': str(p), 'text': p.read_text(encoding='utf-8', errors='replace')}
    except OSError:
        pass
    return None
