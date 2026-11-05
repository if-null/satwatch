#!/usr/bin/env python3
"""satwatch: run-once Satisfactory save watcher + offline dashboard.

Double-click 'Satisfactory Update.bat'. Parses new '*.sav' saves with
satparse.py (shipped beside this file), rebuilds the timeline, writes a summary into
the Obsidian vault folder and serves a dashboard on http://127.0.0.1:8777/.
Stdlib only.
"""
import argparse, datetime, http.server, json, os, secrets, shutil, socketserver, sys, threading, time, traceback, webbrowser
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ---------------- configuration (edit here or use flags) ----------------
HOME = Path(os.environ.get('USERPROFILE') or Path.home())
_SAVEGAMES = Path(os.environ.get('LOCALAPPDATA') or (HOME / 'AppData' / 'Local')) / 'FactoryGame' / 'Saved' / 'SaveGames'
def _default_saves():
    # Saves live in SaveGames\<account id>\. Use the only such subfolder if there is exactly one.
    try:
        subs = [d for d in _SAVEGAMES.iterdir() if d.is_dir() and any(d.glob('*.sav'))]
    except OSError:
        subs = []
    return subs[0] if len(subs) == 1 else _SAVEGAMES
DEFAULT_SAVES = _default_saves()
DEFAULT_WORK = HOME / 'satisfactory-work'
DEFAULT_VAULT_OUT = HOME / 'Documents' / 'satwatch' / 'data'
DEFAULT_PATTERN = '*.sav'
DEFAULT_PORT = 8777
SATWATCH_VERSION = '1.3'

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import depotgap  # noqa: E402  (Dimensional Depot gap check)
import satparse  # noqa: E402  (save parser, shipped beside this file; 0.4 adds network data)
import volume  # noqa: E402  (Volume items table, computed from the latest snapshot)
import plan as planmod  # noqa: E402  (plan.json in the work folder; only the dashboard writes it)
import insights  # noqa: E402  (milestone history, census, inventory, parser notes)
try:
    import satmap  # noqa: E402  (map positions/bins; wraps satparse at runtime, never edits it)
    MAPS_OK = satmap.available(satparse)
except Exception:  # pragma: no cover
    satmap, MAPS_OK = None, False
ASSETS = HERE / 'assets'
FACTORY_BOXES = 'factories_boxes.json'  # written by make_factory_boxes.py, shipped beside this file


def say(*a):
    """ASCII-only console output (Windows consoles may be cp437/cp1252)."""
    s = ' '.join(str(x) for x in a)
    print(s.encode('ascii', 'replace').decode('ascii'), flush=True)


def now_iso():
    return datetime.datetime.now().isoformat(timespec='seconds')


def read_json(p, default=None):
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def write_text(p, text):
    p = Path(p)
    tmp = p.with_name(p.name + '.tmp')
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    os.replace(tmp, p)


def write_json(p, obj, compact=False):
    write_text(p, json.dumps(obj, separators=(',', ':')) if compact else json.dumps(obj, indent=1, default=str))


# ---------------- static game data (Satisfactory 1.1, satisfactory.wiki.gg) ----------------
# Milestones per tier as (save class id, name), from the game's own data (greeny/SatisfactoryTools data1.0.json).
# The save stores Schematic_T-N_C ids, so each milestone is matched exactly. Verified: every id in a
# save with max tier 8 matched tiers 1-8 with none left over.
MILESTONES = {
    1: [('Schematic_1-1_C', 'Base Building'), ('Schematic_1-2_C', 'Logistics'), ('Schematic_1-3_C', 'Field Research')],
    2: [('Schematic_2-1_C', 'Part Assembly'), ('Schematic_2-2_C', 'Obstacle Clearing'), ('Schematic_2-3_C', 'Jump Pads'), ('Schematic_2-5_C', 'Resource Sink Bonus Program')],
    3: [('Schematic_3-1_C', 'Coal Power'), ('Schematic_3-2_C', 'Logistics Mk.2'), ('Schematic_3-3_C', 'Vehicular Transport'), ('Schematic_3-4_C', 'Basic Steel Production')],
    4: [('Schematic_4-1_C', 'Advanced Steel Production'), ('Schematic_4-2_C', 'Enhanced Asset Security'), ('Schematic_4-3_C', 'Expanded Power Infrastructure'), ('Schematic_4-4_C', 'Hypertubes'), ('Schematic_4-5_C', 'FICSIT Blueprints')],
    5: [('Schematic_5-1_C', 'Oil Processing'), ('Schematic_5-2_C', 'Industrial Manufacturing'), ('Schematic_5-3_C', 'Logistics Mk.3'), ('Schematic_5-4_C', 'Fluid Packaging'), ('Schematic_5-5_C', 'Petroleum Power')],
    6: [('Schematic_6-1_C', 'Logistics Mk.4'), ('Schematic_6-2_C', 'Jetpack'), ('Schematic_6-3_C', 'Monorail Train Technology'), ('Schematic_6-5_C', 'Pipeline Engineering Mk.2'), ('Schematic_6-6_C', 'FICSIT Blueprints Mk.2'), ('Schematic_6-7_C', 'Railway Signalling')],
    7: [('Schematic_7-1_C', 'Bauxite Refinement'), ('Schematic_7-2_C', 'Logistics Mk.5'), ('Schematic_7-3_C', 'Hazmat Suit'), ('Schematic_7-4_C', 'Aeronautical Engineering'), ('Schematic_7-5_C', 'Control System Development')],
    8: [('Schematic_8-1_C', 'Nuclear Power'), ('Schematic_8-2_C', 'Advanced Aluminum Production'), ('Schematic_8-3_C', 'Hoverpack'), ('Schematic_8-4_C', 'Leading-Edge Production'), ('Schematic_8-5_C', 'Particle Enrichment')],
    9: [('Schematic_9-1_C', 'Matter Conversion'), ('Schematic_9-2_C', 'Quantum Encoding'), ('Schematic_9-3_C', 'FICSIT Blueprints Mk.3'), ('Schematic_9-4_C', 'Spatial Energy Regulation'), ('Schematic_9-5_C', 'Peak Efficiency')],
}
# Tiers unlocked by completed elevator phase (wiki: Phase 1 -> T3/4, 2 -> T5/6, 3 -> T7/8, 4 -> T9)
TIERS_UNLOCKED_BY_PHASE = {0: 2, 1: 4, 2: 6, 3: 8, 4: 9, 5: 9}
# Space Elevator delivery requirements (default cost multiplier), verified against
# https://satisfactory.wiki.gg/wiki/Space_Elevator (2026-11-04).
ELEVATOR = {
    1: ('Distribution Platform', [('Smart Plating', 50)], 'Tiers 3 and 4'),
    2: ('Construction Dock', [('Smart Plating', 1000), ('Versatile Framework', 1000), ('Automated Wiring', 100)], 'Tiers 5 and 6'),
    3: ('Main Body', [('Versatile Framework', 2500), ('Modular Engine', 500), ('Adaptive Control Unit', 100)], 'Tiers 7 and 8'),
    4: ('Propulsion', [('Assembly Director System', 500), ('Magnetic Field Generator', 500), ('Thermal Propulsion Rocket', 250), ('Nuclear Pasta', 100)], 'Tier 9'),
    5: ('Assembly', [('Nuclear Pasta', 1000), ('Biochemical Sculptor', 1000), ('AI Expansion Server', 256), ('Ballistic Warp Drive', 200)], 'Project Assembly launch'),
}
# Desc_SpaceElevatorPart_N_C -> name. NOT on the wiki pages above (game-data knowledge); unmatched ids show raw.
PART_NAMES = {
    'Desc_SpaceElevatorPart_1_C': 'Smart Plating', 'Desc_SpaceElevatorPart_2_C': 'Versatile Framework',
    'Desc_SpaceElevatorPart_3_C': 'Automated Wiring', 'Desc_SpaceElevatorPart_4_C': 'Modular Engine',
    'Desc_SpaceElevatorPart_5_C': 'Adaptive Control Unit', 'Desc_SpaceElevatorPart_6_C': 'Assembly Director System',
    'Desc_SpaceElevatorPart_7_C': 'Magnetic Field Generator', 'Desc_SpaceElevatorPart_8_C': 'Nuclear Pasta',
    'Desc_SpaceElevatorPart_9_C': 'Thermal Propulsion Rocket', 'Desc_SpaceElevatorPart_10_C': 'Biochemical Sculptor',
    'Desc_SpaceElevatorPart_11_C': 'Ballistic Warp Drive', 'Desc_SpaceElevatorPart_12_C': 'AI Expansion Server',
}
GAME_DATA = {'milestones': {str(k): [n for _, n in v] for k, v in MILESTONES.items()},
             'milestone_ids': {str(k): v for k, v in MILESTONES.items()},
             'elevator': {str(k): {'name': v[0], 'cost': v[1], 'unlocks': v[2]} for k, v in ELEVATOR.items()},
             'part_names': PART_NAMES, 'tiers_by_phase': {str(k): v for k, v in TIERS_UNLOCKED_BY_PHASE.items()}}


def phase_num(s):
    try:
        return int(str(s).rsplit('_', 1)[-1])
    except Exception:
        return None


def base_milestones_by_tier(snap):
    """Count base milestone schematics (Schematic_T-N_C, ignoring Schematic_T-N-M_C sub-unlocks)."""
    out = {}
    for n in ((snap.get('schematics') or {}).get('milestones') or []):
        parts = n[len('Schematic_'):-2].split('-') if n.startswith('Schematic_') and n.endswith('_C') else []
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            out[int(parts[0])] = out.get(int(parts[0]), 0) + 1
    return out


def whats_next(snap):
    """Python mirror of the dashboard's 'What's next' (used for status.md)."""
    lines = []
    sc = snap.get('schematics') or {}
    el = snap.get('space_elevator') or {}
    cur = phase_num(el.get('CurrentGamePhase'))
    tgt = phase_num(el.get('TargetGamePhase'))
    avail = TIERS_UNLOCKED_BY_PHASE.get(cur, None) if cur is not None else None
    have_ids = set((sc.get('milestones') or []))
    if not sc:
        return ['- schematics unknown for this snapshot (status %s)' % snap.get('status')]
    for t in range(1, (avail or sc.get('max_tier') or 0) + 1):
        missing = [n for i, n in MILESTONES.get(t, []) if i not in have_ids]
        if missing:
            lines.append('- Tier %d: %d of %d milestones done; not yet: %s' % (t, len(MILESTONES[t]) - len(missing), len(MILESTONES[t]), ', '.join(missing)))
    if not lines:
        lines.append('- All milestones of available tiers (1-%s) are complete' % (avail or '?'))
    if tgt and tgt in ELEVATOR:
        name, cost, unl = ELEVATOR[tgt]
        paid = {PART_NAMES.get(p.get('item'), p.get('item')): p.get('amount', 0) for p in (el.get('paid_toward_target') or [])}
        lines.append('- Space Elevator phase %d "%s" (unlocks %s):' % (tgt, name, unl))
        for item, amt in cost:
            lines.append('    - %s: %s / %s' % (item, paid.get(item, 0), amt))
    elif cur == 5:
        lines.append('- Space Elevator: all 5 phases delivered')
    return lines


# ---------------- scanning / parsing ----------------
def snap_name(save):
    return Path(save).stem + '.json'


def already_parsed(save, st, state, snaps_dir, notes):
    key = state.get(save.name)
    if key:
        return key.get('size') == st.st_size and abs(key.get('mtime', 0) - st.st_mtime) < 0.001
    sp = snaps_dir / snap_name(save)
    if not sp.exists():
        return False
    s = read_json(sp, {}) or {}
    try:
        smt = datetime.datetime.fromisoformat(s.get('file_mtime')).timestamp()
    except Exception:
        smt = None
    if s.get('file_size') == st.st_size and smt is not None and abs(smt - st.st_mtime) < 1.0:
        return True
    if s.get('file_size') == st.st_size and smt is not None and abs(smt - st.st_mtime) <= 2.5:
        notes.append('%s: matched existing snapshot by name+size (mtime differs %.1fs, rounding)' % (save.name, smt - st.st_mtime))
        return True
    if s.get('file_size') == st.st_size:
        # Snapshots may have been made on another machine/timezone: same name + same byte size counts as parsed.
        notes.append('%s: matched existing snapshot by name+size (mtime differs, likely timezone)' % save.name)
        return True
    return False


def parse_one(save, snaps_dir, work=None, maps=False):
    cap = None
    try:
        if maps and MAPS_OK:
            with satmap.capture(satparse) as cap:
                s = satparse.parse_save(str(save))
        else:
            s = satparse.parse_save(str(save))
    except Exception:
        s = {'file': save.name, 'status': 'unparsed', 'warnings': ['fatal: %s' % traceback.format_exc()[-500:]]}
        cap = None
    write_text(snaps_dir / snap_name(save), json.dumps(s, separators=(',', ':'), default=str))
    if cap is not None and work is not None and s.get('status') in ('ok', 'partial'):
        try:
            satmap.save_bins(str(work), str(save), s, cap, satparse)
        except Exception as e:
            s.setdefault('warnings', []).append('map bins failed: %s' % e)
    return s


def bin_only(save, work):
    """Re-read one save just for map bins (snapshot file is not touched). Returns seconds or None."""
    t0 = time.time()
    with satmap.capture(satparse) as cap:
        s = satparse.parse_save(str(save))
    if s.get('status') not in ('ok', 'partial'):
        return None
    satmap.save_bins(str(work), str(save), s, cap, satparse)
    return time.time() - t0


def copy_factory_boxes(vault, warnings):
    """Copy the shipped factories_boxes.json to the vault folder if it is missing there or older."""
    src = HERE / FACTORY_BOXES
    if not src.is_file():
        return
    dst = vault / FACTORY_BOXES
    try:
        if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
            return
        vault.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))
        say('Factory boxes copied to', dst)
    except Exception as e:
        warnings.append('factory boxes copy failed: %s' % e)


def needs_depot(snaps_dir, save):
    """True if this save's snapshot lacks depot or network data and was made by an older satparse (so a re-parse can add it)."""
    s = read_json(snaps_dir / snap_name(save)) or {}
    if 'depot' in s and 'network' in s:
        return False
    try:
        return float(s.get('parser_version') or 0) < float(satparse.PARSER_VERSION)
    except ValueError:
        return True


def depot_reparse(a, saves, snaps_dir, work, warnings):
    """Re-parse the newest save (and with --backfill-depot all older ones) whose snapshot has no depot data."""
    if not saves:
        return
    if getattr(a, 'backfill_depot', False):
        todo = [sv for sv in saves if needs_depot(snaps_dir, sv)]
        say('Depot backfill: %d of %d saves need depot/recipe data (about 1 s each)' % (len(todo), len(saves)))
    else:
        sv = saves[-1]
        todo = [sv] if (getattr(a, 'reparse_latest', False) or needs_depot(snaps_dir, sv)) else []
        if todo:
            say('Depot/volume: re-parsing latest save for depot + network data:', sv.name)
    t_all = time.time()
    for i, sv in enumerate(todo, 1):
        t0 = time.time()
        s = parse_one(sv, snaps_dir, work, getattr(a, 'maps', True))
        if len(todo) > 1 or getattr(a, 'backfill_depot', False):
            say('  depot %d/%d %s status=%s %.1fs' % (i, len(todo), sv.name, s.get('status'), time.time() - t0))
        if s.get('status') not in ('ok', 'partial'):
            warnings.append('depot re-parse %s: status %s' % (sv.name, s.get('status')))
    if len(todo) > 1:
        say('Depot backfill done in %.0fs' % (time.time() - t_all))


