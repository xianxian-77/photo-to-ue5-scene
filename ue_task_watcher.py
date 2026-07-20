# UE5 Editor Python: WATCH mode. Run once inside UE; it polls /latest and, whenever a
# new web upload finishes, auto-imports + grounds + places the objects (and clears the
# previous batch). Zero-mouse: after starting this, you only upload on the web.
#
# Run INSIDE Unreal (Output Log > switch "Cmd" to "Python"):
#   exec(open(r"<repo>/ue_task_watcher.py", encoding="utf-8").read())
# Stop watching:  stop()

import unreal, json, os, tempfile, urllib.request, math

SERVER = "http://127.0.0.1:5001"
DEST = "/Game/AutoImport"
CM_PER_UNIT = 100.0
POLL_SEC = 3.0
GROUND_DIM = 0.5                      # overall ground darkening factor
CAPTURE_VIEW = True                   # render the "photo view" via SceneCapture2D and post it to the backend (avoids blank viewport screenshots)
CAPTURE_W = 1280                      # posted image width (height follows the source aspect ratio)

_state = {"last": None, "acc": 0.0, "handle": None, "actors": [], "lights": []}


def _get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def _assets_in(path):
    reg = unreal.AssetRegistryHelpers.get_asset_registry()
    return [ad.get_asset() for ad in reg.get_assets_by_path(path, recursive=True) if ad.get_asset()]


def _spawn(mesh, loc, rot, scale):
    try:
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        actor = sub.spawn_actor_from_object(mesh, loc, rot)
    except Exception:
        actor = unreal.EditorLevelLibrary.spawn_actor_from_object(mesh, loc, rot)
    actor.set_actor_scale3d(scale)
    return actor


def _destroy(actor):
    try:
        unreal.get_editor_subsystem(unreal.EditorActorSubsystem).destroy_actor(actor)
    except Exception:
        try:
            unreal.EditorLevelLibrary.destroy_actor(actor)
        except Exception:
            pass


def _place_all(objs, scene_scale=10.0):
    # Enable realtime viewport: otherwise the editor viewport/captures may read a stale buffer (all-white frames seen on 5.6).
    try:
        unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).editor_set_viewport_realtime(True)
    except Exception:
        pass
    # Clear previous batch: only this pipeline's actors (OBJ_ / ECHO_ / Auto*); never touch hand-placed/template content.
    # Sweep by label, not just the in-memory _state (which is lost after a UE restart).
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in sub.get_all_level_actors():
        lbl = a.get_actor_label()
        if lbl.startswith("OBJ_") or lbl.startswith("ECHO_") or lbl.startswith("Auto"):
            _destroy(a)
    _state["actors"] = []

    atools = unreal.AssetToolsHelpers.get_asset_tools()
    tmp = tempfile.gettempdir()

    for o in objs:
        oid = o["id"]
        glb = (o.get("glb") or "").strip()
        if not glb:                                          # empty glb = failed 3D reconstruction; don't fetch the server index HTML as a model
            unreal.log_warning("  OBJ_%02d: no glb url, skipped." % oid)
            continue
        dest_obj = "%s/OBJ_%02d" % (DEST, oid)
        local = os.path.join(tmp, "obj_%d.glb" % oid)
        try:                                                 # a failed download/import skips that object only, never the whole batch (matches ue_scene_builder)
            with open(local, "wb") as f:
                f.write(_get(SERVER + glb))
            task = unreal.AssetImportTask()
            task.filename = local
            task.destination_path = dest_obj
            task.automated = True
            task.replace_existing = True
            task.save = True
            atools.import_asset_tasks([task])
        except Exception as e:
            unreal.log_warning("  OBJ_%02d: fetch/import failed: %s" % (oid, e))
            continue

        assets = _assets_in(dest_obj)
        meshes = [a for a in assets if isinstance(a, unreal.StaticMesh)]
        mats = [a for a in assets if isinstance(a, unreal.MaterialInterface)]
        if not meshes:
            unreal.log_warning("  OBJ_%02d: no StaticMesh, skipped." % oid)
            continue

        L, S = o["location"], o["scale"]
        sx, sy, sz = S[0] / CM_PER_UNIT, S[1] / CM_PER_UNIT, S[2] / CM_PER_UNIT
        z = L[2]
        if o.get("ground", True):
            try:
                b = meshes[0].get_bounds()
                z = L[2] - (b.origin.z - b.box_extent.z) * sz     # snap bottom to ground
            except Exception:
                pass
        R = o["rotation"]
        loc = unreal.Vector(L[0], L[1], z)
        rot = unreal.Rotator(pitch=R[0], yaw=R[1], roll=R[2])   # [pitch, yaw, roll]
        scale = unreal.Vector(sx, sy, sz)
        actor = _spawn(meshes[0], loc, rot, scale)
        actor.set_actor_label("OBJ_%02d" % oid)          # label it so the next batch sweep can find and clear it (no stacking)

        if mats:
            try:
                comp = actor.static_mesh_component
                for i, m in enumerate(mats):
                    comp.set_material(i, m)
            except Exception:
                pass

        _state["actors"].append(actor)
    unreal.log("watch: placed %d actors." % len(_state["actors"]))


def _find_or_spawn(cls):
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in sub.get_all_level_actors():
        if isinstance(a, cls):
            return a
    return sub.spawn_actor_from_class(cls, unreal.Vector(0, 0, 300), unreal.Rotator(0, 0, 0))


def _apply_env(env):
    """Write backend-solved lighting/atmosphere into the level: sun (direction/intensity/color temp) + sky light + exponential height fog."""
    if not env:
        return
    try:
        # Sun: light arrives FROM sun_yaw, so the light's forward (travel) direction is yaw+180
        sun = _find_or_spawn(unreal.DirectionalLight)
        yaw = float(env.get("sun_yaw_deg", 135.0)) + 180.0
        pitch = float(env.get("sun_pitch_deg", -45.0))
        sun.set_actor_rotation(unreal.Rotator(pitch=pitch, yaw=yaw, roll=0.0), False)
        c = sun.get_component_by_class(unreal.DirectionalLightComponent)
        if c:
            lux = float(env.get("sun_intensity_lux", 75000.0))
            c.set_editor_property("intensity", max(2.0, min(20.0, lux / 2000.0)))   # physical lux -> UE sun intensity (bright enough to avoid black silhouettes; exposure handled by SceneCapture)
            c.set_editor_property("use_temperature", True)
            c.set_editor_property("temperature", float(env.get("color_temp_k", 6500.0)))

        # Sky light (ambient fill)
        sky = _find_or_spawn(unreal.SkyLight)
        sc = sky.get_component_by_class(unreal.SkyLightComponent)
        if sc:
            sc.set_editor_property("intensity", float(env.get("sky_intensity", 1.0)))
            try:
                sc.recapture_sky()
            except Exception:
                pass

        # Exponential height fog (haze/atmosphere)
        fog = _find_or_spawn(unreal.ExponentialHeightFog)
        fc = fog.get_component_by_class(unreal.ExponentialHeightFogComponent)
        if fc:
            try:
                fc.set_editor_property("fog_density", float(env.get("fog_density", 0.003)))
            except Exception:
                pass
            col = env.get("fog_color") or [0.6, 0.7, 0.85]
            lc = unreal.LinearColor(col[0], col[1], col[2], 1.0)
            for prop in ("fog_inscattering_color", "fog_inscattering_luminance", "inscattering_color"):
                try:
                    fc.set_editor_property(prop, lc); break          # property name differs across UE versions; stop at first hit
                except Exception:
                    continue

        unreal.log("env: %s/%s  sun(yaw=%.0f,pitch=%.0f) %.0fK  fog %.3f"
                   % (env.get("time_of_day", "?"), env.get("weather", "?"),
                      yaw, pitch, float(env.get("color_temp_k", 6500)),
                      float(env.get("fog_density", 0.003))))
    except Exception as e:
        unreal.log_error("apply env failed: %s" % e)


GROUND_MAT_PATH = "/Game/Auto/M_GroundV2"      # new path: with radial center->edge fade (memory island dissolving into the void)


def _ensure_ground_material():
    """Ground master material: BaseColor/Roughness/Metallic + radial fade (full color at center, black at the edge)."""
    if unreal.EditorAssetLibrary.does_asset_exist(GROUND_MAT_PATH):
        return unreal.load_asset(GROUND_MAT_PATH)
    atools = unreal.AssetToolsHelpers.get_asset_tools()
    mat = atools.create_asset("M_GroundV2", "/Game/Auto", unreal.Material, unreal.MaterialFactoryNew())
    mel = unreal.MaterialEditingLibrary
    bc = mel.create_material_expression(mat, unreal.MaterialExpressionVectorParameter, -700, -100)
    bc.set_editor_property("parameter_name", "BaseColor")
    bc.set_editor_property("default_value", unreal.LinearColor(0.12, 0.12, 0.13, 1.0))
    rg = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -700, 250)
    rg.set_editor_property("parameter_name", "Roughness")
    rg.set_editor_property("default_value", 0.85)
    mel.connect_material_property(rg, "", unreal.MaterialProperty.MP_ROUGHNESS)
    mt = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -700, 380)
    mt.set_editor_property("parameter_name", "Metallic")
    mt.set_editor_property("default_value", 0.0)
    mel.connect_material_property(mt, "", unreal.MaterialProperty.MP_METALLIC)
    try:
        # radial fade = saturate(1 - 2*distance(UV, center)): full color at center, black at the edge
        tc = mel.create_material_expression(mat, unreal.MaterialExpressionTextureCoordinate, -700, 60)
        ctr = mel.create_material_expression(mat, unreal.MaterialExpressionConstant2Vector, -700, 150)
        ctr.set_editor_property("r", 0.5)
        ctr.set_editor_property("g", 0.5)
        dist = mel.create_material_expression(mat, unreal.MaterialExpressionDistance, -520, 90)
        mel.connect_material_expressions(tc, "", dist, "A")
        mel.connect_material_expressions(ctr, "", dist, "B")
        m2 = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -380, 90)
        mel.connect_material_expressions(dist, "", m2, "A")
        m2.set_editor_property("const_b", 2.0)
        om = mel.create_material_expression(mat, unreal.MaterialExpressionOneMinus, -250, 90)
        mel.connect_material_expressions(m2, "", om, "")
        cl = mel.create_material_expression(mat, unreal.MaterialExpressionClamp, -150, 90)
        mel.connect_material_expressions(om, "", cl, "")
        mb = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -40, -60)
        mel.connect_material_expressions(bc, "", mb, "A")
        mel.connect_material_expressions(cl, "", mb, "B")
        mel.connect_material_property(mb, "", unreal.MaterialProperty.MP_BASE_COLOR)
    except Exception as e:
        unreal.log_warning("ground radial fade failed, flat fallback: %s" % e)
        mel.connect_material_property(bc, "", unreal.MaterialProperty.MP_BASE_COLOR)
    mel.recompile_material(mat)
    unreal.EditorAssetLibrary.save_asset(GROUND_MAT_PATH)
    return mat


