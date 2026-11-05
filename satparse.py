#!/usr/bin/env python3
"""satparse.py - stdlib-only Satisfactory .sav snapshot extractor + timeline builder.

Usage:
  python satparse.py <save.sav | folder> --out <dir | file.jsonl> [--pattern "Miners*"]
  python satparse.py --timeline <snapshot dir | file.jsonl> --out <dir>
Every parse stage is wrapped; failures become entries in snapshot["warnings"] and
the snapshot is still written with whatever was recovered (status = ok|partial|unparsed).
"""
import argparse, collections, datetime, glob, json, os, re, struct, sys, time, traceback, zlib

PARSER_VERSION = "0.4"
CHUNK_TAG = b'\xc1\x83\x2a\x9e'

FRIENDLY = {
 'Build_ConstructorMk1_C':'Constructor','Build_AssemblerMk1_C':'Assembler','Build_ManufacturerMk1_C':'Manufacturer',
 'Build_SmelterMk1_C':'Smelter','Build_FoundryMk1_C':'Foundry','Build_OilRefinery_C':'Refinery','Build_Blender_C':'Blender',
 'Build_Packager_C':'Packager','Build_HadronCollider_C':'Particle Accelerator','Build_Converter_C':'Converter',
 'Build_QuantumEncoder_C':'Quantum Encoder','Build_MinerMk1_C':'Miner Mk1','Build_MinerMk2_C':'Miner Mk2','Build_MinerMk3_C':'Miner Mk3',
 'Build_OilPump_C':'Oil Extractor','Build_WaterPump_C':'Water Extractor','Build_FrackingSmasher_C':'Resource Well Pressurizer',
 'Build_FrackingExtractor_C':'Resource Well Extractor','Build_GeneratorFuel_C':'Fuel Generator','Build_GeneratorCoal_C':'Coal Generator',
 'Build_GeneratorBiomass_Automated_C':'Biomass Burner','Build_GeneratorBiomass_C':'Biomass Burner (manual)',
 'Build_GeneratorIntegratedBiomass_C':'HUB Biomass Burner','Build_GeneratorGeoThermal_C':'Geothermal Generator',
 'Build_GeneratorNuclear_C':'Nuclear Power Plant','Build_AlienPowerBuilding_C':'Alien Power Augmenter','Build_PowerStorageMk1_C':'Power Storage',
 'Build_TrainStation_C':'Train Station','Build_TrainDockingStation_C':'Freight Platform','Build_TrainDockingStationLiquid_C':'Fluid Freight Platform',
 'Build_TrainPlatformEmpty_C':'Empty Platform','Build_RailroadTrack_C':'Railroad Track','Build_DroneStation_C':'Drone Port',
 'Build_TruckStation_C':'Truck Station','Build_ResourceSink_C':'AWESOME Sink','Build_ResourceSinkShop_C':'AWESOME Shop',
 'Build_CentralStorage_C':'Dimensional Depot','Build_SpaceElevator_C':'Space Elevator','Build_TradingPost_C':'HUB',
 'Build_Mam_C':'MAM','Build_StorageContainerMk1_C':'Storage Container','Build_StorageContainerMk2_C':'Industrial Storage Container',
 'Build_IndustrialTank_C':'Industrial Fluid Buffer','Build_PipeStorageTank_C':'Fluid Buffer','Build_RadarTower_C':'Radar Tower',
 'Build_PipeHyper_C':'Hypertube','Build_ConveyorBeltMk1_C':'Belt Mk1','Build_ConveyorBeltMk2_C':'Belt Mk2','Build_ConveyorBeltMk3_C':'Belt Mk3',
 'Build_ConveyorBeltMk4_C':'Belt Mk4','Build_ConveyorBeltMk5_C':'Belt Mk5','Build_ConveyorBeltMk6_C':'Belt Mk6',
 'Build_PowerLine_C':'Power Line','Build_PowerSwitch_C':'Power Switch','Build_PriorityPowerSwitch_C':'Priority Power Switch',
}
GENERATORS = {k for k in FRIENDLY if k.startswith('Build_Generator')} | {'Build_AlienPowerBuilding_C'}
EXTRACTORS = {'Build_MinerMk1_C','Build_MinerMk2_C','Build_MinerMk3_C','Build_OilPump_C','Build_WaterPump_C',
              'Build_FrackingSmasher_C','Build_FrackingExtractor_C'}
VEHICLES = re.compile(r'^(BP_Locomotive|BP_FreightWagon|BP_DroneTransport|BP_Tractor|BP_Truck|BP_Explorer|Testa_BP_WB|BP_Golfcart|BP_GolfcartGold)')
SPECIAL_ITEMS = {'Desc_HardDrive_C':'hard_drive','Desc_WAT1_C':'somersloop','Desc_WAT2_C':'mercer_sphere',
                 'Desc_Crystal_C':'power_slug_blue','Desc_Crystal_mk2_C':'power_slug_yellow','Desc_Crystal_mk3_C':'power_slug_purple'}

class ParseError(Exception): pass