def depot_pass(snaps_dir, tl, latest, vault, warnings):
    """Gap report for the latest snapshot + diff vs the previous snapshot that has depot data."""
    out = {'rule': depotgap.RULE, 'latest': None, 'previous': None, 'diff': None, 'snapshots_with_depot': 0}
    try:
        cur = depotgap.compute(latest) if latest else None
        out['latest'] = cur
        if latest and cur is None:
            out['note'] = 'no depot data in this snapshot'
        rows = tl.get('rows') or []
        n = 0
        for r in reversed(rows):
            f = r.get('file')
            if not f or (latest and f == latest.get('file')):
                continue
            sn = read_json(snaps_dir / (Path(f).stem + '.json'))
            if depotgap.has_depot(sn):
                n += 1
                if out['previous'] is None:
                    out['previous'] = depotgap.compute(sn)
        out['snapshots_with_depot'] = n + (1 if cur else 0)
        out['diff'] = depotgap.diff(cur, out['previous'])
        if cur:
            vault.mkdir(parents=True, exist_ok=True)
            write_json(vault / 'depot_gap.json', dict(cur, diff=out['diff']))
            write_text(vault / 'depot_gap.md', depotgap.to_md(cur, out['diff']))
            say('Depot: %d gap, %d low, %d uploaders (%s)' % (len(cur['gap']), len(cur['low']), cur['depot_buildings'], cur['save']))
        else:
            say('Depot: no depot data in the latest snapshot')
    except Exception as e:
        warnings.append('depot check failed: %s' % e)
    return out


def volume_pass(latest, vault, warnings, plan=None):
    """Volume items table for the latest snapshot -> vault volume_items.json/.md. Never raises."""
    out = {'available': False, 'rule': volume.RULE}
    try:
        if not volume.has_network(latest):
            out['note'] = 'no network data in the latest snapshot (made by satparse %s); run once more or use --reparse-latest' % ((latest or {}).get('parser_version'))
            say('Volume: no network data in the latest snapshot')
            return out
        bp = HERE / FACTORY_BOXES
        boxes = read_json(bp) if bp.is_file() else None
        t0 = time.time()
        res = volume.compute(latest, boxes)
        res['outpost'] = volume.outpost(latest, boxes)
        out = dict(res, available=True)
        if plan is not None:
            res = dict(res, planned=planmod.planned_map(plan, res))
        write_json(vault / 'volume_items.json', res)
        write_text(vault / 'volume_items.md', volume.to_md(res))
        sm = res['summary']
        say('Volume: %d items, %d depot, %d drone, %d train, %d none (%.2fs)' % (sm['rows'], sm['in_depot'], sm['on_drone'], sm['on_train'], sm['on_none'], time.time() - t0))
    except Exception:
        warnings.append('volume table failed: %s' % traceback.format_exc()[-300:])
    return out


def map_pass(a, saves, work, vault, latest, warnings):
    """Backfill (opt-in), make sure the latest save has bins/points, write latest_points + map_summary."""
    if not getattr(a, 'maps', True):
        return
    if not MAPS_OK:
        warnings.append('map capture disabled: satparse.parse_object_headers differs from the version satmap was written for')
        return
    if getattr(a, 'backfill_maps', False):
        todo = [sv for sv in saves if not satmap.has_bins(str(work), sv.name, sv.stat().st_size)]
        say('Map backfill: %d of %d saves need bins (about 1 s each)' % (len(todo), len(saves)))
        t_all = time.time()
        for i, sv in enumerate(todo, 1):
            try:
                dt = bin_only(sv, work)
                say('  map %d/%d %s %s' % (i, len(todo), sv.name, ('%.1fs' % dt) if dt is not None else 'skipped (parse status)'))
            except Exception as e:
                warnings.append('map backfill %s: %s' % (sv.name, e))
        if todo:
            say('Map backfill done in %.0fs' % (time.time() - t_all))
    if latest and latest.get('file'):
        sv = next((x for x in saves if x.name == latest['file']), None)
        if sv is not None and not satmap.has_bins(str(work), sv.name, sv.stat().st_size):
            say('Map: binning latest save', sv.name)
            try:
                bin_only(sv, work)
            except Exception as e:
                warnings.append('map bins for latest failed: %s' % e)
    mb = work / 'map_bins'
    lst = satmap.list_bins(str(work))
    summ = satmap.meta(str(work))
    summ.pop('snapshots', None)
    summ.update({'generated': now_iso(), 'latest_file': None, 'latest_totals': None,
                 'snapshots_with_bins': len(lst)})
    pts = None
    if latest and latest.get('file'):
        pp = mb / (Path(latest['file']).stem + '.points.json')
        pts = read_json(pp)
        b = read_json(mb / (Path(latest['file']).stem + '.json')) or {}
        summ['latest_file'] = latest['file']
        summ['latest_totals'] = b.get('totals')
        summ['latest_player_cm'] = b.get('player')
    try:
        mb.mkdir(parents=True, exist_ok=True)
        if pts:
            write_json(mb / 'latest_points.json', pts, compact=True)
        write_json(mb / 'map_summary.json', summ)
        if pts:
            write_json(vault / 'latest_points.json', pts, compact=True)
        write_json(vault / 'map_summary.json', summ)
    except Exception as e:
        warnings.append('map output failed: %s' % e)
    say('Map: %d snapshot(s) have bins%s' % (len(lst), '' if pts else ' (no points for latest save yet)'))


def refresh(a):
    saves_dir, work, vault = Path(a.saves), Path(a.work), Path(a.vault_out)
    snaps_dir = work / 'snaps'
    snaps_dir.mkdir(parents=True, exist_ok=True)
    state_p = work / 'state.json'
    state = (read_json(state_p, {}) or {}).get('files', {})
    warnings, notes = [], []
    if not saves_dir.is_dir():
        warnings.append('saves folder not found: %s' % saves_dir)
        saves = []
    else:
        saves = sorted(saves_dir.glob(a.pattern), key=lambda p: p.stat().st_mtime)
    say('satwatch %s / satparse %s' % (SATWATCH_VERSION, satparse.PARSER_VERSION))
    say('Saves: %s  (%d matching %r)' % (saves_dir, len(saves), a.pattern))
    todo = []
    for sv in saves:
        st = sv.stat()
        if already_parsed(sv, st, state, snaps_dir, notes):
            state[sv.name] = {'size': st.st_size, 'mtime': st.st_mtime}
        else:
            todo.append((sv, st))
    for n in notes[:5]:
        say('note:', n)
    if len(notes) > 5:
        say('note: ... %d more name+size matches' % (len(notes) - 5))
    if not todo:
        say('No new saves since last run (%d already parsed).' % len(saves))
    parsed, failed = [], []
    for i, (sv, st) in enumerate(todo, 1):
        t0 = time.time()
        s = parse_one(sv, snaps_dir, work, getattr(a, 'maps', True))
        state[sv.name] = {'size': st.st_size, 'mtime': st.st_mtime}
        stt = s.get('status')
        say('%d/%d %s status=%s %.1fs' % (i, len(todo), sv.name, stt, time.time() - t0))
        (parsed if stt in ('ok', 'partial', 'header_only') else failed).append(sv.name)
        if stt != 'ok':
            warnings.append('%s: status %s %s' % (sv.name, stt, '; '.join(str(w)[:150] for w in (s.get('warnings') or [])[:2])))
        if i % 25 == 0:
            write_json(state_p, {'files': state, 'updated': now_iso()})
    write_json(state_p, {'files': state, 'updated': now_iso()})
    depot_reparse(a, saves, snaps_dir, work, warnings)
    # timeline (satparse's own function, same output as satparse --timeline)
    tl = {'rows': [], 'diffs': []}
    if any(snaps_dir.glob('*.json')):
        try:
            satparse.timeline(str(snaps_dir), str(work))
            tl = read_json(work / 'timeline.json', tl)
        except Exception:
            warnings.append('timeline failed: %s' % traceback.format_exc()[-300:])
    latest = load_latest(snaps_dir, tl)
    try:
        vault.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    map_pass(a, saves, work, vault, latest, warnings)
    copy_factory_boxes(vault, warnings)
    depot = depot_pass(snaps_dir, tl, latest, vault, warnings)
    plan, perr = planmod.load(work)
    if perr:
        warnings.append(perr)
    vol = volume_pass(latest, vault, warnings, plan)
    write_plan_md(vault, plan, vol, warnings)
    ins = {}
    try:
        rows = tl.get('rows') or []
        names = {k: v[0] for k, v in (volume.load_data().get('items') or {}).items()}
        ins = {'history': insights.milestone_history(rows), 'census': insights.census(latest),
               'inventory': insights.inventory(latest, rows, snaps_dir, work, names), 'header': insights.header(latest, rows)}
    except Exception:
        warnings.append('insights failed: %s' % traceback.format_exc()[-300:])
    # vault output
    try:
        vault.mkdir(parents=True, exist_ok=True)
        if latest:
            write_json(vault / 'latest.json', latest)
        write_json(vault / 'timeline.json', tl)
        write_text(vault / 'status.md', status_md(latest, tl, len(todo), parsed, failed, warnings, a, vol))
        say('Vault files updated in', vault)
    except Exception as e:
        warnings.append('could not write vault folder %s: %s' % (vault, e))
        say('WARNING: could not write vault folder:', e)
    for w in warnings:
        say('WARNING:', w)
    try:
        ins['notes'] = insights.parser_notes(latest, tl.get('rows') or [], warnings, snaps_dir)
    except Exception:
        pass
    return {'tl': tl, 'latest': latest, 'snaps_dir': snaps_dir, 'work': work, 'vault': vault, 'depot': depot, 'volume': vol, 'insights': ins, 'new': len(todo), 'parsed': parsed,
            'failed': failed, 'warnings': warnings, 'generated': now_iso()}


def factory_boxes():
    bp = HERE / FACTORY_BOXES
    return read_json(bp) if bp.is_file() else None


def write_plan_md(vault, plan, vol, warnings):
    """Read-only plan.md for the vault (never read back)."""
    try:
        vault = Path(vault)
        vault.mkdir(parents=True, exist_ok=True)
        write_text(vault / 'plan.md', planmod.to_md(plan, vol, factory_boxes(), (vol or {}).get('outpost'), SATWATCH_VERSION))
        return True
    except Exception as e:
        warnings.append('plan.md not written: %s' % e)
        return False


def load_latest(snaps_dir, tl):
    rows = tl.get('rows') or []
    for r in reversed(rows):
        s = read_json(snaps_dir / (Path(r.get('file') or '').stem + '.json'))
        if s and s.get('status') in ('ok', 'partial'):
            return s
    if rows:
        return read_json(snaps_dir / (Path(rows[-1].get('file') or '').stem + '.json'))
    return None


def fmt_hours(sec):
    return '%.1f h' % (sec / 3600.0) if isinstance(sec, (int, float)) else 'unknown'


def status_md(s, tl, n_new, parsed, failed, warnings, a, vol=None):
    L = ['# Satisfactory status', '', 'Generated: %s by satwatch %s (satparse %s)' % (now_iso(), SATWATCH_VERSION, satparse.PARSER_VERSION), '']
    if not s:
        L += ['No snapshots yet.', '']
    else:
        h = s.get('header') or {}
        sc = s.get('schematics') or {}
        el = s.get('space_elevator') or {}
        cur, tgt = phase_num(el.get('CurrentGamePhase')), phase_num(el.get('TargetGamePhase'))
        L += ['## Latest save', '',
              '- File: %s (status %s)' % (s.get('file'), s.get('status')),
              '- Saved: %s (file mtime, local) / %s (in-save, UTC guess)' % (s.get('file_mtime'), h.get('save_datetime_utc_guess')),
              '- Session: %s' % h.get('session_name'),
              '- Play time: %s' % fmt_hours(h.get('play_seconds')),
              '- Tier: max milestone tier %s, available up to tier %s' % (sc.get('max_tier'), TIERS_UNLOCKED_BY_PHASE.get(cur, '?')),
              '- Space Elevator: phase %s delivered, working on phase %s' % (cur, tgt),
              '- Schematics: %s total %s' % (sc.get('total'), json.dumps(sc.get('counts') or {})),
              '- Buildings total: %s' % s.get('buildings_total'),
              '- Power generators: %s' % (', '.join('%s %s' % (v, k) for k, v in (s.get('power_generators') or {}).items()) or 'none/unknown'),
              '- Extractors: %s' % (', '.join('%s %s' % (v, k) for k, v in (s.get('extractors') or {}).items()) or 'none/unknown'),
              '']
        L += ["## What's next", ''] + whats_next(s) + ['']
        if vol and vol.get('available'):
            sm = vol['summary']
            L += ['## Volume items', '', '- %d items: %d in depot, %d on drone, %d on train, %d on none (details: volume_items.md)' % (
                sm['rows'], sm['in_depot'], sm['on_drone'], sm['on_train'], sm['on_none']), '']
    L += ['## This run', '', '- New saves parsed: %d (failed: %d)' % (n_new, len(failed)),
          '- Snapshots in timeline: %d' % len(tl.get('rows') or [])]
    if warnings:
        L += ['- Warnings:'] + ['    - ' + w.replace('\n', ' ')[:300] for w in warnings[:20]]
    else:
        L += ['- Warnings: none']
    L += ['', '_Individual snapshots stay in the work folder; this folder holds latest.json, timeline.json, status.md, depot_gap.*, volume_items.*, plan.md (read-only; edit in the dashboard) and map files._', '']
    return '\n'.join(L)


# ---------------- HTTP server ----------------
MAX_BODY = 64 * 1024