TERRAIN_VAR_MAT_PATH = "/Game/Auto/M_AutoTerrainVar"


def _ensure_terrain_var_material(default_tex=None):
    """Noise-blended multi-texture terrain material: three decorrelated seamless textures from the same source
    (AlbedoTexA/B/C) mixed by two lerps on one low-frequency, world-aligned (XY-only) noise mask, plus an even
    lower-frequency macro brightness layer (MacroNoise), so distant views show no regular repeating grid.
    Every sampler carries a default texture + explicit SAMPLERTYPE_COLOR, otherwise the base material fails to
    compile and degrades to dark gray/black. No StaticSwitch (avoids the shader-compile race).
    Scalar params (MIC edits need no recompile): TilingU/TilingV, NoiseScale, MacroScale, MacroAmount, Roughness, Metallic."""
    if unreal.EditorAssetLibrary.does_asset_exist(TERRAIN_VAR_MAT_PATH):
        return unreal.load_asset(TERRAIN_VAR_MAT_PATH)
    atools = unreal.AssetToolsHelpers.get_asset_tools()
    mat = atools.create_asset("M_AutoTerrainVar", "/Game/Auto", unreal.Material, unreal.MaterialFactoryNew())
    mel = unreal.MaterialEditingLibrary
    if default_tex is None:                                     # fall back to an engine texture so every sampler has a valid Texture2D and the material compiles
        default_tex = unreal.load_asset("/Engine/EngineResources/DefaultTexture")

    def _sampler(name, py):
        ts = mel.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, -640, py)
        ts.set_editor_property("parameter_name", name)
        if default_tex is not None:
            try:
                ts.set_editor_property("texture", default_tex)
            except Exception:
                pass
        try:
            ts.set_editor_property("sampler_type", unreal.MaterialSamplerType.SAMPLERTYPE_COLOR)
        except Exception:
            pass
        return ts

    # UV tiling: TexCoord * float2(TilingU, TilingV), shared by all three textures
    tc = mel.create_material_expression(mat, unreal.MaterialExpressionTextureCoordinate, -1120, 40)
    tu = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -1120, 180)
    tu.set_editor_property("parameter_name", "TilingU"); tu.set_editor_property("default_value", 8.0)
    tv = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -1120, 290)
    tv.set_editor_property("parameter_name", "TilingV"); tv.set_editor_property("default_value", 8.0)
    apuv = mel.create_material_expression(mat, unreal.MaterialExpressionAppendVector, -960, 230)
    mel.connect_material_expressions(tu, "", apuv, "A"); mel.connect_material_expressions(tv, "", apuv, "B")
    uvmul = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -820, 120)
    mel.connect_material_expressions(tc, "", uvmul, "A"); mel.connect_material_expressions(apuv, "", uvmul, "B")

    tsA = _sampler("AlbedoTexA", -120); tsB = _sampler("AlbedoTexB", 120); tsC = _sampler("AlbedoTexC", 360)
    for ts in (tsA, tsB, tsC):
        mel.connect_material_expressions(uvmul, "", ts, "UVs")

    # world-aligned noise coords: WorldPosition XY only (drop Z to avoid vertical streaks on slopes) -> append 0 -> *NoiseScale -> Noise "World Position"
    wp = mel.create_material_expression(mat, unreal.MaterialExpressionWorldPosition, -1320, 560)
    wpxy = mel.create_material_expression(mat, unreal.MaterialExpressionComponentMask, -1140, 560)
    wpxy.set_editor_property("r", True); wpxy.set_editor_property("g", True)
    wpxy.set_editor_property("b", False); wpxy.set_editor_property("a", False)
    mel.connect_material_expressions(wp, "", wpxy, "")
    z0 = mel.create_material_expression(mat, unreal.MaterialExpressionConstant, -1140, 680)
    z0.set_editor_property("r", 0.0)
    wp3 = mel.create_material_expression(mat, unreal.MaterialExpressionAppendVector, -980, 600)
    mel.connect_material_expressions(wpxy, "", wp3, "A"); mel.connect_material_expressions(z0, "", wp3, "B")
    nscale = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -1140, 760)
    nscale.set_editor_property("parameter_name", "NoiseScale"); nscale.set_editor_property("default_value", 0.0008)
    wpns = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -820, 600)
    mel.connect_material_expressions(wp3, "", wpns, "A"); mel.connect_material_expressions(nscale, "", wpns, "B")

    def _noise(py, omin, omax, lv):
        nz = mel.create_material_expression(mat, unreal.MaterialExpressionNoise, -660, py)
        for k, vv in (("scale", 1.0), ("output_min", omin), ("output_max", omax),
                      ("levels", lv), ("turbulence", False)):
            try:
                nz.set_editor_property(k, vv)
            except Exception:
                pass
        return nz                                              # the "function" property is protected and cannot be set; the default is fine

    mask = _noise(560, 0.0, 1.0, 2)
    mel.connect_material_expressions(wpns, "", mask, "World Position")

    # blend the three textures with two lerps: lerp(lerp(A,B,mask), C, mask)
    lab = mel.create_material_expression(mat, unreal.MaterialExpressionLinearInterpolate, -360, 60)
    mel.connect_material_expressions(tsA, "RGB", lab, "A"); mel.connect_material_expressions(tsB, "RGB", lab, "B")
    mel.connect_material_expressions(mask, "", lab, "Alpha")
    labc = mel.create_material_expression(mat, unreal.MaterialExpressionLinearInterpolate, -200, 200)
    mel.connect_material_expressions(lab, "", labc, "A"); mel.connect_material_expressions(tsC, "RGB", labc, "B")
    mel.connect_material_expressions(mask, "", labc, "Alpha")

    # macro shading: a second, lower-frequency noise -> 1 + noise(-1..1)*MacroAmount, multiplied into the blended albedo
    mscale = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -1140, 900)
    mscale.set_editor_property("parameter_name", "MacroScale"); mscale.set_editor_property("default_value", 0.00025)
    wpms = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -820, 820)
    mel.connect_material_expressions(wp3, "", wpms, "A"); mel.connect_material_expressions(mscale, "", wpms, "B")
    macro = _noise(820, -1.0, 1.0, 1)
    mel.connect_material_expressions(wpms, "", macro, "World Position")
    mamt = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -660, 960)
    mamt.set_editor_property("parameter_name", "MacroAmount"); mamt.set_editor_property("default_value", 0.18)
    macromul = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -500, 820)
    mel.connect_material_expressions(macro, "", macromul, "A"); mel.connect_material_expressions(mamt, "", macromul, "B")
    macrobright = mel.create_material_expression(mat, unreal.MaterialExpressionAdd, -360, 840)
    macrobright.set_editor_property("const_a", 1.0)            # 1 + noise*amount
    mel.connect_material_expressions(macromul, "", macrobright, "B")

    finalc = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -60, 320)
    mel.connect_material_expressions(labc, "", finalc, "A"); mel.connect_material_expressions(macrobright, "", finalc, "B")
    mel.connect_material_property(finalc, "", unreal.MaterialProperty.MP_BASE_COLOR)

    rg = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -360, 470)
    rg.set_editor_property("parameter_name", "Roughness"); rg.set_editor_property("default_value", 0.9)
    mel.connect_material_property(rg, "", unreal.MaterialProperty.MP_ROUGHNESS)
    mt = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -360, 580)
    mt.set_editor_property("parameter_name", "Metallic"); mt.set_editor_property("default_value", 0.0)
    mel.connect_material_property(mt, "", unreal.MaterialProperty.MP_METALLIC)

    mel.recompile_material(mat)
    unreal.EditorAssetLibrary.save_asset(TERRAIN_VAR_MAT_PATH)
    return mat


def _ensure_mic(name, parent_mat):
    """Create/load a savable material instance (MaterialInstanceConstant) parented to parent_mat.
    Replaces transient dynamic instances so ground/sky materials survive save/reload. Returns (instance, asset path)."""
    path = "/Game/Auto/%s" % name
    if unreal.EditorAssetLibrary.does_asset_exist(path):
        mic = unreal.load_asset(path)
    else:
        atools = unreal.AssetToolsHelpers.get_asset_tools()
        mic = atools.create_asset(name, "/Game/Auto", unreal.MaterialInstanceConstant,
                                  unreal.MaterialInstanceConstantFactoryNew())
    unreal.MaterialEditingLibrary.set_material_instance_parent(mic, parent_mat)
    return mic, path


