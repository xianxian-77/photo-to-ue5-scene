"""Layer-2 reveal mechanism -- consolidated (seduce-then-reveal + original-photo compare).
================================================================================
The SINGLE reveal module left after consolidating all iterations; self-contained and reproducible.
Exec the whole file inside the UE5.7 editor (or `python ue_remote_bridge.py ue_scene_reveal.py`): it builds all
materials, writes the stencils, and arms the key listener.
Prerequisite: the scene is already built with ue_scene_builder (the seductive state). VR/packaged builds
need a Blueprint instead (see dev/BP_Reveal_recipe.md).

Controls (editor viewport; hold RMB + WASD to fly; do NOT press Play):
  R = next stage (fade transition)   beauty -> desaturated -> line art (structure lines) -> red truth (fabrication red + blue outline) -> back to beauty
  P = autoplay
  O = original-photo compare (jump to the photo camera + overlay the original photo; press again to remove it and view the reconstruction from the same camera)
  Stop: unreal.unregister_slate_post_tick_callback(unreal._REVEAL_STATE['handle'])

================================================================================
Gotchas (READ before editing this file; do not repeat them):
  1. unreal.Rotator positional argument order is (roll,pitch,yaw) -- always use keywords Rotator(pitch=,yaw=,roll=).
  2. The UV input pin on SceneTexture / TextureSample is named "UVs" (not "UV"; a wrong name fails silently, no error).
  3. Transitions invisible = viewport realtime is off -> arm must call editor_set_viewport_realtime(True).
  4. Never use PPV color_gain for a dip-to-dark transition -- a snapshot taken at the darkened value gets
     multiplied into full black and never recovers. Use post-process-material WEIGHT cross-fades only and
     never touch object materials -> blackout becomes structurally impossible.
  5. EditorAssetLibrary.delete_asset is unreliable (fails silently while the asset is referenced/loaded) ->
     stop the listener to release references before deleting.
  6. Inside PIE (Play) the slate tick cannot reach the game world (get_game_world wrongly returns the editor
     world, find_object is flaky) -> this scheme works in the EDITOR VIEWPORT only.
  7. The sky dome actor label is "AutoSky" (the distant skyline is "AutoSkyline" -- must not be excluded by
     mistake); the sky gets no stencil -> the red stage keeps the original dark night sky, so red objects
     stand out against a dark background (otherwise the whole screen is flat dead red).
  8. Use flat dead red (no luminance-driven transparency); the "fabricated" verdict comes from the pipeline's
     own record: the imagined flags in result.json (Gemini grounded fabrication) + Auto* procedural
     background (hallucinations the pipeline itself logged).
================================================================================
"""
import unreal, json, os, ctypes

# ============================ Config ============================
ROOT = r"C:\Users\Strix\Desktop\毕业设计\python-kana"
def _tid_from_level():
    """Recover the scene tid from actor asset paths /Game/Auto*/<tid>/ in the current level.
    Level content is authoritative -> running ue_scene_reveal.py directly recognizes the current scene, and a stale
    UE_REVEAL_TID left over from a previous, different scene cannot mislead it."""
    import re as _re
    try:
        _eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        _cnt = {}
        for _a in _eas.get_all_level_actors():
            for _c in (_a.get_components_by_class(unreal.StaticMeshComponent) or []):
                _paths = []
                _sm = _c.static_mesh
                if _sm: _paths.append(_sm.get_path_name())
                for _mi in (_c.get_materials() or []):
                    if _mi: _paths.append(_mi.get_path_name())
                for _p in _paths:
                    _m = _re.search(r"/Game/Auto\w*/([0-9a-fA-F]{8,})/", _p)
                    if _m: _cnt[_m.group(1)] = _cnt.get(_m.group(1), 0) + 1
        if _cnt: return max(_cnt, key=_cnt.get)
    except Exception as _e:
        unreal.log_warning("ue_scene_reveal.py tid auto-detect failed: %s" % _e)
    return None

# TID resolution order: current level content (the O-overlay photo / stats panel must match the viewport) -> UE_REVEAL_TID fallback -> explicit error.
# NEVER silently default to one scene -- the old default (street 1d8f4d43186b) silently showed the street photo in other scenes whenever env was unset; removed.
TID  = _tid_from_level() or os.environ.get("UE_REVEAL_TID")
if not TID:
    raise RuntimeError(
        "ue_scene_reveal.py 无法确定场景 tid: 当前关卡没有 /Game/Auto*/<tid>/ 资产, 且未设 UE_REVEAL_TID。"
        " 请在已用 ue_scene_builder 建好的场景关卡里运行(或显式设 UE_REVEAL_TID)。")