class Handler(http.server.BaseHTTPRequestHandler):
    ctx = None
    token = secrets.token_urlsafe(24)   # per run; embedded in the served HTML, required as X-Plan-Token on POST
    port = DEFAULT_PORT

    def _host_ok(self):
        h = (self.headers.get('Host') or '').strip().lower()
        return h in ('127.0.0.1:%d' % self.port, 'localhost:%d' % self.port)

    def _origin_ok(self):
        o = self.headers.get('Origin')
        if o is None:
            return True
        return o.strip().lower() in ('http://127.0.0.1:%d' % self.port, 'http://localhost:%d' % self.port)

    def jsend(self, code, obj):
        return self.send(code, json.dumps(obj, default=str, ensure_ascii=False), 'application/json; charset=utf-8')

    def plan_view(self):
        c = self.ctx
        p, err = planmod.load(c['work'])
        vol = c.get('volume') or {}
        return {'plan': p, 'load_note': err, 'exists': planmod.exists_map(vol),
                'next_default_pos': ((vol.get('outpost') or {}).get('player_cm') or [None, None])[:2],
                'limits': dict(planmod.LIMITS, design_url=planmod.MAX_URL), 'networks': list(planmod.NETWORKS)}

    def do_POST(self):
        try:
            u = urlparse(self.path)
            if u.path not in ('/api/plan/factory', '/api/plan/transport'):
                return self.jsend(404, {'error': 'not found'})
            if not self._host_ok():
                return self.jsend(403, {'error': 'bad host'})
            if not self._origin_ok():
                return self.jsend(403, {'error': 'cross-origin request refused'})
            if not secrets.compare_digest(self.headers.get('X-Plan-Token') or '', self.token):
                return self.jsend(403, {'error': 'missing or wrong X-Plan-Token (reload the dashboard)'})
            if 'application/json' not in (self.headers.get('Content-Type') or ''):
                return self.jsend(415, {'error': 'Content-Type must be application/json'})
            try:
                n = int(self.headers.get('Content-Length') or '-1')
            except ValueError:
                n = -1
            if n < 0:
                return self.jsend(411, {'error': 'Content-Length required'})
            if n > MAX_BODY:
                self.close_connection = True
                return self.jsend(413, {'error': 'body too large (max %d bytes)' % MAX_BODY})
            try:
                body = json.loads(self.rfile.read(n).decode('utf-8'))
                if not isinstance(body, dict):
                    raise ValueError('not an object')
            except Exception:
                return self.jsend(400, {'error': 'body must be a JSON object'})
            c = self.ctx
            with planmod.LOCK:
                p, _ = planmod.load(c['work'])
                try:
                    (planmod.set_factory if u.path.endswith('/factory') else planmod.set_transport)(p, body)
                except planmod.PlanError as e:
                    return self.jsend(400, {'error': str(e)})
                changed = planmod.save(c['work'], p)
                md = write_plan_md(c['vault'], p, c.get('volume'), c['warnings']) if changed else None
            v = self.plan_view()
            v.update(ok=True, changed=changed, plan_md=md)
            return self.jsend(200, v)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            try:
                self.jsend(500, {'error': 'server error: %s' % traceback.format_exc()[-200:]})
            except Exception:
                pass

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype):
        b = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        c = self.ctx
        try:
            if not self._host_ok():
                return self.send(403, 'bad host', 'text/plain')
            if u.path in ('/', '/index.html'):
                return self.send(200, DASHBOARD_HTML.replace('__PLAN_TOKEN__', self.token), 'text/html; charset=utf-8')
            if u.path == '/api/plan':
                return self.jsend(200, self.plan_view())
            if u.path == '/api/insights':
                d = dict(c.get('insights') or {})
                d['todo'] = insights.read_todo(c['vault'])
                return self.jsend(200, d)
            if u.path == '/api/data':
                d = {'timeline': c['tl'].get('rows') or [], 'diffs': c['tl'].get('diffs') or [], 'latest': c['latest'],
                     'game': GAME_DATA, 'run': {'new': c['new'], 'failed': c['failed'], 'warnings': c['warnings'],
                     'generated': c['generated'], 'satwatch': SATWATCH_VERSION, 'parser_version': satparse.PARSER_VERSION}}
                return self.send(200, json.dumps(d, default=str), 'application/json')
            if u.path == '/api/snapshot':
                name = (parse_qs(u.query).get('name') or [''])[0]
                p = (c['snaps_dir'] / (Path(name).stem + '.json'))
                if Path(name).name != name or not p.is_file():
                    return self.send(404, '{"error":"not found"}', 'application/json')
                return self.send(200, p.read_text(encoding='utf-8'), 'application/json')
            if u.path == '/api/mapmeta':
                if not MAPS_OK:
                    return self.send(200, json.dumps({'snapshots': [], 'disabled': True}), 'application/json')
                m = satmap.meta(str(c['work']), len(c['tl'].get('rows') or []), ASSETS)
                m['image_present'] = (ASSETS / satmap.pick_image(ASSETS)).is_file()
                m['latest_file'] = (c['latest'] or {}).get('file')
                return self.send(200, json.dumps(m), 'application/json')
            if u.path == '/api/mapbins':
                snap = (parse_qs(u.query).get('snap') or [''])[0]
                st = Path(snap).stem
                p = c['work'] / 'map_bins' / (st + '.json')
                if not st or Path(snap).name != snap or st.endswith('.points') or st in ('latest_points', 'map_summary') or not p.is_file():
                    return self.send(404, '{"error":"not found"}', 'application/json')
                return self.send(200, p.read_text(encoding='utf-8'), 'application/json')
            if u.path == '/api/depot':
                return self.send(200, json.dumps(c.get('depot') or {'rule': depotgap.RULE}), 'application/json')
            if u.path == '/api/volume':
                return self.send(200, json.dumps(c.get('volume') or {'available': False, 'rule': volume.RULE}, default=str), 'application/json')
            if u.path == '/api/factories':
                for p in (HERE / FACTORY_BOXES, Path(c.get('vault') or HERE) / FACTORY_BOXES):
                    if p.is_file():
                        return self.send(200, p.read_text(encoding='utf-8'), 'application/json')
                return self.send(404, '{"error":"no factories_boxes.json"}', 'application/json')
            if u.path == '/api/mappoints':
                p = c['work'] / 'map_bins' / 'latest_points.json'
                if not p.is_file():
                    return self.send(200, '{"points":[],"player":null}', 'application/json')
                return self.send(200, p.read_text(encoding='utf-8'), 'application/json')
            if u.path.startswith('/assets/'):
                name = u.path[len('/assets/'):]
                p = ASSETS / name
                if '/' in name or '\\' in name or name.startswith('.') or not p.is_file():
                    return self.send(404, 'not found', 'text/plain')
                ct = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp'}.get(p.suffix.lower(), 'application/octet-stream')
                b = p.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', ct)
                self.send_header('Content-Length', str(len(b)))
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                self.wfile.write(b)
                return
            if u.path == '/favicon.ico':
                return self.send(204, b'', 'image/x-icon')
            self.send(404, 'not found', 'text/plain')
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def serve(ctx, a):
    Handler.ctx = ctx
    srv = None
    for port in range(a.port, a.port + 20):
        try:
            srv = Server(('127.0.0.1', port), Handler)
            Handler.port = port
            break
        except OSError:
            continue
    if srv is None:
        say('ERROR: no free port in %d-%d' % (a.port, a.port + 19))
        return 2
    url = 'http://127.0.0.1:%d/' % srv.server_address[1]
    say('Dashboard: ' + url)
    if not a.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    say('Press Ctrl+C or close this window to stop')
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        say('Stopped.')
    finally:
        srv.server_close()
    return 0


def selftest(a):
    f = Path(a.file) if a.file else None
    if f is None:
        cands = sorted(Path(a.saves).glob(a.pattern), key=lambda p: p.stat().st_mtime) if Path(a.saves).is_dir() else []
        if not cands:
            say('SELFTEST FAIL: no save found in %s matching %r' % (a.saves, a.pattern))
            return 1
        f = cands[-1]
    t0 = time.time()
    try:
        s = satparse.parse_save(str(f))
    except Exception as e:
        say('SELFTEST FAIL: %s raised %r' % (f.name, e))
        return 1
    ok = s.get('status') in ('ok', 'partial')
    mapinfo = 'maps=disabled'
    if MAPS_OK and ok:
        try:
            with satmap.capture(satparse) as cap:
                s2 = satparse.parse_save(str(f))
            b, pts, pl, tot = satmap.build(cap, satparse)
            nb = sum(v for k, v in tot.items() if k != 'collectibles')
            veh = sum((s2.get('vehicles') or {}).values())
            mok = nb - veh == s2.get('buildings_total') and satparse.parse_object_headers.__name__ == 'parse_object_headers' and pts
            mapinfo = 'maps=%s(%d placed, %d points)' % ('OK' if mok else 'MISMATCH', nb, len(pts))
            ok = ok and bool(mok)
        except Exception as e:
            mapinfo = 'maps=FAIL(%r)' % e
            ok = False
    sc = s.get('schematics') or {}
    try:
        dg = depotgap.compute(s)
        depinfo = ('depot=OK(%d uploaders, %d gap, %d low)' % (dg['depot_buildings'], len(dg['gap']), len(dg['low']))) if dg else 'depot=none'
    except Exception as e:
        depinfo = 'depot=FAIL(%r)' % e
        ok = False
    try:
        bp = HERE / FACTORY_BOXES
        vr = volume.compute(s, read_json(bp) if bp.is_file() else None)
        volinfo = ('volume=OK(%d items)' % vr['summary']['rows']) if vr else 'volume=none'
    except Exception as e:
        volinfo = 'volume=FAIL(%r)' % e
        ok = False
    say('SELFTEST %s: %s status=%s %.1fs buildings=%s milestones=%s max_tier=%s %s %s %s python=%s' % (
        'OK' if ok else 'FAIL', f.name, s.get('status'), time.time() - t0, s.get('buildings_total'),
        (sc.get('counts') or {}).get('milestones'), sc.get('max_tier'), mapinfo, depinfo, volinfo, sys.version.split()[0]))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description='Satisfactory save watcher + dashboard (run once).')
    ap.add_argument('--saves', default=str(DEFAULT_SAVES), help='folder with .sav files')
    ap.add_argument('--work', default=str(DEFAULT_WORK), help='work folder (snaps\\, state.json, timeline.*)')
    ap.add_argument('--vault-out', default=str(DEFAULT_VAULT_OUT), help='vault output folder (created if missing)')
    ap.add_argument('--pattern', default=DEFAULT_PATTERN)
    ap.add_argument('--port', type=int, default=DEFAULT_PORT)
    ap.add_argument('--no-open', action='store_true', help='do not open the browser')
    ap.add_argument('--no-serve', action='store_true', help='refresh files only, then exit')
    ap.add_argument('--selftest', action='store_true', help='parse one save and report OK/FAIL')
    ap.add_argument('--file', help='save file for --selftest')
    ap.add_argument('--maps', dest='maps', action='store_true', default=True, help='bin building positions of new saves (default on)')
    ap.add_argument('--no-maps', dest='maps', action='store_false', help='skip map bins')
    ap.add_argument('--reparse-latest', action='store_true', help='re-parse the newest save even if already parsed')
    ap.add_argument('--backfill-depot', action='store_true', help='re-read older saves (~1 s each) to add depot/recipe/network data')
    ap.add_argument('--backfill-maps', action='store_true', help='re-read older saves (~1 s each) to build missing map bins')
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest(a)
    ctx = refresh(a)
    if a.no_serve:
        return 1 if ctx['failed'] else 0
    return serve(ctx, a)