def _apply_ground(env, scene_scale=10.0, footprint=None, base_z=-2.0):
    """Lay the ground: by default a square plane sized to the scene; with footprint=(half_x_m,half_y_m,cx_m,cy_m)
    it fits the terrain footprint exactly and sinks to the skirt bottom (base_z) to seal the box underside as a
    reload fallback, never showing as a second floating plane."""
    if not env:
        return
    try:
        mat = _ensure_ground_material()
        plane = unreal.load_asset("/Engine/BasicShapes/Plane")
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        actor = None
        for a in sub.get_all_level_actors():
            if isinstance(a, unreal.StaticMeshActor) and a.get_actor_label() == "AutoGround":
                actor = a
                break
        if actor is None:
            actor = sub.spawn_actor_from_object(plane, unreal.Vector(0, 0, 0))
            actor.set_actor_label("AutoGround")
        comp = actor.static_mesh_component
        comp.set_static_mesh(plane)
        if footprint:                                   # fit the footprint and sink to the skirt bottom (base_z): seals the box underside, never pokes out
            hx, hy, cx, cy = footprint
            actor.set_actor_location(
                unreal.Vector(float(cx) * CM_PER_UNIT, float(cy) * CM_PER_UNIT, float(base_z)), False, False)
            actor.set_actor_scale3d(unreal.Vector(
                max(2.0, 2.0 * float(hx)), max(2.0, 2.0 * float(hy)), 1.0))
        else:
            actor.set_actor_location(unreal.Vector(0, 0, 0), False, False)
            gs = max(40.0, 6.0 * float(scene_scale))                  # ground side length (m) scales with the scene
            actor.set_actor_scale3d(unreal.Vector(gs, gs, 1.0))

        wet = float(env.get("ground_wetness", 0.0))
        rough = max(0.05, float(env.get("ground_roughness", 0.85)) * (1.0 - 0.75 * wet))
        col = env.get("ground_color") or [0.18, 0.18, 0.19]
        dark = (1.0 - 0.3 * wet) * GROUND_DIM           # extra darkening so the ground isn't washed-out white
        mic, mic_path = _ensure_mic("MI_AutoGround", mat)
        mel = unreal.MaterialEditingLibrary
        mel.set_material_instance_vector_parameter_value(
            mic, "BaseColor", unreal.LinearColor(col[0] * dark, col[1] * dark, col[2] * dark, 1.0))
        mel.set_material_instance_scalar_parameter_value(mic, "Roughness", rough)
        mel.set_material_instance_scalar_parameter_value(mic, "Metallic", 0.0)
        comp.set_material(0, mic)
        unreal.EditorAssetLibrary.save_asset(mic_path)
        unreal.log("ground: %s rough=%.2f wet=%.2f" % (env.get("ground_material", "?"), rough, wet))
    except Exception as e:
        unreal.log_error("apply ground failed: %s" % e)


SKIRT_FRAC = 0.12       # skirt drop as a fraction of relief (short skirt: a solid island, not a tall floating block)
SKIRT_MIN_CM = 150.0    # minimum skirt drop (1.5 m)


def _terrain_base_z(terrain):
    """World z (cm) of the skirt bottom / bottom cap: the terrain's lowest vertex sits at z=0 (grid min=0), so
    sealing only a short way down already yields a solid box; sealing too deep (e.g. -1.5*relief) turns it into
    a tall floating block. The fallback plane is pressed down to this height too."""
    relief = max(1.0, float((terrain or {}).get("relief_m", 2.0))) * CM_PER_UNIT
    return -max(SKIRT_MIN_CM, SKIRT_FRAC * relief)


def _build_terrain_mesh(terrain):
    """Relief terrain StaticMesh with skirt + bottom cap: top z=grid(0..1)*relief (min=0, rising from z=0);
    boundary vertices drop vertically to z_base as skirt walls and a flat bottom seals a solid box, so side/low
    camera angles no longer see the floating plane underneath (the 'two surfaces' artifact).
    UE is left-handed: top faces [a,cc,b]/[a,dd,cc] point up; walls wind outward; bottom faces down.
    fast_build=False computes normals automatically (otherwise everything renders black)."""
    grid = terrain["grid"]; n = int(terrain["n"])
    cx = float(terrain.get("cx_m", 0.0)) * CM_PER_UNIT
    cy = float(terrain.get("cy_m", 0.0)) * CM_PER_UNIT
    hx = max(50.0, float(terrain.get("half_fwd_m", 10.0)) * CM_PER_UNIT)
    hy = max(50.0, float(terrain.get("half_lat_m", 10.0)) * CM_PER_UNIT)
    relief = max(1.0, float(terrain.get("relief_m", 2.0))) * CM_PER_UNIT
    z_base = _terrain_base_z(terrain)
    sm = unreal.StaticMesh()
    smd = sm.create_static_mesh_description()
    pg = smd.create_polygon_group()

    def _xy(r, c):
        return (cx + (r / (n - 1.0) - 0.5) * 2.0 * hx,
                cy + (c / (n - 1.0) - 0.5) * 2.0 * hy)

    def _mk(x, y, z, u, vv):
        v = smd.create_vertex(); smd.set_vertex_position(v, unreal.Vector(x, y, z))
        vi = smd.create_vertex_instance(v); smd.set_vertex_instance_uv(vi, unreal.Vector2D(u, vv), 0)
        return vi

    # -- top surface --
    VI = [[None] * n for _ in range(n)]
    for r in range(n):
        for c in range(n):
            x, y = _xy(r, c)
            VI[r][c] = _mk(x, y, grid[r][c] * relief, c / (n - 1.0), r / (n - 1.0))
    for r in range(n - 1):
        for c in range(n - 1):
            a, b, cc, dd = VI[r][c], VI[r + 1][c], VI[r + 1][c + 1], VI[r][c + 1]
            smd.create_triangle(pg, [a, cc, b])      # normals face +Z (up)
            smd.create_triangle(pg, [a, dd, cc])

    # -- perimeter skirt walls: boundary top vertices -> base vertices at the same XY (clockwise ring, outward normals) --
    ring = ([(0, c) for c in range(n)] +
            [(r, n - 1) for r in range(1, n)] +
            [(n - 1, c) for c in range(n - 2, -1, -1)] +
            [(r, 0) for r in range(n - 2, 0, -1)])
    buv = lambda r, c: (c / (n - 1.0), r / (n - 1.0))
    vspan = max(1.0, 2.0 * hx)               # wall vertical UV scale: push base-vertex V down by the drop so the wall texture flows at world scale, breaking vertical streaks
    for k in range(len(ring)):
        r0, c0 = ring[k]; r1, c1 = ring[(k + 1) % len(ring)]
        x0, y0 = _xy(r0, c0); x1, y1 = _xy(r1, c1)
        zt0 = grid[r0][c0] * relief; zt1 = grid[r1][c1] * relief
        u0, v0 = buv(r0, c0); u1, v1 = buv(r1, c1)
        t0 = _mk(x0, y0, zt0, u0, v0); t1 = _mk(x1, y1, zt1, u1, v1)
        b0 = _mk(x0, y0, z_base, u0, v0 + (zt0 - z_base) / vspan)
        b1 = _mk(x1, y1, z_base, u1, v1 + (zt1 - z_base) / vspan)
        smd.create_triangle(pg, [t0, b0, t1])
        smd.create_triangle(pg, [t1, b0, b1])

    # -- bottom cap: four corner base vertices, normals facing down --
    ax, ay = _xy(0, 0);         bA = _mk(ax, ay, z_base, 0.0, 0.0)
    bx, by = _xy(0, n - 1);     bB = _mk(bx, by, z_base, 1.0, 0.0)
    ccx, ccy = _xy(n - 1, n - 1); bC = _mk(ccx, ccy, z_base, 1.0, 1.0)
    dx, dy = _xy(n - 1, 0);     bD = _mk(dx, dy, z_base, 0.0, 1.0)
    smd.create_triangle(pg, [bA, bB, bC])
    smd.create_triangle(pg, [bA, bC, bD])

    sm.build_from_static_mesh_descriptions([smd], False, False)
    return sm


