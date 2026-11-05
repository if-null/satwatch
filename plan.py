"""plan.py: The user's plan file (plan.json in the satwatch WORK folder), stdlib only.

Only the dashboard writes plan.json (POST /api/plan/*). The vault gets a read-only plan.md
view; the plan is never read back from the vault. Atomic writes + timestamped backups.
"""
import datetime, json, os, re, shutil, threading
from pathlib import Path
from urllib.parse import urlparse

PLAN_VERSION = 1
PLAN_FILE = 'plan.json'
BACKUP_DIR = 'plan_backups'
KEEP_BACKUPS = 30
NETWORKS = ('depot', 'drone', 'train')
STATES = ('', 'want')
MAX_URL = 32 * 1024
LIMITS = {'name': 120, 'purpose': 500, 'notes': 4000, 'note': 500}
KEY_RE = re.compile(r'^(F\d{1,4}|next)$')
ITEM_RE = re.compile(r'^[A-Za-z0-9_\-. ]{1,120}$')
LOCK = threading.Lock()


class PlanError(ValueError):
    pass


def _ts():
    return datetime.datetime.now().strftime('%Y%m%d-%H%M%S')


def empty():
    return {'version': PLAN_VERSION, 'updated': None, 'factories': {}, 'transport': {}}


def _clean(d):
    """Keep only known keys/types from a loaded plan (tolerates hand edits)."""
    out = empty()
    out['updated'] = d.get('updated') if isinstance(d.get('updated'), str) else None
    for k, f in (d.get('factories') or {}).items():
        if not (isinstance(k, str) and KEY_RE.match(k) and isinstance(f, dict)):
            continue
        g = {x: f[x] for x in ('name', 'purpose', 'design_url', 'notes') if isinstance(f.get(x), str) and f.get(x)}
        p = f.get('pos')
        if isinstance(p, list) and len(p) == 2 and all(isinstance(v, (int, float)) for v in p):
            g['pos'] = [float(p[0]), float(p[1])]
        if g:
            out['factories'][k] = g
    for k, t in (d.get('transport') or {}).items():
        if not (isinstance(k, str) and ITEM_RE.match(k) and isinstance(t, dict)):
            continue
        g = {n: 'want' for n in NETWORKS if t.get(n) == 'want'}
        if isinstance(t.get('note'), str) and t['note']:
            g['note'] = t['note']
        if g:
            out['transport'][k] = g
    return out


def load(work):
    """Read plan.json. Missing -> empty. Corrupt -> keep a copy as plan.corrupt-<ts>.json, start empty."""
    p = Path(work) / PLAN_FILE
    if not p.is_file():
        return empty(), None
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
        if not isinstance(d, dict) or d.get('version') != PLAN_VERSION:
            raise ValueError('unknown plan version %r' % (d.get('version') if isinstance(d, dict) else type(d).__name__))
        return _clean(d), None
    except Exception as e:
        bad = p.with_name('plan.corrupt-%s.json' % _ts())
        try:
            os.replace(str(p), str(bad))
        except OSError:
            pass
        return empty(), 'plan.json was unreadable (%s); kept as %s and started empty' % (str(e)[:120], bad.name)


def _dump(plan):
    return json.dumps(plan, indent=1, ensure_ascii=False, sort_keys=True)


def save(work, plan):
    """Atomic write; back up the new content into plan_backups when it differs from the file on disk."""
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    p = work / PLAN_FILE
    old = None
    try:
        old = p.read_text(encoding='utf-8') if p.is_file() else None
    except Exception:
        pass
    body = _dump({k: v for k, v in plan.items() if k != 'updated'})
    if old is not None:
        try:
            if _dump({k: v for k, v in json.loads(old).items() if k != 'updated'}) == body:
                return False
        except Exception:
            pass
    plan['updated'] = datetime.datetime.now().isoformat(timespec='seconds')
    text = _dump(plan)
    tmp = p.with_name(PLAN_FILE + '.tmp')
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    os.replace(str(tmp), str(p))
    bd = work / BACKUP_DIR
    bd.mkdir(exist_ok=True)
    def _age(b):
        m = re.match(r'plan-(\d{8}-\d{6})(?:-(\d+))?\.json$', b.name)
        return (m.group(1), int(m.group(2) or 1)) if m else ('', 0)
    ts = _ts()
    same = [_age(b)[1] for b in bd.glob('plan-%s*.json' % ts) if _age(b)[0] == ts]
    n = max(same) + 1 if same else 1  # several edits in the same second get -02, -03, ...
    shutil.copy2(str(p), str(bd / ('plan-%s.json' % ts if n == 1 else 'plan-%s-%02d.json' % (ts, n))))
    for old_b in sorted(bd.glob('plan-*.json'), key=_age)[:-KEEP_BACKUPS]:
        try:
            old_b.unlink()
        except OSError:
            pass
    return True


def _text(v, field):
    if v is None:
        return ''
    if not isinstance(v, str):
        raise PlanError('%s must be text' % field)
    v = v.strip()
    if len(v) > LIMITS[field]:
        raise PlanError('%s too long (max %d characters)' % (field, LIMITS[field]))
    return v