class R:
    __slots__=('d','o','end')
    def __init__(s,d,o=0,end=None): s.d=d; s.o=o; s.end=len(d) if end is None else end
    def chk(s,n):
        if n<0 or s.o+n>s.end: raise ParseError('read past end at %d (+%d)'%(s.o,n))
    def i32(s): s.chk(4); v=struct.unpack_from('<i',s.d,s.o)[0]; s.o+=4; return v
    def u32(s): s.chk(4); v=struct.unpack_from('<I',s.d,s.o)[0]; s.o+=4; return v
    def i64(s): s.chk(8); v=struct.unpack_from('<q',s.d,s.o)[0]; s.o+=8; return v
    def f32(s): s.chk(4); v=struct.unpack_from('<f',s.d,s.o)[0]; s.o+=4; return v
    def f64(s): s.chk(8); v=struct.unpack_from('<d',s.d,s.o)[0]; s.o+=8; return v
    def u8(s): s.chk(1); v=s.d[s.o]; s.o+=1; return v
    def skip(s,n): s.chk(n); s.o+=n
    def raw(s,n): s.chk(n); v=s.d[s.o:s.o+n]; s.o+=n; return v
    def s(s):
        n=s.i32()
        if n==0: return ''
        if n<0:
            if -n>1_000_000: raise ParseError('bad utf16 strlen %d at %d'%(n,s.o))
            b=s.raw(-2*n); return b.decode('utf-16le','replace').rstrip('\0')
        if n>1_000_000: raise ParseError('bad strlen %d at %d'%(n,s.o))
        return s.raw(n).decode('latin1').rstrip('\0')
    def ref(s): return (s.s(), s.s())