def _apply_terrain(terrain, env, scene_scale=10.0):
    """With an inferred height grid, build real relief terrain above the persistent flat ground; without one
    (indoor/flat) fall back to _apply_ground. The terrain mesh is transient (lost on save), so a saved flat
    AutoGround stays underneath as a fallback and the ground survives reload."""
    grid = (terrain or {}).get("grid")
    if not grid or int((terrain or {}).get("n", 0)) < 2:
        _apply_ground(env, scene_scale)
        return
    try:
        # persistent fallback: flat AutoGround fitted exactly to the terrain footprint, right below it (the terrain mesh is transient and lost on save; keep a plane underneath)
        hf = float(terrain.get("half_fwd_m", 10.0)); hl = float(terrain.get("half_lat_m", 10.0))
        cx = float(terrain.get("cx_m", 0.0)); cy = float(terrain.get("cy_m", 0.0))
        _apply_ground(env, scene_scale, footprint=(hf, hl, cx, cy), base_z=_terrain_base_z(terrain))
        mesh = _build_terrain_mesh(terrain)
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        actor = None
        for a in sub.get_all_level_actors():
            if isinstance(a, unreal.StaticMeshActor) and a.get_actor_label() == "AutoTerrain":
                actor = a
                break
        if actor is None:
            actor = sub.spawn_actor_from_class(unreal.StaticMeshActor, unreal.Vector(0, 0, 0))
            actor.set_actor_label("AutoTerrain")
        actor.set_mobility(unreal.ComponentMobility.MOVABLE)
        comp = actor.static_mesh_component
        comp.set_static_mesh(mesh)
        actor.set_actor_location(unreal.Vector(0, 0, 0), False, False)
        # surface material: prefer the seamless tiling textures matched to the source photo, tiled over the real footprint; otherwise fall back to Gemini's flat ground color
        wet = float((env or {}).get("ground_wetness", 0.0))
        rough = max(0.05, float((env or {}).get("ground_roughness", 0.85)) * (1.0 - 0.75 * wet))
        mel = unreal.MaterialEditingLibrary
        urls = list(terrain.get("albedo_urls") or [])
        if not urls and (terrain.get("albedo_url") or "").strip():
            urls = [terrain["albedo_url"]]                       # backward compat: single-texture field
        texes = []
        for i, u in enumerate(urls[:3]):
            u = (u or "").strip()
            if not u:
                continue
            try:
                local = os.path.join(tempfile.gettempdir(), "auto_terrain_tex_%d.png" % i)
                with open(local, "wb") as f:
                    f.write(_get(SERVER + u))
                task = unreal.AssetImportTask()
                task.filename = local
                task.destination_path = "/Game/AutoImport/Terrain"
                task.destination_name = "T_AutoTerrain%s" % "ABC"[i]
                task.automated = True; task.replace_existing = True; task.save = True
                unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
                t = unreal.load_asset("/Game/AutoImport/Terrain/T_AutoTerrain%s" % "ABC"[i])
                if t is not None:
                    texes.append(t)
            except Exception as e:
                unreal.log_warning("terrain texture %d import failed: %s" % (i, e))
        if texes:
            orig = len(texes)
            while len(texes) < 3:                               # fewer than 3 textures: reuse cyclically (blending still works; macro shading still breaks repetition)
                texes.append(texes[len(texes) % orig])
            TILE_M = 40.0                                       # ~40 m per tile (large tiles, less repetition); U/V tiling counts computed per footprint axis
            til_u = min(200.0, max(1.0, 2.0 * hl / TILE_M))     # U = lateral (2*half_lat)
            til_v = min(200.0, max(1.0, 2.0 * hf / TILE_M))     # V = forward (2*half_fwd)
            mat = _ensure_terrain_var_material(texes[0])        # the first texture doubles as the default on first creation
            mic, mic_path = _ensure_mic("MI_AutoTerrainVar", mat)
            mel.set_material_instance_texture_parameter_value(mic, "AlbedoTexA", texes[0])
            mel.set_material_instance_texture_parameter_value(mic, "AlbedoTexB", texes[1])
            mel.set_material_instance_texture_parameter_value(mic, "AlbedoTexC", texes[2])
            mel.set_material_instance_scalar_parameter_value(mic, "TilingU", til_u)
            mel.set_material_instance_scalar_parameter_value(mic, "TilingV", til_v)
            foot_cm = max(1.0, 2.0 * max(hf, hl) * CM_PER_UNIT)  # noise frequency adapts to the footprint: ~10 blend patches per footprint (breaks repetition), ~3 macro patches
            mel.set_material_instance_scalar_parameter_value(mic, "NoiseScale", 10.0 / foot_cm)
            mel.set_material_instance_scalar_parameter_value(mic, "MacroScale", 3.0 / foot_cm)
            mel.set_material_instance_scalar_parameter_value(mic, "MacroAmount", 0.38)
            mel.set_material_instance_scalar_parameter_value(mic, "Roughness", rough)
            mel.set_material_instance_scalar_parameter_value(mic, "Metallic", 0.0)
            comp.set_material(0, mic)
            unreal.EditorAssetLibrary.save_asset(mic_path)
            # apply the same MIC to the fallback ground: bottom cap/skirt peek-through stays seamless, no odd color
            for g in sub.get_all_level_actors():
                if isinstance(g, unreal.StaticMeshActor) and g.get_actor_label() == "AutoGround":
                    g.static_mesh_component.set_material(0, mic); break
            unreal.log("terrain: %dx%d relief=%.1fm footprint=%.0fx%.0fm  var-tex(%d imgs, tileU=%.0f,V=%.0f)" % (
                terrain["n"], terrain["n"], float(terrain.get("relief_m", 0)),
                hf * 2, hl * 2, orig, til_u, til_v))
        else:
            col = (env or {}).get("ground_color") or [0.18, 0.18, 0.19]
            dark = (1.0 - 0.3 * wet) * GROUND_DIM
            mat = _ensure_ground_material()
            mic, mic_path = _ensure_mic("MI_AutoTerrain", mat)
            mel.set_material_instance_vector_parameter_value(
                mic, "BaseColor", unreal.LinearColor(col[0] * dark, col[1] * dark, col[2] * dark, 1.0))
            mel.set_material_instance_scalar_parameter_value(mic, "Roughness", rough)
            mel.set_material_instance_scalar_parameter_value(mic, "Metallic", 0.0)
            comp.set_material(0, mic)
            unreal.EditorAssetLibrary.save_asset(mic_path)
            unreal.log("terrain: %dx%d relief=%.1fm footprint=%.0fx%.0fm  flat-color(no tex)" % (
                terrain["n"], terrain["n"], float(terrain.get("relief_m", 0)), hf * 2, hl * 2))
    except Exception as e:
        unreal.log_warning("apply terrain failed: %s" % e)
        _apply_ground(env, scene_scale)


# -- Ground dressing: backend-computed instances (solid "rock" / crossed "card") mass-instanced via HISM (GPU), tinted per layer --
DRESS_MAT_PATH = "/Game/Auto/M_Dress"


def _ensure_dress_material():
    """Shared dressing master material: two-sided lit; BaseColor (vector param) + Roughness/Metallic (scalar params); each layer tints via its own MIC."""
    if unreal.EditorAssetLibrary.does_asset_exist(DRESS_MAT_PATH):
        return unreal.load_asset(DRESS_MAT_PATH)
    atools = unreal.AssetToolsHelpers.get_asset_tools()
    mat = atools.create_asset("M_Dress", "/Game/Auto", unreal.Material, unreal.MaterialFactoryNew())
    mat.set_editor_property("two_sided", True)              # cards must be visible from both sides
    mel = unreal.MaterialEditingLibrary
    bc = mel.create_material_expression(mat, unreal.MaterialExpressionVectorParameter, -400, -60)
    bc.set_editor_property("parameter_name", "BaseColor")
    bc.set_editor_property("default_value", unreal.LinearColor(0.5, 0.5, 0.5, 1.0))
    mel.connect_material_property(bc, "", unreal.MaterialProperty.MP_BASE_COLOR)
    rg = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -400, 120)
    rg.set_editor_property("parameter_name", "Roughness"); rg.set_editor_property("default_value", 0.9)
    mel.connect_material_property(rg, "", unreal.MaterialProperty.MP_ROUGHNESS)
    mt = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -400, 230)
    mt.set_editor_property("parameter_name", "Metallic"); mt.set_editor_property("default_value", 0.0)
    mel.connect_material_property(mt, "", unreal.MaterialProperty.MP_METALLIC)
    mel.recompile_material(mat)
    unreal.EditorAssetLibrary.save_asset(DRESS_MAT_PATH)
    return mat


DRESS_CARD_MAT_PATH = "/Game/Auto/M_DressCard"


def _ensure_dress_card_material(default_tex=None):
    """Alpha grass/plant card material: PlantTex (single plant on pure black) -> BaseColor; its luminance
    (dot with luma weights) -> OpacityMask (black turns transparent); Masked blend + two-sided. Each card layer
    swaps PlantTex via a MIC (grass/shrub/cactus...). The sampler carries a default texture + Color sampler
    type so it always compiles."""
    if unreal.EditorAssetLibrary.does_asset_exist(DRESS_CARD_MAT_PATH):
        return unreal.load_asset(DRESS_CARD_MAT_PATH)
    atools = unreal.AssetToolsHelpers.get_asset_tools()
    mat = atools.create_asset("M_DressCard", "/Game/Auto", unreal.Material, unreal.MaterialFactoryNew())
    mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_MASKED)
    mat.set_editor_property("two_sided", True)
    try:
        mat.set_editor_property("opacity_mask_clip_value", 0.1)
    except Exception:
        pass
    mel = unreal.MaterialEditingLibrary
    if default_tex is None:
        default_tex = unreal.load_asset("/Engine/EngineResources/DefaultTexture")
    ts = mel.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, -500, 0)
    ts.set_editor_property("parameter_name", "PlantTex")
    if default_tex is not None:
        try:
            ts.set_editor_property("texture", default_tex)
        except Exception:
            pass
    try:
        ts.set_editor_property("sampler_type", unreal.MaterialSamplerType.SAMPLERTYPE_COLOR)
    except Exception:
        pass
    mel.connect_material_property(ts, "RGB", unreal.MaterialProperty.MP_BASE_COLOR)
    lw = mel.create_material_expression(mat, unreal.MaterialExpressionConstant3Vector, -500, 220)
    lw.set_editor_property("constant", unreal.LinearColor(0.299, 0.587, 0.114, 0.0))   # luminance weights
    dp = mel.create_material_expression(mat, unreal.MaterialExpressionDotProduct, -300, 120)
    mel.connect_material_expressions(ts, "RGB", dp, "A")
    mel.connect_material_expressions(lw, "", dp, "B")
    mel.connect_material_property(dp, "", unreal.MaterialProperty.MP_OPACITY_MASK)
    mel.recompile_material(mat)
    unreal.EditorAssetLibrary.save_asset(DRESS_CARD_MAT_PATH)
    return mat