KEY_NEXT  = 0x52    # R
KEY_AUTO  = 0x50    # P
KEY_PHOTO = 0x4F    # O
KEY_INFO  = 0x49    # I = evidence view (full-screen classification: fabricated red / real green / sky blue + top-left "fabrication ratio" panel)
DUR  = 1.2          # transition duration (s)
HOLD = 2.6          # autoplay hold per stage (s)
PX   = 1.5          # line-art sample offset in pixels
K_LINE = 0.25       # line-art edge sensitivity
ASP_PHOTO = 0.6667  # photo width/height (portrait)
SCALE_PHOTO = (16.0 / 9.0) / ASP_PHOTO          # horizontal scale to center the portrait photo on a 16:9 screen with black side bars
LINE_COLOR = unreal.LinearColor(0.55, 1.7, 2.1, 1.0)
RED_COLOR  = unreal.LinearColor(2.6, 0.04, 0.05, 1.0)
BLUE_OUTLINE = unreal.LinearColor(0.05, 0.6, 5.0, 1.0)
FAB = ("Auto",)   # generic: every Auto* procedural actor = fabricated (sky dome AutoSky excluded separately in arm); scene-agnostic so nothing is missed (e.g. the forest AutoFx_Water lake)
VIS = FAB + ("OBJ_",)
# asset paths (canonical names)
M_LINE_P  = "/Game/Reveal/M_RevealLine"
M_RED_P   = "/Game/Reveal/M_RevealRed"
M_PHOTO_P = "/Game/Reveal/M_Photo_" + TID    # photo assets isolated per tid: no cross-scene leakage, and each keeps its correct aspect (street portrait 0.667 / forest 0.8)
T_PHOTO_P = "/Game/Reveal/T_Photo_" + TID
M_CLS_P   = "/Game/Reveal/M_RevealClassify"   # classification view (fabricated red / real green / sky blue)
M_STAT_P  = "/Game/Reveal/M_Stat_" + TID      # stats panel isolated per tid too (each scene keeps its own numbers, no cross-scene leakage); text only on a transparent background
T_STAT_P  = "/Game/Reveal/T_Stat_" + TID
ASP_STAT  = 720.0 / 300.0                      # panel image width/height
# =============================================================

ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
mel = unreal.MaterialEditingLibrary
af  = unreal.AssetToolsHelpers.get_asset_tools()
EAL = unreal.EditorAssetLibrary

_data = json.load(open(os.path.join(ROOT, "output", TID, "result.json"), encoding="utf-8"))
IMAG = set("OBJ_%02d" % (o.get("id", 0)) for o in (_data.get("results") or []) if o.get("imagined"))
PHOTO_FN = os.path.basename(_data.get("original_url") or ("%s_street.png" % TID))   # original photo filename (_street / _forest ... per scene)
ASP_PHOTO = float(_data.get("img_aspect") or ASP_PHOTO)                              # photo width/height (per scene)
SCALE_PHOTO = (16.0 / 9.0) / ASP_PHOTO
_cam = _data.get("camera") or {}
# some scenes' solved photo-camera z is buried in the built terrain (e.g. forest lake: z=160 is below the waterline) -> per-tid override to a viewable camera
CAM_OVERRIDE = {"71dd52cba668": dict(loc=(4300.0, -130.0, 780.0), pitch=-4.0, yaw=180.0)}
if TID in CAM_OVERRIDE:
    _ov = CAM_OVERRIDE[TID]; AUTOCAM_LOC = _ov["loc"]; AUTOCAM_ROT = dict(pitch=_ov["pitch"], yaw=_ov["yaw"], roll=0.0)
else:
    AUTOCAM_LOC = (0.0, 0.0, _cam.get("height_m", 1.65) * 100.0)
    AUTOCAM_ROT = dict(pitch=_cam.get("pitch_deg", -8.0), yaw=0.0, roll=0.0)   # keyword args, always!
def _is_fab(l): return l.startswith(FAB) or l in IMAG


# ============================ Material building ============================
def _N(mat, cls, x, y): return mel.create_material_expression(mat, cls, x, y)
def _C(a, ao, b, bi): mel.connect_material_expressions(a, ao, b, bi)
def _mask(mat, node, x, y, r, g, b, a):
    m = _N(mat, unreal.MaterialExpressionComponentMask, x, y)
    for ch, v in (("r", r), ("g", g), ("b", b), ("a", a)): m.set_editor_property(ch, v)
    _C(node[0], node[1], m, ""); return m