def check_url(v):
    if v is None or v == '':
        return ''
    if not isinstance(v, str):
        raise PlanError('design_url must be text')
    v = v.strip()
    if len(v) > MAX_URL:
        raise PlanError('design_url too long (max %d characters)' % MAX_URL)
    if any(c in v for c in '\r\n\t ') or any(ord(c) < 32 for c in v):
        raise PlanError('design_url must not contain spaces or control characters')
    u = urlparse(v)
    if u.scheme.lower() not in ('http', 'https') or not u.netloc:
        raise PlanError('design_url must be an http:// or https:// link')
    return v


def set_factory(plan, body):
    key = body.get('key')
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise PlanError('bad factory key')
    f = dict(plan['factories'].get(key) or {})
    for fld in ('name', 'purpose', 'notes'):
        if fld in body:
            f[fld] = _text(body[fld], fld)
    if 'design_url' in body:
        f['design_url'] = check_url(body['design_url'])
    if 'pos' in body:
        p = body['pos']
        if p is None:
            f.pop('pos', None)
        elif key == 'next' and isinstance(p, list) and len(p) == 2 and all(isinstance(v, (int, float)) and abs(v) < 1e7 for v in p):
            f['pos'] = [round(float(p[0]), 1), round(float(p[1]), 1)]
        else:
            raise PlanError('pos must be [x, y] in cm (next factory only)')
    f = {k: v for k, v in f.items() if v not in ('', None)}
    if f:
        plan['factories'][key] = f
    else:
        plan['factories'].pop(key, None)
    return plan


def set_transport(plan, body):
    item = body.get('item')
    if not isinstance(item, str) or not ITEM_RE.match(item):
        raise PlanError('bad item key')
    t = dict(plan['transport'].get(item) or {})
    if 'network' in body or 'state' in body:
        n, s = body.get('network'), body.get('state')
        if n not in NETWORKS:
            raise PlanError('network must be one of %s' % ', '.join(NETWORKS))
        if s not in STATES:
            raise PlanError('state must be "want" or "" (done is shown automatically from the save)')
        if s:
            t[n] = s
        else:
            t.pop(n, None)
    if 'note' in body:
        t['note'] = _text(body['note'], 'note')
    t = {k: v for k, v in t.items() if v}
    if t:
        plan['transport'][item] = t
    else:
        plan['transport'].pop(item, None)
    return plan


def exists_map(vol):
    """item class -> {network: bool} from the latest volume table."""
    out = {}
    for r in (vol or {}).get('rows') or []:
        out[r.get('class') or r.get('item')] = {'depot': (r.get('depot') or 0) > 0, 'drone': bool(r.get('drone_routes')),
                                                'train': bool(r.get('train')), 'name': r.get('item')}
    return out


def planned_map(plan, vol):
    """For volume_items.json: item class -> {network: 'want'|'want, now exists'}."""
    ex = exists_map(vol)
    out = {}
    for k, t in plan['transport'].items():
        e = ex.get(k) or {}
        g = {n: ('want, now exists' if e.get(n) else 'want') for n in NETWORKS if t.get(n) == 'want'}
        if t.get('note'):
            g['note'] = t['note']
        if g:
            out[k] = g
    return out


def _md(s):
    return str(s).replace('|', '\\|').replace('\n', ' ')


def to_md(plan, vol=None, boxes=None, outpost=None, generated=''):
    names = {f.get('label') or 'F%s' % f.get('id'): f.get('name') for f in ((boxes or {}).get('factories') or [])}
    L = ['# Satisfactory plan', '', '> Generated by satwatch %s. Read-only view: **edit in the dashboard** (satwatch, http://127.0.0.1). '
         'Changes made here are overwritten. Source: plan.json in the satwatch work folder.' % generated, '']
    L += ['## Factories', '']
    keys = sorted(set(names) | set(plan['factories']), key=lambda k: (k == 'next', int(k[1:]) if k[1:].isdigit() else 0))
    if 'next' not in keys:
        keys.append('next')
    L += ['| Factory | Name | Purpose | Design | Notes |', '|---|---|---|---|---|']
    for k in keys:
        f = plan['factories'].get(k) or {}
        lbl = 'Next factory' if k == 'next' else k
        nm = f.get('name') or names.get(k) or ''
        if k == 'next':
            pos = f.get('pos') or (outpost or {}).get('player_cm')
            if pos:
                lbl += ' (at %d, %d m%s)' % (round(pos[0] / 100), round(pos[1] / 100), '' if f.get('pos') else ', pioneer position')
        url = f.get('design_url')
        L.append('| %s | %s | %s | %s | %s |' % (lbl, _md(nm), _md(f.get('purpose', '')), ('[Open design](%s)' % url.replace(')', '%29').replace(' ', '%20').replace('|', '%7C').replace('<', '%3C').replace('>', '%3E')) if url else 'no design link', _md(f.get('notes', ''))))
    L += ['', '## Planned transport changes', '']
    ex = exists_map(vol)
    any_ = False
    for n in NETWORKS:
        rows = [(k, t) for k, t in sorted(plan['transport'].items()) if t.get(n) == 'want']
        L += ['### %s (%d planned)' % (n.capitalize(), len(rows)), '']
        if not rows:
            L += ['- none', '']
            continue
        any_ = True
        for k, t in rows:
            e = ex.get(k) or {}
            L.append('- [%s] %s%s%s' % ('x' if e.get(n) else ' ', e.get('name') or k, ' (now exists in the save)' if e.get(n) else '', (' - ' + _md(t['note'])) if t.get('note') else ''))
        L.append('')
    if not any_:
        L += ['_No transport changes planned yet._', '']
    return '\n'.join(L)