def _dress_archetype(shape):
    """Generic low-poly primitives (transient, rebuilt each time), bottom at z=0, ~1 m size; instance scale
    resizes them to real size: rock = radially jittered icosahedron (solid rock/pebble/clod); card = two
    crossed vertical cards (grass tufts/weeds/small plants)."""
    sm = unreal.StaticMesh(); smd = sm.create_static_mesh_description(); pg = smd.create_polygon_group()

    def mk(x, y, z, u, v):
        vv = smd.create_vertex(); smd.set_vertex_position(vv, unreal.Vector(x, y, z))
        vi = smd.create_vertex_instance(vv); smd.set_vertex_instance_uv(vi, unreal.Vector2D(u, v), 0)
        return vi
    if shape == "rock":
        t = (1.0 + math.sqrt(5.0)) / 2.0
        base = [(-1, t, 0), (1, t, 0), (-1, -t, 0), (1, -t, 0), (0, -1, t), (0, 1, t),
                (0, -1, -t), (0, 1, -t), (t, 0, -1), (t, 0, 1), (-t, 0, -1), (-t, 0, 1)]
        faces = [(0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11), (1, 5, 9), (5, 11, 4),
                 (11, 10, 2), (10, 7, 6), (7, 1, 8), (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8),
                 (3, 8, 9), (4, 9, 5), (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1)]
        pts = []
        for i, (x, y, z) in enumerate(base):
            r = math.sqrt(x * x + y * y + z * z)
            jit = 0.72 + 0.5 * (((i * 1664525 + 1013904223) % 1000) / 1000.0)   # deterministic radial jitter -> irregular rock
            s = jit / r * 50.0                                                  # unit sphere -> 50 cm radius
            pts.append([x * s, y * s, z * s])
        zmin = min(p[2] for p in pts)
        for p in pts:
            p[2] -= zmin                                                        # bottom at z=0
        vis = [mk(p[0], p[1], p[2], p[0] / 100.0 + 0.5, p[1] / 100.0 + 0.5) for p in pts]
        for a, b, c in faces:
            smd.create_triangle(pg, [vis[a], vis[c], vis[b]])                   # outward normals
    else:  # card: two crossed vertical quads, bottom at z=0, ~100 cm tall/wide
        for q in (((-50, 0, 0), (50, 0, 0), (50, 0, 100), (-50, 0, 100)),
                  ((0, -50, 0), (0, 50, 0), (0, 50, 100), (0, -50, 100))):
            a = mk(q[0][0], q[0][1], q[0][2], 0, 1); b = mk(q[1][0], q[1][1], q[1][2], 1, 1)
            c = mk(q[2][0], q[2][1], q[2][2], 1, 0); d = mk(q[3][0], q[3][1], q[3][2], 0, 0)
            smd.create_triangle(pg, [a, b, c]); smd.create_triangle(pg, [a, c, d])
    sm.build_from_static_mesh_descriptions([smd], False, False)
    return sm


def _spawn_hism(label, mesh):
    """Spawn an empty Actor + one HISM component (registered via SubobjectDataSubsystem, or it won't render) and attach the mesh. Returns (actor, hism)."""
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actor = sub.spawn_actor_from_class(unreal.Actor, unreal.Vector(0, 0, 0))
    actor.set_actor_label(label)
    sds = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
    handles = sds.k2_gather_subobject_data_for_instance(actor)
    params = unreal.AddNewSubobjectParams()
    params.set_editor_property("parent_handle", handles[0])
    params.set_editor_property("new_class", unreal.HierarchicalInstancedStaticMeshComponent)
    new_handle, _fail = sds.add_new_subobject(params)
    sdl = unreal.SubobjectDataBlueprintFunctionLibrary
    hism = sdl.get_object(sdl.get_data(new_handle))
    hism.set_static_mesh(mesh)
    return actor, hism


def _dress_tint(hism, i, L):
    """Flat-color tint (rock layers / textureless cards): M_Dress + a MIC setting BaseColor/Roughness."""
    mel = unreal.MaterialEditingLibrary
    col = L.get("color") or [0.5, 0.5, 0.5]
    mic, mic_path = _ensure_mic("MI_Dress_%02d" % i, _ensure_dress_material())
    mel.set_material_instance_vector_parameter_value(
        mic, "BaseColor", unreal.LinearColor(col[0], col[1], col[2], 1.0))
    mel.set_material_instance_scalar_parameter_value(mic, "Roughness", float(L.get("roughness", 0.9)))
    hism.set_material(0, mic)
    unreal.EditorAssetLibrary.save_asset(mic_path)


def _apply_dressing(dressing):
    """Backend-computed dressing instances per layer -> mass HISM instancing (GPU); rock layers get a flat tint,
    card layers (grass/plants) use single-plant-on-black images as alpha cards. Transient (rebuilt each time),
    labeled AutoDress_*."""
    if not dressing:
        return
    try:
        mel = unreal.MaterialEditingLibrary
        cache = {}; total = 0
        for i, L in enumerate(dressing):
            shape = L.get("shape", "rock")
            mesh = cache.get(shape)
            if mesh is None:
                mesh = _dress_archetype(shape); cache[shape] = mesh
            actor, hism = _spawn_hism("AutoDress_%02d" % i, mesh)
            tex_url = (L.get("tex_url") or "").strip()
            if shape == "card" and tex_url:                 # alpha grass/plant card: import the single-plant image on pure black
                ptex = None
                try:
                    local = os.path.join(tempfile.gettempdir(), "auto_dress_card_%d.png" % i)
                    with open(local, "wb") as f:
                        f.write(_get(SERVER + tex_url))
                    task = unreal.AssetImportTask()
                    task.filename = local; task.destination_path = "/Game/AutoImport/Dress"
                    task.destination_name = "T_DressCard%02d" % i
                    task.automated = True; task.replace_existing = True; task.save = True
                    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
                    ptex = unreal.load_asset("/Game/AutoImport/Dress/T_DressCard%02d" % i)
                except Exception as e:
                    unreal.log_warning("dress card tex import failed: %s" % e); ptex = None
                if ptex is not None:
                    mic, mic_path = _ensure_mic("MI_DressCard_%02d" % i, _ensure_dress_card_material(ptex))
                    mel.set_material_instance_texture_parameter_value(mic, "PlantTex", ptex)
                    hism.set_material(0, mic)
                    unreal.EditorAssetLibrary.save_asset(mic_path)
                else:
                    _dress_tint(hism, i, L)
            else:
                _dress_tint(hism, i, L)
            inst = L.get("instances", [])
            for x, y, z, yaw, sc in inst:
                hism.add_instance(unreal.Transform(unreal.Vector(x, y, z), unreal.Rotator(0, yaw, 0),
                                                   unreal.Vector(sc, sc, sc)), True)
            total += len(inst)
            unreal.log("dressing layer %s: %s ×%d" % (shape, L.get("name", shape), len(inst)))
        unreal.log("dressing: %d instances across %d layers" % (total, len(dressing)))
    except Exception as e:
        unreal.log_warning("apply dressing failed: %s" % e)


# -- Particle/fluid FX: translucent "water" plane / alpha "card" clusters (HISM) / "fog". Static single frame (no animation). Labeled AutoFx_* --
FX_WATER_MAT_PATH = "/Game/Auto/M_FxWater"
FX_CARD_MAT_PATH = "/Game/Auto/M_FxCard"


def _ensure_fx_water_material():
    """Water master material: translucent (BLEND_TRANSLUCENT, TLM_SURFACE), colored by WaterColor, opacity driven by Fresnel (more solid at grazing angles, more transparent from above)."""
    if unreal.EditorAssetLibrary.does_asset_exist(FX_WATER_MAT_PATH):
        return unreal.load_asset(FX_WATER_MAT_PATH)
    atools = unreal.AssetToolsHelpers.get_asset_tools()
    mat = atools.create_asset("M_FxWater", "/Game/Auto", unreal.Material, unreal.MaterialFactoryNew())
    mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_TRANSLUCENT)
    try:
        mat.set_editor_property("translucency_lighting_mode", unreal.TranslucencyLightingMode.TLM_SURFACE)
    except Exception:
        pass
    mel = unreal.MaterialEditingLibrary
    wc = mel.create_material_expression(mat, unreal.MaterialExpressionVectorParameter, -600, -80)
    wc.set_editor_property("parameter_name", "WaterColor")
    wc.set_editor_property("default_value", unreal.LinearColor(0.06, 0.18, 0.22, 1.0))
    mel.connect_material_property(wc, "", unreal.MaterialProperty.MP_BASE_COLOR)
    rg = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -600, 120)
    rg.set_editor_property("parameter_name", "Roughness"); rg.set_editor_property("default_value", 0.06)
    mel.connect_material_property(rg, "", unreal.MaterialProperty.MP_ROUGHNESS)
    mt = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -600, 230)
    mt.set_editor_property("parameter_name", "Metallic"); mt.set_editor_property("default_value", 0.0)
    mel.connect_material_property(mt, "", unreal.MaterialProperty.MP_METALLIC)
    fr = mel.create_material_expression(mat, unreal.MaterialExpressionFresnel, -600, 360)
    bo = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -600, 470)
    bo.set_editor_property("parameter_name", "BaseOpacity"); bo.set_editor_property("default_value", 0.30)
    one = mel.create_material_expression(mat, unreal.MaterialExpressionConstant, -600, 560); one.set_editor_property("r", 1.0)
    lp = mel.create_material_expression(mat, unreal.MaterialExpressionLinearInterpolate, -380, 430)
    mel.connect_material_expressions(bo, "", lp, "A"); mel.connect_material_expressions(one, "", lp, "B")
    mel.connect_material_expressions(fr, "", lp, "Alpha")
    mel.connect_material_property(lp, "", unreal.MaterialProperty.MP_OPACITY)
    mel.recompile_material(mat)
    unreal.EditorAssetLibrary.save_asset(FX_WATER_MAT_PATH)
    return mat