# ---------------- header & decompression ----------------
def ticks_to_iso(t):
    try: return (datetime.datetime(1,1,1)+datetime.timedelta(microseconds=t//10)).isoformat()
    except Exception: return None

def parse_header(r):
    h={}
    h['header_version']=r.i32(); h['save_version']=r.i32(); h['build']=r.i32()
    hv=h['header_version']
    if hv>=14: h['save_name']=r.s()
    h['map_name']=r.s(); mo=r.s(); h['map_options_len']=len(mo); h['session_name']=r.s()
    h['play_seconds']=r.i32(); t=r.i64(); h['save_datetime_utc_guess']=ticks_to_iso(t)
    h['session_visibility']=r.u8()
    if hv>=7: h['editor_object_version']=r.i32()
    if hv>=8: h['mod_metadata']=r.s()[:200]; h['is_modded']=r.i32()
    if hv>=10: h['save_identifier']=r.s()
    if hv>=11: h['is_partitioned_world']=r.i32()
    if hv>=12: r.skip(20)  # save data hash
    if hv>=13: h['creative_mode']=r.i32()
    return h

def decompress(d, start, warn):
    p=d.find(CHUNK_TAG, start)
    if p<0: raise ParseError('no compressed chunk found')
    out=[]
    while p+16<len(d):
        if d[p:p+4]!=CHUNK_TAG:
            q=d.find(CHUNK_TAG,p)
            if q<0: warn.append('trailing %d bytes after chunks'%(len(d)-p)); break
            warn.append('chunk resync skip %d bytes'%(q-p)); p=q
        # tag4, archivever4 (0x22222222 => v2 has compressor byte), maxchunk8, [comp1], csize8, usize8, csize8, usize8
        v2 = d[p+4:p+8]==b'\x22\x22\x22\x22'
        off = 8+8+(1 if v2 else 0)
        cs=struct.unpack_from('<q',d,p+off)[0]
        dstart=p+off+32
        out.append(zlib.decompress(d[dstart:dstart+cs])); p=dstart+cs
    return b''.join(out)

# ---------------- properties ----------------
def read_typename(r, depth=0):
    name=r.s(); n=r.i32()
    if n<0 or n>16 or depth>8: raise ParseError('bad typename params')
    return [name]+[read_typename(r,depth+1) for _ in range(n)]

def tn_inner(tn, i=0):
    return tn[1+i] if len(tn)>1+i else ['?']

def read_props(r, fmt, end, depth=0):
    """Read tagged properties until 'None'. fmt: 'new' (UE5.5 type trees, SF 1.1+) or 'old'."""
    props={}
    while True:
        name=r.s()
        if name=='None' or name=='': break
        if fmt=='new':
            tn=read_typename(r); size=r.i32(); flags=r.u8(); boolv=None
            if flags & 0x01: r.i32()
            if flags & 0x02: r.skip(16)
            if flags & 0x04:
                ext=r.u8()
                if ext & 0x02: r.skip(2)
            ptype=tn[0]
            if ptype=='BoolProperty': boolv = bool(flags & 0x10)
        else:
            ptype=r.s(); size=r.i32(); r.i32(); boolv=None
            if ptype=='BoolProperty': boolv=bool(r.u8()); tn=[ptype]
            elif ptype in ('ByteProperty','EnumProperty'): tn=[ptype,[r.s()]]
            elif ptype in ('ArrayProperty','SetProperty'): tn=[ptype,[r.s()]]
            elif ptype=='MapProperty': tn=[ptype,[r.s()],[r.s()]]
            elif ptype=='StructProperty': tn=[ptype,[r.s()]]; r.skip(16)
            else: tn=[ptype]
            if r.u8(): r.skip(16)
        if size<0 or r.o+size>end: raise ParseError('prop %s size %d overflow'%(name,size))
        vend=r.o+size
        if ptype=='BoolProperty':
            val=boolv
        else:
            try: val=read_value(R(r.d,r.o,vend), tn, fmt, size, depth)
            except Exception as e: val={'_undecoded':ptype,'_err':str(e)[:80]}
        r.o=vend
        if name in props:
            if not isinstance(props[name],list) or not props.get('_multi_'+name): props[name]=[props[name]]; props['_multi_'+name]=True
            props[name].append(val)
        else: props[name]=val
    return props

def read_struct(r, sname, fmt, end, depth):
    if sname in ('Vector','Rotator','Vector_NetQuantize'): return [r.f64(),r.f64(),r.f64()] if end-r.o>=24 else [r.f32(),r.f32(),r.f32()]
    if sname=='LinearColor': return [r.f32() for _ in range(4)]
    if sname=='Guid': return r.raw(16).hex()
    if sname=='DateTime': return r.i64()
    if sname in ('InventoryItem','FactoryCustomizationData','UniqueNetIdRepl','ClientIdentityInfo'): return {'_raw':r.raw(end-r.o)}
    # generic: try tagged props, else raw
    st=r.o
    try: return read_props(r, fmt, end, depth+1)
    except Exception:
        r.o=st; return {'_raw':r.raw(end-st)}

def read_value(r, tn, fmt, size, depth):
    t=tn[0]
    if t=='IntProperty': return r.i32()
    if t=='Int64Property': return r.i64()
    if t=='UInt32Property': return r.u32()
    if t=='FloatProperty': return r.f32()
    if t=='DoubleProperty': return r.f64()
    if t in ('StrProperty','NameProperty'): return r.s()
    if t in ('ObjectProperty','InterfaceProperty'): return r.ref()[1]
    if t=='SoftObjectProperty': p=r.s(); q=r.s(); return q or p
    if t=='EnumProperty': return r.s()
    if t=='TextProperty':  # 0.4: names of train stations / trains
        r.i32(); ht=r.u8()
        if ht==255: return r.s() if r.i32() else ''
        if ht==0: r.s(); r.s(); return r.s()
        return {'_type':t,'size':size}
    if t=='ByteProperty':
        en=tn_inner(tn)[0]
        return r.u8() if en in ('None','?') else r.s()
    if t=='StructProperty':
        sname=tn_inner(tn)[0]
        return read_struct(r, sname, fmt, r.end, depth)
    if t in ('ArrayProperty','SetProperty'):
        it=tn_inner(tn)[0]
        if t=='SetProperty': r.i32()
        n=r.i32()
        if n<0 or n>5_000_000: raise ParseError('bad array count')
        if it in ('ObjectProperty','InterfaceProperty'): return [r.ref()[1] for _ in range(n)]
        if it=='IntProperty': return [r.i32() for _ in range(n)]
        if it in ('StrProperty','NameProperty','EnumProperty'): return [r.s() for _ in range(n)]
        if it=='StructProperty':
            if fmt=='old':
                r.s(); r.s(); r.i32(); r.i32(); sname=r.s(); r.skip(16); r.u8()
            else:
                inner=tn_inner(tn); sname=inner[1][0] if len(inner)>1 else '?'
            # each element up to end
            return [read_struct(r, sname, fmt, r.end, depth) for _ in range(n)]
        return {'_array_of':it,'count':n}
    return {'_type':t,'size':size}

# ---------------- body / levels ----------------
def _qnorm(d,o):
    try: x,y,z,w=struct.unpack_from('<4f',d,o); return abs(x*x+y*y+z*z+w*w-1.0)
    except Exception: return 9.0
def _actor_pre(r):
    # 1.0: int32 needTransform; newer builds: int32 flags + int32 needTransform. Pick the layout whose rotation quat is unit length.
    def score(p):
        sc=_qnorm(r.d,r.o+p)
        try:
            pl=struct.unpack_from('<i',r.d,r.o+p+40)[0]; sx=struct.unpack_from('<f',r.d,r.o+p+28)[0]
            if pl not in (0,1): sc+=5
            if not (-1e6<sx<1e6) or sx==0: sc+=1
        except Exception: sc+=9
        return sc
    a=score(4); b=score(8)
    if abs(a-b)<1e-3: return 4 if struct.unpack_from('<i',r.d,r.o)[0] in (0,1) and struct.unpack_from('<i',r.d,r.o+4)[0] not in (0,1) else 8
    return 8 if b<a else 4

# 0.4: actor positions for the network/volume extraction (only classes matching NET_POS)
_POS=None
NET_POS=re.compile(r'Build_(ConstructorMk1|AssemblerMk1|SmelterMk1|FoundryMk1|ManufacturerMk1|OilRefinery|Packager|Blender|HadronCollider|Converter|QuantumEncoder|DroneStation|TrainStation|TrainDockingStation|TrainDockingStationLiquid|TrainPlatformEmpty\w*)_C|Char_Player_C')
def note_pos(cls,path,d,o):
    try:
        if NET_POS.search(cls): _POS[path]=struct.unpack_from('<3f',d,o+16)
    except Exception: pass

def parse_object_headers(r, n, sv=99):
    hdrs=[]
    for _ in range(n):
        typ=r.i32()
        cls=r.s(); lvl=r.s(); path=r.s()
        if typ==1:
            # newer builds insert a 4-byte flags field before needTransform (which is 0/1)
            r.skip(4 if sv<=46 else _actor_pre(r))
            if _POS is not None: note_pos(cls,path,r.d,r.o)  # 0.4: position of network actors (pos = floats 4..6 of the transform)
            r.skip(40); r.i32()  # [flags+]needTransform, rot(4f) pos(3f) scale(3f), placedInLevel
            hdrs.append((1,cls,path))
        elif typ==0:
            n1=struct.unpack_from('<i',r.d,r.o)[0] if r.o+4<=r.end else 0
            if sv>46 and not (0<n1<5000 and r.d[r.o+4:r.o+5].isalpha()): r.skip(4)  # object flags (newer builds)
            r.s(); hdrs.append((0,cls,path))
        else: raise ParseError('unknown object header type %d at %d'%(typ,r.o))
    return hdrs

def parse_level(r, persistent, sv, want, out, warn):
    """Returns (name, headers, collected_refs). Fills out[path]=raw object bytes for wanted ones."""
    name = '' if persistent else r.s()
    if sv>=41: hsize=r.i64()
    else: hsize=r.i32()
    hstart=r.o
    n=r.i32(); hdrs=parse_object_headers(r,n,sv)
    collected=[]
    if r.o < hstart+hsize:
        c=r.i32(); collected=[r.ref()[1] for _ in range(c)]
    r.o=hstart+hsize
    osize=r.i64() if sv>=41 else r.i32(); ostart=r.o
    m=r.i32()
    if m!=len(hdrs): warn.append('level %s: %d headers vs %d objects'%(name or 'Persistent',len(hdrs),m))
    for i in range(min(m,len(hdrs))):
        osv=r.i32(); r.i32(); bs=r.i32(); st=r.o; r.skip(bs)
        # optional per-object trailer (1.1+ builds): int32 0, or int32 1 + custom-version block
        if r.o+4<=r.end:
            f=struct.unpack_from('<i',r.d,r.o)[0]
            if f==0: r.o+=4
            elif f==1 and r.d.find(b'++FactoryGame',r.o,r.o+80)>0: r.o+=4; read_version_block(r)
        typ,cls,path=hdrs[i]
        if want(cls,path): out.append((cls,path,typ,osv,st,bs))
    r.o=ostart+osize
    # tail: [int32 level save version (newer saves)] int32 collectable count + refs, [int32 versionFlag (+version block)]
    st=r.o
    try:
        a=r.i32()
        if not (30<=a<=200 and abs(a-sv)<=25): r.o=st
        c=r.i32()
        if c<0 or c>1_000_000: raise ParseError('bad collectables count')
        collected+=[r.ref()[1] for _ in range(c)]
    except ParseError:
        r.o=st; warn.append('level %s: collectables tail unreadable'%(name or 'Persistent'))
    if r.o+4<=r.end:
        f=struct.unpack_from('<i',r.d,r.o)[0]
        if f==0: r.o+=4
        elif f==1:
            r.o+=4; read_version_block(r)
            if r.o+4<=r.end and abs(struct.unpack_from('<i',r.d,r.o)[0]-sv)<=25: r.o+=4
    return name,hdrs,collected

def read_version_block(r):
    r.i32(); r.i32(); r.i32(); r.i32(); r.skip(6); r.u32(); br=r.s()
    c=r.i32()
    if c<0 or c>1000: raise ParseError('bad custom version count')
    r.skip(20*c); return br

def find_grids_start(b):
    # body = int64 size, [1.2+: versioning block], validation grids
    r=R(b,8)
    if b.find(b'++FactoryGame',8,400)>0:
        read_version_block(r)
    return r

def skip_grids(r):
    g=r.i32()
    for _ in range(g):
        r.s(); r.i32(); r.u32(); c=r.i32()
        for _ in range(c): r.s(); r.u32()

# ---------------- extraction ----------------
WANT_CLASSES = re.compile(r'(FGDroneStationInfo|FGTrainStationIdentifier|FGRailroadTimeTable|BP_Train_C|Build_(ConstructorMk1|AssemblerMk1|SmelterMk1|FoundryMk1|ManufacturerMk1|OilRefinery|Packager|Blender|HadronCollider|Converter|QuantumEncoder|TrainDockingStation|TrainDockingStationLiquid)_C|BP_GameState_C|BP_GamePhaseManager|schematicManager|ResearchManager|Char_Player_C|FGInventoryComponent|BP_DropPod|BP_WAT1|BP_WAT2|BP_Crystal|CentralStorage|RecipeManager)')
def want(cls,path):
    pl=path.lower()
    return bool(WANT_CLASSES.search(cls)) or pl.endswith(('.schematicmanager','.gamephasemanager','.researchmanager','.unlocksubsystem'))

def obj_props(b, cls, typ, st, bs, fmt_pref):
    end=st+bs
    errs=[]
    for fmt in fmt_pref:
        r=R(b,st,end)
        try:
            extra={}
            if typ==1:
                r.ref(); nc=r.i32(); extra['components']=[r.ref()[1] for _ in range(nc)]
            if fmt=='new': r.u8()
            p=read_props(r,fmt,end); p.update(extra); return p,fmt
        except Exception as e: errs.append('%s:%s'%(fmt,e))
    raise ParseError('; '.join(errs)[:200])

def short(p):
    if not isinstance(p,str): return p
    return p.rsplit('.',1)[-1]

def classify_schematic(name, path):
    n=name
    if '/Research/' in path or n.startswith('Research_'): return 'mam_research'
    if '/Alternate/' in path or n.startswith('Schematic_Alternate'): return 'alt_recipes'
    if '/Tutorial/' in path or 'Tutorial' in n or n=='Schematic_StartingRecipes_C': return 'tutorial'
    if re.match(r'Schematic_\d+-\d+', n): return 'milestones'
    if '/ResourceSinkShop' in path or 'ResourceSink' in path or n.startswith('ResourceSink_'): return 'awesome_shop'
    if '/Customizer' in path or 'Customizer' in n: return 'awesome_shop'
    return 'other'

def parse_save(path, opts=None):
    global _POS
    _POS={}
    t0=time.time(); warn=[]; snap={'file':os.path.basename(path),'parser_version':PARSER_VERSION,'status':'unparsed','warnings':warn}
    timing={}
    try:
        snap['file_size']=os.path.getsize(path); snap['file_mtime']=datetime.datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec='seconds')
        d=open(path,'rb').read()
    except Exception as e:
        warn.append('read failed: %s'%e); return snap
    try:
        r=R(d); h=parse_header(r); snap['header']=h; snap['status']='header_only'
    except Exception as e:
        warn.append('header parse failed: %s'%e); return snap
    sv=h.get('save_version',0)
    try:
        b=decompress(d, r.o, warn); timing['decompress']=round(time.time()-t0,2)
        snap['body_size']=len(b)
    except Exception as e:
        warn.append('decompress failed: %s'%e); return snap
    del d
    snap['status']='partial'
    counts=collections.Counter(); comp_counts=collections.Counter(); wanted=[]; collected=[]; seen_col=set(); sublevels=0
    if sv < 41: warn.append('save_version %d < 41 (pre-1.0 layout); body structure parse attempted but likely fails'%sv)
    try:
        r=find_grids_start(b); skip_grids(r)
        nl=r.i32()
        if nl<0 or nl>100000: raise ParseError('bad level count %d'%nl)
        for i in range(nl+1):
            name,hdrs,col=parse_level(r, i==nl, sv, want, wanted, warn)
            sublevels+=1; collected+=[x for x in col if x not in seen_col]; seen_col.update(col)
            for typ,cls,p in hdrs:
                (counts if typ==1 else comp_counts)[cls.rsplit('.',1)[-1]]+=1
        snap['status']='partial'
    except Exception as e:
        warn.append('level/object walk failed after %d levels: %s'%(sublevels,e))
    timing['walk']=round(time.time()-t0,2)
    snap['levels_parsed']=sublevels
    # buildings
    try:
        bld={k:v for k,v in counts.items() if k.startswith('Build_')}
        snap['buildings_by_class']=dict(sorted(bld.items(),key=lambda x:-x[1]))
        snap['buildings_friendly']={FRIENDLY[k]:v for k,v in bld.items() if k in FRIENDLY}
        snap['buildings_total']=sum(bld.values())
        snap['power_generators']={FRIENDLY.get(k,k):v for k,v in bld.items() if k in GENERATORS}
        snap['extractors']={FRIENDLY.get(k,k):v for k,v in bld.items() if k in EXTRACTORS}
        veh={k:v for k,v in counts.items() if VEHICLES.match(k)}
        snap['vehicles']=veh
        snap['logistics']={x:bld.get(k,0) for x,k in [('train_stations','Build_TrainStation_C'),('freight_platforms','Build_TrainDockingStation_C'),
            ('fluid_freight_platforms','Build_TrainDockingStationLiquid_C'),('rail_segments','Build_RailroadTrack_C'),('drone_ports','Build_DroneStation_C'),
            ('truck_stations','Build_TruckStation_C')]}
        snap['logistics']['locomotives']=sum(v for k,v in veh.items() if k.startswith('BP_Locomotive'))
        snap['logistics']['freight_wagons']=sum(v for k,v in veh.items() if k.startswith('BP_FreightWagon'))
        snap['logistics']['drones']=sum(v for k,v in veh.items() if k.startswith('BP_DroneTransport'))
        snap['other_actor_classes_top']=dict(collections.Counter({k:v for k,v in counts.items() if not k.startswith('Build_')}).most_common(25))
    except Exception as e: warn.append('building counts failed: %s'%e)
    # collected pickups (refs in collected/destroyed lists)
    try:
        cc=collections.Counter()
        for p in collected:
            s=p.rsplit('.',1)[-1]
            for key,pat in (('somersloop','BP_WAT1'),('mercer_sphere','BP_WAT2'),('hard_drive_droppod','BP_DropPod'),
                            ('power_slug_blue','BP_Crystal_C'),('power_slug_yellow','BP_Crystal_mk2'),('power_slug_purple','BP_Crystal_mk3')):
                if s.startswith(pat): cc[key]+=1
        snap['collected_refs']={'total_refs':len(collected), **cc}
        snap['uncollected_in_world']={k:v for k,v in counts.items() if re.match(r'BP_(WAT1|WAT2|Crystal)',k)}
    except Exception as e: warn.append('collectables failed: %s'%e)
    # object properties of interest
    fmt_pref=['new','old'] if sv>=53 else ['old','new']
    objs={}
    for cls,p,typ,osv,st,bs in wanted:
        objs.setdefault(cls.rsplit('.',1)[-1],[]).append((p,typ,st,bs))
    def props_of(clsname_pat, path_suffix=None):
        res=[]
        for c,lst in objs.items():
            if re.search(clsname_pat,c):
                for p,typ,st,bs in lst:
                    if path_suffix and not p.endswith(path_suffix): continue
                    try: res.append((p,obj_props(b,c,typ,st,bs,fmt_pref)[0]))
                    except Exception as e: warn.append('props %s: %s'%(short(p),e))
        return res
    try:
        purchased=[]
        for p,pr in props_of(r'BP_GameState_C|schematicManager|FGSchematicManager|BP_SchematicManager'):
            v=pr.get('mPurchasedSchematics')
            if isinstance(v,list) and len(v)>len(purchased): purchased=v
        cats=collections.defaultdict(list)
        for path in purchased:
            n=path.rsplit('.',1)[-1]; cats[classify_schematic(n,path)].append(n)
        snap['schematics']={'total':len(purchased),'counts':{k:len(v) for k,v in cats.items()},**{k:sorted(v) for k,v in cats.items()}}
        ms=cats.get('milestones',[])
        tiers=collections.Counter(int(re.match(r'Schematic_(\d+)',m).group(1)) for m in ms)
        snap['schematics']['milestones_by_tier']=dict(sorted(tiers.items()))
        snap['schematics']['max_tier']=max(tiers) if tiers else 0
        if not purchased: warn.append('mPurchasedSchematics not found')
    except Exception as e: warn.append('schematics failed: %s'%e)
    try:
        el={}
        for p,pr in props_of(r'BP_GameState_C|GamePhaseManager'):
            for k in ('mCurrentGamePhase','mTargetGamePhase'):
                if isinstance(pr.get(k),str): el[k[1:]]=short(pr[k])
            paid=pr.get('mTargetGamePhasePaidOffCosts')
            if isinstance(paid,list):
                items=[]
                for s in paid:
                    if isinstance(s,dict):
                        it=s.get('ItemClass'); amt=s.get('Amount')
                        if it is None and '_raw' in s:
                            m=re.search(rb'(Desc_\w+_C)',s['_raw']); it=m.group(1).decode() if m else None
                        items.append({'item':short(it),'amount':amt})
                el['paid_toward_target']=items
            if 'mGamePhase' in pr: el['legacy_game_phase']=pr['mGamePhase']
        snap['space_elevator']=el
    except Exception as e: warn.append('space elevator failed: %s'%e)
    try:
        players=[]
        comp_props={}
        inv_paths=set()
        pl=props_of(r'^Char_Player_C$')
        for p,pr in pl:
            for k in ('mInventory','mInventoryComponent'):
                if isinstance(pr.get(k),str): inv_paths.add(pr[k])
        if not inv_paths:
            inv_paths={c for p,pr in pl for c in pr.get('components',[]) if c.endswith('.inventory')}
        for p,pr in props_of(r'FGInventoryComponent'):
            if p in inv_paths:
                stacks=collections.Counter()
                for s in pr.get('mInventoryStacks',[]) or []:
                    if not isinstance(s,dict): continue
                    num=s.get('NumItems',0); item=s.get('Item'); name=None
                    if isinstance(item,dict):
                        raw=item.get('_raw',b'')
                        m=re.search(rb'(Desc_\w+_C|BP_\w+_C)',raw); name=m.group(1).decode() if m else None
                    if name and isinstance(num,int) and num>0: stacks[name]+=num
                players.append({'inventory_component':short(p),'items':dict(stacks.most_common()),
                                'special':{SPECIAL_ITEMS[k]:v for k,v in stacks.items() if k in SPECIAL_ITEMS}})
        snap['players']=players
        snap['player_count']=len(pl)
    except Exception as e: warn.append('player inventory failed: %s'%e)
    try:
        opened=0; total=0
        for p,pr in props_of(r'^BP_DropPod'):
            total+=1
            if pr.get('mHasBeenOpened'): opened+=1
        snap['drop_pods']={'actors':total,'opened':opened}
    except Exception as e: warn.append('drop pods failed: %s'%e)
    try:
        cn=collections.Counter()
        for p,pr in props_of(r'ResearchManager'):
            for k,v in pr.items():
                if isinstance(v,list): cn[k]=len(v)
        snap['research_manager_lists']=dict(cn)
    except Exception as e: warn.append('research manager failed: %s'%e)
    try:
        stored={}; rec=None; found=False
        for p,pr in props_of(r'CentralStorageSubsystem|RecipeManager'):
            if 'mStoredItems' in pr: found=True
            for s in pr.get('mStoredItems') or []:
                if isinstance(s,dict) and s.get('ItemClass'): stored[short(s['ItemClass'])]=s.get('Amount',0)
            if isinstance(pr.get('mAvailableRecipes'),list): rec=[short(x) for x in pr['mAvailableRecipes']]
        if found or counts.get('Build_CentralStorage_C',0): snap['depot']={'stored':stored,'uploaders':counts.get('Build_CentralStorage_C',0)}
        if rec is not None: snap['recipes']=rec
    except Exception as e: warn.append('depot/recipes failed: %s'%e)
    try: snap['network']=extract_network(objs,b,fmt_pref,_POS,warn)
    except Exception as e: warn.append('network failed: %s'%e)
    _POS=None
    timing['total']=round(time.time()-t0,2)
    snap['timing_s']=timing
    if timing['total']>20: warn.append('SLOW: %.1fs'%timing['total'])
    if snap['status']=='partial' and not [w for w in warn if 'failed' in w]: snap['status']='ok'
    return snap

# ---------------- 0.4: machines, drone/train network, depot-independent ----------------
PROD_CLS=re.compile(r'^Build_(ConstructorMk1|AssemblerMk1|SmelterMk1|FoundryMk1|ManufacturerMk1|OilRefinery|Packager|Blender|HadronCollider|Converter|QuantumEncoder)_C$')
def _inv(pr):
    c=collections.Counter()
    for s in (pr or {}).get('mInventoryStacks') or []:
        if not isinstance(s,dict): continue
        it=s.get('Item'); n=s.get('NumItems',0)
        raw=it.get('_raw',b'') if isinstance(it,dict) else b''
        m=re.search(rb'(Desc_\w+_C|BP_\w+_C)',raw) if isinstance(raw,(bytes,bytearray)) else None
        if m and isinstance(n,int) and n>0: c[m.group(1).decode()]+=n
    return dict(c)
def _xy(pos):
    return [round(pos[0]),round(pos[1]),round(pos[2])] if pos and all(v==v for v in pos) else None
def extract_network(objs,b,fmt_pref,pos,warn):
    """Production machines (recipe+position), drone ports, train stations, freight platforms, timetables, player position.
    Additive snapshot field 'network'; positions need the 0.4 header capture (None when unavailable)."""
    pos=pos or {}
    def each(pat):
        for c,lst in objs.items():
            if re.search(pat,c):
                for p,typ,st,bs in lst:
                    try: yield c,p,obj_props(b,c,typ,st,bs,fmt_pref)[0]
                    except Exception as e: warn.append('network props %s: %s'%(short(p),str(e)[:80]))
    inv_want=re.compile(r'(Build_DroneStation_C_\d+\.(InputInventory|OutputInventory)|Build_TrainDockingStation(Liquid)?_C_\d+\.inventory)$')
    inv={}
    for c,lst in objs.items():
        if c!='FGInventoryComponent': continue
        for p,typ,st,bs in lst:
            if inv_want.search(p):
                try: inv[p]=_inv(obj_props(b,c,typ,st,bs,fmt_pref)[0])
                except Exception as e: warn.append('network inv %s: %s'%(short(p),str(e)[:80]))
    machines=[]
    for c,p,pr in each(r'^Build_(ConstructorMk1|AssemblerMk1|SmelterMk1|FoundryMk1|ManufacturerMk1|OilRefinery|Packager|Blender|HadronCollider|Converter|QuantumEncoder)_C$'):
        machines.append([c[6:-2],short(pr.get('mCurrentRecipe')) or None,_xy(pos.get(p))])
    dinfo={p:pr for c,p,pr in each(r'^FGDroneStationInfo$')}
    drones=[]
    for ip,pr in dinfo.items():
        st=pr.get('mStation') or ''; pair=pr.get('mPairedStation')
        drones.append({'name':pr.get('mBuildingTag'),'path':short(st),'pos':_xy(pos.get(st)),
                       'paired_to':(dinfo.get(pair) or {}).get('mBuildingTag') if pair else None,
                       'input':inv.get(st+'.InputInventory',{}),'output':inv.get(st+'.OutputInventory',{})})
    ident={p:pr for c,p,pr in each(r'^FGTrainStationIdentifier$')}
    stations=[{'name':pr.get('mStationName') if isinstance(pr.get('mStationName'),str) else None,'path':short(pr.get('mStation')),
               'id':short(p),'pos':_xy(pos.get(pr.get('mStation') or ''))} for p,pr in ident.items()]
    trains={}
    for c,p,pr in each(r'^BP_Train_C$'):
        if pr.get('TimeTable'): trains[pr['TimeTable']]=pr.get('mTrainName') if isinstance(pr.get('mTrainName'),str) else short(p)
    tts=[]
    for c,p,pr in each(r'^FGRailroadTimeTable$'):
        stops=[]
        for s in pr.get('mStops') or []:
            if not isinstance(s,dict): continue
            rs=s.get('DockingRuleSet') or {}
            stops.append({'station':short(s.get('Station')),'load':[short(x) for x in rs.get('LoadFilterDescriptors') or [] if isinstance(x,str)],
                          'unload':[short(x) for x in rs.get('UnloadFilterDescriptors') or [] if isinstance(x,str)],
                          'rule':str(rs.get('DockingDefinition') or '').split('::')[-1]})
        tts.append({'train':trains.get(p),'stops':stops})
    plats=[]
    for c,p,pr in each(r'^Build_TrainDockingStation(Liquid)?_C$'):
        plats.append({'kind':c[6:-2],'path':short(p),'pos':_xy(pos.get(p)),'load_mode':pr.get('mIsInLoadMode',True) is not False,'items':inv.get(p+'.inventory',{})})
    for p,xyz in pos.items():
        if 'TrainPlatformEmpty' in p: plats.append({'kind':'TrainPlatformEmpty','path':short(p),'pos':_xy(xyz),'load_mode':None,'items':{}})
    player=next((_xy(v) for k,v in pos.items() if 'Char_Player_C' in k),None)
    return {'version':1,'positions':bool(pos),'machines':machines,'drone_ports':drones,'train_stations':stations,
            'platforms':plats,'timetables':tts,'player_pos':player}

# ---------------- timeline ----------------
KEY_BUILD=['Constructor','Assembler','Manufacturer','Smelter','Foundry','Refinery','Blender','Packager','Particle Accelerator','Converter',
  'Quantum Encoder','Miner Mk1','Miner Mk2','Miner Mk3','Oil Extractor','Water Extractor','Resource Well Extractor','Fuel Generator',
  'Coal Generator','Geothermal Generator','Nuclear Power Plant','Power Storage','Train Station','Drone Port','AWESOME Sink','Dimensional Depot']

def load_snaps(src):
    snaps=[]
    if os.path.isdir(src):
        for f in sorted(glob.glob(os.path.join(src,'*.json'))):
            if os.path.basename(f).startswith('timeline'): continue
            try: snaps.append(json.load(open(f,encoding='utf-8')))
            except Exception as e: print('skip',f,e,file=sys.stderr)
    else:
        for line in open(src,encoding='utf-8'):
            if line.strip(): snaps.append(json.loads(line))
    def key(s):
        h=s.get('header') or {}
        return (h.get('save_datetime_utc_guess') or s.get('file_mtime') or '', s.get('file'))
    return sorted(snaps,key=key)

def timeline(src, outdir):
    snaps=load_snaps(src); os.makedirs(outdir,exist_ok=True)
    rows=[]; diffs=[]; prev=None
    for s in snaps:
        h=s.get('header') or {}; sc=s.get('schematics') or {}; el=s.get('space_elevator') or {}; bf=s.get('buildings_friendly') or {}
        row={'file':s.get('file'),'status':s.get('status'),'save_datetime':h.get('save_datetime_utc_guess'),'session':h.get('session_name'),
             'save_version':h.get('save_version'),'build':h.get('build'),'play_hours':round(h.get('play_seconds',0)/3600,2) if h.get('play_seconds') else None,
             'schematics_total':sc.get('total'),'milestones':(sc.get('counts') or {}).get('milestones'),'max_tier':sc.get('max_tier'),
             'mam_research':(sc.get('counts') or {}).get('mam_research'),'alt_recipes':(sc.get('counts') or {}).get('alt_recipes'),
             'awesome_shop':(sc.get('counts') or {}).get('awesome_shop'),'elevator_phase':el.get('CurrentGamePhase'),
             'buildings_total':s.get('buildings_total')}
        for k in KEY_BUILD: row[k]=bf.get(k,0) if s.get('buildings_friendly') is not None else None
        if s.get('status')!='ok' and not sc.get('total'):
            # header-only stand-in (e.g. v46 snapshots made by parser 0.1): counts unknown, not zero
            for k in list(row):
                if k not in ('file','status','save_datetime','session','save_version','build','play_hours'): row[k]=None
            row['standin']=True
        else: row['standin']=False
        rows.append(row)
        if prev is not None:
            ps=prev.get('schematics') or {}; dd={'file':s.get('file'),'prev_file':prev.get('file'),'save_datetime':row['save_datetime']}
            new=[]; 
            for cat in ('milestones','mam_research','alt_recipes','awesome_shop','tutorial','other'):
                a=set(sc.get(cat,[]) or []); bb=set(ps.get(cat,[]) or [])
                if a-bb: new.append({cat:sorted(a-bb)})
            if new: dd['new_schematics']=new
            pb=prev.get('buildings_by_class') or {}; cb=s.get('buildings_by_class') or {}
            if pb and cb:
                delta={FRIENDLY.get(k,k):cb.get(k,0)-pb.get(k,0) for k in set(pb)|set(cb) if cb.get(k,0)!=pb.get(k,0)}
                if delta: dd['building_deltas']=dict(sorted(delta.items(),key=lambda x:-abs(x[1]))[:40])
            pe=prev.get('space_elevator') or {}
            if el!=pe: dd['elevator']={'from':pe,'to':el}
            if (prev.get('header') or {}).get('session_name')!=h.get('session_name'): dd['session_changed']=True
            diffs.append(dd)
        prev=s
    import csv
    with open(os.path.join(outdir,'timeline.csv'),'w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0].keys()) if rows else ['file']); w.writeheader(); w.writerows(rows)
    json.dump({'rows':rows,'diffs':diffs},open(os.path.join(outdir,'timeline.json'),'w',encoding='utf-8'),indent=1)
    print('timeline: %d snapshots -> %s'%(len(rows),outdir))

def main():
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('src'); ap.add_argument('--out',required=True)
    ap.add_argument('--timeline',action='store_true'); ap.add_argument('--pattern',default='*.sav')
    ap.add_argument('--skip-existing',action='store_true',help='skip saves whose snapshot json already exists (dir output)')
    a=ap.parse_args()
    if a.timeline: return timeline(a.src,a.out)
    files=[a.src] if os.path.isfile(a.src) else sorted(glob.glob(os.path.join(a.src,a.pattern)),key=os.path.getmtime)
    jsonl=a.out.lower().endswith('.jsonl')
    if not jsonl: os.makedirs(a.out,exist_ok=True)
    jf=open(a.out,'a',encoding='utf-8') if jsonl else None
    for i,f in enumerate(files):
        outp=None if jsonl else os.path.join(a.out,os.path.splitext(os.path.basename(f))[0]+'.json')
        if outp and a.skip_existing and os.path.exists(outp): continue
        try: s=parse_save(f)
        except Exception as e: s={'file':os.path.basename(f),'status':'unparsed','warnings':['fatal: %s'%traceback.format_exc()[-500:]]}
        txt=json.dumps(s,separators=(',',':'),default=str)
        if jf: jf.write(txt+'\n')
        else: open(outp,'w',encoding='utf-8').write(txt)
        print('[%d/%d] %s status=%s %.1fs %dKB warnings=%d'%(i+1,len(files),os.path.basename(f),s.get('status'),
              (s.get('timing_s') or {}).get('total',0),len(txt)//1024,len(s.get('warnings',[]))),flush=True)
    if jf: jf.close()

if __name__=='__main__': main()