DASHBOARD_HTML = r'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>FICSIT Progress Monitor</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--or:#FA9549;--or2:#F5A623;--bg:#1b1c1e;--p:#2a2c30;--p2:#33363b;--ln:#45484e;--tx:#e8e6e1;--mu:#9a9ca1;--ok:#7bc96f;--bad:#e5534b}
*{box-sizing:border-box}body{margin:0;background:var(--bg) repeating-linear-gradient(0deg,transparent 0 39px,#ffffff06 39px 40px);color:var(--tx);font:14px/1.4 "Bahnschrift","Segoe UI","Arial Narrow",Arial,sans-serif}
h1,h2,h3{font-family:"Bahnschrift Condensed","Bahnschrift","Arial Narrow",sans-serif;text-transform:uppercase;letter-spacing:.06em;font-stretch:condensed;margin:0}
header{background:#111;border-bottom:3px solid var(--or)}
.haz{height:10px;background:repeating-linear-gradient(45deg,var(--or) 0 14px,#111 14px 28px)}
.hin{display:flex;align-items:center;gap:18px;padding:12px 22px;flex-wrap:wrap}
.logo{background:var(--or);color:#111;font-weight:800;padding:4px 10px;clip-path:polygon(8px 0,100% 0,100% calc(100% - 8px),calc(100% - 8px) 100%,0 100%,0 8px)}
h1{font-size:24px;color:var(--or)}.sub{color:var(--mu);font-size:12px;text-transform:uppercase;letter-spacing:.08em}
select{background:var(--p2);color:var(--tx);border:1px solid var(--or);padding:6px;font:inherit;max-width:420px}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px;padding:18px 22px}
.card{background:linear-gradient(180deg,var(--p2),var(--p));border:1px solid var(--ln);clip-path:polygon(14px 0,100% 0,100% calc(100% - 14px),calc(100% - 14px) 100%,0 100%,0 14px);padding:0 0 14px}
.card h2{font-size:16px;color:#111;background:var(--or);padding:6px 16px 6px 22px;margin-bottom:12px;position:relative}
.card h2:after{content:"";position:absolute;right:0;top:0;bottom:0;width:90px;background:repeating-linear-gradient(45deg,#111 0 8px,transparent 8px 16px);opacity:.35}
.card .b{padding:0 16px}.wide{grid-column:1/-1}
.big{font-size:30px;white-space:nowrap;font-weight:700;color:var(--or2);font-family:"Bahnschrift Condensed","Arial Narrow",sans-serif}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:10px;margin-bottom:10px}
.kv.k2{grid-template-columns:repeat(2,1fr)}.kv div{background:#0003;border-left:3px solid var(--or);padding:6px 10px}.kv small{display:block;color:var(--mu);text-transform:uppercase;font-size:11px;letter-spacing:.08em}
.bar{height:16px;background:#0006;border:1px solid var(--ln);position:relative;margin:3px 0 9px}.bar i{display:block;height:100%;background:repeating-linear-gradient(45deg,var(--or) 0 10px,var(--or2) 10px 20px)}
.bar span{position:absolute;right:6px;top:0;font-size:11px;line-height:16px}
ul{margin:4px 0 10px;padding-left:18px}li{margin:2px 0}.mu{color:var(--mu)}.ok{color:var(--ok)}.unk{color:var(--or2)}.neg{color:var(--bad)}.pos{color:var(--ok)}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:3px 6px;border-bottom:1px solid #ffffff12;text-align:left}th{color:var(--mu);text-transform:uppercase;font-size:11px}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:14px}.chart h3{font-size:13px;color:var(--or);margin:4px 0}
svg text{fill:var(--mu);font-size:10px;font-family:inherit}
.tag{display:inline-block;background:#0005;border:1px solid var(--ln);padding:0 6px;margin:2px;font-size:12px}
footer{padding:10px 22px 24px;color:var(--mu);font-size:12px;border-top:1px solid var(--ln)}
.warn{color:var(--or2)}
.mapbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0}
.btn{background:var(--p2);color:var(--or);border:1px solid var(--or);font:inherit;font-weight:700;padding:3px 10px;cursor:pointer;clip-path:polygon(5px 0,100% 0,100% calc(100% - 5px),calc(100% - 5px) 100%,0 100%,0 5px)}
.btn:hover{background:var(--or);color:#111}input[type=range]{accent-color:#FA9549;flex:1;min-width:200px}
#mapwrap{position:relative}#mapcv{width:100%;height:640px;display:block;border:1px solid var(--ln);cursor:grab;background:#0e1a1f}
#maptip{position:absolute;display:none;pointer-events:none;background:#111e;border:1px solid var(--or);padding:5px 8px;font-size:12px;max-width:340px}
td.n,th.n{text-align:right}#dep h3{margin:10px 0 4px;color:#FA9549;font-size:14px;text-transform:uppercase}.deprule{border-left:4px solid #FA9549;background:repeating-linear-gradient(135deg,#FA954922 0 8px,#0000 8px 16px);padding:6px 10px;margin-bottom:8px;font-weight:bold}
.kpis{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0}.kpi{border:1px solid var(--ln);background:#0005;padding:4px 12px;min-width:110px}.kpi b{display:block;font-size:24px}.kpi span{font-size:12px;color:#aaa}
.depnew td{background:#c0392b33}.depbadge{background:#ff5555;color:#111;font-size:11px;font-weight:bold;padding:0 4px}
#facchips .fc{cursor:pointer;border-color:#FA9549}#facchips .fc.sel{background:#FA9549;color:#111}#facchips .fc.off{opacity:.4}
#facpanel{display:none;border:1px solid #FA9549;border-left:4px solid #FA9549;background:#0006;padding:6px 10px;margin-top:6px;font-size:13px}
.caut{color:#ffcc33}
#mapxy{position:absolute;right:8px;top:8px;background:#000a;padding:2px 8px;font-size:12px;color:var(--or2)}
#mapattr{font-size:11px;color:var(--mu);margin-top:6px}
#vol .vg{display:grid;grid-template-columns:minmax(190px,1.3fr) 100px 110px 90px minmax(220px,1.6fr) minmax(220px,1.6fr)}
#vol .vh{background:var(--p2);border-bottom:2px solid var(--or);overflow-y:hidden;scrollbar-gutter:stable}#vol .vh div{padding:8px;text-transform:uppercase;letter-spacing:1px;font-size:12px;color:var(--or2)}
#vol .vs{height:560px;overflow-y:auto;scrollbar-gutter:stable}#vol .vc{padding:6px 8px;border-bottom:1px solid var(--ln);vertical-align:top;font-size:13px}
#vol .vb{background:#2a2116;border-top:2px solid var(--or2);border-bottom:1px solid var(--ln);padding:6px 10px;display:flex;flex-wrap:wrap;gap:14px;align-items:baseline}
#vol .vb b{font-weight:800;letter-spacing:2px;text-transform:uppercase;color:var(--or);font-size:14px}#vol .mono{font-family:Consolas,"Courier New",monospace;font-size:12px}
#vol .vn{font-weight:600;font-size:14px;border-bottom:1px dotted var(--or);cursor:help}#vol .pk{margin-left:6px;font-size:10px;color:#111;background:#5bc0de;padding:1px 5px}
#vol .y{color:var(--ok)}#vol .no{color:var(--mu);opacity:.6}#vol .po{color:var(--or2);font-size:12px}#vol .vd{background:#151617;border-left:3px solid var(--or)}#vol .sel{background:#202124}
#vol .fb{font:inherit;font-size:12px;letter-spacing:1px;text-transform:uppercase;padding:5px 10px;border:1px solid var(--ln);background:#202124;color:var(--tx);cursor:pointer;margin:0 6px 6px 0}#vol .fb.on{border-color:var(--or);background:var(--or);color:#111}
#vol .pcell{cursor:pointer}#vol .pcell:hover,#vol .pcell:focus{background:#ffffff0c;outline:1px dashed #5bc0de88}#vol .pcell.want{outline:2px solid #5bc0de;outline-offset:-3px;background:#5bc0de14}
.ptag{display:inline-block;font-size:10px;text-transform:uppercase;letter-spacing:1px;background:#5bc0de;color:#111;padding:0 5px;margin-top:2px}.ptag.pdone{background:var(--ok)}
.pnote{display:flex;gap:6px;margin-top:6px;align-items:center}.pnote input,.dg input,.dg textarea{background:#111;color:var(--tx);border:1px solid var(--ln);font:inherit;font-size:13px;padding:3px 6px;width:100%}
.dg{display:grid;grid-template-columns:1fr 1fr;gap:6px 12px;margin:6px 0}.dg label{font-size:11px;color:var(--mu);text-transform:uppercase}.design{border-top:1px solid var(--ln);margin-top:8px;padding-top:6px}.design h3{color:var(--or);font-size:13px}
#facchips .fc.nod{border-style:dashed;color:var(--mu)}#facchips .fc.nx{border-color:#5bc0de;color:#5bc0de}#facchips .fc.nx.sel{background:#5bc0de;color:#111}
.fbr{display:grid;grid-template-columns:150px 1fr;gap:8px;align-items:center}.fbr .bar{margin:3px 0}.fbars{margin:6px 0;max-width:520px}
#todo .md{list-style:none;padding-left:4px}#todo .mdh{color:var(--or);font-size:14px;margin:10px 0 4px}#todo p{margin:4px 0}code{background:#0006;padding:0 3px}
</style></head><body>
<header><div class="haz"></div><div class="hin"><span class="logo">FICSIT</span><div><h1>Progress Monitor</h1><div class="sub" id="sub">loading...</div></div>
<div style="margin-left:auto"><label class="sub">Snapshot&nbsp;</label><select id="pick"></select></div></div><div class="kpis" id="hdr" style="padding:0 22px 10px;margin:0"></div><div class="haz"></div></header>
<main>
<section class="card"><h2>Tier &amp; Space Elevator</h2><div class="b" id="pa"></div></section>
<section class="card"><h2>What's next</h2><div class="b" id="pb"></div></section>
<section class="card wide"><h2>Since previous save</h2><div class="b" id="pc"></div></section>
<section class="card wide"><h2>Dimensional Depot</h2><div class="b" id="dep"><p class="mu">loading...</p></div></section>
<section class="card wide"><h2>Volume items: where each is stored and moved</h2><div class="b" id="vol"><p class="mu">loading...</p></div></section>
<section class="card"><h2>Outpost at the pioneer</h2><div class="b" id="outp"><p class="mu">loading...</p></div></section>
<section class="card wide"><h2>Factory map</h2><div class="b"><div id="mapmsg"></div>
<div id="mapwrap"><div class="mapbar"><button class="btn" id="mapprev">&lt; prev</button><input type="range" id="mapsl" min="0" max="0" value="0"><button class="btn" id="mapnext">next &gt;</button><span class="sub" id="maplbl"></span></div>
<div class="mapbar" id="maplg"></div>
<div style="position:relative"><canvas id="mapcv"></canvas><div id="maptip"></div><div id="mapxy"></div>
<div style="position:absolute;left:8px;top:8px;display:flex;gap:4px"><button class="btn" id="mapzi">+</button><button class="btn" id="mapzo">&minus;</button><button class="btn" id="mapfit">fit</button></div></div>
<div class="mapbar" id="facchips"></div><div id="facpanel"></div><div class="mapbar" id="mapsum"></div><div class="mu" style="font-size:12px">Zoomed out: heatmap. Zoomed in: 50 m cells sized by count; production/extraction dots for the latest save. White squares (zoomed in) = more buildings than the previous binned snapshot.<span id="facnote"></span></div></div>
<div id="mapattr"></div></div></section>
<section class="card wide"><h2>Progress over time</h2><div class="b charts" id="pd"></div></section>
<section class="card"><h2>Milestone history</h2><div class="b" id="hist" style="max-height:520px;overflow-y:auto"><p class="mu">loading...</p></div></section>
<section class="card"><h2>Factory census</h2><div class="b" id="cens"><p class="mu">loading...</p></div></section>
<section class="card"><h2>Inventory &amp; collectibles</h2><div class="b" id="inv"><p class="mu">loading...</p></div></section>
<section class="card wide" id="todocard" style="display:none"><h2>To-do (from the vault)</h2><div class="b" id="todo"></div></section>
<section class="card wide"><h2>What the parser couldn't read</h2><div class="b" id="pnotes"><p class="mu">loading...</p></div></section>
</main>
<footer><div id="ft"></div><div id="ftattr" style="margin-top:6px">Satisfactory is (c) Coffee Stain Studios; unofficial fan tool, not endorsed. Map image: a plain placeholder unless you add your own (see the README). All save data stays on this PC.</div></footer>
<script>
"use strict";
var D=null,CUR=null;
function $(i){return document.getElementById(i);}
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];});}
function num(v){return (typeof v==="number")?v.toLocaleString("en-US"):"?";}
function pnum(s){var m=/(\d+)$/.exec(String(s||""));return m?parseInt(m[1],10):null;}
function hrs(s){return typeof s==="number"?(s/3600).toFixed(1)+" h":"?";}
function baseMs(sn){var o={},l=((sn.schematics||{}).milestones)||[];l.forEach(function(n){var m=/^Schematic_(\d+)-(\d+)_C$/.exec(n);if(m){o[m[1]]=(o[m[1]]||0)+1;}});return o;}
function bar(lbl,have,need){var p=need?Math.min(100,100*have/need):0;return '<div>'+esc(lbl)+'</div><div class="bar"><i style="width:'+p.toFixed(1)+'%"></i><span>'+num(have)+' / '+num(need)+'</span></div>';}
function panelA(sn){
  var h=sn.header||{},sc=sn.schematics||{},el=sn.space_elevator||{},g=D.game;
  var cur=pnum(el.CurrentGamePhase),tgt=pnum(el.TargetGamePhase),avail=cur==null?null:g.tiers_by_phase[String(cur)];
  var x='<div class="kv k2"><div><small>Max tier</small><span class="big">'+(sc.max_tier!=null?sc.max_tier:"?")+'</span></div>'+
   '<div><small>Tiers available</small><span class="big">'+(avail!=null?avail:"?")+'</span></div>'+
   '<div><small>Elevator phase</small><span class="big">'+(cur!=null?cur:"?")+'</span><small>of 5 delivered</small></div>'+
   '<div><small>Play time</small><span class="big">'+hrs(h.play_seconds)+'</span></div></div>';
  x+='<div class="kv"><div><small>Buildings</small>'+num(sn.buildings_total)+'</div><div><small>Milestones</small>'+num((sc.counts||{}).milestones)+'</div><div><small>Schematics</small>'+num(sc.total)+'</div><div><small>Status</small>'+esc(sn.status)+'</div></div>';
  var mt=sc.milestones_by_tier||{};
  if(Object.keys(mt).length){x+='<h3>Milestones by tier (save)</h3><div>';Object.keys(mt).sort(function(a,b){return a-b;}).forEach(function(t){x+='<span class="tag">T'+esc(t)+': '+esc(mt[t])+'</span>';});x+='</div>';}
  if(tgt!=null&&g.elevator[String(tgt)]){var e=g.elevator[String(tgt)],paid={};
    (el.paid_toward_target||[]).forEach(function(p){paid[g.part_names[p.item]||p.item]=p.amount||0;});
    x+='<h3 style="margin-top:10px">Paid toward phase '+tgt+' &mdash; '+esc(e.name)+'</h3>';
    e.cost.forEach(function(c){x+=bar(c[0],paid[c[0]]||0,c[1]);});
    Object.keys(paid).forEach(function(k){if(!e.cost.some(function(c){return c[0]===k;}))x+='<div class="unk">unknown item: '+esc(k)+' = '+num(paid[k])+'</div>';});
  } else if(cur===5){x+='<p class="ok">All five Project Assembly phases delivered.</p>';}
  else x+='<p class="mu">Elevator data not available in this snapshot.</p>';
  $("pa").innerHTML=x;
}
function panelB(sn){
  var sc=sn.schematics||{},el=sn.space_elevator||{},g=D.game,x="";
  if(!sn.schematics){$("pb").innerHTML='<p class="mu">No schematic data in this snapshot (status '+esc(sn.status)+').</p>';return;}
  var cur=pnum(el.CurrentGamePhase),avail=cur==null?(sc.max_tier||0):g.tiers_by_phase[String(cur)],any=false,have={};
  (sc.milestones||[]).forEach(function(n){have[n]=1;});
  for(var t=1;t<=avail;t++){var ms=(g.milestone_ids||{})[String(t)]||[],miss=ms.filter(function(m){return !have[m[0]];});
    if(miss.length){any=true;x+='<h3>Tier '+t+' &mdash; '+(ms.length-miss.length)+' of '+ms.length+' milestones done</h3><ul>';
      ms.forEach(function(m){x+=have[m[0]]?'<li class="mu">&#10003; '+esc(m[1])+'</li>':'<li><span class="unk">not yet</span> '+esc(m[1])+'</li>';});
      x+='</ul>';}}
  if(!any)x+='<p class="ok">Every milestone in tiers 1-'+avail+' is complete.</p>';
  var tgt=pnum(el.TargetGamePhase);
  if(tgt!=null&&g.elevator[String(tgt)]){var e=g.elevator[String(tgt)],paid={};(el.paid_toward_target||[]).forEach(function(p){paid[g.part_names[p.item]||p.item]=p.amount||0;});
    x+='<h3>Space Elevator phase '+tgt+': '+esc(e.name)+'</h3><table><tr><th>Part</th><th>Need</th><th>Paid</th><th>Remaining</th></tr>';
    e.cost.forEach(function(c){var p=paid[c[0]]||0;x+='<tr><td>'+esc(c[0])+'</td><td>'+num(c[1])+'</td><td>'+num(p)+'</td><td>'+num(Math.max(0,c[1]-p))+'</td></tr>';});
    x+='</table><p class="mu">Unlocks: '+esc(e.unlocks)+'. Default cost multiplier assumed.</p>';}
  var nt=(avail||0)+1;if(g.milestones[String(nt)]&&nt<=9)x+='<h3>Coming in tier '+nt+'</h3><div>'+g.milestones[String(nt)].map(function(n){return '<span class="tag">'+esc(n)+'</span>';}).join("")+'</div>';
  $("pb").innerHTML=x;
}
function panelC(name){
  var d=null;(D.diffs||[]).forEach(function(z){if(z.file===name)d=z;});
  if(!d){$("pc").innerHTML='<p class="mu">No earlier snapshot to compare with.</p>';return;}
  var x='<p class="mu">'+esc(d.prev_file)+' &rarr; '+esc(d.file)+'</p><div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px"><div><h3>New schematics</h3>';
  if(d.new_schematics&&d.new_schematics.length){d.new_schematics.forEach(function(o){Object.keys(o).forEach(function(k){x+='<div><b>'+esc(k)+'</b> ('+o[k].length+')</div><div>'+o[k].map(function(n){return '<span class="tag">'+esc(n)+'</span>';}).join("")+'</div>';});});}else x+='<p class="mu">none</p>';
  x+='</div><div><h3>Building changes</h3>';var bd=d.building_deltas||{},ks=Object.keys(bd);
  if(ks.length){x+='<table>';ks.forEach(function(k){var v=bd[k];x+='<tr><td>'+esc(k)+'</td><td class="'+(v>0?"pos":"neg")+'">'+(v>0?"+":"")+v+'</td></tr>';});x+='</table>';}else x+='<p class="mu">none</p>';
  x+='</div><div><h3>Other</h3>';
  if(d.elevator){x+='<div>Elevator: '+esc((d.elevator.from||{}).CurrentGamePhase||"?")+' &rarr; '+esc((d.elevator.to||{}).CurrentGamePhase||"?")+'</div>';}
  x+='<div>Session changed: '+(d.session_changed?'<span class="warn">yes</span>':'no')+'</div></div></div>';
  $("pc").innerHTML=x;
}
function chart(title,rows,fy,step){
  var W=440,H=170,L=44,R=8,T=8,B=24,pts=[];
  rows.forEach(function(r){var t=Date.parse(r.save_datetime||"");var y=fy(r);if(!isNaN(t)&&typeof y==="number"&&isFinite(y))pts.push([t,y]);});
  var s='<div class="chart"><h3>'+esc(title)+'</h3>';
  if(pts.length<1)return s+'<p class="mu">no data</p></div>';
  var x0=pts[0][0],x1=pts[pts.length-1][0],y0=0,y1=Math.max.apply(null,pts.map(function(p){return p[1];}));
  if(x1===x0)x1=x0+1;if(y1===y0)y1=y0+1;
  function X(t){return L+(W-L-R)*(t-x0)/(x1-x0);}function Y(v){return T+(H-T-B)*(1-(v-y0)/(y1-y0));}
  var g='<svg viewBox="0 0 '+W+' '+H+'" width="100%" role="img">';
  for(var i=0;i<=4;i++){var v=y0+(y1-y0)*i/4;g+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+Y(v)+'" y2="'+Y(v)+'" stroke="#ffffff14"/><text x="'+(L-4)+'" y="'+(Y(v)+3)+'" text-anchor="end">'+(Math.round(v*10)/10)+'</text>';}
  [x0,(x0+x1)/2,x1].forEach(function(t,j){g+='<text x="'+X(t)+'" y="'+(H-6)+'" text-anchor="'+["start","middle","end"][j]+'">'+new Date(t).toISOString().slice(0,10)+'</text>';});
  var d="";pts.forEach(function(p,k){var px=X(p[0]).toFixed(1),py=Y(p[1]).toFixed(1);d+=(k?(step?"H"+px+"V"+py:"L"+px+","+py):"M"+px+","+py);});
  g+='<path d="'+d+'" fill="none" stroke="#FA9549" stroke-width="2"/>';
  var lp=pts[pts.length-1];g+='<circle cx="'+X(lp[0])+'" cy="'+Y(lp[1])+'" r="3" fill="#F5A623"/></svg>';
  return s+g+'</div>';
}
function sumObj(o){if(!o)return null;var t=0;Object.keys(o).forEach(function(k){t+=+o[k]||0;});return t;}
function panelD(){
  var R=(D.timeline||[]).filter(function(r){return !r.standin;}),A=D.timeline||[];
  var gens=["Coal Generator","Fuel Generator","Geothermal Generator","Nuclear Power Plant"],ex=["Miner Mk1","Miner Mk2","Miner Mk3","Oil Extractor","Water Extractor","Resource Well Extractor"];
  function s(r,ks){var t=0,any=false;ks.forEach(function(k){if(typeof r[k]==="number"){t+=r[k];any=true;}});return any?t:null;}
  $("pd").innerHTML=chart("Play time (h) vs save date",A,function(r){return r.play_hours;})+
    chart("Buildings total",R,function(r){return r.buildings_total;})+
    chart("Power generators (coal+fuel+geo+nuclear)",R,function(r){return s(r,gens);})+
    chart("Extractors (miners, oil, water, wells)",R,function(r){return s(r,ex);})+
    chart("Milestones",R,function(r){return r.milestones;},true)+
    chart("Schematics total",R,function(r){return r.schematics_total;})+
    chart("Max tier",R,function(r){return r.max_tier;},true)+
    chart("Elevator phase",R,function(r){return pnum(r.elevator_phase);},true);
}
function show(sn){
  CUR=sn;var h=sn.header||{};
  $("sub").textContent=(h.session_name||"?")+" | "+(sn.file||"?")+" | saved "+(sn.file_mtime||h.save_datetime_utc_guess||"?");
  try{panelA(sn);}catch(e){$("pa").innerHTML='<p class="neg">error: '+esc(e)+'</p>';}
  try{panelB(sn);}catch(e){$("pb").innerHTML='<p class="neg">error: '+esc(e)+'</p>';}
  try{panelC(sn.file);}catch(e){$("pc").innerHTML='<p class="neg">error: '+esc(e)+'</p>';}
  var r=D.run||{};
  $("ft").innerHTML='parser_version '+esc(sn.parser_version||"?")+' (running satparse '+esc(r.parser_version)+', satwatch '+esc(r.satwatch)+') &middot; generated '+esc(r.generated)+' &middot; new saves this run: '+esc(r.new)+(r.warnings&&r.warnings.length?' &middot; <span class="warn">'+r.warnings.length+' warning(s): '+esc(r.warnings.slice(0,3).join(" | "))+'</span>':'');
}
function load(name){
  fetch("/api/snapshot?name="+encodeURIComponent(name)).then(function(r){if(!r.ok)throw new Error("HTTP "+r.status);return r.json();}).then(show).catch(function(e){$("pa").innerHTML='<p class="neg">could not load '+esc(name)+': '+esc(e)+'</p>';});
}
/* ---------------- plan (plan.json in the work folder; edited only here) ---------------- */
var P={tok:"__PLAN_TOKEN__",d:null,pick:false};
function pl(){return (P.d&&P.d.plan)||{factories:{},transport:{}};}
function pFac(k){return pl().factories[k]||{};}
function pUrlOk(u){return /^https?:\/\/[^\s]+$/i.test(u||"");}
function fName(f){return pFac(f.label).name||f.name;}
function planLoad(){return fetch("/api/plan").then(function(r){return r.json();}).then(function(d){P.d=d;planRefresh();}).catch(function(e){console.log("plan load failed",e);});}
function planPost(path,body){return fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-Plan-Token":P.tok},body:JSON.stringify(body)}).then(function(r){return r.json().then(function(j){if(!r.ok)throw new Error(j.error||("HTTP "+r.status));P.d=j;planRefresh();return j;});});}
function planRefresh(){if(V.d)volDraw();if(M.fac){facChips();if(M.cv)mapDraw();}}
function planWant(cls,n){return (pl().transport[cls]||{})[n]==="want";}
function planExists(cls,n){return !!((P.d&&P.d.exists||{})[cls]||{})[n];}
function planCell(r,n,inner){var c=r["class"]||r.item,w=planWant(c,n),ex=planExists(c,n);
  return '<div class="vc pcell'+(w?' want':'')+'" tabindex="0" role="button" aria-pressed="'+w+'" data-pc="'+esc(c)+'" data-pn="'+n+'" title="click: '+(w?'clear the plan':'plan '+esc(r.item)+' on '+n)+'">'+inner+
   (w?'<div><span class="ptag'+(ex?' pdone':'')+'">'+(ex?'planned &#10003; now exists':'planned')+'</span></div>':'')+'</div>';}
function planAnyWant(r){var t=pl().transport[r["class"]||r.item]||{};return t.depot==="want"||t.drone==="want"||t.train==="want";}
function designHtml(key,f){var p=pFac(key),u=p.design_url||"",L=(P.d&&P.d.limits)||{};
  var h='<div class="design"><h3>Design'+(key==="next"?' &mdash; next factory':'')+'</h3>';
  h+=(pUrlOk(u)?'<a class="btn" id="dzopen" href="'+esc(u)+'" target="_blank" rel="noopener noreferrer">Open design &#128279;</a> ':'<span class="mu">no design link yet</span> ');
  h+='<div class="dg"><label>Name<input id="dzname" maxlength="'+(L.name||120)+'" value="'+esc(p.name||"")+'" placeholder="'+esc(f?f.name:"Next factory")+'"></label>'+
   '<label>Design URL<input id="dzurl" type="url" maxlength="'+(L.design_url||32768)+'" value="'+esc(u)+'" placeholder="https://satisfactory-calculator.com/... or any http(s) link"></label>'+
   '<label>Purpose<input id="dzpur" maxlength="'+(L.purpose||500)+'" value="'+esc(p.purpose||"")+'"></label>'+
   '<label>Notes<textarea id="dznotes" rows="3" maxlength="'+(L.notes||4000)+'">'+esc(p.notes||"")+'</textarea></label></div>';
  h+='<button class="btn" id="dzsave">Save design</button> <span id="dzmsg" class="mu"></span></div>';return h;}
function designWire(key,after){$("dzsave").onclick=function(){var u=$("dzurl").value.trim();
    if(u&&!pUrlOk(u)){$("dzmsg").innerHTML='<span class="neg">link must start with http:// or https://</span>';return;}
    $("dzmsg").textContent="saving...";
    planPost("/api/plan/factory",{key:key,name:$("dzname").value,design_url:u,purpose:$("dzpur").value,notes:$("dznotes").value}).then(function(j){if(after)after();$("dzmsg").innerHTML='<span class="ok">saved'+(j.changed?'':' (no change)')+'</span>';}).catch(function(e){$("dzmsg").innerHTML='<span class="neg">'+esc(e.message)+'</span>';});};}
function nextPos(){var p=pFac("next").pos;if(p)return p;var d=P.d&&P.d.next_default_pos;return (d&&d[0]!=null)?d:null;}
function nextSelect(){M.facSel="next";var p=$("facpanel"),pos=nextPos();
  p.innerHTML='<b style="color:#5bc0de">&#9670; Next factory</b> <span class="mu">'+(pos?'at '+Math.round(pos[0]/100)+', '+Math.round(pos[1]/100)+' m'+(pFac("next").pos?'':' (default: pioneer position)'):'no position')+'</span>'+designHtml("next",null)+
   '<div style="margin-top:4px"><button class="btn" id="nxpick">'+(P.pick?'click the map...':'Set position on map')+'</button> <button class="btn" id="nxreset">Use pioneer position</button> <button class="btn" id="nxfit">Show on map</button> <button class="btn" id="facclose">close</button></div>';
  p.style.display="block";designWire("next",nextSelect);
  $("nxpick").onclick=function(){P.pick=!P.pick;nextSelect();};
  $("nxreset").onclick=function(){planPost("/api/plan/factory",{key:"next",pos:null}).then(nextSelect).catch(function(e){alert(e.message);});};
  $("nxfit").onclick=function(){var q=nextPos();if(!q||!M.cv)return;q=w2i(q[0],q[1]);M.s=Math.max(M.s,0.6);M.ox=M.cv.width/2-q[0]*M.s;M.oy=M.cv.height/2-q[1]*M.s;mapDraw();};
  $("facclose").onclick=function(){M.facSel=null;P.pick=false;p.style.display="none";facChips();mapDraw();};
  facChips();mapDraw();}
function nextDraw(){var q=nextPos();if(!q||!M.meta)return;var x=M.ctx,a=w2i(q[0],q[1]),sx=M.ox+a[0]*M.s,sy=M.oy+a[1]*M.s,z=M.facSel==="next"?11:8;
  x.save();x.fillStyle="#5bc0de";x.strokeStyle="#111";x.lineWidth=2;x.beginPath();x.moveTo(sx,sy-z);x.lineTo(sx+z,sy);x.lineTo(sx,sy+z);x.lineTo(sx-z,sy);x.closePath();x.fill();x.stroke();
  x.font="bold 12px Bahnschrift,Arial";var t="NEXT"+(pFac("next").name?": "+pFac("next").name:"");if(t.length>34)t=t.slice(0,32)+"..";x.lineWidth=3;x.strokeText(t,sx+z+3,sy+4);x.fillText(t,sx+z+3,sy+4);x.restore();}
/* ---------------- building bars on the factory card ---------------- */
var BN={SmelterMk1:"Smelter",FoundryMk1:"Foundry",ConstructorMk1:"Constructor",AssemblerMk1:"Assembler",ManufacturerMk1:"Manufacturer",OilRefinery:"Refinery",Blender:"Blender",Packager:"Packager",HadronCollider:"Particle Accelerator",QuantumEncoder:"Quantum Encoder",Converter:"Converter"};
function facBars(f){var o=f.buildings||{},ex={};Object.keys(f.extractors||{}).forEach(function(k){var t=k.split(":")[0].replace(/Mk(\d)/," Mk$1").replace("FrackingExtractor","Resource Well").replace("WaterPump","Water Extractor").replace("OilPump","Oil Extractor");ex[t]=(ex[t]||0)+f.extractors[k];});
  var L=Object.keys(o).map(function(k){return [BN[k]||k,o[k]];}).concat(Object.keys(ex).map(function(k){return [k,ex[k]];})).filter(function(a){return a[1]>0;}).sort(function(a,b){return b[1]-a[1];});
  if(!L.length)return "";var mx=L[0][1];
  return '<div class="fbars">'+L.map(function(a){return '<div class="fbr"><span>'+esc(a[0])+'</span><div class="bar"><i style="width:'+(100*a[1]/mx).toFixed(1)+'%"></i><span>'+num(a[1])+'</span></div></div>';}).join("")+'</div>';}
/* ---------------- insights panels ---------------- */
function mdInline(s){s=esc(s);return s.replace(/`([^`]+)`/g,"<code>$1</code>").replace(/\*\*([^*]+)\*\*/g,"<b>$1</b>").replace(/(^|[^*])\*([^*\s][^*]*)\*/g,"$1<i>$2</i>").replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g,function(m,a,b){return '<span class="tag">'+(b||a)+'</span>';});}
function mdRender(t){var L=String(t||"").replace(/\r/g,"").split("\n"),h="",inl=false,i=0;
  function close(){if(inl){h+="</ul>";inl=false;}}
  if(L[0]==="---"){for(i=1;i<L.length&&L[i]!=="---";i++);i++;}
  for(;i<L.length;i++){var l=L[i],m;
    if(/^\s*\|.*\|\s*$/.test(l)){close();var rows=[];while(i<L.length&&/^\s*\|.*\|\s*$/.test(L[i])){rows.push(L[i]);i++;}i--;
      h+="<table>";rows.forEach(function(r,j){if(/^\s*\|[\s:\-|]+\|\s*$/.test(r))return;var c=r.trim().slice(1,-1).split("|");h+="<tr>"+c.map(function(x){return (j===0?"<th>":"<td>")+mdInline(x.trim())+(j===0?"</th>":"</td>");}).join("")+"</tr>";});h+="</table>";continue;}
    if((m=/^(#{1,6})\s+(.*)$/.exec(l))){close();var n=Math.min(5,m[1].length+2);h+="<h"+n+' class="mdh">'+mdInline(m[2])+"</h"+n+">";continue;}
    if((m=/^(\s*)[-*+]\s+\[( |x|X)\]\s+(.*)$/.exec(l))){if(!inl){h+='<ul class="md">';inl=true;}h+='<li class="ck" style="margin-left:'+(m[1].length*6)+'px"><input type="checkbox" disabled'+(m[2]===" "?"":" checked")+'> '+(m[2]===" "?mdInline(m[3]):"<s>"+mdInline(m[3])+"</s>")+"</li>";continue;}
    if((m=/^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$/.exec(l))){if(!inl){h+='<ul class="md">';inl=true;}h+='<li style="margin-left:'+(m[1].length*6)+'px">'+mdInline(m[2])+"</li>";continue;}
    close();if(l.trim())h+="<p>"+mdInline(l)+"</p>";}
  close();return h;}
function hdrDraw(H){if(!H)return;var ls=(H.last_save_local||H.last_save_utc||"").replace("T"," ").slice(0,16);
  $("hdr").innerHTML=[["Play time",H.play_hours!=null?H.play_hours.toFixed(1)+" h":"?"],["Last save",ls||"?"],["Started",(H.started||"?").slice(0,10)],["Buildings",num(H.buildings)],["Saves parsed",H.saves_ok+" ok / "+H.saves_total]].map(function(k){return '<div class="kpi"><span>'+k[0]+'</span><b>'+esc(k[1])+'</b></div>';}).join("");}
function insDraw(d){
  try{hdrDraw(d.header);}catch(e){}
  var hs=d.history||[];$("hist").innerHTML=hs.length?'<table>'+hs.map(function(e){return '<tr><td class="mono" style="white-space:nowrap">'+esc(e.date)+'</td><td class="n mu">'+(e.hours!=null?(+e.hours).toFixed(1)+" h":"")+'</td><td'+(e.major?' style="color:var(--or2);font-weight:700"':'')+'>'+esc(e.event)+'</td></tr>';}).join("")+'</table><p class="mu" style="font-size:12px">First save in which each change shows up, from the timeline (not the in-game date of the unlock).</p>':'<p class="mu">no timeline yet</p>';
  var c=d.census;if(c){var mx=Math.max.apply(null,[1].concat(c.categories.map(function(k){return k.total;})));
    $("cens").innerHTML=c.categories.filter(function(k){return k.total;}).map(function(k){return '<div class="fbr"><span title="'+esc(k.items.map(function(a){return a[0]+" "+a[1];}).join(", "))+'">'+esc(k.category)+'</span><div class="bar"><i style="width:'+(100*k.total/mx).toFixed(1)+'%"></i><span>'+num(k.total)+'</span></div></div><div class="mu" style="font-size:11px;margin:-6px 0 6px">'+k.items.slice(0,6).map(function(a){return esc(a[0])+" "+num(a[1]);}).join(" &middot; ")+'</div>';}).join("")+
      '<p class="mu" style="font-size:12px">'+num(c.itemized)+' itemized of '+num(c.buildings_total)+' buildings (the rest: foundations, walls, belts, pipes, power poles and other structure). Vehicles: '+Object.keys(c.vehicles).map(function(k){return esc(k)+" "+num(c.vehicles[k]);}).join(", ")+'.</p>';}
  else $("cens").innerHTML='<p class="mu">not available</p>';
  var v=d.inventory;if(v){var h='<div class="kpis">'+v.special_now.map(function(a){return '<div class="kpi"><span>'+esc(a[0])+' carried</span><b>'+num(a[1])+'</b></div>';}).join("")+
      '<div class="kpi"><span>Drop pods opened</span><b>'+(v.drop_pods.opened!=null?num(v.drop_pods.opened)+(v.drop_pods.total?" / "+num(v.drop_pods.total):""):"?")+'</b></div></div>';
    if(v.peaks.length)h+='<div class="mu" style="font-size:12px">Most ever carried: '+v.peaks.map(function(a){return esc(a[0])+" "+num(a[1])+(a[2]?" ("+esc(a[2])+")":"");}).join(" &middot; ")+'</div>';
    if(v.left_in_world.length)h+='<div style="font-size:12px">Still in the world: '+v.left_in_world.map(function(a){return esc(a[0])+" "+num(a[1]);}).join(" &middot; ")+'</div>';
    h+='<h3 style="margin-top:8px">Player inventory highlights</h3><div>'+(v.top_items.length?v.top_items.map(function(a){return '<span class="tag">'+esc(a[0])+' '+num(a[1])+'</span>';}).join(""):'<span class="mu">empty or not read</span>')+'</div><p class="mu" style="font-size:12px">'+num(v.units)+' units in '+num(v.stacks)+' item types across '+v.players+' player(s); storage containers are not counted.</p>';
    $("inv").innerHTML=h;} else $("inv").innerHTML='<p class="mu">not available</p>';
  var n=d.notes;if(n){$("pnotes").innerHTML='<h3>From this run</h3>'+(n.auto.length?'<ul>'+n.auto.map(function(s){return '<li class="caut">'+esc(s)+'</li>';}).join("")+'</ul>':'<p class="ok">no warnings</p>')+'<h3>Always true</h3><ul>'+n.caveats.map(function(s){return '<li class="mu">'+esc(s)+'</li>';}).join("")+'</ul>';}
  var t=d.todo;if(t&&t.text){$("todocard").style.display="";$("todo").innerHTML='<div class="mu" style="font-size:12px">read-only from '+esc(t.path)+' &middot; edit it in Obsidian</div>'+mdRender(t.text);}else $("todocard").style.display="none";}
function insLoad(){fetch("/api/insights").then(function(r){return r.json();}).then(insDraw).catch(function(e){$("pnotes").innerHTML='<p class="neg">insights failed: '+esc(e)+'</p>';});}
/* ---------------- map panel ---------------- */
var M={fac:null,facOn:true,staOn:true,facHide:{},facSel:null,facHov:null,down:null,meta:null,img:null,imgOk:false,i:-1,bins:null,prev:null,pts:null,on:{},showNew:true,s:0.1,ox:0,oy:0,cv:null,ctx:null,mx:-1,my:-1,drag:null};
function mW(){var b=M.meta.bounds_cm;return [b.x1-b.x0,b.y1-b.y0];}
function w2i(x,y){var b=M.meta.bounds_cm,P=M.meta.image_px;return [(x-b.x0)/(b.x1-b.x0)*P,(y-b.y0)/(b.y1-b.y0)*P];}
function i2w(u,v){var b=M.meta.bounds_cm,P=M.meta.image_px;return [b.x0+u/P*(b.x1-b.x0),b.y0+v/P*(b.y1-b.y0)];}
function binPx(){return M.meta.bin_cm/mW()[0]*M.meta.image_px;}
function mapMsg(t){$("mapmsg").innerHTML=t;}
function mapFit(){var c=M.cv,P=M.meta.image_px;M.s=Math.min(c.width,c.height)/P;M.ox=(c.width-P*M.s)/2;M.oy=(c.height-P*M.s)/2;mapDraw();}
function mapZoom(f,cx,cy){var c=M.cv;if(cx==null){cx=c.width/2;cy=c.height/2;}var ns=Math.max(0.05,Math.min(8,M.s*f));f=ns/M.s;M.ox=cx-(cx-M.ox)*f;M.oy=cy-(cy-M.oy)*f;M.s=ns;mapDraw();}
function mapCatsMax(b){var mx=1;Object.keys(b||{}).forEach(function(k){if(!M.on[k])return;var o=b[k];Object.keys(o).forEach(function(g){if(o[g]>mx)mx=o[g];});});return mx;}
function mapDraw(){
  var c=M.cv,x=M.ctx;if(!c)return;x.setTransform(1,0,0,1,0,0);x.fillStyle="#0e1a1f";x.fillRect(0,0,c.width,c.height);
  var P=M.meta.image_px;
  if(M.imgOk){x.imageSmoothingEnabled=M.s<1;x.drawImage(M.img,M.ox,M.oy,P*M.s,P*M.s);}
  else{x.strokeStyle="#45484e";x.strokeRect(M.ox,M.oy,P*M.s,P*M.s);}
  x.fillStyle="rgba(0,0,0,0.25)";x.fillRect(0,0,c.width,c.height);
  var B=M.bins,bp=binPx(),bs=bp*M.s,cols=M.meta.colors,o=M.meta.bins_origin||[0,0];
  if(B&&B.bins){
    var bins=B.bins,mx=mapCatsMax(bins),heat=bs<7;
    x.save();if(heat)x.globalCompositeOperation="lighter";
    M.meta.categories.forEach(function(k){if(!M.on[k]||!bins[k])return;var col=cols[k],o2=bins[k];
      Object.keys(o2).forEach(function(g){var a=g.split(","),gx=+a[0],gy=+a[1],n=o2[g];
        var sx=M.ox+(gx+0.5)*bs,sy=M.oy+(gy+0.5)*bs;if(sx<-60||sy<-60||sx>c.width+60||sy>c.height+60)return;
        var t=Math.log(1+n)/Math.log(1+mx);
        if(heat){var r=Math.max(3,bs*1.2+3*t);var gr=x.createRadialGradient(sx,sy,0,sx,sy,r);gr.addColorStop(0,hexA(col,0.06+0.34*t));gr.addColorStop(1,hexA(col,0));x.fillStyle=gr;x.fillRect(sx-r,sy-r,2*r,2*r);}
        else{var w=Math.max(2,bs*(0.3+0.7*Math.sqrt(t)));x.fillStyle=hexA(col,0.55);x.fillRect(sx-w/2,sy-w/2,w,w);x.strokeStyle=hexA(col,0.95);x.lineWidth=1;x.strokeRect(sx-w/2,sy-w/2,w,w);}
      });});
    x.restore();
    if(M.showNew&&!heat&&M.prev&&M.prev.bins){x.save();x.strokeStyle="#ffffff";x.lineWidth=Math.max(1,Math.min(2,bs/10));
      M.meta.categories.forEach(function(k){if(!M.on[k]||!bins[k]||k==="collectibles")return;var pv=M.prev.bins[k]||{};
        Object.keys(bins[k]).forEach(function(g){if(bins[k][g]>(pv[g]||0)){var a=g.split(","),sx=M.ox+(+a[0])*bs,sy=M.oy+(+a[1])*bs;var w=Math.max(4,bs);x.strokeRect(sx+(bs-w)/2,sy+(bs-w)/2,w,w);}});});x.restore();}
    if(M.pts&&isLatest()&&bs>=18){(M.pts.points||[]).forEach(function(p){if(!M.on[p[4]])return;var q=w2i(p[0],p[1]),sx=M.ox+q[0]*M.s,sy=M.oy+q[1]*M.s;
      if(sx<-5||sy<-5||sx>c.width+5||sy>c.height+5)return;x.fillStyle=cols[p[4]];x.beginPath();x.arc(sx,sy,3,0,7);x.fill();x.strokeStyle="#111";x.stroke();});}
    facDraw();nextDraw();
    var pl=B.player;if(pl){var q=w2i(pl[0],pl[1]),sx=M.ox+q[0]*M.s,sy=M.oy+q[1]*M.s;x.fillStyle="#fff";x.strokeStyle="#FA9549";x.lineWidth=2;x.beginPath();x.moveTo(sx,sy-9);x.lineTo(sx+7,sy+6);x.lineTo(sx-7,sy+6);x.closePath();x.fill();x.stroke();x.font="bold 12px Bahnschrift,Arial";x.fillStyle="#fff";x.fillText("PIONEER",sx+9,sy+4);}
  }
  // scale bar
  var mpp=mW()[0]/100/P/M.s,target=mpp*110,nice=[1,2,5,10,20,50,100,200,250,500,1000,2000,5000],m=nice[0];nice.forEach(function(n){if(n<=target)m=n;});
  var L=m/mpp;x.fillStyle="rgba(0,0,0,0.6)";x.fillRect(10,c.height-34,L+70,24);x.fillStyle="#FA9549";x.fillRect(16,c.height-24,L,5);x.fillStyle="#e8e6e1";x.font="12px Bahnschrift,Arial";x.fillText(m>=1000?(m/1000)+" km":m+" m",22+L,c.height-17);
  mapHover();
}
function hexA(h,a){var n=parseInt(h.slice(1),16);return "rgba("+(n>>16&255)+","+(n>>8&255)+","+(n&255)+","+a.toFixed(3)+")";}
function isLatest(){var s=M.meta.snapshots[M.i];return !!(s&&M.pts&&M.pts.file===s.file);}
function mapHover(){
  var tip=$("maptip"),rd=$("mapxy");if(M.mx<0){tip.style.display="none";return;}
  var u=(M.mx-M.ox)/M.s,v=(M.my-M.oy)/M.s,w=i2w(u,v);rd.textContent="X "+Math.round(w[0]/100)+" m, Y "+Math.round(w[1]/100)+" m";
  var fh=facAt(M.mx,M.my);if(fh!==M.facHov){M.facHov=fh;mapDraw();return;}
  if(fh){tip.innerHTML=facHtml(fh,true);tip.style.display="block";tip.style.left=Math.max(0,Math.min(M.mx+14,M.cv.clientWidth-350))+"px";tip.style.top=(M.my+14)+"px";return;}
  var B=M.bins;if(!B||!B.bins){tip.style.display="none";return;}
  var bp=binPx(),gx=Math.floor(u/bp),gy=Math.floor(v/bp),g=gx+","+gy,lines=[];
  if(M.pts&&isLatest()&&bp*M.s>=18){var best=null,bd=81;(M.pts.points||[]).forEach(function(p){if(!M.on[p[4]])return;var q=w2i(p[0],p[1]),dx=M.ox+q[0]*M.s-M.mx,dy=M.oy+q[1]*M.s-M.my,d=dx*dx+dy*dy;if(d<bd){bd=d;best=p;}});
    if(best)lines.push("<b>"+esc(best[3])+"</b> ("+esc(best[4])+")<br>height "+Math.round(best[2]/100)+" m");}
  M.meta.categories.forEach(function(k){var n=(B.bins[k]||{})[g];if(n&&M.on[k]){var pv=M.prev&&M.prev.bins?((M.prev.bins[k]||{})[g]||0):null;lines.push('<span style="color:'+M.meta.colors[k]+'">&#9632;</span> '+esc(k.replace("_"," / "))+": "+n+(pv!=null&&n>pv?' <span class="pos">(+'+(n-pv)+')</span>':""));}});
  if(!lines.length){tip.style.display="none";return;}
  tip.innerHTML='<div class="mu">50 m cell '+esc(g)+'</div>'+lines.join("<br>");tip.style.display="block";
  tip.style.left=Math.min(M.mx+14,M.cv.clientWidth-200)+"px";tip.style.top=(M.my+14)+"px";
}
function mapLoad(i){
  var L=M.meta.snapshots;if(i<0||i>=L.length)return;M.i=i;var s=L[i];$("mapsl").value=i;
  $("maplbl").textContent=(s.play_hours!=null?(+s.play_hours).toFixed(1)+" h":"?")+"  |  "+s.file+"  ("+(i+1)+" of "+L.length+")";
  var get=function(f){return fetch("/api/mapbins?snap="+encodeURIComponent(f)).then(function(r){if(!r.ok)throw new Error("HTTP "+r.status);return r.json();});};
  Promise.all([get(s.file),i>0?get(L[i-1].file).catch(function(){return null;}):Promise.resolve(null)]).then(function(r){M.bins=r[0];M.prev=r[1];
    var t=r[0].totals||{};$("mapsum").innerHTML=M.meta.categories.map(function(k){return '<span class="tag" style="border-color:'+M.meta.colors[k]+'">'+esc(k.replace("_"," / "))+": "+num(t[k]||0)+"</span>";}).join("")+(M.prev?'':' <span class="mu">(no earlier binned snapshot for the "new" layer)</span>');mapDraw();}).catch(function(e){mapMsg('<span class="neg">could not load bins: '+esc(e)+"</span>");});
}

/* ---------------- depot panel ---------------- */
function depRows(L,cls,extra){return L.map(function(r){return '<tr class="'+(cls||"")+'"><td>'+esc(r.name)+'</td><td class="n">'+num(r.stock)+'</td><td class="n">'+num(r.stack)+'</td><td class="mu" style="font-size:12px">'+esc(r.buildings.slice(0,6).join(", "))+(r.buildings.length>6?" +"+(r.buildings.length-6):"")+'</td>'+(extra?extra(r):"")+'</tr>';}).join("");}
function depInit(){fetch("/api/depot").then(function(r){return r.json();}).then(function(d){
  var e=$("dep"),h='<div class="deprule">'+esc(d.rule||"")+'</div>',c=d.latest;
  if(!c){e.innerHTML=h+'<p class="unk">no depot data in this snapshot</p><p class="mu">Run satwatch once (it re-parses the newest save automatically) or use --reparse-latest.</p>';return;}
  var df=d.diff,ng={};(df&&df.new_gap||[]).forEach(function(r){ng[r.item]=1;});
  h+='<div class="kpis"><div class="kpi"><b class="'+(c.gap.length?"neg":"pos")+'">'+c.gap.length+'</b><span>gap items</span></div><div class="kpi"><b class="'+(c.low.length?"warn":"pos")+'">'+c.low.length+'</b><span>low (&lt; '+c.low_threshold_stacks+' stack)</span></div><div class="kpi"><b>'+num(c.depot_buildings)+'</b><span>uploaders built'+(df&&df.uploaders_delta?' ('+(df.uploaders_delta>0?"+":"")+df.uploaders_delta+')':'')+'</span></div><div class="kpi"><b>'+c.build_required_items+'</b><span>build-required items</span></div><div class="kpi"><b>'+c.depot_item_types+'</b><span>item types in depot</span></div></div>';
  h+='<div class="mu" style="font-size:12px">'+esc(c.save||"")+(c.play_hours!=null?" | "+c.play_hours.toFixed(1)+" h":"")+(df?' | compared with '+esc(df.previous_save):' | no earlier snapshot with depot data (run Satisfactory Depot Backfill for history)')+'</div>';
  var th='<tr><th>item</th><th class="n">in depot</th><th class="n">stack</th><th>needed by</th></tr>';
  h+='<h3>Gap: to-do (0 in depot)</h3>'+(c.gap.length?'<table class="t">'+th+c.gap.map(function(r){return depRows([r],ng[r.item]?"depnew":"",null).replace("<td>"+esc(r.name),"<td>"+(ng[r.item]?'<span class="depbadge">NEW</span> ':"")+esc(r.name));}).join("")+'</table>':'<p class="pos">None: every build-required item is in the depot.</p>');
  h+='<h3>Low (&lt; '+c.low_threshold_stacks+' stack)</h3>'+(c.low.length?'<table class="t">'+th+depRows(c.low)+'</table>':'<p class="pos">None.</p>');
  if(df){var res=df.resolved_gap||[];h+='<h3>Since previous depot snapshot</h3>';
    var bits=[];if(df.new_gap.length)bits.push('<span class="neg">new gap: '+df.new_gap.map(function(r){return esc(r.name);}).join(", ")+'</span>');
    if(res.length)bits.push('<span class="pos">resolved: '+res.map(function(r){return '<s>'+esc(r.name)+'</s> (now '+num(r.stock_now)+')';}).join(", ")+'</span>');
    if(df.new_low.length)bits.push('<span class="warn">newly low: '+df.new_low.map(function(r){return esc(r.name);}).join(", ")+'</span>');
    if(df.left_low.length)bits.push('<span class="pos">no longer low: '+df.left_low.map(function(r){return esc(r.name);}).join(", ")+'</span>');
    h+='<p>'+(bits.join("<br>")||'<span class="mu">no change</span>')+'</p>';}
  e.innerHTML=h;}).catch(function(){$("dep").innerHTML='<p class="unk">no depot data in this snapshot</p>';});}
depInit();
var V={only:"all",tip:null,pin:null,d:null};
function volOn(r){return r.depot>0||r.drone_routes.length>0||r.train.length>0;}
function volYes(t){return '<span class="y mono">&#9679; '+esc(t)+'</span>';}
function volDraw(){var d=V.d,e=$("vol"),o=V.only,show=V.pin!==null?V.pin:V.tip;
  var rows=d.rows.map(function(r,i){return {r:r,i:i};}).filter(function(x){var r=x.r;return o==="all"?true:o==="none"?!volOn(r):o==="nodrone"?!r.drone_routes.length:o==="planned"?planAnyWant(r):!r.train.length;});
  var sm=d.summary,no='<span class="no mono">none</span>';
  var h='<div class="kpis">'+[["Volume items",sm.rows],["In depot",sm.in_depot],["On drone",sm.on_drone],["On train",sm.on_train],["On none",sm.on_none]].concat(["depot","drone","train"].map(function(n){return ["Planned "+n,d.rows.filter(function(r){return planWant(r["class"]||r.item,n);}).length];})).map(function(k){return '<div class="kpi"><span>'+k[0]+'</span><b>'+k[1]+'</b></div>';}).join("")+'</div>';
  h+='<div>'+[["all","All"],["none","Not on any network or depot"],["nodrone","No drone route"],["notrain","No train"],["planned","Planned"]].map(function(f){return '<button class="fb'+(o===f[0]?' on':'')+'" data-vf="'+f[0]+'">'+f[1]+'</button>';}).join("")+'</div>';
  h+='<div style="overflow-x:auto;border:1px solid var(--ln)"><div style="min-width:960px"><div class="vg vh"><div>Item</div><div>Level</div><div>Made at</div><div style="text-align:right">Depot</div><div>Drone network</div><div>Train network</div></div><div class="vs">';
  (d.groups_all||[]).forEach(function(g){var gr=rows.filter(function(x){return x.r.tier===g[0];});if(!gr.length)return;
    h+='<div class="vb"><b>'+esc(g[1])+'</b><span class="mono">'+gr.length+' item'+(gr.length===1?'':'s')+'</span><span class="mono mu">depot '+gr.filter(function(x){return x.r.depot>0;}).length+' &middot; drone '+gr.filter(function(x){return x.r.drone_routes.length;}).length+' &middot; train '+gr.filter(function(x){return x.r.train.length;}).length+'</span></div>';
    gr.forEach(function(x){var r=x.r,i=x.i;
      h+='<div class="vg'+(show===i?' sel':'')+'"><div class="vc"><span class="vn" tabindex="0" role="button" aria-expanded="'+(show===i)+'" data-vi="'+i+'">'+esc(r.item)+'</span>'+(r.kind==="Packaged"?'<span class="pk mono">packaged</span>':'')+'</div>'+
       '<div class="vc mu">'+esc(r.kind)+'</div><div class="vc mono">'+(r.made_at.length?esc(r.made_at.join(", ")):'<span class="no">not made yet</span>')+'</div>'+
       planCell(r,"depot",'<div style="text-align:right">'+(r.depot>0?volYes(num(r.depot)):no)+'</div>')+
       planCell(r,"drone",(r.drone_routes.length?r.drone_routes.map(function(t){return '<div>'+volYes("")+' '+esc(t)+'</div>';}).join(""):no)+r.drone_ports_unpaired.map(function(t){return '<div class="po">port only: '+esc(t)+'</div>';}).join(""))+
       planCell(r,"train",(r.train.length?r.train.map(function(t){return '<div>'+volYes("")+' '+esc(t)+'</div>';}).join(""):no))+'</div>';
      var pn=(pl().transport[r["class"]||r.item]||{}).note;if(pn&&show!==i)h+='<div class="vc mu" style="font-size:12px;padding-left:24px">&#9998; '+esc(pn)+'</div>';
      if(show===i){h+='<div class="vc vd"><div class="pnote"><span class="mu" style="font-size:12px;white-space:nowrap">Plan note</span><input data-nt="'+esc(r["class"]||r.item)+'" maxlength="500" value="'+esc((V.nd&&V.nd[r["class"]||r.item])!=null?V.nd[r["class"]||r.item]:((pl().transport[r["class"]||r.item]||{}).note||""))+'" placeholder="optional note for the transport plan"><button class="btn" data-ns="'+esc(r["class"]||r.item)+'">save note</button></div><div style="text-transform:uppercase;letter-spacing:1px;font-size:12px;color:var(--or2);margin-bottom:4px">'+esc(r.item)+' is used to make</div>'+
        r.used_to_make.map(function(u){return '<div style="padding:2px 0"><b>'+esc(u[0])+'</b> <span class="mono mu">'+esc(u[1].join(" \u00b7 "))+'</span>'+(u[0]==="Unpackages into"?'<span class="mu" style="font-size:12px"> (note, not a volume use)</span>':'')+'</div>';}).join("")+'</div>';}
    });});
  h+='</div></div></div><p class="mu" style="font-size:12px">Hover, focus (Tab) or tap an item name to see what it makes; tap again to unpin. Quantities are per recipe cycle (fluids in m&sup3;), "ingredient &rarr; product". &#9733;in save = a factory in the save runs that recipe.</p>';
  h+='<div class="deprule" style="font-weight:normal;font-size:12px">'+esc(d.rule)+'</div>'+((d.notes||[]).length?'<p class="unk" style="font-size:12px">'+d.notes.map(esc).join("<br>")+'</p>':'')+'<p class="mu" style="font-size:12px">From '+esc(d.save||"?")+' &middot; also written to volume_items.md in the vault folder.</p>';
  var sc=e.querySelector(".vs"),top=sc?sc.scrollTop:0;e.innerHTML=h;sc=e.querySelector(".vs");if(sc)sc.scrollTop=top;
  if(V.focus!=null){var f=e.querySelector('[data-vi="'+V.focus+'"]');if(f)f.focus({preventScroll:true});}}
function volInit(){var e=$("vol");V.nd={};e.addEventListener("input",function(ev){var t=ev.target.getAttribute&&ev.target.getAttribute("data-nt");if(t!=null)V.nd[t]=ev.target.value;});
  e.addEventListener("click",function(ev){var b=ev.target.closest("[data-vf]");if(b){V.only=b.getAttribute("data-vf");V.pin=null;volDraw();return;}
    var ns=ev.target.closest("[data-ns]");if(ns){var k=ns.getAttribute("data-ns"),inp=e.querySelector('input[data-nt="'+CSS.escape(k)+'"]');planPost("/api/plan/transport",{item:k,note:inp?inp.value:""}).then(function(){delete V.nd[k];volDraw();}).catch(function(er){alert("note not saved: "+er.message);});return;}
    if(ev.target.closest("[data-nt]"))return;
    var pc=ev.target.closest("[data-pn]");if(pc){var c=pc.getAttribute("data-pc"),n=pc.getAttribute("data-pn");V.pfocus=[c,n];planPost("/api/plan/transport",{item:c,network:n,state:planWant(c,n)?"":"want"}).then(function(){var f=e.querySelector('[data-pc="'+CSS.escape(c)+'"][data-pn="'+n+'"]');if(f)f.focus({preventScroll:true});}).catch(function(er){alert("plan not saved: "+er.message);});return;}
    var n=ev.target.closest("[data-vi]");if(n){var i=+n.getAttribute("data-vi");V.pin=V.pin===i?null:i;V.focus=i;volDraw();}});
  e.addEventListener("keydown",function(ev){var n=ev.target.closest("[data-vi],[data-pn]");if(n&&(ev.key==="Enter"||ev.key===" ")){ev.preventDefault();n.click();}});
  e.addEventListener("mouseover",function(ev){var n=ev.target.closest("[data-vi]");if(n){var i=+n.getAttribute("data-vi");if(V.tip!==i){V.tip=i;V.focus=null;volDraw();}}});
  e.addEventListener("mouseout",function(ev){var n=ev.target.closest("[data-vi]");if(n&&!(ev.relatedTarget&&n.contains(ev.relatedTarget))){V.tip=null;V.focus=null;volDraw();}});
  e.addEventListener("focusin",function(ev){var n=ev.target.closest("[data-vi]");if(n){var i=+n.getAttribute("data-vi");if(V.tip!==i){V.tip=i;V.focus=i;volDraw();}}});
  fetch("/api/volume").then(function(r){return r.json();}).then(function(d){
    var op=$("outp"),o=d.outpost;
    if(!d.available){e.innerHTML='<p class="unk">Volume table not available: '+esc(d.note||"no network data")+'</p><p class="mu">satwatch re-parses the newest save automatically once; or run with --reparse-latest.</p>';op.innerHTML='<p class="unk">not available (no network data)</p>';return;}
    d.groups_all=[[0,"Tier 0"],[1,"Tier 1"],[2,"Tier 2"],[3,"Tier 3"],[4,"Tier 4"],[5,"Tier 5"],[6,"Tier 6"],[7,"Tier 7"],[8,"Tier 8"],[9,"Tier 9"],[-1,"MAM / alternate-only"],[99,"Unknown tier"]];
    V.d=d;volDraw();
    if(!o){op.innerHTML='<p class="unk">not available: no player position in this save</p>';return;}
    op.innerHTML='<div class="kv"><div><small>Pioneer at (m)</small>'+o.player_cm.map(function(v){return Math.round(v/100).toLocaleString("en-US");}).join(", ")+'</div>'+
      (o.nearest_factory?'<div><small>Nearest factory</small>'+esc(o.nearest_factory.label)+' '+esc(o.nearest_factory.name)+'</div><div><small>Distance</small>'+(o.distance_m===0?'inside the box':num(o.distance_m)+' m')+'</div>':'<div><small>Nearest factory</small><span class="unk">no factory boxes</span></div>')+
      '</div><p class="mu" style="font-size:12px">The map below marks the pioneer (white triangle). Resource nodes and inventory are not shown in this version.</p>';
  }).catch(function(err){e.innerHTML='<p class="neg">volume data failed: '+esc(err)+'</p>';});}
volInit();
/* ---------------- factory overlay ---------------- */
function facBoxPx(f){var b=f.bbox_cm,a=w2i(b.min_x,b.min_y),z=w2i(b.max_x,b.max_y);return [M.ox+a[0]*M.s,M.oy+a[1]*M.s,M.ox+z[0]*M.s,M.oy+z[1]*M.s];}
function facAt(px,py){if(!M.fac||!M.facOn||!M.meta)return null;var best=null,ba=1e18;M.fac.factories.forEach(function(f){if(M.facHide[f.id])return;var r=facBoxPx(f);
  if(px>=r[0]-3&&px<=r[2]+3&&py>=r[1]-3&&py<=r[3]+3){var ar=(r[2]-r[0])*(r[3]-r[1]);if(ar<ba){ba=ar;best=f;}}});return best;}
function facTagPath(x,X,Y,w,h){var k=5;x.beginPath();x.moveTo(X,Y);x.lineTo(X+w-k,Y);x.lineTo(X+w,Y+k);x.lineTo(X+w,Y+h);x.lineTo(X+k,Y+h);x.lineTo(X,Y+h-k);x.closePath();}
function facDraw(){
  if(!M.fac||!M.facOn)return;var x=M.ctx,c=M.cv,placed=[];x.save();
  var F=M.fac.factories.filter(function(f){return !M.facHide[f.id];});
  F.forEach(function(f){var r=facBoxPx(f),w=r[2]-r[0],h=r[3]-r[1];if(r[2]<-20||r[3]<-20||r[0]>c.width+20||r[1]>c.height+20)return;
    var hi=(M.facHov===f||M.facSel===f.id),du=pUrlOk(pFac(f.label).design_url);
    x.fillStyle=hi?"rgba(250,149,73,0.20)":"rgba(250,149,73,0.07)";x.fillRect(r[0],r[1],w,h);
    if(w>10&&h>10){x.beginPath();x.arc(r[2]-7,r[1]+7,5,0,7);if(du){x.fillStyle="#5bc0de";x.fill();}else{x.strokeStyle="#9a9ca188";x.lineWidth=1;x.stroke();}}
    x.strokeStyle=hi?"#ffd2a8":"#FA9549";x.lineWidth=hi?3:2;x.setLineDash(w>40?[8,4]:[]);x.strokeRect(r[0],r[1],w,h);x.setLineDash([]);
    var t=Math.max(4,Math.min(14,Math.min(w,h)/4));x.lineWidth=hi?4:3;x.beginPath();
    [[r[0],r[1],1,1],[r[2],r[1],-1,1],[r[0],r[3],1,-1],[r[2],r[3],-1,-1]].forEach(function(q){x.moveTo(q[0]+q[2]*t,q[1]);x.lineTo(q[0],q[1]);x.lineTo(q[0],q[1]+q[3]*t);});x.stroke();
  });
  if(M.staOn)F.forEach(function(f){(f.stations||[]).forEach(function(s){var q=w2i(s.pos[0],s.pos[1]),sx=M.ox+q[0]*M.s,sy=M.oy+q[1]*M.s;if(sx<-8||sy<-8||sx>c.width+8||sy>c.height+8)return;
    var ro=(s.role||""),col=s.conflict?"#ff5555":/^input/.test(ro)?"#5fb7ff":/^output/.test(ro)?"#FA9549":"#bbbbbb",z=M.s>0.5?7:5;
    x.fillStyle=col;x.strokeStyle="#111";x.lineWidth=1.5;x.beginPath();
    if(s.kind==="train"){x.moveTo(sx,sy-z);x.lineTo(sx+z,sy+z*0.8);x.lineTo(sx-z,sy+z*0.8);}else{x.moveTo(sx,sy-z);x.lineTo(sx+z,sy);x.lineTo(sx,sy+z);x.lineTo(sx-z,sy);}
    x.closePath();x.fill();x.stroke();});});
  // labels: largest boxes first; shrink to "F#" (then skip) when they would overlap
  var far=M.s<0.25;F.slice().sort(function(a,b){return (b.counts.production||0)-(a.counts.production||0);}).forEach(function(f){var r=facBoxPx(f);if(r[2]<0||r[3]<0||r[0]>c.width||r[1]>c.height)return;
    var hi=(M.facHov===f||M.facSel===f.id),fs=far?11:12,opts=[f.label+" "+fName(f),f.label];if(far&&!hi)opts.shift();
    x.font="bold "+fs+"px Bahnschrift,Arial";var X=Math.max(2,r[0]),Y=Math.max(2,r[1]-fs-6),done=false;
    for(var i=0;i<opts.length&&!done;i++){var tx=opts[i];if(tx.length>34&&!hi)tx=tx.slice(0,32)+"..";var w=x.measureText(tx).width+12,h=fs+6;
      var ov=placed.some(function(p){return X<p[0]+p[2]&&p[0]<X+w&&Y<p[1]+p[3]&&p[1]<Y+h;});
      if(ov&&!hi&&i<opts.length-1)continue;
      if(ov&&!hi){Y=Y+h+2;}
      facTagPath(x,X,Y,w,h);x.fillStyle=hi?"#FA9549":"rgba(20,20,20,0.85)";x.fill();x.strokeStyle="#FA9549";x.lineWidth=1;x.stroke();
      x.fillStyle=hi?"#111":"#FA9549";x.fillText(tx,X+6,Y+fs+1);placed.push([X,Y,w,h]);done=true;}
  });
  x.restore();
}
function facHtml(f,brief){
  var pf=pFac(f.label),h='<b style="color:#FA9549">'+esc(f.label+" "+fName(f))+'</b> <span class="mu">('+(pf.name?'your name; save: '+esc(f.name):esc(f.name_source||""))+')</span>'+(pUrlOk(pf.design_url)?' <span class="ptag">design &#128279;</span>':' <span class="mu" style="font-size:11px">no design link</span>')+'<br>'+(pf.purpose?'<i>'+esc(pf.purpose)+'</i><br>':'');
  h+='box '+f.size_m[0]+' x '+f.size_m[1]+' m'+(f.region?' | '+esc(f.region):'')+'<br>';
  h+='buildings: '+(f.counts.production||0)+' production, '+(f.counts.extraction||0)+' extractors, '+(f.counts.stations||0)+' stations<br>';
  h+='<span class="mu">key recipes:</span> '+(f.top_recipes||[]).map(function(r){return esc(String(r[0]).replace(/^Alternate_/,"Alt: ").replace(/_/g," "))+" x"+r[1];}).join(", ")+'<br>';
  if((f.stations||[]).length)h+='<span class="mu">stations:</span><br>'+f.stations.map(function(s){return (s.kind==="train"?"&#9650; train":"&#9670; drone")+' <b>'+esc(s.name)+'</b> <span class="mu">'+esc(s.role||"")+'</span>'+(s.conflict?' <span class="caut">conflict</span>':'');}).join("<br>")+'<br>';
  var W=f.warnings||[];if(W.length){var L=brief?W.slice(0,4):W;h+=L.map(function(w){return '<span class="caut">&#9888; '+esc(w)+'</span>';}).join("<br>");if(brief&&W.length>4)h+='<br><span class="caut">+'+(W.length-4)+' more (click for all)</span>';}
  else h+='<span class="mu">no warnings</span>';
  if(brief)h+='<br><span class="mu">click to fit factory</span>';else h+=facBars(f);
  return h;
}
function facFit(f){var b=f.bbox_cm,a=w2i(b.min_x,b.min_y),z=w2i(b.max_x,b.max_y),c=M.cv,pad=60;
  var s=Math.min((c.width-2*pad)/(z[0]-a[0]),(c.height-2*pad)/(z[1]-a[1]));M.s=Math.max(0.05,Math.min(8,s));
  M.ox=c.width/2-(a[0]+z[0])/2*M.s;M.oy=c.height/2-(a[1]+z[1])/2*M.s;mapDraw();}
function facSelect(id,fit){if(id==="next")return nextSelect();var f=null;M.fac.factories.forEach(function(g){if(g.id===id)f=g;});if(!f)return;M.facSel=id;
  var p=$("facpanel");p.innerHTML=facHtml(f,false)+designHtml(f.label,f)+'<div style="margin-top:4px"><button class="btn" id="facfitb">Fit factory</button> <button class="btn" id="facclose">close</button></div>';p.style.display="block";designWire(f.label,function(){facSelect(id,false);});
  $("facfitb").onclick=function(){facFit(f);};$("facclose").onclick=function(){M.facSel=null;p.style.display="none";facChips();mapDraw();};
  facChips();if(fit)facFit(f);else mapDraw();}
function facChips(){if(!M.fac)return;$("facchips").innerHTML='<span class="mu">Factories:</span> '+M.fac.factories.map(function(f){var du=pUrlOk(pFac(f.label).design_url);return '<span class="tag fc'+(M.facSel===f.id?" sel":"")+(M.facHide[f.id]?" off":"")+(du?"":" nod")+'" data-f="'+esc(f.id)+'" title="'+esc(fName(f))+(du?' (design linked)':' (no design link)')+' (click: select + zoom, right-click: hide/show)">'+esc(f.label)+(du?' &#128279;':'')+'</span>';}).join("")+' <span class="tag fc nx'+(M.facSel==="next"?" sel":"")+'" data-f="next" title="planned next factory">&#9670; Next factory'+(pUrlOk(pFac("next").design_url)?' &#128279;':'')+'</span>';
  Array.prototype.forEach.call($("facchips").querySelectorAll(".fc"),function(e){var id=e.getAttribute("data-f");
    e.onclick=function(){if(id==="next"){nextSelect();return;}M.facHide[id]=false;facSelect(id,true);};
    e.oncontextmenu=function(ev){ev.preventDefault();if(id==="next")return;M.facHide[id]=!M.facHide[id];facChips();mapDraw();};});}
function facLoad(){fetch("/api/factories").then(function(r){if(!r.ok)throw 0;return r.json();}).then(function(d){if(!d||!d.factories||!d.factories.length)return;M.fac=d;
  var lg=$("maplg");lg.insertAdjacentHTML("beforeend",'<label class="tag" style="border-color:#FA9549"><input type="checkbox" id="facon" checked> <span style="color:#FA9549">&#9633;</span> Factories</label><label class="tag" style="border-color:#FA9549"><input type="checkbox" id="staon" checked> &#9650; train / &#9670; drone stations <span style="color:#FA9549">out</span> <span style="color:#5fb7ff">in</span></label>');
  $("facon").onchange=function(){M.facOn=this.checked;$("facchips").style.display=this.checked?"":"none";mapDraw();};$("staon").onchange=function(){M.staOn=this.checked;mapDraw();};
  var ds=d.derived_from_save||{};$("facnote").textContent=" Factory boxes reflect the "+(ds.date||"?")+" save ("+(ds.file||"?")+"); re-derive after new saves (see README).";
  facChips();var fm=/fac=(\w+)/.exec(location.hash);if(fm)facSelect(fm[1],true);else mapDraw();}).catch(function(){});}
function mapInit(){
  fetch("/api/mapmeta").then(function(r){return r.json();}).then(function(m){
    M.meta=m;var L=m.snapshots||[];
    $("mapattr").textContent=(m.attribution||"")+(m.source_url?" Source: "+m.source_url:"");
    if(!L.length||m.disabled){$("mapwrap").style.display="none";mapMsg('<p class="unk">No map data yet: run Satisfactory Map Backfill</p>');return;}
    if(m.snapshots_total&&L.length<m.snapshots_total)mapMsg('<span class="mu">Map bins exist for '+L.length+' of '+m.snapshots_total+' snapshots; the slider only offers those. Run "Satisfactory Map Backfill" to add the rest.</span>');
    var lg="";m.categories.forEach(function(k){M.on[k]=(k!=="structure");lg+='<label class="tag" style="border-color:'+m.colors[k]+'"><input type="checkbox" data-k="'+k+'"'+(M.on[k]?" checked":"")+'> <span style="color:'+m.colors[k]+'">&#9632;</span> '+esc(k.replace("_"," / "))+"</label>";});
    lg+='<label class="tag" style="border-color:#fff"><input type="checkbox" id="mapnew" checked> &#9633; new since previous</label><span class="tag">&#9650; pioneer</span>';
    $("maplg").innerHTML=lg;
    Array.prototype.forEach.call($("maplg").querySelectorAll("input[data-k]"),function(e){e.onchange=function(){M.on[e.getAttribute("data-k")]=e.checked;mapDraw();};});
    $("mapnew").onchange=function(){M.showNew=this.checked;mapDraw();};
    var sl=$("mapsl");sl.max=L.length-1;sl.oninput=function(){mapLoad(+sl.value);};
    $("mapprev").onclick=function(){mapLoad(M.i-1);};$("mapnext").onclick=function(){mapLoad(M.i+1);};
    $("mapzi").onclick=function(){mapZoom(1.6);};$("mapzo").onclick=function(){mapZoom(1/1.6);};$("mapfit").onclick=mapFit;
    var c=$("mapcv");M.cv=c;M.ctx=c.getContext("2d");
    var size=function(){c.width=c.clientWidth;c.height=c.clientHeight;};size();
    window.addEventListener("resize",function(){size();mapDraw();});
    c.addEventListener("wheel",function(e){e.preventDefault();var r=c.getBoundingClientRect();mapZoom(e.deltaY<0?1.25:0.8,e.clientX-r.left,e.clientY-r.top);},{passive:false});
    c.addEventListener("mousedown",function(e){M.drag=[e.clientX,e.clientY,M.ox,M.oy];M.down=[e.clientX,e.clientY];});
    c.addEventListener("click",function(e){if(!M.down||Math.abs(e.clientX-M.down[0])+Math.abs(e.clientY-M.down[1])>4)return;var r=c.getBoundingClientRect();if(P.pick){var w=i2w((e.clientX-r.left-M.ox)/M.s,(e.clientY-r.top-M.oy)/M.s);P.pick=false;planPost("/api/plan/factory",{key:"next",pos:[Math.round(w[0]),Math.round(w[1])]}).then(nextSelect).catch(function(er){alert(er.message);});return;}var f=facAt(e.clientX-r.left,e.clientY-r.top);if(f)facSelect(f.id,true);});
    window.addEventListener("mouseup",function(){M.drag=null;});
    c.addEventListener("mousemove",function(e){var r=c.getBoundingClientRect();M.mx=e.clientX-r.left;M.my=e.clientY-r.top;if(M.drag){M.ox=M.drag[2]+e.clientX-M.drag[0];M.oy=M.drag[3]+e.clientY-M.drag[1];mapDraw();}else mapHover();});
    c.addEventListener("mouseleave",function(){M.mx=-1;mapHover();});
    var latest=L.length-1;L.forEach(function(s,j){if(s.file===m.latest_file)latest=j;});
    M.img=new Image();M.img.onload=function(){M.imgOk=true;mapDraw();};M.img.onerror=function(){mapMsg('<span class="warn">map image missing (assets/map_placeholder.png); drawing data only</span>');mapDraw();};M.img.src=m.image;
    facLoad();
    fetch("/api/mappoints").then(function(r){return r.json();}).then(function(p){M.pts=p;mapDraw();}).catch(function(){});
    mapFit();
    var hm=/map=(-?[\d.]+),(-?[\d.]+),([\d.]+)/.exec(location.hash);
    if(hm){$("mapwrap").scrollIntoView();var q=w2i(+hm[1]*100,+hm[2]*100);M.s=+hm[3];M.ox=c.width/2-q[0]*M.s;M.oy=c.height/2-q[1]*M.s;}
    else if(/map/.test(location.hash))$("mapwrap").scrollIntoView();
    mapLoad(latest);
  }).catch(function(e){mapMsg('<p class="unk">No map data yet: run Satisfactory Map Backfill</p>');});
}
mapInit();
planLoad();insLoad();
fetch("/api/data").then(function(r){return r.json();}).then(function(d){
  D=d;var p=$("pick"),rows=(d.timeline||[]).slice().reverse();
  rows.forEach(function(r){var o=document.createElement("option");o.value=r.file;o.textContent=(r.save_datetime||"?").slice(0,16).replace("T"," ")+"  "+r.file+(r.status!=="ok"?" ["+r.status+"]":"");p.appendChild(o);});
  p.onchange=function(){load(p.value);};
  try{panelD();}catch(e){$("pd").innerHTML='<p class="neg">chart error: '+esc(e)+'</p>';}
  if(d.latest){p.value=d.latest.file;show(d.latest);}else{$("pa").innerHTML='<p class="mu">No snapshots yet.</p>';$("ft").textContent="generated "+((d.run||{}).generated||"");}
}).catch(function(e){$("sub").textContent="failed to load /api/data: "+e;});
</script></body></html>'''

if __name__ == '__main__':
    sys.exit(main())