def _ensure_fx_card_material(default_tex=None):
    """FX card master material: translucent two-sided; FxTex (FX image on pure black) x Tint -> BaseColor and
    x Emissive -> glow (bright water/sparks); luminance x Opacity -> opacity (black turns transparent).
    One material covers both soft and additive looks (tuned via Emissive/Opacity)."""
    if unreal.EditorAssetLibrary.does_asset_exist(FX_CARD_MAT_PATH):
        return unreal.load_asset(FX_CARD_MAT_PATH)
    atools = unreal.AssetToolsHelpers.get_asset_tools()
    mat = atools.create_asset("M_FxCard", "/Game/Auto", unreal.Material, unreal.MaterialFactoryNew())
    mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_MASKED)   # masked renders reliably in SceneCapture (translucent HISM does not show)
    mat.set_editor_property("two_sided", True)
    try:
        mat.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)   # unlit: white water/bright mist self-illuminates instead of being shaded gray-black by unlit vertical faces
    except Exception:
        pass
    try:
        mat.set_editor_property("opacity_mask_clip_value", 0.08)
    except Exception:
        pass
    mel = unreal.MaterialEditingLibrary
    if default_tex is None:
        default_tex = unreal.load_asset("/Engine/EngineResources/DefaultTexture")
    ts = mel.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, -700, 0)
    ts.set_editor_property("parameter_name", "FxTex")
    if default_tex is not None:
        try:
            ts.set_editor_property("texture", default_tex)
        except Exception:
            pass
    try:
        ts.set_editor_property("sampler_type", unreal.MaterialSamplerType.SAMPLERTYPE_COLOR)
    except Exception:
        pass
    tint = mel.create_material_expression(mat, unreal.MaterialExpressionVectorParameter, -700, 250)
    tint.set_editor_property("parameter_name", "Tint"); tint.set_editor_property("default_value", unreal.LinearColor(1, 1, 1, 1))
    mulC = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -460, 40)
    mel.connect_material_expressions(ts, "RGB", mulC, "A"); mel.connect_material_expressions(tint, "", mulC, "B")
    mel.connect_material_property(mulC, "", unreal.MaterialProperty.MP_BASE_COLOR)
    em = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -700, 380)
    em.set_editor_property("parameter_name", "Emissive"); em.set_editor_property("default_value", 0.0)
    mulE = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -300, 200)
    mel.connect_material_expressions(mulC, "", mulE, "A"); mel.connect_material_expressions(em, "", mulE, "B")
    mel.connect_material_property(mulE, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    lw = mel.create_material_expression(mat, unreal.MaterialExpressionConstant3Vector, -700, 520)
    lw.set_editor_property("constant", unreal.LinearColor(0.299, 0.587, 0.114, 0.0))
    dp = mel.create_material_expression(mat, unreal.MaterialExpressionDotProduct, -460, 480)
    mel.connect_material_expressions(ts, "RGB", dp, "A"); mel.connect_material_expressions(lw, "", dp, "B")
    op = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -460, 600)
    op.set_editor_property("parameter_name", "Opacity"); op.set_editor_property("default_value", 0.6)
    mulO = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -260, 520)
    mel.connect_material_expressions(dp, "", mulO, "A"); mel.connect_material_expressions(op, "", mulO, "B")
    mel.connect_material_property(mulO, "", unreal.MaterialProperty.MP_OPACITY_MASK)
    mel.recompile_material(mat)
    unreal.EditorAssetLibrary.save_asset(FX_CARD_MAT_PATH)
    return mat


def _fx_quad():
    """Single billboard quad (YZ plane, normal +X, bottom at z=0, ~100 cm); combined with camera-facing yaw there is no crossing or edge-on slicing. Two-sided material shows both faces."""
    sm = unreal.StaticMesh(); smd = sm.create_static_mesh_description(); pg = smd.create_polygon_group()

    def mk(x, y, z, u, v):
        vv = smd.create_vertex(); smd.set_vertex_position(vv, unreal.Vector(x, y, z))
        vi = smd.create_vertex_instance(vv); smd.set_vertex_instance_uv(vi, unreal.Vector2D(u, v), 0)
        return vi
    a = mk(0, -50, 0, 0, 1); b = mk(0, 50, 0, 1, 1); c = mk(0, 50, 100, 1, 0); d = mk(0, -50, 100, 0, 0)
    smd.create_triangle(pg, [a, b, c]); smd.create_triangle(pg, [a, c, d])
    sm.build_from_static_mesh_descriptions([smd], False, False)
    return sm


def _apply_fx_fog(fog):
    """FX fog: override ExponentialHeightFog density/inscattering color (reuses the env multi-name fallback)."""
    if not fog:
        return
    try:
        fogm = _find_or_spawn(unreal.ExponentialHeightFog)
        fc = fogm.get_component_by_class(unreal.ExponentialHeightFogComponent)
        if fc:
            try:
                fc.set_editor_property("fog_density", float(fog.get("density", 0.01)))
            except Exception:
                pass
            col = fog.get("color") or [0.7, 0.75, 0.8]
            lc = unreal.LinearColor(col[0], col[1], col[2], 1.0)
            for prop in ("fog_inscattering_color", "fog_inscattering_luminance", "inscattering_color"):
                try:
                    fc.set_editor_property(prop, lc); break
                except Exception:
                    continue
        unreal.log("fx fog: density=%.3f" % float(fog.get("density", 0.01)))
    except Exception as e:
        unreal.log_warning("fx fog failed: %s" % e)


def _apply_effects(effects):
    """Gemini FX plan -> UE: translucent water plane / alpha card clusters (HISM) / fog. Transient, labeled AutoFx_*."""
    if not effects:
        return
    try:
        mel = unreal.MaterialEditingLibrary
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        cardmesh = None
        for i, L in enumerate(effects):
            prim = L.get("primitive")
            if prim == "fog":
                _apply_fx_fog(L.get("fog"))
                continue
            if prim == "water":
                plane = L.get("plane")
                if not plane:
                    continue
                pl = unreal.load_asset("/Engine/BasicShapes/Plane")
                actor = sub.spawn_actor_from_object(pl, unreal.Vector(plane[0], plane[1], plane[2]))
                actor.set_actor_label("AutoFx_Water_%02d" % i)
                actor.static_mesh_component.set_static_mesh(pl)
                actor.set_actor_scale3d(unreal.Vector(plane[3] / 100.0, plane[4] / 100.0, 1.0))
                col = L.get("color") or [0.06, 0.18, 0.22]
                mic, mic_path = _ensure_mic("MI_FxWater_%02d" % i, _ensure_fx_water_material())
                mel.set_material_instance_vector_parameter_value(mic, "WaterColor", unreal.LinearColor(col[0], col[1], col[2], 1.0))
                mel.set_material_instance_scalar_parameter_value(mic, "BaseOpacity", float(L.get("opacity", 0.3)))
                mel.set_material_instance_scalar_parameter_value(mic, "Roughness", 0.06)
                actor.static_mesh_component.set_material(0, mic)
                unreal.EditorAssetLibrary.save_asset(mic_path)
                unreal.log("fx water %02d @ %s size=%.0fx%.0f" % (i, plane[:3], plane[3], plane[4]))
                continue
            # card / sheet
            inst = L.get("instances") or []
            sheets = L.get("sheets") or []
            tex_url = (L.get("tex_url") or "").strip()
            if not (inst or sheets) or not tex_url:
                continue
            try:
                local = os.path.join(tempfile.gettempdir(), "auto_fx_%d.png" % i)
                with open(local, "wb") as f:
                    f.write(_get(SERVER + tex_url))
                task = unreal.AssetImportTask()
                task.filename = local; task.destination_path = "/Game/AutoImport/Fx"
                task.destination_name = "T_FxCard%02d" % i
                task.automated = True; task.replace_existing = True; task.save = True
                unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
                ftex = unreal.load_asset("/Game/AutoImport/Fx/T_FxCard%02d" % i)
            except Exception as e:
                unreal.log_warning("fx tex import failed: %s" % e); ftex = None
            if ftex is None:
                continue
            if cardmesh is None:
                cardmesh = _fx_quad()
            actor, hism = _spawn_hism("AutoFx_%02d" % i, cardmesh)
            col = L.get("color") or [1.0, 1.0, 1.0]
            mic, mic_path = _ensure_mic("MI_FxCard_%02d" % i, _ensure_fx_card_material(ftex))
            mel.set_material_instance_texture_parameter_value(mic, "FxTex", ftex)
            mel.set_material_instance_vector_parameter_value(mic, "Tint", unreal.LinearColor(col[0], col[1], col[2], 1.0))
            mel.set_material_instance_scalar_parameter_value(mic, "Opacity", float(L.get("opacity", 0.6)))
            mel.set_material_instance_scalar_parameter_value(mic, "Emissive", float(L.get("emissive", 0.0)))
            hism.set_material(0, mic)
            unreal.EditorAssetLibrary.save_asset(mic_path)
            for x, y, z, yaw, pit, sc in inst:                   # single-card particles (rain/dust/mist/birds), facing the camera
                hism.add_instance(unreal.Transform(unreal.Vector(x, y, z), unreal.Rotator(pit, yaw, 0),
                                                   unreal.Vector(sc, sc, sc)), True)
            for sx, sy, cz, w, h, yaw in sheets:                 # vertical water sheet: non-uniform scale (width x height), one piece
                hism.add_instance(unreal.Transform(unreal.Vector(sx, sy, cz - h / 2.0), unreal.Rotator(0, yaw, 0),
                                                   unreal.Vector(w / 100.0, 1.0, h / 100.0)), True)
            unreal.log("fx card %02d %s cards=%d sheets=%d" % (i, L.get("name", ""), len(inst), len(sheets)))
    except Exception as e:
        unreal.log_warning("apply effects failed: %s" % e)


SKY_MAT_PATH = "/Game/Auto/M_Sky"