def _const(mat, val, x, y, three=True):
    c = _N(mat, unreal.MaterialExpressionConstant3Vector if three else unreal.MaterialExpressionConstant, x, y)
    c.set_editor_property("constant" if three else "r", val); return c
def _clamp01(mat, src, x, y):
    cl = _N(mat, unreal.MaterialExpressionClamp, x, y); _C(src[0], src[1], cl, "")
    cl.set_editor_property("min_default", 0.0); cl.set_editor_property("max_default", 1.0); return cl
def _newmat(path, domain_pp=True):
    name = path.rsplit("/", 1)[1]; folder = path.rsplit("/", 1)[0]
    mat = af.create_asset(name, folder, unreal.Material, unreal.MaterialFactoryNew())
    if domain_pp: mat.set_editor_property("material_domain", unreal.MaterialDomain.MD_POST_PROCESS)
    return mat


def _build_line(path):
    """Line art: second-order difference of scene depth (multi-tap; geometric structure lines/silhouettes; flat surfaces = 0, no triangle wireframe). Dark base + bright lines."""
    mat = _newmat(path)
    cST = _N(mat, unreal.MaterialExpressionSceneTexture, -1600, -100); cST.set_editor_property("scene_texture_id", unreal.SceneTextureId.PPI_SCENE_DEPTH)
    dC = _mask(mat, (cST, "Color"), -1430, -100, 1, 0, 0, 0)
    tc = _N(mat, unreal.MaterialExpressionTextureCoordinate, -1600, 220)
    c2x = _N(mat, unreal.MaterialExpressionConstant2Vector, -1430, 140); c2x.set_editor_property("r", PX); c2x.set_editor_property("g", 0.0)
    c2y = _N(mat, unreal.MaterialExpressionConstant2Vector, -1430, 320); c2y.set_editor_property("r", 0.0); c2y.set_editor_property("g", PX)
    offX = _N(mat, unreal.MaterialExpressionMultiply, -1250, 160); _C(cST, "InvSize", offX, "A"); _C(c2x, "", offX, "B")
    offY = _N(mat, unreal.MaterialExpressionMultiply, -1250, 320); _C(cST, "InvSize", offY, "A"); _C(c2y, "", offY, "B")
    uvR = _N(mat, unreal.MaterialExpressionAdd, -1080, 120); _C(tc, "", uvR, "A"); _C(offX, "", uvR, "B")
    uvL = _N(mat, unreal.MaterialExpressionSubtract, -1080, 220); _C(tc, "", uvL, "A"); _C(offX, "", uvL, "B")
    uvU = _N(mat, unreal.MaterialExpressionSubtract, -1080, 320); _C(tc, "", uvU, "A"); _C(offY, "", uvU, "B")
    uvD = _N(mat, unreal.MaterialExpressionAdd, -1080, 420); _C(tc, "", uvD, "A"); _C(offY, "", uvD, "B")
    def depth_at(uv, y):
        st = _N(mat, unreal.MaterialExpressionSceneTexture, -900, y); st.set_editor_property("scene_texture_id", unreal.SceneTextureId.PPI_SCENE_DEPTH)
        _C(uv, "", st, "UVs")                                   # note: "UVs"
        return _mask(mat, (st, "Color"), -730, y, 1, 0, 0, 0)
    dR = depth_at(uvR, 120); dL = depth_at(uvL, 220); dU = depth_at(uvU, 320); dD = depth_at(uvD, 420)
    twoC = _N(mat, unreal.MaterialExpressionMultiply, -560, -100); _C(dC, "", twoC, "A"); twoC.set_editor_property("const_b", 2.0)
    sumX = _N(mat, unreal.MaterialExpressionAdd, -560, 120); _C(dL, "", sumX, "A"); _C(dR, "", sumX, "B")
    sdX = _N(mat, unreal.MaterialExpressionSubtract, -400, 120); _C(sumX, "", sdX, "A"); _C(twoC, "", sdX, "B")
    aX = _N(mat, unreal.MaterialExpressionAbs, -240, 120); _C(sdX, "", aX, "")
    sumY = _N(mat, unreal.MaterialExpressionAdd, -560, 360); _C(dU, "", sumY, "A"); _C(dD, "", sumY, "B")
    sdY = _N(mat, unreal.MaterialExpressionSubtract, -400, 360); _C(sumY, "", sdY, "A"); _C(twoC, "", sdY, "B")
    aY = _N(mat, unreal.MaterialExpressionAbs, -240, 360); _C(sdY, "", aY, "")
    sd = _N(mat, unreal.MaterialExpressionAdd, -80, 240); _C(aX, "", sd, "A"); _C(aY, "", sd, "B")
    rel = _N(mat, unreal.MaterialExpressionDivide, 80, 240); _C(sd, "", rel, "A"); _C(dC, "", rel, "B")
    bo = _N(mat, unreal.MaterialExpressionMultiply, 240, 240); _C(rel, "", bo, "A"); bo.set_editor_property("const_b", K_LINE)
    edge = _clamp01(mat, (bo, ""), 400, 240)
    sST = _N(mat, unreal.MaterialExpressionSceneTexture, 80, 560); sST.set_editor_property("scene_texture_id", unreal.SceneTextureId.PPI_POST_PROCESS_INPUT0)
    sm = _mask(mat, (sST, "Color"), 260, 560, 1, 1, 1, 0)
    dark = _N(mat, unreal.MaterialExpressionMultiply, 430, 560); _C(sm, "", dark, "A"); dark.set_editor_property("const_b", 0.13)
    line = _const(mat, LINE_COLOR, 430, 720)
    lp = _N(mat, unreal.MaterialExpressionLinearInterpolate, 640, 400); _C(dark, "", lp, "A"); _C(line, "", lp, "B"); _C(edge, "", lp, "Alpha")
    mel.connect_material_property(lp, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    mel.recompile_material(mat); return mat


def _build_red(path):
    """Red truth: stencil==1 (fabricated) painted flat red + DDX/DDY blue outer outline; real (stencil 2) / sky (stencil 0) stay as-is."""
    mat = _newmat(path)
    st = _N(mat, unreal.MaterialExpressionSceneTexture, -1200, -200); st.set_editor_property("scene_texture_id", unreal.SceneTextureId.PPI_CUSTOM_STENCIL)
    sm = _mask(mat, (st, "Color"), -1020, -200, 1, 0, 0, 0)
    sub1 = _N(mat, unreal.MaterialExpressionSubtract, -860, -200); _C(sm, "", sub1, "A"); sub1.set_editor_property("const_b", 1.0)
    ab = _N(mat, unreal.MaterialExpressionAbs, -720, -200); _C(sub1, "", ab, "")
    cl = _clamp01(mat, (ab, ""), -580, -200)
    M1 = _N(mat, unreal.MaterialExpressionSubtract, -440, -200); M1.set_editor_property("const_a", 1.0); _C(cl, "", M1, "B")  # 1-saturate(|S-1|)
    ddx = _N(mat, unreal.MaterialExpressionDDX, -280, -260); ddy = _N(mat, unreal.MaterialExpressionDDY, -280, -140)
    _C(M1, "", ddx, ""); _C(M1, "", ddy, "")
    axx = _N(mat, unreal.MaterialExpressionAbs, -140, -260); ayy = _N(mat, unreal.MaterialExpressionAbs, -140, -140)
    _C(ddx, "", axx, ""); _C(ddy, "", ayy, "")
    ed = _N(mat, unreal.MaterialExpressionAdd, 0, -200); _C(axx, "", ed, "A"); _C(ayy, "", ed, "B")
    bo = _N(mat, unreal.MaterialExpressionMultiply, 140, -200); _C(ed, "", bo, "A"); bo.set_editor_property("const_b", 12.0)
    omask = _clamp01(mat, (bo, ""), 280, -200)
    sc = _N(mat, unreal.MaterialExpressionSceneTexture, -280, 200); sc.set_editor_property("scene_texture_id", unreal.SceneTextureId.PPI_POST_PROCESS_INPUT0)
    scm = _mask(mat, (sc, "Color"), -100, 200, 1, 1, 1, 0)
    red = _const(mat, RED_COLOR, -100, 360)
    base = _N(mat, unreal.MaterialExpressionLinearInterpolate, 140, 240); _C(scm, "", base, "A"); _C(red, "", base, "B"); _C(M1, "", base, "Alpha")
    blue = _const(mat, BLUE_OUTLINE, 140, 420)
    fin = _N(mat, unreal.MaterialExpressionLinearInterpolate, 400, 120); _C(base, "", fin, "A"); _C(blue, "", fin, "B"); _C(omask, "", fin, "Alpha")
    mel.connect_material_property(fin, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    mel.recompile_material(mat); return mat


def _build_photo(path, tex):
    """Photo overlay: show the original photo full-screen (portrait images centered at their aspect ratio, black bars at the sides)."""
    mat = _newmat(path)
    tc = _N(mat, unreal.MaterialExpressionTextureCoordinate, -1200, -100)
    uvX = _mask(mat, (tc, ""), -1020, -160, 1, 0, 0, 0)
    uvY = _mask(mat, (tc, ""), -1020, -40, 0, 1, 0, 0)
    cen = _N(mat, unreal.MaterialExpressionSubtract, -680, -160); _C(uvX, "", cen, "A"); cen.set_editor_property("const_b", 0.5)
    scl = _N(mat, unreal.MaterialExpressionMultiply, -520, -100); _C(cen, "", scl, "A"); scl.set_editor_property("const_b", SCALE_PHOTO)
    pU = _N(mat, unreal.MaterialExpressionAdd, -360, -100); _C(scl, "", pU, "A"); pU.set_editor_property("const_b", 0.5)
    app = _N(mat, unreal.MaterialExpressionAppendVector, -200, -60); _C(pU, "", app, "A"); _C(uvY, "", app, "B")
    sm = _N(mat, unreal.MaterialExpressionTextureSample, -20, -60); sm.set_editor_property("texture", tex); _C(app, "", sm, "UVs")  # "UVs"
    rgb = _mask(mat, (sm, ""), 200, -60, 1, 1, 1, 0)
    m1 = _N(mat, unreal.MaterialExpressionMultiply, -200, 160); _C(pU, "", m1, "A"); m1.set_editor_property("const_b", 1000.0)
    m1c = _clamp01(mat, (m1, ""), -60, 160)
    inv = _N(mat, unreal.MaterialExpressionSubtract, -200, 280); inv.set_editor_property("const_a", 1.0); _C(pU, "", inv, "B")
    m2 = _N(mat, unreal.MaterialExpressionMultiply, -60, 280); _C(inv, "", m2, "A"); m2.set_editor_property("const_b", 1000.0)
    m2c = _clamp01(mat, (m2, ""), 100, 280)
    mask = _N(mat, unreal.MaterialExpressionMultiply, 260, 220); _C(m1c, "", mask, "A"); _C(m2c, "", mask, "B")
    out = _N(mat, unreal.MaterialExpressionMultiply, 440, 60); _C(rgb, "", out, "A"); _C(mask, "", out, "B")
    mel.connect_material_property(out, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    mel.recompile_material(mat); return mat


def _build_classify(path):
    """Classification view: stencil 1 -> red (fabricated), stencil 2 -> green (real), stencil 0 -> blue (sky/background). Feeds the evidence view + the fabrication-ratio stats."""
    mat = _newmat(path)
    st = _N(mat, unreal.MaterialExpressionSceneTexture, -900, 0); st.set_editor_property("scene_texture_id", unreal.SceneTextureId.PPI_CUSTOM_STENCIL)
    sm = _mask(mat, (st, "Color"), -720, 0, 1, 0, 0, 0)
    def eqmask(val, y):
        s1 = _N(mat, unreal.MaterialExpressionSubtract, -560, y); _C(sm, "", s1, "A"); s1.set_editor_property("const_b", float(val))
        ab = _N(mat, unreal.MaterialExpressionAbs, -420, y); _C(s1, "", ab, "")
        cl = _clamp01(mat, (ab, ""), -280, y)
        m = _N(mat, unreal.MaterialExpressionSubtract, -140, y); m.set_editor_property("const_a", 1.0); _C(cl, "", m, "B")
        return m
    M1 = eqmask(1, -120); M2 = eqmask(2, 120)
    red = _const(mat, unreal.LinearColor(2, 0, 0, 1), 60, -200); grn = _const(mat, unreal.LinearColor(0, 2, 0, 1), 60, 300); blu = _const(mat, unreal.LinearColor(0, 0, 2, 1), 60, 460)
    r1 = _N(mat, unreal.MaterialExpressionLinearInterpolate, 260, 200); _C(blu, "", r1, "A"); _C(red, "", r1, "B"); _C(M1, "", r1, "Alpha")
    r2 = _N(mat, unreal.MaterialExpressionLinearInterpolate, 460, 100); _C(r1, "", r2, "A"); _C(grn, "", r2, "B"); _C(M2, "", r2, "Alpha")
    mel.connect_material_property(r2, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    mel.recompile_material(mat); return mat


def _build_stat(path, tex):
    """Top-left badge: composite the "fabrication ratio" panel image (with alpha) into the top-left corner of the screen (hardcoded 16:9 box, no distortion)."""
    BX0, BY0 = 0.02, 0.03; BW = 0.28; BH = BW * (16.0 / 9.0) / ASP_STAT   # box: top-left, width 0.28, height by aspect
    mat = _newmat(path)
    tc = _N(mat, unreal.MaterialExpressionTextureCoordinate, -1100, -100)
    uvX = _mask(mat, (tc, ""), -920, -160, 1, 0, 0, 0)
    uvY = _mask(mat, (tc, ""), -920, -40, 0, 1, 0, 0)
    tU = _N(mat, unreal.MaterialExpressionSubtract, -740, -160); _C(uvX, "", tU, "A"); tU.set_editor_property("const_b", BX0)
    tU2 = _N(mat, unreal.MaterialExpressionMultiply, -600, -160); _C(tU, "", tU2, "A"); tU2.set_editor_property("const_b", 1.0 / BW)
    tV = _N(mat, unreal.MaterialExpressionSubtract, -740, -40); _C(uvY, "", tV, "A"); tV.set_editor_property("const_b", BY0)
    tV2 = _N(mat, unreal.MaterialExpressionMultiply, -600, -40); _C(tV, "", tV2, "A"); tV2.set_editor_property("const_b", 1.0 / BH)
    app = _N(mat, unreal.MaterialExpressionAppendVector, -440, -100); _C(tU2, "", app, "A"); _C(tV2, "", app, "B")
    smp = _N(mat, unreal.MaterialExpressionTextureSample, -260, -100); smp.set_editor_property("texture", tex); _C(app, "", smp, "UVs")
    rgb = _mask(mat, (smp, ""), -60, -160, 1, 1, 1, 0)
    def inb(u, y):       # u in (0,1)
        a = _N(mat, unreal.MaterialExpressionMultiply, -440, y); _C(u, "", a, "A"); a.set_editor_property("const_b", 1000.0)
        ac = _clamp01(mat, (a, ""), -300, y)
        iv = _N(mat, unreal.MaterialExpressionSubtract, -440, y + 60); iv.set_editor_property("const_a", 1.0); _C(u, "", iv, "B")
        b = _N(mat, unreal.MaterialExpressionMultiply, -300, y + 60); _C(iv, "", b, "A"); b.set_editor_property("const_b", 1000.0)
        bc = _clamp01(mat, (b, ""), -160, y + 60)
        m = _N(mat, unreal.MaterialExpressionMultiply, -20, y + 30); _C(ac, "", m, "A"); _C(bc, "", m, "B"); return m
    mU = inb(tU2, 120); mV = inb(tV2, 300)
    box = _N(mat, unreal.MaterialExpressionMultiply, 160, 220); _C(mU, "", box, "A"); _C(mV, "", box, "B")   # inside box = 1
    a2 = _N(mat, unreal.MaterialExpressionMultiply, 320, 120); _C(smp, "A", a2, "A"); _C(box, "", a2, "B")   # text alpha (the "A" output) * box -> text only
    sc = _N(mat, unreal.MaterialExpressionSceneTexture, 160, 460); sc.set_editor_property("scene_texture_id", unreal.SceneTextureId.PPI_POST_PROCESS_INPUT0)
    scm = _mask(mat, (sc, "Color"), 340, 460, 1, 1, 1, 0)
    out = _N(mat, unreal.MaterialExpressionLinearInterpolate, 560, 200); _C(scm, "", out, "A"); _C(rgb, "", out, "B"); _C(a2, "", out, "Alpha")
    mel.connect_material_property(out, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    mel.recompile_material(mat); return mat


def build_assets():
    """Build all reveal assets (canonical names; idempotent: loaded if they already exist). Returns (M_LINE, M_RED, M_PHOTO, M_CLS, M_STAT)."""
    def _imp(path, fname):
        t = unreal.AssetImportTask()                       # always re-import (refreshes the photo/stats on scene switch; no does_asset_exist skip)
        t.filename = os.path.join(ROOT, "uploads", fname)
        t.destination_path = "/Game/Reveal"; t.destination_name = path.rsplit("/", 1)[1]
        t.replace_existing = True; t.automated = True; t.save = True
        af.import_asset_tasks([t])
        return unreal.load_asset(path)
    tex  = _imp(T_PHOTO_P, PHOTO_FN)
    stex = _imp(T_STAT_P, "reveal_stat_%s.png" % TID)
    mline  = unreal.load_asset(M_LINE_P)  if EAL.does_asset_exist(M_LINE_P)  else _build_line(M_LINE_P)
    mred   = unreal.load_asset(M_RED_P)   if EAL.does_asset_exist(M_RED_P)   else _build_red(M_RED_P)
    mphoto = unreal.load_asset(M_PHOTO_P) if EAL.does_asset_exist(M_PHOTO_P) else _build_photo(M_PHOTO_P, tex)
    mcls   = unreal.load_asset(M_CLS_P)   if EAL.does_asset_exist(M_CLS_P)   else _build_classify(M_CLS_P)
    mstat  = unreal.load_asset(M_STAT_P)  if EAL.does_asset_exist(M_STAT_P)  else _build_stat(M_STAT_P, stex)
    for p in (M_LINE_P, M_RED_P, M_PHOTO_P, M_CLS_P, M_STAT_P, T_PHOTO_P, T_STAT_P): EAL.save_asset(p, only_if_is_dirty=False)
    return mline, mred, mphoto, mcls, mstat


# ============================ Runtime (listener + stages) ============================
def arm():
    M_LINE, M_RED, M_PHOTO, M_CLS, M_STAT = build_assets()
    ST = getattr(unreal, "_REVEAL_STATE", None)
    if ST is None:
        ST = {"handle": None}; unreal._REVEAL_STATE = ST
    ST.update(stage=0, ticks=0, lw=0.0, rw=0.0, satv=1.0,
              anim={"active": False}, auto={"on": False, "hold": 0.0}, photo=False, info=False,
              photo_state=0, cam_saved=None)
    ST["world"] = ues.get_editor_world()

    def acts():
        try: return unreal.GameplayStatics.get_all_actors_of_class(ues.get_editor_world(), unreal.Actor)  # fetch the CURRENT world every time so level switches don't kill it (the old cached ST["world"] became a dangling pointer after a switch -> all keys dead)
        except Exception: return []
    def ppv(): return next((a for a in acts() if isinstance(a, unreal.PostProcessVolume)), None)

    # stencils: fabricated=1, real=2 (occluders); sky dome (AutoSky, not AutoSkyline) left unset -> the red stage keeps the dark night sky
    for a in acts():
        l = a.get_actor_label()
        if not l.startswith(VIS): continue
        if l.startswith("AutoSky") and not l.startswith("AutoSkyline"):
            for c in a.get_components_by_class(unreal.StaticMeshComponent):
                try: c.set_render_custom_depth(False)
                except Exception: pass
            continue
        val = 1 if _is_fab(l) else 2
        for c in a.get_components_by_class(unreal.StaticMeshComponent):
            try: c.set_render_custom_depth(True); c.set_custom_depth_stencil_value(val)
            except Exception: pass
    try: unreal.SystemLibrary.execute_console_command(ues.get_editor_world(), "r.CustomDepth 3")
    except Exception: pass

    def set_pp(lw, rw):
        p = ppv()
        if not p: return
        s = p.get_editor_property("settings"); arr = []
        def add(obj, w):
            wb = unreal.WeightedBlendable(); wb.set_editor_property("weight", min(1.0, w)); wb.set_editor_property("object", obj); arr.append(wb)
        if ST.get("info"): add(M_CLS, 1.0); add(M_STAT, 1.0)   # evidence view: classification + badge
        elif ST.get("photo"): add(M_PHOTO, 1.0)            # photo covers everything
        else:
            if lw > 0.01: add(M_LINE, lw)
            if rw > 0.01: add(M_RED, rw)
        wbs = unreal.WeightedBlendables(); wbs.set_editor_property("array", arr)
        s.set_editor_property("weighted_blendables", wbs); p.set_editor_property("settings", s)

    def set_sat(v):
        p = ppv()
        if p:
            s = p.get_editor_property("settings")
            s.set_editor_property("override_color_saturation", True)
            s.set_editor_property("color_saturation", unreal.Vector4(v, v, v, 1.0))
            p.set_editor_property("settings", s)

    def target(stage):
        return {0: (0.0, 0.0, 1.0), 1: (0.0, 0.0, 0.06), 2: (1.0, 0.0, 0.06), 3: (0.0, 1.0, 1.0)}[stage]

    def apply(lw, rw, sat):
        ST["lw"], ST["rw"], ST["satv"] = lw, rw, sat; set_pp(lw, rw); set_sat(sat)

    def set_stage(stage):
        apply(*target(stage)); ST["stage"] = stage; ST["anim"] = {"active": False}

    def go(to):
        ST["anim"] = {"active": True, "t": 0.0, "f": (ST["lw"], ST["rw"], ST["satv"]), "to": target(to)}
        ST["stage"] = to

    set_stage(0)
    unreal._REVEAL_SET = set_stage; unreal._REVEAL_GO = go

    sm_ = lambda t: t * t * (3.0 - 2.0 * t)
    lerp = lambda a, b, e: a + (b - a) * e
    kb = {"n": False, "a": False, "o": False, "i": False}
    def tick(dt):
        try:
            ST["ticks"] += 1; step = dt if (dt and dt > 0) else 0.016
            an = ST["anim"]
            if an.get("active"):
                an["t"] = min(1.0, an["t"] + step / DUR); e = sm_(an["t"]); f = an["f"]; to = an["to"]
                apply(lerp(f[0], to[0], e), lerp(f[1], to[1], e), lerp(f[2], to[2], e))
                if an["t"] >= 1.0: apply(*to); an["active"] = False
            au = ST["auto"]
            if au["on"] and not an.get("active"):
                au["hold"] += step
                if au["hold"] >= HOLD: au["hold"] = 0.0; go((ST["stage"] + 1) % 4)
            u = ctypes.windll.user32
            # Focus gate: GetAsyncKeyState is a global hook and fires even when the editor is not foreground (seen
            # live: typing pinyin "yiyang" toggled the evidence view twice). Skip keys unless the foreground window belongs to this (UE) process.
            pid_ = ctypes.c_ulong(0)
            u.GetWindowThreadProcessId(u.GetForegroundWindow(), ctypes.byref(pid_))
            if pid_.value != os.getpid():
                kb["n"] = kb["a"] = kb["o"] = kb["i"] = True   # mark as held: no rising edge at the instant focus returns
                return
            n = bool(u.GetAsyncKeyState(KEY_NEXT) & 0x8000)
            if n and not kb["n"]:
                au["on"] = False; ST["info"] = False; ST["photo"] = False; ST["photo_state"] = 0   # R first exits the evidence/photo views
                go((ST["stage"] + 1) % 4)
            kb["n"] = n
            a2 = bool(u.GetAsyncKeyState(KEY_AUTO) & 0x8000)
            if a2 and not kb["a"]: au["on"] = not au["on"]; au["hold"] = 0.0
            kb["a"] = a2
            o = bool(u.GetAsyncKeyState(KEY_PHOTO) & 0x8000)
            if o and not kb["o"]:
                ST["info"] = False                                   # O exits the evidence view
                if not ST.get("photo"):                             # exploring -> save the current camera + jump to the photo camera + overlay the original
                    ST["cam_saved"] = ues.get_level_viewport_camera_info()
                    try: ues.set_level_viewport_camera_info(unreal.Vector(*AUTOCAM_LOC), unreal.Rotator(**AUTOCAM_ROT))
                    except Exception: pass
                    ST["photo"] = True; set_sat(1.0)
                else:                                                # photo shown -> remove it + return straight to your saved exploring camera
                    cs = ST.get("cam_saved")
                    if cs:
                        try: ues.set_level_viewport_camera_info(cs[0], cs[1])
                        except Exception: pass
                    ST["photo"] = False; set_sat(ST["satv"])
                set_pp(ST["lw"], ST["rw"])
            kb["o"] = o
            ii = bool(u.GetAsyncKeyState(KEY_INFO) & 0x8000)
            if ii and not kb["i"]:
                ST["info"] = not ST.get("info", False); ST["photo"] = False; ST["photo_state"] = 0   # I toggles the evidence view (also clears the photo overlay)
                if ST["info"]: set_sat(1.0)               # the classification view needs full saturation
                else: set_sat(ST["satv"])
                try: les.editor_set_viewport_realtime(True)                    # force an immediate viewport redraw after toggling (otherwise it looks stuck)
                except Exception: pass
                set_pp(ST["lw"], ST["rw"])
            kb["i"] = ii
        except Exception as ex:
            unreal.log_warning("reveal tick: %s" % ex)

    if ST.get("handle") is not None:
        try: unreal.unregister_slate_post_tick_callback(ST["handle"])
        except Exception: pass
    ST["handle"] = unreal.register_slate_post_tick_callback(tick)
    try: les.editor_set_viewport_realtime(True); les.editor_set_game_view(True)
    except Exception: pass
    print("REVEAL armed. R=next  P=autoplay  O=原图对照  I=证据视图(分类图+虚构占比)   [编辑器视口: 右键+WASD, 别按 Play]")


if __name__ == "__main__":
    arm()