def _ensure_sky_material():
    """Unlit, two-sided sky master material: emissive color driven by the SkyColor param (auto-created on first use)."""
    if unreal.EditorAssetLibrary.does_asset_exist(SKY_MAT_PATH):
        return unreal.load_asset(SKY_MAT_PATH)
    atools = unreal.AssetToolsHelpers.get_asset_tools()
    mat = atools.create_asset("M_Sky", "/Game/Auto", unreal.Material, unreal.MaterialFactoryNew())
    mat.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)
    mat.set_editor_property("two_sided", True)
    mel = unreal.MaterialEditingLibrary
    sky = mel.create_material_expression(mat, unreal.MaterialExpressionVectorParameter, -400, 0)
    sky.set_editor_property("parameter_name", "SkyColor")
    sky.set_editor_property("default_value", unreal.LinearColor(0.35, 0.55, 0.85, 1.0))
    mel.connect_material_property(sky, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    mel.recompile_material(mat)
    unreal.EditorAssetLibrary.save_asset(SKY_MAT_PATH)
    return mat


def _apply_sky(env, is_indoor=False):
    """Spread the photo-solved sky color over the inside of a giant sky sphere (unlit, two-sided). Indoor scenes build no sky."""
    if not env or is_indoor:
        if is_indoor:
            unreal.log("sky: skipped (indoor)")
        return
    try:
        mat = _ensure_sky_material()
        sphere = unreal.load_asset("/Engine/BasicShapes/Sphere")
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        actor = None
        for a in sub.get_all_level_actors():
            if isinstance(a, unreal.StaticMeshActor) and a.get_actor_label() == "AutoSky":
                actor = a
                break
        if actor is None:
            actor = sub.spawn_actor_from_object(sphere, unreal.Vector(0, 0, 0))
            actor.set_actor_label("AutoSky")
        comp = actor.static_mesh_component
        comp.set_static_mesh(sphere)
        comp.set_editor_property("cast_shadow", False)
        actor.set_actor_location(unreal.Vector(0, 0, 0), False, False)
        actor.set_actor_scale3d(unreal.Vector(20000.0, 20000.0, 20000.0))   # giant dome with the camera inside
        col = env.get("sky_color") or [0.35, 0.55, 0.85]
        mic, mic_path = _ensure_mic("MI_AutoSky", mat)
        unreal.MaterialEditingLibrary.set_material_instance_vector_parameter_value(
            mic, "SkyColor", unreal.LinearColor(col[0], col[1], col[2], 1.0))
        comp.set_material(0, mic)
        unreal.EditorAssetLibrary.save_asset(mic_path)
        unreal.log("sky: color (%.2f,%.2f,%.2f)" % (col[0], col[1], col[2]))
    except Exception as e:
        unreal.log_error("apply sky failed: %s" % e)


def _apply_reflection(env):
    """Place a reflection capture only when needed: wet ground (wet>0.2) or dark/night (lux<5000); in dry daylight
    place none and remove any old one (avoids mirror-like specular everywhere)."""
    try:
        wet = float((env or {}).get("ground_wetness", 0.0))
        lux = float((env or {}).get("sun_intensity_lux", 75000.0))
        need = wet > 0.2 or lux < 5000.0
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        cap = None
        for a in sub.get_all_level_actors():
            if isinstance(a, unreal.SphereReflectionCapture):
                cap = a
                break
        if not need:
            if cap is not None:
                try:
                    sub.destroy_actor(cap)
                except Exception:
                    pass
                unreal.log("reflection: removed (dry daylight)")
            return
        if cap is None:
            cap = sub.spawn_actor_from_class(unreal.SphereReflectionCapture, unreal.Vector(0, 0, 400),
                                             unreal.Rotator(0, 0, 0))
        comp = cap.get_component_by_class(unreal.SphereReflectionCaptureComponent)
        if comp:
            comp.set_editor_property("influence_radius", 30000.0)
        try:
            unreal.EditorLevelLibrary.build_reflection_captures()
        except Exception:
            pass
        unreal.log("reflection: ensured (wet/night)")
    except Exception as e:
        unreal.log_error("apply reflection failed: %s" % e)


def _apply_grade(env):
    """Post-process grade: unbound PostProcessVolume using the solved exposure/saturation/contrast to approach the photo's tone."""
    if not env:
        return
    try:
        ppv = _find_or_spawn(unreal.PostProcessVolume)
        ppv.set_editor_property("unbound", True)
        ppv.set_editor_property("priority", 1.0)
        s = ppv.get_editor_property("settings")
        ev = float(env.get("exposure_ev", 0.0))
        sat = float(env.get("saturation", 1.0))
        con = float(env.get("contrast", 1.0))
        s.set_editor_property("override_auto_exposure_bias", True)
        s.set_editor_property("auto_exposure_bias", ev)
        # clamp the auto-exposure range so bright scenes don't blow out to white
        s.set_editor_property("override_auto_exposure_min_brightness", True)
        s.set_editor_property("auto_exposure_min_brightness", 0.03)
        s.set_editor_property("override_auto_exposure_max_brightness", True)
        s.set_editor_property("auto_exposure_max_brightness", 1.5)
        s.set_editor_property("override_color_saturation", True)
        s.set_editor_property("color_saturation", unreal.Vector4(sat, sat, sat, 1.0))
        s.set_editor_property("override_color_contrast", True)
        s.set_editor_property("color_contrast", unreal.Vector4(con, con, con, 1.0))
        bloom = float(env.get("bloom", 0.5)) * 0.7         # 0..1 -> UE bloom (one notch lower, less glare)
        s.set_editor_property("override_bloom_intensity", True)
        s.set_editor_property("bloom_intensity", bloom)
        ppv.set_editor_property("settings", s)
        unreal.log("grade: ev=%.2f sat=%.2f con=%.2f bloom=%.2f" % (ev, sat, con, bloom))
    except Exception as e:
        unreal.log_error("apply grade failed: %s" % e)


def _apply_lights(lights):
    """Night/dark scenes: place point lights at solved positions (color/lumens/attenuation); clears the previous batch first."""
    for a in _state.get("lights", []):
        _destroy(a)
    _state["lights"] = []
    if not lights:
        return
    try:
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        for l in lights:
            L = l["location"]
            actor = sub.spawn_actor_from_class(unreal.PointLight,
                                               unreal.Vector(L[0], L[1], L[2]), unreal.Rotator(0, 0, 0))
            actor.set_actor_label("AutoLight_%d" % l.get("id", 0))
            c = actor.get_component_by_class(unreal.PointLightComponent)
            if c:
                try:
                    c.set_editor_property("intensity_units", unreal.LightUnits.LUMENS)
                except Exception:
                    pass
                c.set_editor_property("intensity", float(l.get("intensity_lm", 3000.0)))
                c.set_editor_property("attenuation_radius", float(l.get("radius_cm", 2000.0)))
                col = l.get("color") or [1.0, 0.85, 0.6]
                c.set_light_color(unreal.LinearColor(col[0], col[1], col[2], 1.0))
            _state["lights"].append(actor)
        unreal.log("lights: placed %d point lights" % len(_state["lights"]))
    except Exception as e:
        unreal.log_error("apply lights failed: %s" % e)


SKY_HDRI_MAT = "/Game/Auto/M_SkyHDRI"


def _ensure_sky_hdri_material(default_tex):
    """Unlit, two-sided sky material using the SkyTex texture as emissive (wraps the panorama onto the inside of the AutoSky sphere)."""
    if unreal.EditorAssetLibrary.does_asset_exist(SKY_HDRI_MAT):
        return unreal.load_asset(SKY_HDRI_MAT)
    atools = unreal.AssetToolsHelpers.get_asset_tools()
    mat = atools.create_asset("M_SkyHDRI", "/Game/Auto", unreal.Material, unreal.MaterialFactoryNew())
    mat.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)
    mat.set_editor_property("two_sided", True)
    mel = unreal.MaterialEditingLibrary
    ts = mel.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, -400, 0)
    ts.set_editor_property("parameter_name", "SkyTex")
    if default_tex:
        try:
            ts.set_editor_property("texture", default_tex)
        except Exception:
            pass
    mel.connect_material_property(ts, "RGB", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    mel.recompile_material(mat)
    unreal.EditorAssetLibrary.save_asset(SKY_HDRI_MAT)
    return mat


def _apply_hdri(hdri_url):
    """Apply the photo-outpainted sky panorama to the inside of the AutoSky sphere (self-contained; no HDRIBackdrop plugin needed)."""
    if not hdri_url:
        return
    try:
        local = os.path.join(tempfile.gettempdir(), "auto_hdri.png")
        with open(local, "wb") as f:
            f.write(_get(SERVER + hdri_url))
        task = unreal.AssetImportTask()
        task.filename = local
        task.destination_path = "/Game/AutoImport/HDRI"
        task.destination_name = "T_AutoHDRI"
        task.automated = True
        task.replace_existing = True
        task.save = True
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
        tex = unreal.load_asset("/Game/AutoImport/HDRI/T_AutoHDRI")
        if tex is None:
            unreal.log_warning("hdri: import failed")
            return
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        sky = None
        for a in sub.get_all_level_actors():
            if isinstance(a, unreal.StaticMeshActor) and a.get_actor_label() == "AutoSky":
                sky = a
                break
        if sky is None:
            unreal.log("hdri: no AutoSky (indoor?), skipped")
            return
        mat = _ensure_sky_hdri_material(tex)
        mic, mic_path = _ensure_mic("MI_AutoSkyHDRI", mat)
        unreal.MaterialEditingLibrary.set_material_instance_texture_parameter_value(mic, "SkyTex", tex)
        sky.static_mesh_component.set_material(0, mic)
        unreal.EditorAssetLibrary.save_asset(mic_path)
        unreal.log("hdri: photo sky panorama applied to AutoSky")
    except Exception as e:
        unreal.log_error("apply hdri failed: %s" % e)


def _apply_camera(cam):
    """Write the backend-solved camera into the level: move the editor viewport to the "photo view" and place a CameraActor with the same params."""
    if not cam:
        return
    try:
        h = float(cam.get("height_m", 1.6)) * CM_PER_UNIT
        pitch = float(cam.get("pitch_deg", 0.0))     # negative = look down, positive = look up (same sign as UE pitch)
        fov = float(cam.get("fov_deg", 65.0))
        loc = unreal.Vector(0.0, 0.0, h)             # camera above the origin looking along +X (matches the placement coords)
        rot = unreal.Rotator(pitch=pitch, yaw=0.0, roll=0.0)

        # 1) jump the editor viewport straight to this camera
        try:
            unreal.EditorLevelLibrary.set_level_viewport_camera_info(loc, rot)
        except Exception:
            pass

        # 2) place/reuse a CameraActor (for runtime / Sequencer)
        cam_actor = None
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        for a in sub.get_all_level_actors():
            if isinstance(a, unreal.CameraActor) and a.get_actor_label() == "AutoCam":
                cam_actor = a
                break
        if cam_actor is None:
            cam_actor = sub.spawn_actor_from_class(unreal.CameraActor, loc, rot)
            cam_actor.set_actor_label("AutoCam")
        else:
            cam_actor.set_actor_location_and_rotation(loc, rot, False, False)
        cc = cam_actor.get_component_by_class(unreal.CameraComponent)
        if cc:
            cc.set_field_of_view(fov)
        unreal.log("camera: height=%.1fm pitch=%.0f fov=%.0f" % (h / CM_PER_UNIT, pitch, fov))
    except Exception as e:
        unreal.log_error("apply camera failed: %s" % e)


def _capture_view(data):
    """Auto-render the reconstruction from a high 3/4 angle with SceneCapture2D (frames all objects, avoids the
    empty low-camera foreground). SceneCapture carries its own post/exposure, dodging the all-white editor
    viewport screenshot problem. 8-bit RenderTarget -> PNG -> posted to the backend."""
    if not CAPTURE_VIEW:
        return
    try:
        import math
        task_id = data.get("task_id") or "latest"
        cam = data.get("camera", {})
        env = data.get("environment", {})
        fov = float(cam.get("fov_deg", 60.0))
        ev = float(env.get("exposure_ev", 0.0))
        asp = float(data.get("img_aspect", 1.333))
        W = int(CAPTURE_W)
        H = int(round(W / max(0.5, asp)))
        world = unreal.EditorLevelLibrary.get_editor_world()
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        # auto 3/4 framing: center+radius of all OBJ_ actors, pulled back from a high diagonal far enough to frame them all
        ps = [a.get_actor_location() for a in sub.get_all_level_actors()
              if a.get_actor_label().startswith("OBJ_")]
        if not ps:                                   # terrain-only scene (no objects): frame the AutoTerrain bounds
            for a in sub.get_all_level_actors():
                if a.get_actor_label() == "AutoTerrain":
                    org, ext = a.get_actor_bounds(False)
                    ps = [org + unreal.Vector(ext.x, ext.y, ext.z), org - unreal.Vector(ext.x, ext.y, ext.z), org]
                    break
        if not ps:
            unreal.log_warning("capture: nothing to frame, skipped")
            return
        cx = sum(p.x for p in ps) / len(ps)
        cy = sum(p.y for p in ps) / len(ps)
        cz = sum(p.z for p in ps) / len(ps)
        rad = max(300.0, max(((p.x - cx) ** 2 + (p.y - cy) ** 2) ** 0.5 for p in ps))
        fov = 52.0                                                        # presentation FOV
        fit = (rad * 1.25 + 1500.0) / math.tan(math.radians(fov * 0.5))   # pull back far enough to frame everything
        az, elev = math.radians(38.0), math.radians(18.0)                 # 38 deg azimuth, 18 deg elevation (flat-ish, shows less dark ground)
        loc = unreal.Vector(cx - fit * math.cos(elev) * math.cos(az),
                            cy - fit * math.cos(elev) * math.sin(az),
                            cz + fit * math.sin(elev))
        dx, dy, dz = cx - loc.x, cy - loc.y, cz - loc.z
        rot = unreal.Rotator(pitch=math.degrees(math.atan2(dz, (dx * dx + dy * dy) ** 0.5)),
                             yaw=math.degrees(math.atan2(dy, dx)), roll=0.0)
        rt = unreal.RenderingLibrary.create_render_target2d(world, W, H, unreal.TextureRenderTargetFormat.RTF_RGBA8)
        cap = sub.spawn_actor_from_class(unreal.SceneCapture2D, loc, rot)
        cap.set_actor_label("AutoCapture")        # "Auto" prefix so the next batch sweep clears it automatically
        comp = cap.get_component_by_class(unreal.SceneCaptureComponent2D)
        comp.set_editor_property("capture_every_frame", False)   # capture on demand only, not every frame
        comp.set_editor_property("capture_on_movement", False)
        comp.set_editor_property("capture_source", unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR)
        comp.set_editor_property("texture_target", rt)
        comp.set_editor_property("fov_angle", fov)
        comp.set_editor_property("post_process_blend_weight", 1.0)
        pp = comp.get_editor_property("post_process_settings")
        # exposure/saturation/contrast all come from Gemini's solved env values (exposure_ev/saturation/contrast): generic for any photo, no hardcoded magic numbers.
        sat = float(env.get("saturation", 1.0))
        con = float(env.get("contrast", 1.0))
        pp.set_editor_property("override_auto_exposure_bias", True)
        pp.set_editor_property("auto_exposure_bias", ev)                 # ev = Gemini's exposure_ev (matches the photo brightness)
        pp.set_editor_property("override_auto_exposure_min_brightness", True)
        pp.set_editor_property("auto_exposure_min_brightness", 0.03)
        pp.set_editor_property("override_auto_exposure_max_brightness", True)
        pp.set_editor_property("auto_exposure_max_brightness", 1.0)      # technical anti-blowout cap only, scene-independent
        pp.set_editor_property("override_auto_exposure_speed_up", True)
        pp.set_editor_property("auto_exposure_speed_up", 100.0)
        pp.set_editor_property("override_auto_exposure_speed_down", True)
        pp.set_editor_property("auto_exposure_speed_down", 100.0)
        pp.set_editor_property("override_color_saturation", True)
        pp.set_editor_property("color_saturation", unreal.Vector4(sat, sat, sat, 1.0))   # Gemini saturation
        pp.set_editor_property("override_color_contrast", True)
        pp.set_editor_property("color_contrast", unreal.Vector4(con, con, con, 1.0))     # Gemini contrast
        comp.set_editor_property("post_process_settings", pp)
        for _ in range(3):
            comp.capture_scene()
        od = unreal.Paths.convert_relative_path_to_full(
            os.path.join(unreal.Paths.project_saved_dir(), "AutoCapture"))
        unreal.RenderingLibrary.export_render_target(world, rt, od, "ue_view.png")
        png = os.path.join(od, "ue_view.png")
        with open(png, "rb") as f:
            body = f.read()
        try:
            req = urllib.request.Request(SERVER + "/capture/" + task_id, body, {"Content-Type": "image/png"})
            urllib.request.urlopen(req, timeout=30)
            unreal.log("capture: ue_view.png posted (%d bytes, %dx%d) for %s" % (len(body), W, H, task_id))
        except Exception as e:
            unreal.log_warning("capture: post failed (saved at %s): %s" % (png, e))
    except Exception as e:
        unreal.log_warning("capture view failed: %s" % e)


def _tick(delta):
    _state["acc"] += delta
    if _state["acc"] < POLL_SEC:
        return
    _state["acc"] = 0.0
    try:
        data = json.loads(_get(SERVER + "/latest"))
    except Exception:
        return
    tid = data.get("task_id")
    if not tid or tid == _state["last"]:
        return                      # no new task
    _state["last"] = tid
    objs = data.get("objects", [])
    unreal.log("watch: NEW task %s (%d objects)" % (tid, len(objs)))
    try:
        env = data.get("environment", {})
        S = data.get("scene_scale", 10.0)
        indoor = bool(data.get("is_indoor"))
        _place_all(objs, S)
        _apply_sky(env, indoor)                     # sky color (skipped indoors)
        _apply_terrain(data.get("terrain"), env, S)  # same as ue_scene_builder: real relief terrain if a height grid exists, else flat ground
        _apply_dressing((data.get("terrain") or {}).get("dressing"))  # ground dressing (Gemini picks the layers, HISM-instanced)
        _apply_effects(data.get("effects"))         # particle/fluid FX (water/waterfall/rain/fog/dust/birds; Gemini-decided, statically instanced)
        _apply_reflection(env)                      # reflection capture (wet ground / night only)
        _apply_env(env)                             # lighting/atmosphere
        _apply_grade(env)
        _apply_lights(data.get("lights", []))       # artificial point lights (night/dark scenes)
        _apply_hdri(data.get("hdri", ""))           # photo-outpainted sky panorama
        _apply_camera(data.get("camera", {}))
        _capture_view(data)                          # render the "photo view" via SceneCapture2D and post to the backend (reliable output)
    except Exception as e:
        unreal.log_error("watch place failed: %s" % e)


def start():
    if _state["handle"] is None:
        _state["handle"] = unreal.register_slate_post_tick_callback(_tick)
        unreal.log("WATCH started. Upload on the web -> scene auto-builds. Stop with: stop()")


def stop():
    if _state["handle"] is not None:
        unreal.unregister_slate_post_tick_callback(_state["handle"])
        _state["handle"] = None
        unreal.log("WATCH stopped.")


# WARNING: no auto-start. Rebuilding the scene inside a slate tick callback (especially forced SceneCapture renders) crashes the UE render thread (RenderCore).
# The stable path is exec'ing ue_scene_builder.py manually in the UE Python console. Call start() manually only to try the experimental auto mode.
print("ue_task_watcher loaded. 实验性自动模式可能崩 UE；稳定做法用 ue_scene_builder.py。要试自动模式手动调 start()。")
