# UE5 Editor Python: pull /latest, download GLBs, import (with materials/textures)
# and place by world transform.
#
# Run INSIDE Unreal (not a normal python.exe):
#   Edit > Plugins > enable "Python Editor Script Plugin" (restart).
#   Window > Output Log > switch bottom dropdown from "Cmd" to "Python".
#   Type the file path and Enter, or:
#     exec(open(r"<repo>/ue_scene_builder.py", encoding="utf-8").read())

import unreal, json, os, tempfile, urllib.request, math, random

SERVER = "http://127.0.0.1:5001"      # backend (LAN IP if UE is on another machine)
DEST = "/Game/AutoImport"             # where imported assets go

_TASK_TID = ""   # set in run() from UE_PLACE_TASK; "" → fall back to shared paths (backward compatible)


def _apath(name):
    """Per-task generated-asset path under /Game/Auto. With a tid → /Game/Auto/<tid>/<name> (each scene isolated, no cross-level overwrite); without → shared /Game/Auto/<name>."""
    return ("/Game/Auto/%s/%s" % (_TASK_TID, name)) if _TASK_TID else ("/Game/Auto/" + name)


def _aipath(name):
    """Per-task imported-asset path under /Game/AutoImport (mirror of _apath)."""
    n = ("/" + name) if name else ""
    return ("/Game/AutoImport/%s%s" % (_TASK_TID, n)) if _TASK_TID else ("/Game/AutoImport" + n)
CM_PER_UNIT = 100.0                   # glTF meters -> UE centimeters
GROUND_DIM = 0.5                      # (3) global ground darkening factor (smaller = darker)
CAPTURE_VIEW = True                   # render the "photo view" via SceneCapture2D and send to backend (bypasses white viewport screenshots)
CAPTURE_W = 1280                      # capture width (height follows source aspect)
ENABLE_VR = True                      # Tier-0 VR playable loop: terrain/object collision + PlayerStart (eye height, level) + DefaultPawn GameMode; engine built-ins only, project-portable
ENABLE_SKY_ATMOSPHERE = True          # physical sky: SkyAtmosphere replaces the emissive dome; sun direction/color temp driven by Gemini (noon blue / dusk gold / dark night, auto per photo)
CRITIQUE_ROUNDS = 2                   # visual critique loop: build -> screenshot -> Gemini review (/adjustments) -> constrained tweaks -> re-review; 0 disables
SAT_BASE = 0.85                       # global desaturation base (hand-tuned: filmic/faded-memory look, not candy-saturated); 0.85 = -15% vs original


def _get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


_EXT_LUM = None


def _extended_luminance():
    """Whether the project has Extended Default Luminance Range (physical brightness) enabled. If on: sun intensity
    is true lux (daytime ~1e4-1e5) and exposure adaptation returns to the engine's default histogram; if off, the
    legacy conversion formulas and clamps apply. Auto-detected -> the same script yields correct brightness in
    either kind of project (generic mechanism)."""
    global _EXT_LUM
    if _EXT_LUM is None:
        try:
            _EXT_LUM = unreal.SystemLibrary.get_console_variable_int_value(
                "r.DefaultFeature.AutoExposure.ExtendDefaultLuminanceRange") > 0
        except Exception:
            _EXT_LUM = False
        unreal.log("luminance range: %s" % ("EXTENDED (physical lux)" if _EXT_LUM else "legacy"))
    return _EXT_LUM


def _assets_in(path):
    reg = unreal.AssetRegistryHelpers.get_asset_registry()
    out = []
    for ad in reg.get_assets_by_path(path, recursive=True):
        a = ad.get_asset()
        if a:
            out.append(a)
    return out


def _spawn(mesh, loc, rot, scale):
    try:
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        actor = sub.spawn_actor_from_object(mesh, loc, rot)
    except Exception:
        actor = unreal.EditorLevelLibrary.spawn_actor_from_object(mesh, loc, rot)
    actor.set_actor_scale3d(scale)
    return actor


def _find_or_spawn(cls):
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in sub.get_all_level_actors():
        if isinstance(a, cls):
            return a
    return sub.spawn_actor_from_class(cls, unreal.Vector(0, 0, 300), unreal.Rotator(0, 0, 0))


def _apply_env(env):
    """Backend-solved lighting/atmosphere -> sun (direction/intensity/color temp) + skylight + exponential height fog."""
    if not env:
        return
    try:
        sun = _find_or_spawn(unreal.DirectionalLight)
        yaw = float(env.get("sun_yaw_deg", 135.0)) + 180.0     # light arrives from sun_yaw
        pitch = float(env.get("sun_pitch_deg", -45.0))
        sun.set_actor_rotation(unreal.Rotator(pitch=pitch, yaw=yaw, roll=0.0), False)
        c = sun.get_component_by_class(unreal.DirectionalLightComponent)
        if c:
            lux = float(env.get("sun_intensity_lux", 75000.0))
            if env.get("indoor_lux_override"):
                # Indoors: no sun. Window light is fully carried by RectLights (lux x hole area); a directional sun
                # raking through the hole = double lighting + grazing-angle VSM triangle artifacts (PIE showed ghosting)
                c.set_editor_property("intensity", 0.0)
            elif _extended_luminance():
                # The floor only guards daytime (AI sometimes gives absurdly low daytime lux -> all black); the night moon may truly be 0 --
                # night ambient comes from skylight/city glow/practicals; a 10 lux hard floor creates a shadow-casting "ghost moon" that turns night streets into snowfields (observed)
                floor_ = 0.0 if "night" in str(env.get("time_of_day", "")).lower() else 10.0
                c.set_editor_property("intensity", max(floor_, min(130000.0, lux)))   # physical range: true lux passthrough (daytime ~1e4-1e5)
            else:
                c.set_editor_property("intensity", max(2.0, min(20.0, lux / 2000.0)))   # legacy range: lux -> UE sun intensity conversion
            c.set_editor_property("use_temperature", True)
            c.set_editor_property("temperature", float(env.get("color_temp_k", 6500.0)))
            try:
                # god-ray strength from AI (godray_strength 0-1, judged from photo light shafts): 0.2 baseline .. 2.2 strong shafts
                c.set_editor_property("volumetric_scattering_intensity",
                                      0.2 + 2.0 * float(env.get("godray_strength", 0.3)))
            except Exception:
                pass
            try:
                # shadow-edge softness from AI (sun_soft_angle_deg, judged from photo shadow sharpness): clear 0.5 / thin haze 1.5 / overcast 4-8 deg
                c.set_editor_property("light_source_angle", float(env.get("sun_soft_angle_deg", 1.0)))
            except Exception:
                pass
            try:
                c.set_editor_property("indirect_lighting_intensity", 2.5)         # indirect bounce x2.5: keeps Lumen shadows from going pitch black (observed too dark)
            except Exception:
                pass
            try:
                # unique key light: sky-fill is a second directional light; without declaring priority UE warns about ForwardShadingPriority competition
                c.set_editor_property("forward_shading_priority", 10)
            except Exception:
                pass
            try:
                c.set_editor_property("contact_shadow_length", 0.06)            # generic: screen-space contact shadows -> tight dark contact at object/building-ground seams, no more "floating decal" look; scene-agnostic
            except Exception:
                pass
        sky = _find_or_spawn(unreal.SkyLight)
        sc = sky.get_component_by_class(unreal.SkyLightComponent)
        if sc:
            if env.get("indoor_lux_override"):
                # Indoors there is no atmosphere to capture (realtime skylight capture reuses the last outdoor build -> engine "will go black" warning);
                # indoor lighting = window area lights + practicals + Lumen GI, skylight muted
                sc.set_editor_property("intensity", 0.0)
                try:
                    sc.set_editor_property("real_time_capture", False)
                except Exception:
                    pass
            else:
                sc.set_editor_property("intensity", _skylight_intensity(env))   # same source as _apply_sky_atmosphere (night = blue fill 3.0)
                try:
                    sc.recapture_sky()
                except Exception:
                    pass
        fog = _find_or_spawn(unreal.ExponentialHeightFog)
        fc = fog.get_component_by_class(unreal.ExponentialHeightFogComponent)
        # Night/dusk: cap height fog thin. Height fog accumulates along the view ray and smears the AI sky dome (HDRI sphere at 10km) into grey ->
        # "HDRI sky invisible" (observed). At night the AI sky is the lead actor and must stay visible -> keep fog thin; rain/night mood goes to wet reflections + rain particles + horizon fog band.
        # Daytime dense fog whitening the day is correct (real fog = white sky) -> no cap.
        _tod = str(env.get("time_of_day", "")).lower()
        fd_eff = float(env.get("fog_density", 0.003))
        if "night" in _tod or _tod in ("dusk", "dawn"):
            fd_eff = min(fd_eff, 0.006)
        if fc:
            try:
                fc.set_editor_property("fog_density", fd_eff)
            except Exception:
                pass
            try:
                # Aerial perspective / distant fade: lower the height falloff (UE default 0.2 keeps fog ground-hugging, barely fogs the horizon) -> fog fills the air column, distant ground fades to sky color.
                # This is a universal atmosphere profile constant (same every scene); fog AMOUNT still comes from Gemini fog_density (density=0 -> no fog regardless).
                fc.set_editor_property("fog_height_falloff", 0.05)
            except Exception:
                pass
            col = env.get("fog_color") or [0.6, 0.7, 0.85]
            lc = unreal.LinearColor(col[0], col[1], col[2], 1.0)
            for prop in ("fog_inscattering_color", "fog_inscattering_luminance", "inscattering_color"):
                try:
                    fc.set_editor_property(prop, lc); break          # property name differs across UE versions; stop on first hit
                except Exception:
                    continue
            try:                                                     # generic atmosphere: volumetric fog + Tyndall shafts; density driven by Gemini fog_density -> low sun yields god-rays naturally, differs per photo
                fc.set_editor_property("enable_volumetric_fog", True)
                fc.set_editor_property("volumetric_fog_extinction_scale", max(0.3, min(2.8, fd_eff * 70.0)))   # use thinned fd_eff; 70x restrains density (150x smeared the scene into a gold haze read as clouds); cap 2.8 keeps true heavy-fog photos readable
                fc.set_editor_property("volumetric_fog_scattering_distribution", 0.85)   # strong forward scattering -> brighter toward the sun (Tyndall)
            except Exception:
                pass
        unreal.log("env applied: %s/%s sun(%.0f,%.0f) %.0fK fog %.3f"
                   % (env.get("time_of_day", "?"), env.get("weather", "?"), yaw, pitch,
                      float(env.get("color_temp_k", 6500)), fd_eff))
    except Exception as e:
        unreal.log_warning("apply env failed: %s" % e)


def _apply_sky_atmosphere(env):
    """Generic physical sky: SkyAtmosphere renders the sky; sun direction/color temp driven by Gemini env (_apply_env
    already set the sun from sun_yaw/pitch/color_temp) -> noon blue, dusk gold, dark night, automatic per photo.
    Replaces the emissive sky dome (it only showed a pale upper half with warped UVs). The skylight switches to
    realtime atmosphere capture, scaled by Gemini sky_intensity and capped (an over-bright atmosphere blows out
    the scene -- measured: 1.2 blown, 0.5 right)."""
    try:
        eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        acts = eas.get_all_level_actors()
        dome = next((a for a in acts if isinstance(a, unreal.StaticMeshActor)
                     and a.get_actor_label() == "AutoSky"), None)
        dome_is_hdri = False                                   # has an HDRI dome (AI png / real .hdr) taken over the sky?
        if dome is not None:
            try:
                m0 = dome.static_mesh_component.get_material(0)
                dome_is_hdri = bool(m0) and "AutoSkyHDRI" in m0.get_name()
            except Exception:
                dome_is_hdri = False
        sun = next((a for a in acts if isinstance(a, unreal.DirectionalLight)), None)
        if sun:
            dlc = sun.get_component_by_class(unreal.DirectionalLightComponent)
            if dlc:                                             # HDRI dome is the sky -> sun no longer drives the procedural atmosphere disk (dome would occlude it)
                dlc.set_editor_property("atmosphere_sun_light", not dome_is_hdri)
        if not dome_is_hdri and next((a for a in acts if isinstance(a, unreal.SkyAtmosphere)), None) is None:
            atmo = eas.spawn_actor_from_class(unreal.SkyAtmosphere, unreal.Vector(0, 0, 0))
            atmo.set_actor_label("AutoSkyAtmo")
        sky = next((a for a in acts if isinstance(a, unreal.SkyLight)), None)
        if sky:
            slc = sky.get_component_by_class(unreal.SkyLightComponent)
            if slc:
                slc.set_editor_property("real_time_capture", True)
                # Skylight intensity: procedural atmosphere uses _skylight_intensity calibration (3.0 night). But an IsSky LDR (8bit) AI panorama
                # clamps highlights to 1.0 -> captured ambient radiance is far weaker than a real atmosphere -> dead-black shadows (observed scene-wide). Compensate by time of day:
                # night to ~30, dusk ~12, day sky is already bright so only x1.5 (else blowout).
                si = _skylight_intensity(env)
                if dome_is_hdri:
                    tod = str(env.get("time_of_day", "")).lower()
                    si = 12.0 if "night" in tod else (12.0 if tod in ("dusk", "dawn") else 3.0)   # night skylight 12 (30 turned night streets into daylight / whitened the ground; street test); day boosted to 3 (shadows were dead black)
                slc.set_editor_property("intensity", si)
                try:
                    slc.recapture_sky()
                except Exception:
                    pass
        if dome_is_hdri:
            # AI/real HDRI dome = full sky: do not hide it; remove the old procedural atmosphere disk (its aerial-perspective tint mismatches the HDRI).
            # Skylight live-captures this dome -> ambient light comes from the "AI-fabricated sky" (thesis angle: the whole world is AI-made).
            for a in acts:
                if isinstance(a, unreal.SkyAtmosphere) and a.get_actor_label() == "AutoSkyAtmo":
                    try: eas.destroy_actor(a)
                    except Exception: pass
            # IsSky is required for the skylight to light the scene (without it: all black), but a finite sphere cannot be UE's infinitely-far sky ->
            # editor spams "skydome does not cover screen" messages (editor-only warning, not an error; absent in game/VR). Disable on-screen messages to keep the viewport clean.
            try:
                _w = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
                unreal.SystemLibrary.execute_console_command(_w, "DisableAllScreenMessages")
            except Exception:
                pass
            unreal.log("sky: HDRI dome IS the visible sky (SkyLight captures it; procedural atmosphere off)")
        else:
            s = dome                                            # procedural sky: hide the emissive dome (SkyAtmosphere replaces it)
            if s:
                s.static_mesh_component.set_visibility(False)
                s.set_actor_enable_collision(False)
            unreal.log("sky: SkyAtmosphere driven by Gemini sun (%s, %.0fK)"
                       % (env.get("time_of_day", "?"), float(env.get("color_temp_k", 6500))))
    except Exception as e:
        unreal.log_warning("apply sky atmosphere failed: %s" % e)


CLOUD_MAT_PATH = "/Engine/EngineSky/VolumetricClouds/m_SimpleVolumetricCloud"


def _apply_clouds(env):
    """Generic clouds: real VolumetricCloud; coverage/density driven by Gemini cloud_coverage, storm clouds by weather
    -> clear sky none, cloudy/overcast some, thunderstorms storm clouds, automatic per photo. Requires
    SkyAtmosphere (built by _apply_sky_atmosphere). Clear sky (cc<0.1) destroys AutoCloud."""
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in eas.get_all_level_actors():
        if isinstance(a, unreal.VolumetricCloud) and a.get_actor_label() == "AutoCloud":
            try:
                eas.destroy_actor(a)
            except Exception:
                pass
    cc = float(env.get("cloud_coverage", 0.0))
    if cc < 0.1:                                 # clear sky: no clouds
        unreal.log("clouds: clear sky (cc=%.2f) — none" % cc)
        return
    try:
        base = unreal.load_asset(CLOUD_MAT_PATH)
        if base is None:
            unreal.log_warning("clouds: engine cloud material missing")
            return
        actor = eas.spawn_actor_from_class(unreal.VolumetricCloud, unreal.Vector(0, 0, 0))
        actor.set_actor_label("AutoCloud")
        comp = actor.get_component_by_class(unreal.VolumetricCloudComponent)
        mic, _ = _ensure_mic("MI_AutoCloud", base)
        mel = unreal.MaterialEditingLibrary
        cov = max(0.05, min(0.95, 0.1 + cc * 0.8))           # cc -> global coverage
        dens = max(0.2, min(1.0, 0.3 + cc * 0.6))            # more overcast = thicker
        weather = str(env.get("weather", "")).lower()
        storm = 1.0 if any(w in weather for w in ("storm", "thunder", "squall")) else 0.0
        for pname, pval in (("Cloud_GlobalCoverage", cov), ("Cloud_GlobalDensity", dens), ("StormClouds", storm)):
            try:
                mel.set_material_instance_scalar_parameter_value(mic, pname, pval)
            except Exception:
                pass
        if comp:
            comp.set_editor_property("material", mic)
        unreal.log("clouds: cc=%.2f cov=%.2f dens=%.2f storm=%.0f (%s)" % (cc, cov, dens, storm, weather))
    except Exception as e:
        unreal.log_warning("apply clouds failed: %s" % e)


GROUND_MAT_PATH = "/Game/Auto/M_GroundV2"      # new path: radial center->edge falloff (memory island dissolving into the void)


def _ensure_ground_material():
    if unreal.EditorAssetLibrary.does_asset_exist(GROUND_MAT_PATH):
        return unreal.load_asset(GROUND_MAT_PATH)
    atools = unreal.AssetToolsHelpers.get_asset_tools()
    mat = atools.create_asset("M_GroundV2", _apath(""), unreal.Material, unreal.MaterialFactoryNew())
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
        # radial falloff = saturate(1 - 2*distance(UV, center)): center = full color, edge -> black
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
    """Noise-blended multi-texture ground material: 3 sibling decorrelated seamless textures (AlbedoTexA/B/C) blended
    by a low-frequency, world-aligned (XY-only) noise mask through two lerps, plus an even lower-frequency macro
    light/dark layer (MacroNoise) -> no regular repeat grid at distance (breaks the tiling).
    Every sampler carries a default texture + explicit SAMPLERTYPE_COLOR, else the base material fails to compile
    -> degraded dark grey/black. No StaticSwitch (avoids the shader race).
    Scalar params (MIC tweaks without recompiling): TilingU/TilingV, NoiseScale, MacroScale, MacroAmount,
    Roughness, Metallic."""
    if unreal.EditorAssetLibrary.does_asset_exist(TERRAIN_VAR_MAT_PATH):
        return unreal.load_asset(TERRAIN_VAR_MAT_PATH)
    atools = unreal.AssetToolsHelpers.get_asset_tools()
    mat = atools.create_asset("M_AutoTerrainVar", _apath(""), unreal.Material, unreal.MaterialFactoryNew())
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
        return nz                                              # 'function' property is protected and cannot be set; default is fine

    mask = _noise(560, 0.0, 1.0, 2)
    mel.connect_material_expressions(wpns, "", mask, "World Position")

    # two-level lerp of three textures: lerp(lerp(A,B,mask), C, mask)
    lab = mel.create_material_expression(mat, unreal.MaterialExpressionLinearInterpolate, -360, 60)
    mel.connect_material_expressions(tsA, "RGB", lab, "A"); mel.connect_material_expressions(tsB, "RGB", lab, "B")
    mel.connect_material_expressions(mask, "", lab, "Alpha")
    labc = mel.create_material_expression(mat, unreal.MaterialExpressionLinearInterpolate, -200, 200)
    mel.connect_material_expressions(lab, "", labc, "A"); mel.connect_material_expressions(tsC, "RGB", labc, "B")
    mel.connect_material_expressions(mask, "", labc, "Alpha")

    # macro shading: a second, lower-frequency noise -> 1 + noise(-1..1)*MacroAmount, multiplied into blended albedo
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
    """Create/load a saveable MaterialInstanceConstant parented to parent_mat.
    Replaces dynamic material instances: those are transient and lost on save/reload (ground turned into a mirror,
    sky reverted to default); a saved instance + comp.set_material survives saving. Returns (instance, asset path)."""
    path = _apath("%s") % name
    if unreal.EditorAssetLibrary.does_asset_exist(path):
        mic = unreal.load_asset(path)
    else:
        atools = unreal.AssetToolsHelpers.get_asset_tools()
        mic = atools.create_asset(name, _apath(""), unreal.MaterialInstanceConstant,
                                  unreal.MaterialInstanceConstantFactoryNew())
    unreal.MaterialEditingLibrary.set_material_instance_parent(mic, parent_mat)
    return mic, path


# -- Professional blend ground: bundled MA_YX_Blend (ue_library, deployed into the project by ue_deploy_library.py) --
# 3 photo-matched sibling textures feed the Base/Middle/Top layers, decorrelated by masks (white = show that layer);
# Gemini ground_wetness drives puddles (two switches: Use Puddle Layer = master water-look gate + Use_Water_ImageAlpha = area mask).
# Assets not deployed / any step fails -> return None; caller falls back to the procedural material (portable, no crash).
# Params are engine-measured (docs/MATERIAL_ALPHA_MASKS.md): Foam? always OFF (strobes), Water_curve=0 = water full strength (curve decays),
# top_curve=4 + darkened mask = accent-level top layer, masks staggered by use (dirt 02 / rock 03_x60 / water separate) so water never covers dirt.
BLEND_MASTER = "/Game/Tool/MA_Blend/MA_YX_Blend"
BLEND_TEMPLATE = "/Game/Tool/MA_Blend/matellic/MA_YX_Blend_Inst"
BLEND_MIC_PATH = "/Game/Auto/MI_AutoBlendGround"


def _blend_water_mask(wet):
    """Gemini ground_wetness -> puddle area mask (white=water). Thresholds = measured white coverage (03=11%, 01=28%).
    Capped ~28%: ground_wetness means 'wetness', not 'flood' -- even soaked ground is wet sheen + scattered puddles,
    never a full water sheet (observed wet0.9 -> T_Alpha_04 at 71% turned the ground into a flood/lake). The 'wet'
    reflective look mainly comes from lowering ground roughness (whole road sheen), not from water area."""
    if wet < 0.5:
        return "/Game/Tool/Masks/T_Alpha_03"     # slightly wet: sparse small water spots (~11%)
    return "/Game/Tool/Masks/T_Alpha_01"         # wet/very wet: cracks/hollow puddles (~28%, capped, never a flood)


def _ensure_blend_ground_mic(texes, env, til_u, til_v):
    """Build the ground MIC from MA_YX_Blend: texes (>=3, photo-matched) into the three layers + mask decorrelation +
    wetness puddles. Returns the MIC on success; None if the library is not deployed or anything fails (caller
    falls back to procedural). First build compiles shaders async; may show grey for a few seconds."""
    try:
        eal = unreal.EditorAssetLibrary
        need = [BLEND_MASTER, BLEND_TEMPLATE, "/Game/Tool/Masks/T_Alpha_01",
                "/Game/Tool/Masks/T_Alpha_02", "/Game/Tool/Masks/T_Alpha_03",
                "/Game/Tool/Masks/T_Alpha_03_x60", "/Game/Tool/Masks/T_Alpha_04"]
        if any(not eal.does_asset_exist(p) for p in need):
            unreal.log("blend ground: library not deployed (run ue_deploy_library.py), fallback")
            return None
        if len(texes) < 3:
            return None
        mel = unreal.MaterialEditingLibrary
        # Self-heal: masks must be Linear + TC_MASKS (master material samples them as MASKS). Wrong settings fail the whole MI compile
        # and the ground falls back to the default material (hit in a fresh project when repo masks were out of sync). Normalize in place, idempotent.
        for mp in ("/Game/Tool/Masks/T_Alpha_01", "/Game/Tool/Masks/T_Alpha_02",
                   "/Game/Tool/Masks/T_Alpha_03", "/Game/Tool/Masks/T_Alpha_03_x60",
                   "/Game/Tool/Masks/T_Alpha_04", "/Game/Tool/Masks/T_Alpha_05"):
            t_ = unreal.load_asset(mp)
            if t_ and (t_.get_editor_property("srgb")
                       or t_.get_editor_property("compression_settings") != unreal.TextureCompressionSettings.TC_MASKS):
                t_.set_editor_property("srgb", False)
                t_.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_MASKS)
                eal.save_asset(mp)
                unreal.log("blend ground: normalized mask colorspace %s" % mp)
        if not eal.does_asset_exist(BLEND_MIC_PATH):
            # copy from the vendor's working template (all texture slots filled, never hits a NULL compile failure), then re-parent to the repo master
            eal.duplicate_asset(BLEND_TEMPLATE, BLEND_MIC_PATH)
        mic = unreal.load_asset(BLEND_MIC_PATH)
        mel.set_material_instance_parent(mic, unreal.load_asset(BLEND_MASTER))
        for slot, t in (("Base Layer Albedo Map", texes[0]),
                        ("Middle Layer Albedo Map", texes[1]),
                        ("Top Layer Albedo Map", texes[2])):
            mel.set_material_instance_texture_parameter_value(mic, slot, t)
        # mask decorrelation: dirt patches (soft) use 02, top accents use the darkened 03_x60; white = show that layer
        mel.set_material_instance_texture_parameter_value(
            mic, "base_imagealpha", unreal.load_asset("/Game/Tool/Masks/T_Alpha_02"))
        mel.set_material_instance_texture_parameter_value(
            mic, "top_imagealpha", unreal.load_asset("/Game/Tool/Masks/T_Alpha_03_x60"))
        for name, v in (("Use_Base_ImageAlpha", True), ("Use_Top_ImageAlpha", True),
                        ("Foam?", False), ("Moss?", False), ("Leaves?", False), ("Rain?", False)):
            mel.set_material_instance_static_switch_parameter_value(mic, name, v)
        # curve sample positions: 0/1/4 = engine-verified baseline (wateralpha/topalpha decay, basealpha rises)
        for name, v in (("base_curve", 1.0), ("top_curve", 4.0), ("Water_curve", 0.0)):
            mel.set_material_instance_scalar_parameter_value(mic, name, v)
        # Tiling: terrain UV 0-1 fits the footprint; the three layers tile at staggered rates (x1/x0.63/x1.47) -- synced tiling phase-locks
        # the repeat grids and reads as a checkerboard from afar (seen in review); staggering decorrelates masks and frequencies.
        for layer, mul in (("Base", 1.0), ("Middle", 0.63), ("Top", 1.47)):
            mel.set_material_instance_vector_parameter_value(
                mic, "%s Layer Tiling/Offset" % layer,
                unreal.LinearColor(max(1.0, til_u * mul), max(1.0, til_v * mul), 0.0, 0.0))
            # Gemini ground_roughness -> per-layer roughness range (wetness reduction already folded into rough by the caller)
        rough = max(0.05, float((env or {}).get("ground_roughness", 0.85)))
        wet = float((env or {}).get("ground_wetness", 0.0))
        weather_l = str((env or {}).get("weather", "")).lower()
        if any(w in weather_l for w in ("rain", "storm", "drizzle", "wet")):
            wet = max(wet, 0.7)        # consistency: rain -> ground must be wet (observed: AI gave wet=0 on a rainy night, leaving the street to the water sheet = canal)
        elif not any(w in weather_l for w in ("snow", "sleet", "fog", "mist", "damp")):
            wet = min(wet, 0.22)       # clear/no precipitation: ground should not be a wet mirror (observed: weather=clear with wet=0.7 turned a clear night street into a water mirror)
        if str((env or {}).get("ground_material", "")).lower() == "water":
            wet = min(wet, 0.2)        # water-dominated scenes: a walkable lakeshore is not a wet mirror (observed wet=1.00 mirrored the whole shore); the water body itself is the FX water plane
        rough = max(0.32, rough * (1.0 - 0.5 * wet))   # wet roughness floor 0.32: below = oily specular (observed "greasy"); wet look = mild sheen + puddles, not mirror
        for layer in ("Base", "Middle", "Top"):
            mel.set_material_instance_scalar_parameter_value(
                mic, "%s Layer Roughness Min" % layer, max(0.28, rough - 0.18))
            mel.set_material_instance_scalar_parameter_value(
                mic, "%s Layer Roughness Max" % layer, min(1.0, rough + 0.15))
        # Night/dusk ground darkening (generic): photo albedo (often light brick/concrete) passed through + night skylight 12 renders night streets
        # like snowfields (observed on the blend night street). Same rule as the flat-ground fallback: (1-0.3*wet)*GROUND_DIM,
        # but only at night/dusk -- the daytime blend look is validated (forest lake), leave it.
        tod_l = str((env or {}).get("time_of_day", "")).lower()
        dim = (1.0 - 0.3 * wet) * (GROUND_DIM if ("night" in tod_l or tod_l in ("dusk", "dawn")) else 1.0)
        for layer in ("Base", "Middle", "Top"):
            mel.set_material_instance_vector_parameter_value(
                mic, "%s Layer Albedo Tint" % layer, unreal.LinearColor(dim, dim, dim, 1.0))
        # wetness -> puddles (two switches); dry scenes turn both off (static switches, saves shader work)
        if wet >= 0.35:
            for name, v in (("Use Puddle Layer", True), ("Use_Water_ImageAlpha", True), ("Refraction?", True)):
                mel.set_material_instance_static_switch_parameter_value(mic, name, v)
            mel.set_material_instance_texture_parameter_value(
                mic, "water_imagealpha", unreal.load_asset(_blend_water_mask(wet)))
            mel.set_material_instance_scalar_parameter_value(mic, "Water_Depth", 260.0)
            if "night" in tod_l or tod_l in ("dusk", "dawn"):
                # Night-rain puddles (rain-alley test): default Water_Roughness 0.1 = mirror, reflecting the [brightened-for-viewing] night dome
                # back off the ground = glowing blue marble (same family as the night-skirt bug: a boosted sky becomes light pollution in reflections).
                # Night puddles should reflect a dark sky -> roughness 0.28 + water color forced to night-asphalt dark + shallower depth feel (puddles, not lakes). Daytime recipe untouched.
                mel.set_material_instance_scalar_parameter_value(mic, "Water_Roughness", 0.28)
                mel.set_material_instance_scalar_parameter_value(mic, "Water_Depth", 40.0)
                mel.set_material_instance_vector_parameter_value(
                    mic, "Water_Color_01", unreal.LinearColor(0.010, 0.011, 0.014, 1.0))
                mel.set_material_instance_vector_parameter_value(
                    mic, "Water_Color_02", unreal.LinearColor(0.018, 0.020, 0.026, 1.0))
        else:
            for name in ("Use Puddle Layer", "Use_Water_ImageAlpha", "Refraction?"):
                mel.set_material_instance_static_switch_parameter_value(mic, name, False)
        mel.update_material_instance(mic)
        eal.save_asset(BLEND_MIC_PATH)
        unreal.log("blend ground: MA_YX_Blend instance (tile %.0fx%.0f, wet=%.2f%s)"
                   % (til_u, til_v, wet, ", puddles" if wet >= 0.35 else ""))
        return mic
    except Exception as e:
        unreal.log_warning("blend ground failed, fallback to procedural: %s" % e)
        return None


def _apply_ground(env, scene_scale=10.0, footprint=None, base_z=-2.0):
    """Lay the ground: by default a square slab at scene scale; with footprint=(half_x_m,half_y_m,cx_m,cy_m) it fits
    the terrain footprint exactly and drops to the skirt base (base_z) to seal the box bottom -- a reload fallback
    that never peeks out as a second floating plane."""
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
        if ENABLE_VR:                                   # fallback ground must be standable too (reload / terrain-less scenes stay walkable)
            try:
                comp.set_collision_profile_name("BlockAll")
            except Exception:
                pass
        if footprint:                                   # fit the footprint + drop to skirt base (base_z): seals the box bottom, never peeks out
            hx, hy, cx, cy = footprint
            actor.set_actor_location(
                unreal.Vector(float(cx) * CM_PER_UNIT, float(cy) * CM_PER_UNIT, float(base_z)), False, False)
            actor.set_actor_scale3d(unreal.Vector(
                max(2.0, 2.0 * float(hx)), max(2.0, 2.0 * float(hy)), 1.0))
        else:
            actor.set_actor_location(unreal.Vector(0, 0, 0), False, False)
            gs = max(40.0, 6.0 * float(scene_scale))
            actor.set_actor_scale3d(unreal.Vector(gs, gs, 1.0))
        wet = float(env.get("ground_wetness", 0.0))
        rough = max(0.05, float(env.get("ground_roughness", 0.85)) * (1.0 - 0.75 * wet))
        col = env.get("ground_color") or [0.18, 0.18, 0.19]
        dark = (1.0 - 0.3 * wet) * GROUND_DIM           # (3) extra darkening, not a washed-out white sheet
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
        unreal.log_warning("apply ground failed: %s" % e)


SKIRT_FRAC = 0.12       # skirt drop = fraction of relief (short skirt: a solid island, not a towering floating slab)
SKIRT_MIN_CM = 150.0    # minimum skirt drop (1.5m)
EDGE_FADE = 0.30        # outer-ring fraction of the terrain top fading down to z_base (gentle slope into the skirt, no cliff edge). 0.42 too wide -> outer ring fell below the waterline, treated as underwater so vegetation got skipped = empty; 0.30 balances.
# CRITICAL: _build_terrain_mesh (mesh) and _terrain_surface_cm (height used to place vegetation/objects) must use the SAME EDGE_FADE,
# else the mesh edge drops while vegetation stays at raw height -> floating plants at the edges (observed in forest).


def _terrain_base_z(terrain):
    """World z (cm) of the skirt bottom / bottom cap: the terrain's lowest top vertex sits at z=0 (grid min=0), so only
    a short downward seal makes a solid box; sealing too deep (e.g. -1.5*relief) creates a towering floating slab.
    The fallback plane also drops to this height to seal the box bottom."""
    relief = max(1.0, float((terrain or {}).get("relief_m", 2.0))) * CM_PER_UNIT
    return -max(SKIRT_MIN_CM, SKIRT_FRAC * relief)


def _terrain_surface_cm(terrain, X, Y):
    """Sample the terrain surface world height (cm) at world (X,Y) cm; bilinear; no terrain -> 0. Mirrors the _xy
    mapping in _build_terrain_mesh (X<->row r/fore-aft, Y<->col c/lateral)."""
    try:
        grid = (terrain or {}).get("grid"); n = int((terrain or {}).get("n", 0))
        if not grid or n < 2:
            return 0.0
        cx = float(terrain.get("cx_m", 0.0)) * CM_PER_UNIT
        cy = float(terrain.get("cy_m", 0.0)) * CM_PER_UNIT
        hx = max(50.0, float(terrain.get("half_fwd_m", 10.0)) * CM_PER_UNIT)
        hy = max(50.0, float(terrain.get("half_lat_m", 10.0)) * CM_PER_UNIT)
        relief = max(1.0, float(terrain.get("relief_m", 2.0))) * CM_PER_UNIT
        fr = min(max(((X - cx) / (2.0 * hx) + 0.5) * (n - 1.0), 0.0), n - 1.0)
        fc = min(max(((Y - cy) / (2.0 * hy) + 0.5) * (n - 1.0), 0.0), n - 1.0)
        r0 = int(fr); c0 = int(fc); r1 = min(r0 + 1, n - 1); c1 = min(c0 + 1, n - 1)
        tr = fr - r0; tc = fc - c0
        h = (grid[r0][c0] * (1 - tr) * (1 - tc) + grid[r1][c0] * tr * (1 - tc) +
             grid[r0][c1] * (1 - tr) * tc + grid[r1][c1] * tr * tc)
        # apply the same outer-ring EDGE_FADE as _build_terrain_mesh: otherwise edge vegetation/objects sit at raw height while the mesh dropped -> floating
        z_base = _terrain_base_z(terrain)
        d = min(min(fr, (n - 1.0) - fr), min(fc, (n - 1.0) - fc)) / (n - 1.0)   # normalized distance to nearest border (0 = edge)
        tt = max(0.0, min(1.0, d / EDGE_FADE)); fade = tt * tt * (3.0 - 2.0 * tt)
        return z_base + (h * relief - z_base) * fade
    except Exception:
        return 0.0


def _build_terrain_mesh(terrain):
    """Real relief terrain StaticMesh with skirt + bottom cap: top z=grid(0..1)*relief (min=0 rises from z=0), boundary
    verts drop vertically to z_base forming skirt walls, bottom sealed flat -> a solid box; side/low camera angles
    no longer reveal the floating plane below (the 'two surfaces' artifact).
    UE is left-handed: top faces [a,cc,b]/[a,dd,cc] wind upward; skirt winds outward; bottom faces down.
    fast_build=False auto-computes normals (otherwise all black)."""
    grid = terrain["grid"]; n = int(terrain["n"])
    cx = float(terrain.get("cx_m", 0.0)) * CM_PER_UNIT
    cy = float(terrain.get("cy_m", 0.0)) * CM_PER_UNIT
    hx = max(50.0, float(terrain.get("half_fwd_m", 10.0)) * CM_PER_UNIT)
    hy = max(50.0, float(terrain.get("half_lat_m", 10.0)) * CM_PER_UNIT)
    relief = max(1.0, float(terrain.get("relief_m", 2.0))) * CM_PER_UNIT
    z_base = _terrain_base_z(terrain)
    _TPATH = _apath("SM_AutoTerrain")                 # persistent asset: transient StaticMeshes get GC'd across PIE (terrain relief vanished, only the flat fallback remained)
    # UE 5.8 minefield: build_from_static_mesh_descriptions in place on an old asset still holding render resources -> Fatal
    # "FRenderResource was deleted without being released" (editor crash). Detach referencing components first + GC the destroyed
    # actors' render proxies, then delete the old asset and create anew; rebuild in place only if deletion fails.
    eal = unreal.EditorAssetLibrary
    if eal.does_asset_exist(_TPATH):
        sub0 = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        for a0 in sub0.get_all_level_actors():
            try:
                c0 = a0.static_mesh_component if isinstance(a0, unreal.StaticMeshActor) else None
                if c0 and c0.get_editor_property("static_mesh") == unreal.load_asset(_TPATH):
                    c0.set_static_mesh(None)
            except Exception:
                pass
        try:
            unreal.SystemLibrary.collect_garbage()
        except Exception:
            pass
        try:
            eal.delete_asset(_TPATH)
        except Exception as e:
            unreal.log_warning("terrain: old mesh delete failed (%s), in-place rebuild" % e)
    sm = (unreal.load_asset(_TPATH) if eal.does_asset_exist(_TPATH)
          else unreal.AssetToolsHelpers.get_asset_tools().create_asset("SM_AutoTerrain", _apath(""), unreal.StaticMesh, None))
    smd = sm.create_static_mesh_description()
    pg = smd.create_polygon_group()

    def _xy(r, c):
        return (cx + (r / (n - 1.0) - 0.5) * 2.0 * hx,
                cy + (c / (n - 1.0) - 0.5) * 2.0 * hy)

    def _mk(x, y, z, u, vv):
        v = smd.create_vertex(); smd.set_vertex_position(v, unreal.Vector(x, y, z))
        vi = smd.create_vertex_instance(v); smd.set_vertex_instance_uv(vi, unreal.Vector2D(u, vv), 0)
        return vi

    # -- Top surface (outer ring fades radially to z_base, seamless with the surrounding flat ground; roots out "terrain edge cliffs/seams") --
    # Root cause: old top boundary = grid_edge*relief (metres high on slopes) next to outer flat ground at z_base -> metre-high step.
    # Fix: smoothstep the outer EDGE_FADE ring of the top surface down to z_base; boundary = z_base = flat-ground height -> no step.
    # EDGE_FADE is the module constant (must stay in sync with _terrain_surface_cm or edge vegetation floats -- see definition at top of file)
    VI = [[None] * n for _ in range(n)]
    for r in range(n):
        for c in range(n):
            x, y = _xy(r, c)
            d = min(min(r, n - 1 - r), min(c, n - 1 - c)) / (n - 1.0)   # normalized distance to nearest border (0 = edge)
            t = max(0.0, min(1.0, d / EDGE_FADE))
            fade = t * t * (3.0 - 2.0 * t)                              # smoothstep: edge=0, interior=1
            z_top = z_base + (grid[r][c] * relief - z_base) * fade
            VI[r][c] = _mk(x, y, z_top, c / (n - 1.0), r / (n - 1.0))
    for r in range(n - 1):
        for c in range(n - 1):
            a, b, cc, dd = VI[r][c], VI[r + 1][c], VI[r + 1][c + 1], VI[r][c + 1]
            smd.create_triangle(pg, [a, cc, b])      # normal +Z up
            smd.create_triangle(pg, [a, dd, cc])

    # -- Perimeter skirt wall: boundary top verts -> base verts at same XY (clockwise ring order, outward normals) --
    ring = ([(0, c) for c in range(n)] +
            [(r, n - 1) for r in range(1, n)] +
            [(n - 1, c) for c in range(n - 2, -1, -1)] +
            [(r, 0) for r in range(n - 2, 0, -1)])
    # Skirt UV: perimeter arc length -> U, vertical drop -> V (both normalized by footprint width, matching top-surface texel scale).
    # The old version reused top grid UVs: east/west walls had constant u -> one texel column stretched into a full "striped wall" (degenerate mapping).
    vspan = max(1.0, 2.0 * hx)
    arc = 0.0
    for k in range(len(ring)):
        r0, c0 = ring[k]; r1, c1 = ring[(k + 1) % len(ring)]
        x0, y0 = _xy(r0, c0); x1, y1 = _xy(r1, c1)
        zt0 = grid[r0][c0] * relief; zt1 = grid[r1][c1] * relief
        seg = math.hypot(x1 - x0, y1 - y0)
        u0, u1 = arc / vspan, (arc + seg) / vspan
        arc += seg
        t0 = _mk(x0, y0, zt0, u0, 0.0); t1 = _mk(x1, y1, zt1, u1, 0.0)
        b0 = _mk(x0, y0, z_base, u0, (zt0 - z_base) / vspan)
        b1 = _mk(x1, y1, z_base, u1, (zt1 - z_base) / vspan)
        smd.create_triangle(pg, [t0, b0, t1])
        smd.create_triangle(pg, [t1, b0, b1])

    # -- Bottom cap: four corner base verts, normals down --
    ax, ay = _xy(0, 0);         bA = _mk(ax, ay, z_base, 0.0, 0.0)
    bx, by = _xy(0, n - 1);     bB = _mk(bx, by, z_base, 1.0, 0.0)
    ccx, ccy = _xy(n - 1, n - 1); bC = _mk(ccx, ccy, z_base, 1.0, 1.0)
    dx, dy = _xy(n - 1, 0);     bD = _mk(dx, dy, z_base, 0.0, 1.0)
    smd.create_triangle(pg, [bA, bB, bC])
    smd.create_triangle(pg, [bA, bC, bD])

    # VR: build the mesh with a body_setup (simple collision), then set the trace flag to complex-as-simple -> characters/capsules walk the real bumpy triangles
    sm.build_from_static_mesh_descriptions([smd], bool(ENABLE_VR), False)
    if ENABLE_VR:
        try:
            bs = sm.get_editor_property("body_setup")
            if bs:
                bs.set_editor_property("collision_trace_flag", unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE)
        except Exception as e:
            unreal.log_warning("terrain collision setup: %s" % e)
    try:
        unreal.EditorAssetLibrary.save_asset(_apath("SM_AutoTerrain"))   # save to disk -> relief survives PIE round-trips
    except Exception as e:
        unreal.log_warning("save terrain mesh: %s" % e)
    return sm


def _apply_micro_relief(data):
    """Light terrain undulation (design rule: even cities get a little relief, never dead flat): adds a very light
    low-frequency roll onto data['terrain']['grid'] in place. Must run before _carve_water_basin (water areas are
    flattened afterwards; the lake bed is untouched -> no flicker regression). Amplitude fixed ~AMP cm (divided by
    relief to normalize, independent of scene relief_m): negligible amid big natural relief, exactly the micro-roll
    in flat cities."""
    terrain = data.get("terrain") or {}
    grid = terrain.get("grid")
    n = int(terrain.get("n", 0))
    if not grid or n < 2:
        return
    relief_cm = max(1.0, float(terrain.get("relief_m", 2.0))) * CM_PER_UNIT
    AMP = 28.0                                           # micro-relief peak ~28cm (very light)
    for r in range(n):
        u = r / (n - 1.0)
        for c in range(n):
            v = c / (n - 1.0)                            # a few low-frequency sines at staggered freq/phase -> gentle aperiodic rolling
            h = (math.sin(u * 6.2832 * 1.3 + 0.6) * math.cos(v * 6.2832 * 1.1 + 0.2)
                 + 0.55 * math.sin(u * 6.2832 * 2.7 + 1.7) * math.sin(v * 6.2832 * 2.1 + 0.9)
                 + 0.35 * math.cos(u * 6.2832 * 0.7 + 2.3) * math.sin(v * 6.2832 * 3.1 + 0.4))
            grid[r][c] += (AMP * 0.5 * h) / relief_cm    # normalize to a fixed cm amplitude and add onto grid
    terrain["grid"] = grid
    unreal.log("micro relief: gentle ~%.0fcm undulation added to terrain grid (even city not dead-flat)" % AMP)


def _carve_water_basin(data):
    """Water-depth fix (observed "lake became a dried-out quarry"): depth reconstruction projects a reflective lake
    surface into pebble terrain -> pebbles exposed, no basin to fill.
    Mechanism (iron rule: code does geometry): inside the AI-judged water region, flatten the terrain grid into a
    lake bed below the waterline (high shores/hills kept), pin the water plane at the waterline extended to the
    shores; re-seat objects (shore objects on the shore, waterside objects float at the waterline). Gives the real
    water material a real basin to fill.
    Must run after object import and before _apply_terrain builds the mesh. Edits data['terrain']['grid'] in place."""
    try:
        terr = data.get("terrain") or {}
        grid = terr.get("grid"); n = int(terr.get("n", 0))
        eff = data.get("effects")
        if not (grid and n >= 2 and isinstance(eff, list)):
            return
        def _real_water_body(L):                          # wet road != water body: no basin carved for it (regression: a trench got carved into the street)
            nm = str(L.get("name", "")).lower()
            return (L.get("primitive") == "water" and L.get("plane")
                    and not any(k in nm for k in ("road", "street", "pavement", "asphalt", "wet ")))
        water = next((L for L in eff if _real_water_body(L)), None)
        if not water:
            return
        cx = float(terr.get("cx_m", 0.0)) * CM_PER_UNIT
        cy = float(terr.get("cy_m", 0.0)) * CM_PER_UNIT
        hx = max(50.0, float(terr.get("half_fwd_m", 10.0)) * CM_PER_UNIT)
        hy = max(50.0, float(terr.get("half_lat_m", 10.0)) * CM_PER_UNIT)
        relief = max(1.0, float(terr.get("relief_m", 2.0))) * CM_PER_UNIT
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        objs = [a for a in sub.get_all_level_actors()
                if a.get_actor_label().startswith("OBJ_") and isinstance(a, unreal.StaticMeshActor)]
        if not objs:
            return
        wp = water["plane"]
        # Lake region = the AI-judged water footprint (compute_effects already expanded it to "fill visible water"), no more "front edge -> main structure".
        # The old method flooded under the camera in "big trees on the shore" photos (observed: camera soaked in the lake, midground trees became swamp).
        # Camera sits at the origin by convention -> keep at least 3m of shore underfoot.
        # Pull the lake inside the outer fade band (0.85 x half-extent): carving to the mesh edge cuts through the "outer smoothstep to flat skirt"
        # fix and reopens edge cliffs/floating seams (seen in review). The far shore is handled by tree wall + fog.
        ry0 = max(cy - 0.85 * hy, wp[1] - wp[4] / 2.0)
        ry1 = min(cy + 0.85 * hy, wp[1] + wp[4] / 2.0)
        rx0 = max(cx - 0.85 * hx, wp[0] - wp[3] / 2.0, 300.0)
        rx1 = min(cx + 0.80 * hx, wp[0] + wp[3] / 2.0)    # far edge 0.80: leave ~8m of far-shore band for the shoreline tree line (the far view is trees)
        # Photo-evidence rule: a real object whose bbox bottom sits below the water's lower edge stands on shore in front of the water. Depth solving
        # overestimates distance for thin truncated objects (trunks), dropping them into the lake as "swamp trees" -- do not push the lake (that inherits
        # placement error and can erase it); instead pull such objects back to the near-shore band (X only, keep composition Y; photo evidence > depth estimate).
        wbv = float(water.get("water_bottom_v", 2.0))
        front = set()
        if wbv < 1.5:
            res_by = {"OBJ_%02d" % o.get("id", 0): o for o in (data.get("results") or [])}
            for a in objs:
                o_ = res_by.get(a.get_actor_label())
                if not o_ or o_.get("imagined"):
                    continue
                bb = o_.get("bbox") or []
                if len(bb) >= 4 and float(bb[1]) + float(bb[3]) > wbv + 0.02:   # bbox=[x0,y0,w,h] normalized
                    front.add(a.get_actor_label())
        if rx1 - rx0 < 400.0:
            rx1 = rx0 + 400.0                             # water squeezed out by the shore band (photo water too close) -> keep at least a 4m lake
        # Waterline = 20th percentile of terrain inside the lake region - 2cm: the lake settles into the depression the depth map already gave; high shores stay dry.
        # (The old "pin to the largest object's bottom" only fits waterfront structures like boathouses; when the main structure is a shore tree it lifts the waterline to the tree base and floods everything.)
        zs = []
        for r in range(n):
            X = cx + (r / (n - 1.0) - 0.5) * 2.0 * hx
            if not (rx0 <= X <= rx1):
                continue
            for c in range(n):
                Y = cy + (c / (n - 1.0) - 0.5) * 2.0 * hy
                if ry0 <= Y <= ry1:
                    zs.append(grid[r][c] * relief)
        if zs:
            zs.sort()
            waterline = zs[max(0, int(0.20 * len(zs)) - 1)] - 2.0
        else:                                             # fallback: sampling failed (region cropped away) -> old method
            hero = max(objs, key=lambda a: (lambda o, e: e.x * e.y)(*a.get_actor_bounds(False)))
            ho, he = hero.get_actor_bounds(False)
            waterline = (ho.z - he.z) - 12.0
        bed = waterline - {"shallow": 35.0, "medium": 75.0, "deep": 140.0}.get(  # lake depth judged by Gemini (shallow pond / normal lake / deep water)
            str(water.get("water_depth_feel", "medium")), 75.0)
        bed_norm = bed / relief
        lcx, lcy = (rx0 + rx1) / 2.0, (ry0 + ry1) / 2.0       # lake center
        hax = max(200.0, (rx1 - rx0) / 2.0); hay = max(200.0, (ry1 - ry0) / 2.0)
        shore = waterline + 9.0                              # shore = above the waterline -> the rectangular water sheet only shows inside the organic basin (roots out "square remnant")
        # Organic basin: ellipse + angular-noise outline. Inside the basin: center = deep bed rising to shore level near the rim; inside the lake region but outside the basin: raise to shore level
        # (above the water sheet; higher terrain/hills kept as shore); far from the lake region (rad>1.5) leave terrain alone (no water sheet there, nothing to raise).
        for r in range(n):
            X = cx + (r / (n - 1.0) - 0.5) * 2.0 * hx
            for c in range(n):
                Y = cy + (c / (n - 1.0) - 0.5) * 2.0 * hy
                nx = (X - lcx) / hax; ny = (Y - lcy) / hay
                rad = math.hypot(nx, ny)
                if rad > 1.5:
                    continue                                 # far outside the lake region: leave terrain (no water sheet)
                ang = math.atan2(ny, nx)
                outline = 0.80 + 0.11 * math.sin(3.0 * ang + 1.3) + 0.06 * math.sin(7.0 * ang + 2.7)  # irregular shoreline
                orig = grid[r][c] * relief
                if rad <= outline:
                    t = rad / max(0.01, outline)
                    z = bed + (shore - bed) * (t * t)        # deep bed at center -> rises to shore near the rim (quadratic, mostly water)
                else:
                    z = max(orig, shore)                     # shore outside the basin: raise above the waterline to hide the rectangular sheet; higher terrain/hills kept
                grid[r][c] = z / relief
        water["plane"] = [round(lcx, 1), round(lcy, 1), round(waterline, 1),
                          round(2.0 * hax, 1), round(2.0 * hay, 1)]
        data["_waterline_cm"] = waterline
        data["_water_region"] = [rx0, rx1, ry0, ry1]
        pulled = 0
        for a in objs:                                    # re-seat objects (grid changed): shore objects sit on shore, in-water objects float at the waterline
            o, e = a.get_actor_bounds(False)
            L0 = a.get_actor_location()
            if a.get_actor_label() in front and o.x > (rx0 - 40.0) and (ry0 < o.y < ry1):
                ratio = max(0.06, (rx0 - 80.0) / max(60.0, o.x + e.x))   # reproject along the camera ray: pull the far edge in front of the waterline
                nx, ny = o.x * ratio, o.y * ratio         # azimuth unchanged (X/Y scaled equally), trees do not crowd into frame center
                a.set_actor_location(unreal.Vector(L0.x + (nx - o.x), L0.y + (ny - o.y), L0.z), False, False)
                try:                                      # angular size preserved: shrink by the same ratio it is pulled closer (depth solve gave
                    sc0 = a.get_actor_scale3d()           # "far + huge"; pulling in without shrinking = bark filling the screen, observed)
                    a.set_actor_scale3d(unreal.Vector(sc0.x * ratio, sc0.y * ratio, sc0.z * ratio))
                except Exception:
                    pass
                o, e = a.get_actor_bounds(False); L0 = a.get_actor_location()
                pulled += 1
            gz = _terrain_surface_cm(terr, o.x, o.y)
            in_water = (rx0 < o.x < rx1 and ry0 < o.y < ry1)
            target = max(gz, waterline - 4.0) if in_water else gz
            a.set_actor_location(unreal.Vector(L0.x, L0.y, L0.z + (target - (o.z - e.z))), False, False)
        if pulled:
            unreal.log("shore-pull: %d front-evidence object(s) moved to the near bank (photo bbox bottom > water edge)" % pulled)
        unreal.log("water basin carved: waterline=%.0f bed=%.0f region X[%.0f,%.0f] Y[%.0f,%.0f]"
                   % (waterline, bed, rx0, rx1, ry0, ry1))
    except Exception as ex:
        unreal.log_warning("carve water basin failed: %s" % ex)


def _apply_terrain(terrain, env, scene_scale=10.0):
    """With a depth-derived height grid -> build real relief terrain above the persistent flat ground; without one
    (indoor/flat) -> fall back to _apply_ground. The terrain mesh is transient (lost on save), so a saved flat
    AutoGround remains underneath as the reload fallback."""
    grid = (terrain or {}).get("grid")
    if not grid or int((terrain or {}).get("n", 0)) < 2:
        _apply_ground(env, scene_scale)
        return
    try:
        # persistent fallback: flat AutoGround exactly fits the terrain footprint, pushed just below it (transient terrain is lost on save; the flat plane below survives)
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
        if ENABLE_VR:                                   # terrain standable/walkable (with the mesh's complex-as-simple collision)
            try:
                comp.set_collision_profile_name("BlockAll")
                comp.set_collision_enabled(unreal.CollisionEnabled.QUERY_AND_PHYSICS)
            except Exception as e:
                unreal.log_warning("terrain collision profile: %s" % e)
        actor.set_actor_location(unreal.Vector(0, 0, 0), False, False)
        # ground material: prefer the photo-matched seamless tiling texture over the true footprint; else fall back to Gemini flat ground color
        wet = float((env or {}).get("ground_wetness", 0.0))
        rough = max(0.05, float((env or {}).get("ground_roughness", 0.85)) * (1.0 - 0.75 * wet))
        mel = unreal.MaterialEditingLibrary
        urls = list(terrain.get("albedo_urls") or [])
        if not urls and (terrain.get("albedo_url") or "").strip():
            urls = [terrain["albedo_url"]]                       # backward-compat single-image field
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
                task.destination_path = _aipath("Terrain")
                task.destination_name = "T_AutoTerrain%s" % "ABC"[i]
                task.automated = True; task.replace_existing = True; task.save = True
                unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
                t = unreal.load_asset(_aipath("Terrain/T_AutoTerrain%s") % "ABC"[i])
                if t is not None:
                    texes.append(t)
            except Exception as e:
                unreal.log_warning("terrain texture %d import failed: %s" % (i, e))
        if texes:
            orig = len(texes)
            while len(texes) < 3:                               # under 3 textures: cycle-reuse (blend still works, macro shading still breaks repetition)
                texes.append(texes[len(texes) % orig])
            TILE_M = 4.0    # tile physical scale ~4m = real-world span of a photo texture patch (2048px/4m ~= 512px/m, sharp at eye height).
                            # The old span/6 formula always tiled 6 patches (fear of repeats); on a 400m scene one texture stretched to 66m -> mush up close
                            # (rain-alley test). Anti-repetition = 3-layer mask decorrelation + staggered frequencies (x1/0.63/1.47) + macro shading, not huge tiles.
            til_u = min(200.0, max(1.0, 2.0 * hl / TILE_M))     # U <-> lateral (2*half_lat)
            til_v = min(200.0, max(1.0, 2.0 * hf / TILE_M))     # V <-> fore-aft (2*half_fwd)
            # prefer the bundled pro blend material MA_YX_Blend (3-layer mask decorrelation + wetness puddles); not deployed / failed -> procedural noise blend
            mic = _ensure_blend_ground_mic(texes, env, til_u, til_v)
            kind = "MA_YX_Blend"
            if mic is None:
                kind = "var-tex"
                mat = _ensure_terrain_var_material(texes[0])        # first creation uses texture 1 as the default
                mic, mic_path = _ensure_mic("MI_AutoTerrainVar", mat)
                mel.set_material_instance_texture_parameter_value(mic, "AlbedoTexA", texes[0])
                mel.set_material_instance_texture_parameter_value(mic, "AlbedoTexB", texes[1])
                mel.set_material_instance_texture_parameter_value(mic, "AlbedoTexC", texes[2])
                mel.set_material_instance_scalar_parameter_value(mic, "TilingU", til_u)
                mel.set_material_instance_scalar_parameter_value(mic, "TilingV", til_v)
                foot_cm = max(1.0, 2.0 * max(hf, hl) * CM_PER_UNIT)  # noise frequency adapts to footprint: ~10 blend patches per footprint (breaks repeats), macro ~3
                mel.set_material_instance_scalar_parameter_value(mic, "NoiseScale", 18.0 / foot_cm)   # slightly higher frequency -> denser blend patches, breaks the tiling grid
                mel.set_material_instance_scalar_parameter_value(mic, "MacroScale", 6.0 / foot_cm)
                mel.set_material_instance_scalar_parameter_value(mic, "MacroAmount", 0.6)              # stronger macro shading -> further hides repetition/seams
                mel.set_material_instance_scalar_parameter_value(mic, "Roughness", rough)
                mel.set_material_instance_scalar_parameter_value(mic, "Metallic", 0.0)
                unreal.EditorAssetLibrary.save_asset(mic_path)
            comp.set_material(0, mic)
            try:
                # Also write into the asset's material slots: the asset-editor preview shows the real ground (else the engine checkerboard default invites "there's a grid" misreads).
                # Description-built meshes have no slots; set_material(0) silently no-ops -> create slots via the static_materials property.
                _sm_mat = unreal.StaticMaterial()
                _sm_mat.set_editor_property("material_interface", mic)
                mesh.set_editor_property("static_materials", [_sm_mat])
                unreal.EditorAssetLibrary.save_asset(_apath("SM_AutoTerrain"))
            except Exception:
                pass
            # fallback underside gets the same MIC: box bottom/skirt peek-through stays seamless, no off-color
            for g in sub.get_all_level_actors():
                if isinstance(g, unreal.StaticMeshActor) and g.get_actor_label() == "AutoGround":
                    g.static_mesh_component.set_material(0, mic); break
            unreal.log("terrain: %dx%d relief=%.1fm footprint=%.0fx%.0fm  %s(%d imgs, tileU=%.0f,V=%.0f)" % (
                terrain["n"], terrain["n"], float(terrain.get("relief_m", 0)),
                hf * 2, hl * 2, kind, orig, til_u, til_v))
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


# -- Ground dressing: backend-computed instances (rock solids / crossed cards) mass-instanced via HISM (GPU), tinted per layer --
DRESS_MAT_PATH = "/Game/Auto/M_Dress"


def _ensure_dress_material():
    """Generic dressing master material: two-sided lit, BaseColor (vector param) + Roughness/Metallic (scalar params);
    each layer tints via its own MIC."""
    if unreal.EditorAssetLibrary.does_asset_exist(DRESS_MAT_PATH):
        return unreal.load_asset(DRESS_MAT_PATH)
    atools = unreal.AssetToolsHelpers.get_asset_tools()
    mat = atools.create_asset("M_Dress", _apath(""), unreal.Material, unreal.MaterialFactoryNew())
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
    """Alpha grass/plant card material: PlantTex (single plant on pure black) -> BaseColor; its luminance (weighted
    dot product) -> OpacityMask (black -> transparent); Masked blend + two-sided. Each card layer swaps PlantTex
    via a MIC (grass/shrub/cactus...). The sampler node carries a default texture + Color type so it always
    compiles."""
    if unreal.EditorAssetLibrary.does_asset_exist(DRESS_CARD_MAT_PATH):
        return unreal.load_asset(DRESS_CARD_MAT_PATH)
    atools = unreal.AssetToolsHelpers.get_asset_tools()
    mat = atools.create_asset("M_DressCard", _apath(""), unreal.Material, unreal.MaterialFactoryNew())
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
    """Generic low-poly primitives (transient, rebuilt each run), bottom at z=0, ~1m scale; instance scale resizes to
    real size: rock = radially jittered icosahedron (solid rock/pebble/clod); card = two crossed vertical cards
    (grass tufts/weeds/small plants)."""
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
            jit = 0.72 + 0.5 * (((i * 1664525 + 1013904223) % 1000) / 1000.0)   # deterministic radial jitter -> irregular rocks
            s = jit / r * 50.0                                                  # unit sphere -> 50cm radius
            pts.append([x * s, y * s, z * s])
        zmin = min(p[2] for p in pts)
        for p in pts:
            p[2] -= zmin                                                        # bottom at z=0
        vis = [mk(p[0], p[1], p[2], p[0] / 100.0 + 0.5, p[1] / 100.0 + 0.5) for p in pts]
        for a, b, c in faces:
            smd.create_triangle(pg, [vis[a], vis[c], vis[b]])                   # outward normals
    else:  # card: two crossed vertical quads, bottom z=0, ~100cm tall/wide
        for q in (((-50, 0, 0), (50, 0, 0), (50, 0, 100), (-50, 0, 100)),
                  ((0, -50, 0), (0, 50, 0), (0, 50, 100), (0, -50, 100))):
            a = mk(q[0][0], q[0][1], q[0][2], 0, 1); b = mk(q[1][0], q[1][1], q[1][2], 1, 1)
            c = mk(q[2][0], q[2][1], q[2][2], 1, 0); d = mk(q[3][0], q[3][1], q[3][2], 0, 0)
            smd.create_triangle(pg, [a, b, c]); smd.create_triangle(pg, [a, c, d])
    sm.build_from_static_mesh_descriptions([smd], False, False)
    return sm


def _spawn_hism(label, mesh):
    """Spawn an empty Actor + one HISM component (registered via SubobjectDataSubsystem, else it does not render),
    assign the mesh. Returns (actor, hism)."""
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
    try:                                                  # dressing (grass/shrub/gravel) never collides: players walk through, not blocked by grass (design decision)
        hism.set_collision_profile_name("NoCollision")
        hism.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)
    except Exception:
        pass
    return actor, hism


def _dress_tint(hism, i, L):
    """Solid tint (rock layers / textureless cards): M_Dress + a MIC setting BaseColor/Roughness."""
    mel = unreal.MaterialEditingLibrary
    col = L.get("color") or [0.5, 0.5, 0.5]
    mic, mic_path = _ensure_mic("MI_Dress_%02d" % i, _ensure_dress_material())
    mel.set_material_instance_vector_parameter_value(
        mic, "BaseColor", unreal.LinearColor(col[0], col[1], col[2], 1.0))
    mel.set_material_instance_scalar_parameter_value(mic, "Roughness", float(L.get("roughness", 0.9)))
    hism.set_material(0, mic)
    unreal.EditorAssetLibrary.save_asset(mic_path)


# -- Real vegetation (VegetationPack 3D meshes): Gemini picks species per biome; code mass-instances real trees/grass/shrubs (replaces procedural cards) --
VEG_MESH_DIR = "/Game/VegetationPack/Meshes/LOD0/Vegetation/"
_VEG_SPECIES = {   # species -> mesh variants (randomly assigned per instance for natural variety); real-world scale, instances only jitter 0.82-1.22
    "tree_green":  ["SM_LV_GreenAshTree01a_LOD0", "SM_LV_GreenAshTree01b_LOD0", "SM_LV_GreenAshTree01c_LOD0"],
    "tree_autumn": ["SM_LV_SourwoodTree01a_LOD0", "SM_LV_SourwoodTree01b_LOD0", "SM_LV_SourwoodTree01c_LOD0",
                    "SM_LV_SourwoodTree01d_LOD0", "SM_LV_SourwoodTree01e_LOD0"],
    "tree_dead":   ["SM_LV_PineBranches01_Dead_LOD0"],
    "bush":        ["SM_MV_Bush01a_LOD0", "SM_MV_Bush01b_LOD0", "SM_MV_Bush02a_LOD0", "SM_MV_Bush02b_LOD0"],
    "ivy":         ["SM_MV_Ivy01a_LOD0", "SM_MV_Ivy01b_LOD0", "SM_MV_Ivy01c_LOD0", "SM_MV_Ivy01d_LOD0",
                    "SM_MV_Ivy02a_LOD0", "SM_MV_Ivy02b_LOD0", "SM_MV_Ivy02c_LOD0"],
    "plant":       ["SM_MV_Plant01a_LOD0", "SM_MV_Plant01b_LOD0", "SM_MV_Plant02a_LOD0", "SM_MV_Plant02b_LOD0",
                    "SM_MV_Plant02c_LOD0"],
    "fern":        ["SM_SV_LadyFern01a_LOD0", "SM_SV_LadyFern01b_LOD0"],
    "grass_tall":  ["SM_SV_TallGrass01_LOD0", "SM_SV_TallGrass02_LOD0", "SM_SV_TallGrass03_LOD0", "SM_SV_TallGrass04_LOD0"],
    "grass_short": ["SM_SV_Grass01a_LOD0", "SM_SV_Grass01b_LOD0", "SM_SV_Grass02a_LOD0", "SM_SV_Grass02b_LOD0"],
    "grass_moor":  ["SM_SV_MoorGrass01a_LOD0", "SM_SV_MoorGrass01b_LOD0"],
    "weed":        ["SM_SV_DinerBooth_Weed01_LOD0", "SM_SV_DinerBooth_Weed02_LOD0"],
}


# Per-species instance caps by body size (big items sparse, grass dense): Gemini densities assume small groundcover, but big meshes (trees / 1.6m shrubs)
# at the same density become a wall -> cap by size (geometry/perf safety, not aesthetics); over cap -> even-stride subsample down.
_VEG_MAX = {"tree_green": 600, "tree_autumn": 600, "tree_dead": 400, "bush": 4000, "plant": 5000,
            "ivy": 8000, "fern": 9000, "grass_tall": 26000, "grass_short": 30000, "grass_moor": 28000, "weed": 9000}


def _instance_species(i, species, inst, terrain, waterline):
    """The layer is a real vegetation species -> load VegetationPack real meshes (one HISM per variant, instances
    rotated across variants for variety) and mass-instance. Real meshes are true scale -> instances only jitter
    0.82-1.22 naturally (no card-style size_m x 1m scaling). Returns the number placed."""
    cap = _VEG_MAX.get(species, 20000)
    if len(inst) > cap:                                   # cap big items by size, even-stride subsample (keeps distribution)
        st = int(round(len(inst) / float(cap)))
        inst = inst[::max(1, st)]
        unreal.log("veg %s: %d → cap %d (stride %d)" % (species, cap, len(inst), max(1, st)))
    meshes = [m for m in (unreal.load_asset(VEG_MESH_DIR + n) for n in _VEG_SPECIES[species]) if m]
    if not meshes:
        unreal.log_warning("veg species %s: no meshes loaded (VegetationPack missing?)" % species)
        return 0
    hisms = [_spawn_hism("AutoDress_%02d_%s%d" % (i, species[:6], vi), m)[1] for vi, m in enumerate(meshes)]
    kept = 0
    for k, t in enumerate(inst):
        x, y, z, yaw, _sc = t
        if terrain is not None:
            gz = _terrain_surface_cm(terrain, x, y)
            if waterline is not None and gz < waterline - 4.0:
                continue                          # below the waterline = in the lake, drop
            z = gz + 1.0
        s = 0.82 + 0.40 * (((k * 2654435761) % 997) / 997.0)   # deterministic natural scale jitter
        hisms[k % len(hisms)].add_instance(
            unreal.Transform(unreal.Vector(x, y, z), unreal.Rotator(0.0, yaw, 0.0), unreal.Vector(s, s, s)), True)
        kept += 1
    return kept


def _apply_dressing(dressing, terrain=None, waterline=None):
    """Backend-computed dressing instances per layer -> HISM mass instancing (GPU); rock layers solid-tinted, card
    layers (grass/plants) use single-plant black-background images as alpha cards. Transient (rebuilt each run),
    tagged AutoDress_*. If terrain is given, re-seat every instance on the POST-CARVE terrain (else pebbles float
    at raw height); if waterline is given, drop instances below it (observed "white pebbles floating on the lake":
    dressing was scattered on raw terrain, then the basin carve turned that area into water)."""
    if not dressing:
        return
    try:
        mel = unreal.MaterialEditingLibrary
        cache = {}; total = 0
        # render-density cap (lake test: 86k pebbles = a gravel bowl): even-stride subsample to a sane visible count, keeps spatial distribution unclumped
        planned = sum(len(L.get("instances") or []) for L in dressing)
        stride = max(1, int(round(planned / 28000.0)))       # GPU safety cap only (trust Gemini density, no hardcoded aesthetics)
        if stride > 1:
            unreal.log("dressing: %d planned → stride %d subsample (~%d, 安全上限)" % (planned, stride, planned // stride))
        for i, L in enumerate(dressing):
            shape = L.get("shape", "rock")
            inst0 = L.get("instances", [])
            if stride > 1:
                inst0 = inst0[::stride]            # even-stride subsample (not random; keeps coverage even, no holes)
            # -- real vegetation: shape==plant with a known species -> instance VegetationPack real 3D meshes (trees/grass/shrubs) --
            species = str(L.get("species", "")).strip()
            if shape == "plant" and species in _VEG_SPECIES:
                kept = _instance_species(i, species, inst0, terrain, waterline)
                total += kept
                unreal.log("dressing veg %s: %s ×%d" % (species, L.get("name", species), kept))
                continue
            # -- original procedural rock (solid) / card (crossed quads) --
            mesh = cache.get(shape)
            if mesh is None:
                mesh = _dress_archetype("card" if shape == "plant" else shape); cache[shape] = mesh
            actor, hism = _spawn_hism("AutoDress_%02d" % i, mesh)
            tex_url = (L.get("tex_url") or "").strip()
            if shape == "card" and tex_url:                 # alpha grass/plant cards: import the black-background single-plant image
                ptex = None
                try:
                    local = os.path.join(tempfile.gettempdir(), "auto_dress_card_%d.png" % i)
                    with open(local, "wb") as f:
                        f.write(_get(SERVER + tex_url))
                    task = unreal.AssetImportTask()
                    task.filename = local; task.destination_path = _aipath("Dress")
                    task.destination_name = "T_DressCard%02d" % i
                    task.automated = True; task.replace_existing = True; task.save = True
                    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
                    ptex = unreal.load_asset(_aipath("Dress/T_DressCard%02d") % i)
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
            kept = 0
            for x, y, z, yaw, sc in inst0:
                if terrain is not None:             # re-seat on the post-carve terrain (else rocks float at raw height)
                    gz = _terrain_surface_cm(terrain, x, y)
                    if waterline is not None and gz < waterline - 4.0:
                        continue                    # below the waterline = in the lake, drop (observed "white pebbles floating on the lake")
                    z = gz + 1.0
                hism.add_instance(unreal.Transform(unreal.Vector(x, y, z), unreal.Rotator(0, yaw, 0),
                                                   unreal.Vector(sc, sc, sc)), True)
                kept += 1
            total += kept
            unreal.log("dressing layer %s: %s ×%d" % (shape, L.get("name", shape), kept))
        unreal.log("dressing: %d instances across %d layers" % (total, len(dressing)))
    except Exception as e:
        unreal.log_warning("apply dressing failed: %s" % e)


# -- Particle/fluid FX: water translucent plane / card alpha groups (HISM) / fog. Static single-frame (no animation). Tagged AutoFx_* --
FX_WATER_MAT_PATH = "/Game/Auto/M_FxWater"
FX_CARD_MAT_PATH = "/Game/Auto/M_FxCard"


def _ensure_fx_water_material():
    """Water surface master material: translucent (BLEND_TRANSLUCENT, TLM_SURFACE), tinted by WaterColor; Fresnel
    drives opacity (more solid at grazing angles, clearer from above)."""
    if unreal.EditorAssetLibrary.does_asset_exist(FX_WATER_MAT_PATH):
        return unreal.load_asset(FX_WATER_MAT_PATH)
    atools = unreal.AssetToolsHelpers.get_asset_tools()
    mat = atools.create_asset("M_FxWater", _apath(""), unreal.Material, unreal.MaterialFactoryNew())
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
    """FX card master material: translucent two-sided; FxTex (effect on pure black) x Tint -> BaseColor and
    x Emissive -> glow (bright water/sparks); luminance x Opacity -> opacity (black -> transparent). One material
    covers soft/additive (via the Emissive/Opacity dials)."""
    if unreal.EditorAssetLibrary.does_asset_exist(FX_CARD_MAT_PATH):
        return unreal.load_asset(FX_CARD_MAT_PATH)
    atools = unreal.AssetToolsHelpers.get_asset_tools()
    mat = atools.create_asset("M_FxCard", _apath(""), unreal.Material, unreal.MaterialFactoryNew())
    mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_MASKED)   # masked renders reliably in SceneCapture (translucent HISM does not show)
    mat.set_editor_property("two_sided", True)
    try:
        mat.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)   # unlit: white water/bright mist stays self-bright instead of being shaded grey-black as lit vertical faces
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
    """Single billboard mesh (YZ plane, +X normal, bottom z=0, ~100cm); paired with camera-facing yaw -> no crossing,
    no edge-on views. Two-sided material shows both faces."""
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
    """FX fog: override ExponentialHeightFog density/scatter color (reuses env's multi-name fallbacks)."""
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


# Live particles (Niagara) inline adapter: systems = the hand-built trio in /Game/Tool/FX (deployed via ue_deploy_library),
# measured names User.User_*, Box/VelZ are vec3 (see ue_fx_presets.py = the interactive-version source of truth).
# Preset base params (semantic) -> overridden at spawn by layer color/extent/density. Library missing -> skip that layer (graceful degradation).
_NS_SYS = {"soft": "/Game/Tool/FX/NS_Particles", "leaf": "/Game/Tool/FX/NS_Leaves", "mesh": "/Game/Tool/FX/NS_Mesh"}
_NS_PRESETS = {
    # rain/waterfall/fountain moved off NS_Particles (its additive material is mathematically unreadable in bright physical scenes; lab-tested, even radiance conversion cannot save it)
    "fountain":  ("leaf", dict(SpawnRate=300, Size=12, VelZ=400, VelSpread=120, GravityZ=-600, Lifetime=2.0, RotRate=0, WindXY=10)),
    "waterfall": ("leaf", dict(SpawnRate=400, Size=26, VelZ=-300, VelSpread=40, GravityZ=-800, Lifetime=2.0, RotRate=0, WindXY=5)),
    # Rain (three lab lessons): NS_Leaves has built-in drag (leaf terminal velocity ~0.9m/s), initial velocity eaten within 0.2s -> gravity must be -3000 to overpower
    # the drag (terminal ~10m/s); round-drop sprites (random-orientation square cards = "rocking snowflakes"); lifetime must cover the full fall at terminal velocity (computed in code)
    "rain":      ("leaf", dict(SpawnRate=600, Size=6, VelZ=-1200, VelSpread=10, GravityZ=-3000, Lifetime=1.6, RotRate=0, WindXY=8)),  # Size 6: thin rain threads (10 became thick white bars up close)
    "snow":      ("leaf", dict(SpawnRate=200, Size=8, VelZ=-80, VelSpread=60, GravityZ=-150, Lifetime=6.0, RotRate=15, WindXY=80)),
    # dust/embers moved to the leaf family (spin ~0 = soft sprites): NS_Particles has no material parameter so its wiring cannot be swapped,
    # and its ADDITIVE material is mathematically invisible in physically lit scenes (measured); the leaf family accepts our M_FxSprite translucent material
    "dust":      ("leaf", dict(SpawnRate=60, Size=4, VelZ=5, VelSpread=20, GravityZ=0, Lifetime=5.0, RotRate=8, WindXY=60)),
    "embers":    ("leaf", dict(SpawnRate=80, Size=6, VelZ=200, VelSpread=80, GravityZ=-400, Lifetime=2.5, RotRate=20, WindXY=40)),
    "leaves":    ("leaf", dict(SpawnRate=40, Size=22, VelZ=-70, VelSpread=45, GravityZ=-60, Lifetime=7.0, RotRate=110, WindXY=110)),   # terminal ~0.9m/s: falls more than it drifts (was reported "moving horizontally")
    "petals":    ("leaf", dict(SpawnRate=30, Size=14, VelZ=-60, VelSpread=35, GravityZ=-50, Lifetime=8.0, RotRate=80, WindXY=80)),
    "ash":       ("leaf", dict(SpawnRate=50, Size=8, VelZ=-10, VelSpread=30, GravityZ=-60, Lifetime=9.0, RotRate=60, WindXY=120)),
    "debris":    ("leaf", dict(SpawnRate=40, Size=12, VelZ=-20, VelSpread=50, GravityZ=-120, Lifetime=7.0, RotRate=160, WindXY=260)),  # wind-blown scraps/litter: low tumbling with the scrap texture (burst debris uses gravel)
    "smoke":     ("leaf", dict(SpawnRate=25, Size=60, VelZ=75, VelSpread=22, GravityZ=4, Lifetime=6.0, RotRate=6, WindXY=90)),    # smoke column: big soft puffs rising slowly on the wind (attach=top for chimneys)
    "steam":     ("leaf", dict(SpawnRate=35, Size=38, VelZ=115, VelSpread=30, GravityZ=0, Lifetime=2.4, RotRate=4, WindXY=45)),   # steam: rises fast, dissipates fast
    "fireflies": ("leaf", dict(SpawnRate=45, Size=6, VelZ=6, VelSpread=24, GravityZ=0, Lifetime=0.9, RotRate=0, WindXY=35)),      # fireflies: short-lived light points = flicker illusion
    "mist":      ("leaf", dict(SpawnRate=3, Size=320, VelZ=2, VelSpread=4, GravityZ=0, Lifetime=14.0, RotRate=2, WindXY=55)),     # mist banks: huge low-opacity soft sprites drifting at ground level
    "gravel":    ("mesh", dict(SpawnRate=60, Size=4, VelZ=150, VelSpread=120, GravityZ=-900, Lifetime=2.5, RotRate=240)),
}
_NS_FLOATS = {"SpawnRate": "User.User_SpawnRate", "Size": "User.User_Size", "Lifetime": "User.User_Lifetime",
              "GravityZ": "User.User_GravityZ", "VelSpread": "User.User_VelSpread",
              "RotRate": "User.User_RotRate", "WindXY": "User.User_WindXY"}


# Sprite textures for leaf-family presets (NS_Leaves with User_Texture unset = default snowflake sprite -> hollow outlines):
# the repo bundles PIL-procedural textures, fetched via /fxsprite/ and imported on demand -> self-contained in any new project.
_FX_SPRITE_BY_PRESET = {"leaves": "leaf", "petals": "petal", "ash": "ash", "debris": "scrap",
                        "dust": "mote", "embers": "mote", "snow": "mote",
                        "smoke": "fog", "steam": "fog", "fireflies": "mote", "mist": "fog",
                        "rain": "mote", "waterfall": "streak", "fountain": "mote"}   # rain = round drop (streak cards at random orientations look broken)


def _ensure_fx_sprite(key):
    path = "/Game/Tool/FX/Sprites/T_Fx" + key.capitalize()
    tex = unreal.load_asset(path)
    if tex:
        return tex
    try:
        png = os.path.join(tempfile.gettempdir(), "fxsprite_%s.png" % key)
        urllib.request.urlretrieve(SERVER + "/fxsprite/" + key + ".png", png)
        task = unreal.AssetImportTask()
        task.set_editor_property("filename", png)
        task.set_editor_property("destination_path", "/Game/Tool/FX/Sprites")
        task.set_editor_property("destination_name", "T_Fx" + key.capitalize())
        task.set_editor_property("automated", True)
        task.set_editor_property("save", True)
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
        return unreal.load_asset(path)
    except Exception as e:
        unreal.log_warning("fx sprite import failed (%s): %s" % (key, e))
        return None


def _ensure_fx_master():
    """Bridge-written particle sprite master material (fully controlled wiring; sidesteps unknown hand-built-material wiring
    + ADDITIVE washout under physical lighting): UNLIT + TRANSLUCENT + two-sided;
    Emissive = Tex.rgb x ParticleColor.rgb; Opacity = Tex.a x ParticleColor.a.
    Brightness is fed scene-nit levels via User_Color (see _fx_lum_scale); leaves can read as dark silhouettes
    (translucency can occlude, additive cannot)."""
    path = "/Game/Tool/FX/Sprites/M_FxSprite"
    m = unreal.load_asset(path)
    if m:
        return m
    try:
        atools = unreal.AssetToolsHelpers.get_asset_tools()
        m = atools.create_asset("M_FxSprite", "/Game/Tool/FX/Sprites",
                                unreal.Material, unreal.MaterialFactoryNew())
        m.set_editor_property("blend_mode", unreal.BlendMode.BLEND_TRANSLUCENT)
        m.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)
        m.set_editor_property("two_sided", True)
        mel = unreal.MaterialEditingLibrary
        tex = mel.create_material_expression(m, unreal.MaterialExpressionTextureSampleParameter2D, -700, -100)
        tex.set_editor_property("parameter_name", "Tex")
        t0 = unreal.load_asset("/Game/Tool/FX/Sprites/T_FxMote") or _ensure_fx_sprite("mote")
        if t0:
            tex.set_editor_property("texture", t0)
        pc = mel.create_material_expression(m, unreal.MaterialExpressionParticleColor, -700, 150)
        mc = mel.create_material_expression(m, unreal.MaterialExpressionMultiply, -380, -60)
        ma = mel.create_material_expression(m, unreal.MaterialExpressionMultiply, -380, 160)
        mel.connect_material_expressions(tex, "RGB", mc, "A")
        mel.connect_material_expressions(pc, "RGB", mc, "B")
        mel.connect_material_expressions(tex, "A", ma, "A")
        mel.connect_material_expressions(pc, "A", ma, "B")
        df = mel.create_material_expression(m, unreal.MaterialExpressionDepthFade, -160, 160)
        df.set_editor_property("fade_distance_default", 150.0)   # soft fade where intersecting ground/objects (standard fog-sprite trick; fixes "flat + hard cut")
        mel.connect_material_expressions(ma, "", df, "InOpacity")
        mel.connect_material_property(mc, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
        mel.connect_material_property(df, "", unreal.MaterialProperty.MP_OPACITY)
        mel.recompile_material(m)
        unreal.EditorAssetLibrary.save_asset(path)
        return m
    except Exception as e:
        unreal.log_warning("fx master material failed: %s" % e)
        return None


def _ensure_fx_sprite_mic(key):
    """Sprite MIC (engine-verified: NS_Leaves' User_Texture is a MaterialInterface param -- feed a material, not a
    texture): our master M_FxSprite with 'Tex' swapped to that sprite image -> one persistent MIC per sprite."""
    mic_path = "/Game/Tool/FX/Sprites/MI_FxS_" + key.capitalize()
    mic = unreal.load_asset(mic_path)
    if mic:
        return mic
    tex = _ensure_fx_sprite(key)
    parent = _ensure_fx_master()
    if not tex or not parent:
        return None
    try:
        atools = unreal.AssetToolsHelpers.get_asset_tools()
        mic = atools.create_asset("MI_FxS_" + key.capitalize(), "/Game/Tool/FX/Sprites",
                                  unreal.MaterialInstanceConstant, unreal.MaterialInstanceConstantFactoryNew())
        mic.set_editor_property("parent", parent)
        unreal.MaterialEditingLibrary.set_material_instance_texture_parameter_value(mic, "Tex", tex)
        unreal.MaterialEditingLibrary.update_material_instance(mic)
        unreal.EditorAssetLibrary.save_asset(mic_path)
        return mic
    except Exception as e:
        unreal.log_warning("fx sprite MIC failed (%s): %s" % (key, e))
        return None


def _playable_center(sub):
    """Playable-area center (player spawn): prefer AutoPlayerStart, fall back to the OBJ centroid. Centers effects
    that must cover the player (rain etc.)."""
    acts = sub.get_all_level_actors()
    ps = next((a for a in acts if a.get_actor_label().startswith("AutoPlayerStart")), None)
    if ps:
        l = ps.get_actor_location()
        return [l.x, l.y, l.z]
    objs = [a for a in acts if a.get_actor_label().startswith("OBJ_")]
    if objs:
        return [sum(a.get_actor_location().x for a in objs) / len(objs),
                sum(a.get_actor_location().y for a in objs) / len(objs),
                min(a.get_actor_location().z for a in objs)]
    return None


def _spawn_fx_niagara(i, L, sub):
    """Spawn live particles per compute_effects' niagara placements (single pos or multiple spots)."""
    info = L.get("niagara") or {}
    preset = info.get("preset", "")
    if preset not in _NS_PRESETS:
        return
    kind, base = _NS_PRESETS[preset]
    sys_asset = None
    if preset == "rain":
        # Dedicated rain system (velocity-aligned stretched streaks + no drag; hand-built once per docs/NS_RAIN_SETUP.md):
        # NS_Leaves cannot do rain at the asset level (random-orientation dots + drag drift = "white snowflakes", observed); if present it takes priority
        sys_asset = unreal.load_asset("/Game/Tool/FX/NS_Rain")
        if sys_asset is not None:
            base = dict(base, GravityZ=-980, VelZ=-1300)   # the no-drag system uses real dynamics
    if sys_asset is None:
        sys_asset = unreal.load_asset(_NS_SYS[kind])
    if sys_asset is None:
        unreal.log_warning("fx niagara '%s': system not deployed (%s), skipped" % (preset, _NS_SYS[kind]))
        return
    spots = info.get("spots") or [info.get("pos") or [0, 0, 300]]
    if preset == "rain":
        # Rain must cover the player (else it is a fixed rain column; walk out of the box and there is no rain): centered high over the playable area + a box spanning it all,
        # so the player is always in rain (true per-frame camera following needs a custom pawn Blueprint; this box is equivalent for bounded scenes)
        cen = _playable_center(sub)
        if cen is not None:
            spots = [[cen[0], cen[1], cen[2] + 2600.0]]
            # the box spans the ACTUAL playable area (observed "rain area too small": hardcoded 4500 covered only half a 97m street);
            # remember Gemini's planned box (box_xy_plan) -- _spawn_one_ns scales spawn rate by area so density is not diluted
            ext = 4500.0
            try:
                g_ = next((a for a in sub.get_all_level_actors() if a.get_actor_label() == "AutoGround"), None)
                if g_ is not None and g_.static_mesh_component.is_visible():
                    ext = max(ext, g_.get_actor_scale3d().x * 100.0 + 800.0)
                else:
                    t_ = next((a for a in sub.get_all_level_actors() if a.get_actor_label() == "AutoTerrain"), None)
                    if t_ is not None:
                        _o, _e = t_.get_actor_bounds(False)
                        ext = max(ext, 2.0 * max(_e.x, _e.y) + 800.0)
                ext = min(ext, 12000.0)   # cap 120m: rain only needs to cover the player + readable range; following 400m city terrain
                                          # would hit the 20k/s rate cap and dilute into drizzle (density conservation breaks)
            except Exception:
                pass
            info = dict(info, box_xy_plan=float(info.get("box_xy", 2000.0)),
                        box_xy=max(float(info.get("box_xy", 3000.0)), ext))
    for k, pos in enumerate(spots):
        _spawn_one_ns(i, k, L, info, preset, kind, base, sys_asset, sub, pos)
    unreal.log("fx niagara %02d '%s' (%s): %d emitter(s)" % (i, L.get("name", ""), preset, len(spots)))


def _spawn_one_ns(i, k, L, info, preset, kind, base, sys_asset, sub, pos):
    a = sub.spawn_actor_from_class(unreal.NiagaraActor, unreal.Vector(pos[0], pos[1], pos[2]))
    a.set_actor_label("AutoFxNS_%02d_%d_%s" % (i, k, preset))
    comp = a.get_component_by_class(unreal.NiagaraComponent)
    comp.set_asset(sys_asset)
    if preset == "rain" and sys_asset.get_name() == "NS_Rain":
        # The hand-built NS_Rain is already finished (transparent, good-looking) -> only set the coverage box + density-conserving spawn rate; looks/dynamics stay its own;
        # overriding User_Color/Size previously turned it into white bars (observed: rain kept rendering white instead of the transparent asset's look)
        bxy = float(info.get("box_xy", 4500.0))
        bz = float(info.get("box_z", 600.0))
        try:
            comp.set_niagara_variable_vec3("User.User_BoxXY", unreal.Vector(bxy, bxy, bz))
            # Density conservation (after widening the box for "rain area too small" the rate must be topped up, else area dilutes the rain): Gemini count = steady-state drops in the planned box
            # -> scale the steady count by area ratio; fall time solved with real dynamics (v0=1300, g=980, spawn band -> ground ~= 2600+bz); rate = count / fall time.
            # Rain-alley check: count=800 / planned box 2000 / actual 10400 -> ~14k/s, matches the hand-tuned baseline. Cap 20k/s for perf.
            dark = _fx_lum_scale() < 5.0
            # Dark scenes slow the dynamics: real rain lands at ~26m/s, which reads "too long, too fast" on camera (seen in review); photographic feel ~9m/s.
            # Daytime keeps real dynamics + the asset's stock look
            v0, g = (500.0, 100.0) if dark else (1300.0, 980.0)
            cnt = float(info.get("count", 0.0))
            plan = max(500.0, float(info.get("box_xy_plan", bxy)))
            t_fall = (-v0 + math.sqrt(v0 ** 2 + 2.0 * g * (2600.0 + bz))) / g
            if cnt > 0:
                alive = cnt * (bxy / plan) ** 2
                # the true name needs the User_ prefix: "User.SpawnRate" silently no-ops (proven in the rain alley; same trap as User_Box)
                comp.set_niagara_variable_float("User.User_SpawnRate", min(20000.0, alive / max(0.5, t_fall)))
            # Dark-scene radiance compensation: NS_Rain's stock look was tuned on bright scenes; in a night alley the whole rain falls below the visibility threshold
            # (proven in the rain alley: "no rain at any camera" while the particles existed all along -- cranking brightness revealed them).
            # Dark override = cool grey-blue x1.1, opacity 0.20, thread width 3, slow fall -- locked over two review rounds
            if dark:
                comp.set_niagara_variable_vec3("User.User_VelZ", unreal.Vector(0.0, 0.0, -v0))
                comp.set_niagara_variable_float("User.User_GravityZ", -g)
                comp.set_niagara_variable_float("User.User_Lifetime", 1.2 * t_fall)
                emi_n = 1.1
                comp.set_niagara_variable_float("User.User_Size", 3.0)
                comp.set_niagara_variable_linear_color(
                    "User.User_Color",
                    unreal.LinearColor(0.55 * emi_n, 0.62 * emi_n, 0.78 * emi_n, 0.20))
        except Exception:
            pass
        comp.activate(True)
        return
    params = dict(base)
    if preset in ("rain", "snow"):
        # Precipitation lifetime must cover spawn band -> ground (observed "never lands": high spawn + short life = vanishes mid-air).
        # Estimate with TERMINAL velocity (NS_Leaves has built-in drag; initial velocity is meaningless): rain at gravity -3000 ~10m/s, snow ~1.5m/s
        term = 1000.0 if preset == "rain" else 150.0
        fall_cm = max(500.0, float(pos[2]) + float(info.get("box_z", 500.0)))
        params["Lifetime"] = min(10.0, max(1.0, 1.2 * fall_cm / term))
    # Per-emitter deterministic jitter: each of a layer's K emitters gets a "personality" (size/rate/wind/spin/lifetime microvariation),
    # else particles look identical in every corner ("nothing seems to change"). Seed=(layer,point), reproducible.
    jr = random.Random(i * 7919 + k * 104729)
    cnt = float(info.get("count", 0.0))
    if preset == "dust":
        cnt = min(cnt, 500.0)           # dust/pollen safety cap only (Gemini judges amount, no hardcoded aesthetics); white speckles are cured by the radiance cap, not by cutting count
    if cnt > 0:
        params["Lifetime"] = float(params.get("Lifetime", 2.0)) * jr.uniform(0.85, 1.2)
        # steady-state density: alive count ~= SpawnRate x lifetime == planned count (independent of box size, density never collapses)
        params["SpawnRate"] = max(1.0, cnt / max(0.2, float(params["Lifetime"]))) * jr.uniform(0.8, 1.25)
    elif "rate_scale" in info:
        params["SpawnRate"] = params["SpawnRate"] * float(info.get("rate_scale", 1.0))
    s_lo = float(info.get("size_cm_lo") or 0.0)
    s_hi = float(info.get("size_cm_hi") or 0.0)
    if s_hi > s_lo > 0.0:
        params["Size"] = jr.uniform(s_lo, s_hi)               # sample per emitter within the Gemini range
    elif info.get("size_cm"):
        params["Size"] = float(info["size_cm"])               # legacy field fallback
    if preset in ("mist", "smoke", "steam"):
        params["Size"] = min(float(params.get("Size", 60.0)), 70.0)   # fog/smoke = fine and soft, not 2m balls (observed 2m white balls by the boathouse)
    if preset == "rain":
        params["Size"] = min(float(params.get("Size", 6.0)), 7.0)     # thin rain threads; guard against a fat Gemini size -> thick white bars up close (observed on a rainy night)
    for sem, lo_, hi_ in (("VelZ", 0.8, 1.3), ("VelSpread", 0.8, 1.4),
                          ("RotRate", 0.7, 1.4), ("WindXY", 0.7, 1.4)):
        if sem in params:
            params[sem] = float(params[sem]) * jr.uniform(lo_, hi_)
    for sem, real in _NS_FLOATS.items():
        if sem in params:
            comp.set_niagara_variable_float(real, float(params[sem]))
    comp.set_niagara_variable_vec3("User.User_VelZ", unreal.Vector(0.0, 0.0, float(params.get("VelZ", 0.0))))
    bxy = float(info.get("box_xy", 3000.0)); bz = float(info.get("box_z", 500.0))
    comp.set_niagara_variable_vec3("User.User_BoxXY", unreal.Vector(bxy, bxy, bz))  # engine-verified true name (User_Box silently no-ops!)
    spr = _FX_SPRITE_BY_PRESET.get(preset)
    if preset == "rain" and sys_asset and sys_asset.get_name() == "NS_Rain":
        spr = "streak"                               # the velocity-aligned system takes streak drops (round drops are the leaf-family fallback)
    if spr and kind == "leaf":                       # card sprites swap in real leaf/petal/ash/scrap (the default MIC texture is snowtexture)
        mic = _ensure_fx_sprite_mic(spr)
        if mic:
            try:
                comp.set_variable_material("User.User_Texture", mic)  # engine-verified: this parameter is a MaterialInterface
            except Exception as e:
                unreal.log_warning("fx sprite material set failed (%s): %s" % (preset, e))
    col = L.get("color") or [0.9, 0.92, 0.95]
    emi = max(1.0, float(L.get("emissive", 1.0)))   # Gemini glow multiplier x scene radiance factor (life-or-death for additive particles under physical lighting)
    emi *= _fx_lum_scale()
    alpha_ = float(L.get("opacity", 1.0))
    if preset in ("fireflies", "embers"):
        # Self-lit layers (fireflies/midges/embers) only glow in DARK scenes: at night their brightness is their own (emi>=40); in bright daylight
        # fireflies/midges do not emit white light; forcing emi 40 = a sky full of white balls (probe-proven: "Lakeside midges" = fireflies@40).
        if _fx_lum_scale() > 30.0:                  # daytime/bright: midges = dark specks, no glow
            emi = min(emi, 1.6); alpha_ = min(alpha_, 0.5)
        else:
            emi = max(emi, 40.0)
    if preset == "rain":
        # rain = cool grey-blue translucent (bright white dots read as snow, not rain); daytime visibility comes from NS_Rain's velocity-aligned streaks.
        col = [0.55, 0.62, 0.78]
        emi = max(emi, 1.6)
        if _fx_lum_scale() < 5.0:      # night/dark: cool rain is invisible on a dark bg -> lift; but emi 4 becomes white bars up close; 2.5 reads far without blowing up near (verified at both distances)
            emi = max(emi, 2.5)
        alpha_ = 0.42                  # more transparent -> threads do not merge into solid white bars (readable up close in VR)
    elif preset == "snow":
        emi = max(emi, 3.0 / max(0.2, max(col)))    # snow = bright white (effective radiance floor, A/B tested)
        alpha_ = max(alpha_, 0.85)
    if preset in ("mist", "smoke", "steam"):
        # fog scatters ambient light: radiance must be ~sky level, else it silhouettes black against the sky dome (observed "very dark"). Low opacity keeps it airy
        emi *= 6.0
        alpha_ = min(alpha_, 0.3)
    if preset in ("mist", "smoke", "steam"):
        emi = min(emi, 1.9)            # fog/smoke: too bright by day becomes white blobs (observed lake white ball = base mist); keep it a soft thin haze
        alpha_ = min(alpha_, 0.13)
    elif preset == "dust":
        # Dust/pollen by day should be barely-there glinting motes, not white speckles (observed): very low radiance + very low opacity;
        # amount is Gemini's call (many motes just read as soft grain). Physics: over-bright additive specks = white dots; a radiance problem, not a count problem.
        emi = min(emi, 1.4)
        alpha_ = min(alpha_, 0.10)
    if preset in ("leaves", "petals", "ash"):
        emi = min(emi, 4.5)        # leaf/petal/ash = textured cards; overdriven radiance blows them to white dots (observed over the lake); the cap preserves texture color
    comp.set_niagara_variable_linear_color(
        "User.User_Color", unreal.LinearColor(col[0] * emi, col[1] * emi, col[2] * emi, alpha_))
    try:
        comp.reinitialize_system()
    except Exception:
        pass
    comp.activate(True)


_FX_ENV = {}    # environment injected by run() (sun illuminance etc.) for particle radiance conversion


def _fx_lum_scale():
    """Scene radiance factor for additive particles (engine-measured: ADDITIVE+UNLIT particle brightness is 0-5 while
    physically lit backgrounds run thousands of nits -> mathematically invisible). nits ~= lux*albedo/pi; particles
    take ~15% of that to read against the background; the legacy (non-physical) range stays at 1. Night moonlight
    is low -> they auto-dim; one rule for day and night."""
    if not _extended_luminance():
        return 1.0
    # indoors: particle readability baseline is room illuminance (45 lux), not the sun outside -- else dust glows like fairy lights (observed)
    lux = float(_FX_ENV.get("indoor_lux_override") or
                _FX_ENV.get("sun_intensity_lux", 75000.0) or 0.0)
    # City rain night: with moon 0 the scene is lit by city glow (10x sky_intensity, same source as the skylight fill) --
    # else reference illuminance = 0 with a 1.0 floor -> rain brightens into confetti / fog pads into glowing balloons (observed)
    lux = max(lux, 10.0 * float(_FX_ENV.get("sky_intensity", 0.0)))
    return max(0.15, min(6000.0, 0.2 * lux * 0.3 / 3.1416))  # 0.2 x scene nits (0.15 = deep-night floor)


# Real-water material presets (AI picks per photo). Source of truth = ue_water_presets.py WATER_PRESETS (change one, sync the other);
# inlined so the exec path needs no import (values engine-measured, see docs/MATERIAL_WATER.md).
_WATER_BODY_PRESETS = {
    "lake_calm":    dict(Wave_Size=2048.0, Ripples_Intensity=1.6, Water_Roughness=0.11, Metallic=1024.0, Water_Depth=170.0,  gh1=0.4, gh2=0.25, c1=(0.03, 0.08, 0.11), c2=(0.06, 0.14, 0.18), moss=False, foam=False),
    "lake_windy":   dict(Wave_Size=1024.0, Ripples_Intensity=2.5, Water_Roughness=0.12, Metallic=1024.0, Water_Depth=400.0,  gh1=2.0, gh2=1.5,  c1=(0.02, 0.09, 0.14), c2=(0.05, 0.17, 0.22), moss=False, foam=True),
    "pond_shallow": dict(Wave_Size=768.0,  Ripples_Intensity=1.5, Water_Roughness=0.08, Metallic=1024.0, Water_Depth=60.0,   gh1=0.4, gh2=0.3,  c1=(0.04, 0.12, 0.14), c2=(0.08, 0.20, 0.22), moss=False, foam=False),
    "deep_dark":    dict(Wave_Size=1536.0, Ripples_Intensity=1.2, Water_Roughness=0.06, Metallic=1024.0, Water_Depth=1000.0, gh1=0.8, gh2=0.5,  c1=(0.01, 0.04, 0.10), c2=(0.03, 0.10, 0.20), moss=False, foam=False),
    "swamp_mossy":  dict(Wave_Size=1024.0, Ripples_Intensity=0.8, Water_Roughness=0.2,  Metallic=512.0,  Water_Depth=150.0,  gh1=0.2, gh2=0.15, c1=(0.03, 0.10, 0.05), c2=(0.08, 0.18, 0.08), moss=True,  foam=False),
}


def _apply_blend_water(idx, actor, L):
    """Hook a water plane up to the real water material MA_YX_Blend (reflections/ripples/depth). AI flow: color/clarity/
    calmness judged by Gemini from the photo (L['water_color']/water_clarity/water_calm); code only MAPS those
    percepts to material params; presets (water_preset) serve as the structural base + fallback. Library not
    deployed / error -> False (caller falls back to the fake plane)."""
    try:
        eal = unreal.EditorAssetLibrary
        if not (eal.does_asset_exist(BLEND_MASTER) and eal.does_asset_exist(BLEND_TEMPLATE)):
            return False
        L = L or {}
        p = _WATER_BODY_PRESETS.get(str(L.get("water_preset", "lake_calm"))) or _WATER_BODY_PRESETS["lake_calm"]
        # -- Gemini water look -> material params (code does the physics mapping, AI does the seeing) --
        gcol = L.get("water_color")
        clarity = float(L.get("water_clarity", 0.5)) if L.get("water_clarity") is not None else 0.5
        calm = float(L.get("water_calm", 0.7)) if L.get("water_calm") is not None else 0.7
        if isinstance(gcol, (list, tuple)) and len(gcol) >= 3:
            c1 = [max(0.0, gcol[i] * 0.55) for i in range(3)]                  # deep-water dark tone
            c2 = [min(1.0, gcol[i] * 1.30) for i in range(3)]                  # shallow-water bright tone
        else:
            c1, c2 = list(p["c1"]), list(p["c2"])                             # fall back to preset colors
        w_depth = 60.0 + clarity * 460.0          # clear -> see far (520) / murky -> near (60)
        w_rough = max(0.03, 0.17 - calm * 0.14)   # calm -> mirror (0.03) / choppy -> matte (0.17)
        w_gh1 = 0.12 + (1.0 - calm) * 2.0; w_gh2 = w_gh1 * 0.65   # calm low waves / windy high waves
        w_ripple = 0.6 + (1.0 - calm) * 3.0       # calm few ripples / windy sparkle
        mel = unreal.MaterialEditingLibrary
        mic_path = _apath("MI_FxWaterBody_%02d") % idx
        if not eal.does_asset_exist(mic_path):
            eal.duplicate_asset(BLEND_TEMPLATE, mic_path)   # duplicate from the fully-textured template, never hits a NULL compile failure
        mic = unreal.load_asset(mic_path)
        mel.set_material_instance_parent(mic, unreal.load_asset(BLEND_MASTER))
        bot = ["/Game/Tool/MA_Blend/CoreContent/textures/Soil_D",
               "/Game/Tool/MA_Blend/CoreContent/textures/T_Leaves2_D",
               "/Game/Tool/MA_Blend/CoreContent/textures/Soil_D"]
        for slot, pth in zip(("Base Layer Albedo Map", "Middle Layer Albedo Map", "Top Layer Albedo Map"), bot):
            t = unreal.load_asset(pth)
            if t:
                mel.set_material_instance_texture_parameter_value(mic, slot, t)
        for n in ("Use_Base_ImageAlpha", "Use_Top_ImageAlpha", "Use Base Layer Adjustments",
                  "Use Middle Layer Adjustments", "Use Top Layer Adjustments",
                  "Use Puddle Layer", "Use_Water_ImageAlpha", "Refraction?"):
            mel.set_material_instance_static_switch_parameter_value(mic, n, True)
        # Design decision (2026-06): water NEVER enables Foam (strobing/flicker); rain belongs to weather.
        # Moss only on still water (calm>=0.55): moss on a windy lake = a green carpet; water reads as mossy ground with no sparkle (observed forest lake)
        for n, v in (("Moss?", calm >= 0.55), ("Foam?", False),
                     ("Leaves?", True), ("Rain?", False)):
            mel.set_material_instance_static_switch_parameter_value(mic, n, v)
        for n, v in (("Water_curve", 0.0), ("base_curve", 1.0), ("top_curve", 4.0),
                     ("Wave_Size", p["Wave_Size"]), ("Metallic", p["Metallic"]),     # structural defaults stay preset
                     ("Ripples_Intensity", w_ripple), ("Water_Roughness", w_rough),  # calmness -> reflection sharpness/ripples
                     ("Water_Depth", w_depth),                                       # clarity -> see-through distance
                     ("WaterPDO", 12.0),                                             # pixel depth offset 12: water wins depth, kills shoreline z-fight flicker (default 0 flickers, observed)
                     ("1_GerstnerWave_Height", w_gh1), ("2_GerstnerWave_Height", w_gh2)):  # calmness -> wave amplitude
            mel.set_material_instance_scalar_parameter_value(mic, n, float(v))
        if p["foam"]:
            mel.set_material_instance_scalar_parameter_value(mic, "Foam_Opacity", 0.25)
        for n, c in (("Water_Color_01", c1), ("Water_Color_02", c2)):    # Gemini-judged water colors (fallback to preset)
            mel.set_material_instance_vector_parameter_value(mic, n, unreal.LinearColor(c[0], c[1], c[2], 1.0))
        m = unreal.load_asset("/Game/Tool/Masks/T_Alpha_05")   # white = water (full sheet)
        if m:
            mel.set_material_instance_texture_parameter_value(mic, "water_imagealpha", m)
        mel.update_material_instance(mic)
        eal.save_asset(mic_path)
        actor.static_mesh_component.set_material(0, mic)
        unreal.log("fx water %02d -> MA_YX_Blend (Gemini: color=%s clarity=%.2f calm=%.2f, preset=%s)"
                   % (idx, ("AI" if isinstance(gcol, (list, tuple)) else "preset"), clarity, calm,
                      L.get("water_preset", "lake_calm")))
        return True
    except Exception as e:
        unreal.log_warning("blend water %02d failed, fallback to flat plane: %s" % (idx, e))
        return False


def _apply_effects(effects, data=None):
    """Gemini FX plan -> UE: niagara live particles / water real material (MA_YX_Blend, fake-plane fallback) / card
    alpha groups (HISM) / fog. Transient, tagged AutoFx_*."""
    if not effects:
        return
    try:
        mel = unreal.MaterialEditingLibrary
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        for a in sub.get_all_level_actors():        # idempotent cleanup (leftover emitters from dev re-runs -> doubled particles, observed)
            if a.get_actor_label().startswith("AutoFx"):
                try:
                    sub.destroy_actor(a)
                except Exception:
                    pass
        cardmesh = None
        for i, L in enumerate(effects):
            prim = L.get("primitive")
            if prim == "niagara":
                _spawn_fx_niagara(i, L, sub)
                continue
            if prim == "fog":
                _apply_fx_fog(L.get("fog"))
                continue
            if prim == "water":
                nmw = str(L.get("name", "")).lower()
                if any(k in nmw for k in ("road", "street", "pavement", "asphalt", "wet ")):
                    # wet road != water body (observed: a 220x200m water sheet paved the street = canal) -- wet roads belong to ground-material wetness
                    unreal.log("fx water %02d '%s' skipped (wet road = ground wetness, not a water body)"
                               % (i, L.get("name", "")))
                    continue
                plane = L.get("plane")
                if not plane:
                    continue
                # Water height comes from the waterline _carve_water_basin already carved (plane[2]=waterline); use it directly.
                # Generic anti-coplanar-flicker: an uncarved water sheet must not sit on the ground -- flat-path z~=0 z-fights the ground
                # (water needs distance from the ground or it flickers). Raise to local surface +6cm.
                z_w = float(plane[2])
                if (data or {}).get("_waterline_cm") is None:
                    terr_ = (data or {}).get("terrain") or {}
                    gz_ = _terrain_surface_cm(terr_, float(plane[0]), float(plane[1])) if terr_.get("grid") else 0.0
                    if z_w < gz_ + 6.0:
                        unreal.log("fx water %02d lifted %.0f->%.0fcm (no basin, anti z-fight)" % (i, z_w, gz_ + 6.0))
                        z_w = gz_ + 6.0
                pl = unreal.load_asset("/Engine/BasicShapes/Plane")
                actor = sub.spawn_actor_from_object(pl, unreal.Vector(plane[0], plane[1], z_w))
                actor.set_actor_label("AutoFx_Water_%02d" % i)
                actor.static_mesh_component.set_static_mesh(pl)
                actor.set_actor_scale3d(unreal.Vector(plane[3] / 100.0, plane[4] / 100.0, 1.0))
                # real water material first (MA_YX_Blend: reflections/ripples/depth, AI picks the preset); library missing -> fall back to the fake translucent plane
                if not _apply_blend_water(i, actor, L):
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
            # Never place fog/haze/cloud cards: flat translucent cards read as pale "patches" on terrain (rejected in review as ugly);
            # atmosphere is handled by ExponentialHeightFog + volumetric fog (the fog primitive stays). Rain/dust/bird cards unaffected.
            nm = str(L.get("name", "")).lower()
            if any(k in nm for k in ("mist", "fog", "cloud", "haze", "smoke", "雾", "霭")):
                unreal.log("fx card %02d '%s' skipped (mist/cloud cards disabled)" % (i, L.get("name", "")))
                continue
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
                task.filename = local; task.destination_path = _aipath("Fx")
                task.destination_name = "T_FxCard%02d" % i
                task.automated = True; task.replace_existing = True; task.save = True
                unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
                ftex = unreal.load_asset(_aipath("Fx/T_FxCard%02d") % i)
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
            for x, y, z, yaw, pit, sc in inst:                   # single-sprite particles (rain/dust/mist/birds), facing the camera
                hism.add_instance(unreal.Transform(unreal.Vector(x, y, z), unreal.Rotator(pit, yaw, 0),
                                                   unreal.Vector(sc, sc, sc)), True)
            for sx, sy, cz, w, h, yaw in sheets:                 # vertical water sheets: non-uniform scale (w x h), one piece
                hism.add_instance(unreal.Transform(unreal.Vector(sx, sy, cz - h / 2.0), unreal.Rotator(0, yaw, 0),
                                                   unreal.Vector(w / 100.0, 1.0, h / 100.0)), True)
            unreal.log("fx card %02d %s cards=%d sheets=%d" % (i, L.get("name", ""), len(inst), len(sheets)))
    except Exception as e:
        unreal.log_warning("apply effects failed: %s" % e)


SKY_MAT_PATH = "/Game/Auto/M_Sky"


def _ensure_sky_material():
    if unreal.EditorAssetLibrary.does_asset_exist(SKY_MAT_PATH):
        return unreal.load_asset(SKY_MAT_PATH)
    atools = unreal.AssetToolsHelpers.get_asset_tools()
    mat = atools.create_asset("M_Sky", _apath(""), unreal.Material, unreal.MaterialFactoryNew())
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
    """Photo sky color painted onto a giant sky sphere's inner wall (unlit, two-sided). Indoors: no sky built."""
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
        comp.set_collision_profile_name("NoCollision")                     # the sky sphere must never collide: it encloses the scene, so any spawn point would be "encroached" -> every PlayerStart rejected
        comp.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)
        actor.set_actor_enable_collision(False)                            # actor-level collision off (hardest gate: survives set_static_mesh resetting component collision, and persists on save)
        actor.set_actor_location(unreal.Vector(0, 0, 0), False, False)
        actor.set_actor_scale3d(unreal.Vector(20000.0, 20000.0, 20000.0))
        col = env.get("sky_color") or [0.35, 0.55, 0.85]
        mic, mic_path = _ensure_mic("MI_AutoSky", mat)
        unreal.MaterialEditingLibrary.set_material_instance_vector_parameter_value(
            mic, "SkyColor", unreal.LinearColor(col[0], col[1], col[2], 1.0))
        comp.set_material(0, mic)
        unreal.EditorAssetLibrary.save_asset(mic_path)
        unreal.log("sky: color (%.2f,%.2f,%.2f)" % (col[0], col[1], col[2]))
    except Exception as e:
        unreal.log_warning("apply sky failed: %s" % e)


def _apply_reflection(env):
    """Generic reflections: every scene gets one scene-covering SphereReflectionCapture so metal/glass/car paint/
    windows/wet ground reflect correctly (without a capture they fall back to the crude skylight cubemap).
    Reflection strength belongs to material roughness, not to capture presence -- so dry daytime scenes get one
    too."""
    try:
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        cap = next((a for a in sub.get_all_level_actors() if isinstance(a, unreal.SphereReflectionCapture)), None)
        if cap is None:
            cap = sub.spawn_actor_from_class(unreal.SphereReflectionCapture, unreal.Vector(0, 0, 400),
                                             unreal.Rotator(0, 0, 0))
            cap.set_actor_label("AutoReflection")
        comp = cap.get_component_by_class(unreal.SphereReflectionCaptureComponent)
        if comp:
            comp.set_editor_property("influence_radius", 30000.0)     # covers the whole scene footprint (300m)
            try:
                comp.set_editor_property("brightness", 0.5)          # half weight: glass/car paint/metal/wet ground keep real reflections without tinting matte stone/asphalt
            except Exception:
                pass
        try:
            unreal.EditorLevelLibrary.build_reflection_captures()     # bake the captures (best-effort)
        except Exception:
            pass
        unreal.log("reflection: global capture ensured (all scenes)")
    except Exception as e:
        unreal.log_warning("apply reflection failed: %s" % e)


UE_MANUAL_EV_BASE = 4.0   # engine manual-metering baseline (bias sweep put mid-grey ~6; 6.0/5.0 still looked overexposed on high-albedo scenes; final calibration 4.0)


def _skylight_intensity(env):
    """Skylight intensity (the 'fill' half of the night kit): day 0.4-0.85 (1.2 measured blown out);
    night 3.0 = blue ambient fill -- captured night-sky radiance is ~0, so shadows are physically dead black;
    games conventionally lift shadow detail with skylight."""
    tod = str(env.get("time_of_day", "")).lower()
    if "night" in tod:
        return 3.0
    if tod in ("dusk", "dawn"):
        return 1.5
    return max(0.4, min(0.85, 0.6 * float(env.get("sky_intensity", 1.0))))


def _set_physical_exposure(s, env, ev, channel="game"):
    """Physical-range exposure (generic): Sunny-16, EV100=log2(lux/2.5) (120k lux -> 15.6 sunny, 20k -> 13.0 overcast);
    manual metering + Gemini exposure_ev as compensation; bias = ev + baseline - EV100 (linear in lux, same formula
    for every scene). Auto-exposure never converges in sync SceneCapture/bridge renders (the old white/black frame
    disease); manual physical metering renders deterministically, and VR gets no exposure breathing.
    Night feel preserved: metering pulls any illuminance to mid-grey -> a moonlit night would expose like day.
    Scotopic convention drops stops, triggered by Gemini time_of_day.
    Per-channel calibration (engine-measured 2026-06-11): the game pipeline has Local Exposure auto-lifting shadows
    (neutralized at night); a viewport sweep put the night look at bias ~-8 -> night offset -6.5; SceneCapture
    responds differently to LE, -3.5 is already dark-but-readable."""
    lux = float(env.get("sun_intensity_lux", 75000.0))
    indoor_lux = env.get("indoor_lux_override")
    if indoor_lux:
        # Indoors: bounded auto-exposure (anchor = EV of the AI room illuminance, +/-2 stops). Fixed metering measured systematically underexposed --
        # AI lumens spread over all room surfaces average below the AI's overall illuminance estimate (the two AI numbers disagree);
        # a real camera adapts; bounded = neither blown to daylight nor crushed to a cellar (immediate fix at the y0 camera).
        # The capture channel must stay manual: SceneCapture sync grabs never converge under adaptation (the old white/black frame disease)
        ev100 = math.log2(max(2.5, float(indoor_lux)) / 2.5)
        s.set_editor_property("override_auto_exposure_method", True)
        try:
            s.set_editor_property("override_auto_exposure_apply_physical_camera_exposure", True)
            s.set_editor_property("auto_exposure_apply_physical_camera_exposure", False)
        except Exception:
            pass
        s.set_editor_property("override_auto_exposure_bias", True)
        night_in = "night" in str(env.get("time_of_day", "")).lower()
        if channel == "capture" and not night_in:
            s.set_editor_property("auto_exposure_method", unreal.AutoExposureMethod.AEM_MANUAL)
            # manual anchor lowered 1 stop to approximate adaptive brightening (sync-grab adaptation does not converge -- the old disease)
            s.set_editor_property("auto_exposure_bias", ev + UE_MANUAL_EV_BASE - ev100 + 1.0)
            return
        s.set_editor_property("auto_exposure_method", unreal.AutoExposureMethod.AEM_HISTOGRAM)
        s.set_editor_property("override_auto_exposure_min_brightness", True)
        s.set_editor_property("override_auto_exposure_max_brightness", True)
        # Night exposure band tightened to +/-0.5 (default +/-2): at 5 lux auto metering pumps max gain -> mottled Lumen noise +
        # relatively blown windows (observed "night looks weird") -- night darkness is an experience calibration; the camera must not auto-rescue it
        band = 0.5 if night_in else 2.0
        s.set_editor_property("auto_exposure_min_brightness", ev100 - band)
        s.set_editor_property("auto_exposure_max_brightness", ev100 + band)
        s.set_editor_property("auto_exposure_bias", ev)     # under auto metering, bias carries only Gemini's exposure compensation
        return
    ev100 = math.log2(max(2.5, lux) / 2.5)
    tod = str(env.get("time_of_day", "")).lower()
    if "night" in tod:
        # calibration 2026-06-11: -8 = all-silhouette "can't see"; -6.3 + the night kit (sky fill / controlled LE / desaturation) = dark but readable
        dflt = -6.3 if channel == "game" else -5.0   # night exposure: back to hand-calibrated -6.3 (-4.7 drifted bright -> the night street measured like day at bias -0.3); capture channel pinned to -5.0
        night_off = float(os.environ.get("UE_NIGHT_EV_OFFSET_" + channel.upper(), str(dflt)))
    elif tod in ("dusk", "dawn"):
        night_off = -2.0 if channel == "game" else -1.5
    else:
        night_off = 0.0
    s.set_editor_property("override_auto_exposure_method", True)
    s.set_editor_property("auto_exposure_method", unreal.AutoExposureMethod.AEM_MANUAL)
    try:
        s.set_editor_property("override_auto_exposure_apply_physical_camera_exposure", True)
        s.set_editor_property("auto_exposure_apply_physical_camera_exposure", False)
    except Exception:
        pass
    s.set_editor_property("override_auto_exposure_bias", True)
    s.set_editor_property("auto_exposure_bias", ev + UE_MANUAL_EV_BASE - ev100 + night_off)


def _apply_grade(env):
    """Post grading: unbound PostProcessVolume; exposure/saturation/contrast approach the photo's tone."""
    if not env:
        return
    try:
        ppv = _find_or_spawn(unreal.PostProcessVolume)
        ppv.set_editor_property("unbound", True)
        ppv.set_editor_property("priority", 1.0)
        try:
            # Clear leftover reveal PP materials (observed: an old tid's M_Photo_* with weight=1 saved into the level; a newly built scene got
            # another scene's photo smeared across game view). Build = clean beauty state; reveal re-creates its blendables when arming.
            s0 = ppv.get_editor_property("settings")
            s0.set_editor_property("weighted_blendables", unreal.WeightedBlendables())
            ppv.set_editor_property("settings", s0)
        except Exception:
            pass
        try:
            # World Partition fallback: streaming may unload distant PPVs in PIE -> global exposure/grading lost
            ppv.set_editor_property("is_spatially_loaded", False)
        except Exception:
            pass
        tod_ = str(env.get("time_of_day", "")).lower()
        if "night" in tod_ or tod_ in ("dusk", "dawn"):
            # Night kit (hand-calibrated): uncontrolled Local Exposure lifts night back to daylight (convicted by toggling the showflag -> all black);
            # switch to CONTROLLED: highlight/detail neutral = 1, shadows 0.9 = shadow detail (the "detail" half of AI+procedural fill).
            # The "tone" half: night desaturation 0.9 (Purkinje effect), skylight blue fill in _apply_env. Daytime untouched.
            s2 = ppv.get_editor_property("settings")
            for k_, v_ in (("local_exposure_highlight_contrast_scale", 1.0),
                           ("local_exposure_shadow_contrast_scale", 0.9),
                           ("local_exposure_detail_strength", 1.0)):
                s2.set_editor_property("override_" + k_, True)
                s2.set_editor_property(k_, v_)
            ppv.set_editor_property("settings", s2)
        s = ppv.get_editor_property("settings")
        ev = float(env.get("exposure_ev", 0.0))
        sat = min(1.05, float(env.get("saturation", 1.0))) * SAT_BASE   # global desaturation (default = filmic/faded-memory, not candy -- design decision); cap 1.05 then x SAT_BASE
        if "night" in tod_:
            sat = min(sat, 0.74)                             # night desaturates further (Purkinje effect + faded-memory look)
        con = float(env.get("contrast", 1.0))
        if _extended_luminance():
            _set_physical_exposure(s, env, ev)               # physical metering: manual exposure EV=log2(lux/2.5)+ev (adaptation does not converge in sync captures)
        else:
            s.set_editor_property("override_auto_exposure_bias", True)
            s.set_editor_property("auto_exposure_bias", ev)
            # clamp the adaptation range only in the legacy brightness range; in physical range (day = thousands of cd/m2) clamping 0.03-1.5 locks exposure at night levels
            s.set_editor_property("override_auto_exposure_min_brightness", True)
            s.set_editor_property("auto_exposure_min_brightness", 0.03)
            s.set_editor_property("override_auto_exposure_max_brightness", True)
            s.set_editor_property("auto_exposure_max_brightness", 1.5)
        s.set_editor_property("override_color_saturation", True)
        s.set_editor_property("color_saturation", unreal.Vector4(sat, sat, sat, 1.0))
        s.set_editor_property("override_color_contrast", True)
        s.set_editor_property("color_contrast", unreal.Vector4(con, con, con, 1.0))
        bloom = float(env.get("bloom", 0.5)) * 0.5   # 0.7 -> 0.5 restrains bloom (no daytime blowout) but not down to 0.4, keeping night neon/bulb signature glow
        s.set_editor_property("override_bloom_intensity", True)
        s.set_editor_property("bloom_intensity", bloom)
        try:                                                     # generic AO: darkens crevices/contacts -> objects grip the ground, more volume (pure geometry, scene-agnostic)
            s.set_editor_property("override_ambient_occlusion_intensity", True)
            s.set_editor_property("ambient_occlusion_intensity", 0.45)
            s.set_editor_property("override_ambient_occlusion_radius", True)
            s.set_editor_property("ambient_occlusion_radius", 120.0)
        except Exception as ao_e:
            unreal.log_warning("AO set failed: %s" % ao_e)
        # -- Cinematic post (Gemini judges the photo's look -> code maps + clamps): white balance / vignette / film grain / chromatic aberration / color cast --
        # Iron rule: Gemini perceives ("what stock/camera/mood is this photo"); code maps -1..1 / 0..1 into UE physical post values, clamped to safe ranges.
        _post = env.get("post") or {}
        try:
            warmth = max(-1.0, min(1.0, float(_post.get("warmth", 0.0))))
            tint = max(-1.0, min(1.0, float(_post.get("tint", 0.0))))
            s.set_editor_property("override_white_temp", True)
            s.set_editor_property("white_temp", 6500.0 + warmth * 3000.0)        # warm up / cool down: 3500..9500K (opened up, more expressive)
            s.set_editor_property("override_white_tint", True)
            s.set_editor_property("white_tint", tint * 0.4)                       # green-magenta trim
            vig = max(0.0, min(0.85, float(_post.get("vignette", 0.4))))         # cap 0.85 (0.9 crushes the edges)
            s.set_editor_property("override_vignette_intensity", True)
            s.set_editor_property("vignette_intensity", vig)
            grain = max(0.0, min(0.7, float(_post.get("grain", 0.0))))           # film grain (thesis: degraded-memory texture; cap 0.7)
            s.set_editor_property("override_film_grain_intensity", True)
            s.set_editor_property("film_grain_intensity", grain)
            ca = max(0.0, min(1.0, float(_post.get("chromatic_aberration", 0.0))))
            s.set_editor_property("override_scene_fringe_intensity", True)
            s.set_editor_property("scene_fringe_intensity", ca * 4.0)            # 0..4 px chromatic aberration
            if ca > 0.01:
                s.set_editor_property("override_chromatic_aberration_start_offset", True)
                s.set_editor_property("chromatic_aberration_start_offset", 0.35)  # center sharp, fringing only at edges (more like a real lens)
            cc = _post.get("color_cast")
            if isinstance(cc, (list, tuple)) and len(cc) >= 3:
                g = [max(0.55, min(1.45, float(cc[i]))) for i in range(3)]       # overall color gain (opened to 0.55-1.45, more expressive); still clamped against blowout
                s.set_editor_property("override_color_gain", True)
                s.set_editor_property("color_gain", unreal.Vector4(g[0], g[1], g[2], 1.0))   # gain master W=1 neutral
            # -- dreamcore: split toning (shadows/highlights tinted separately) + fade lifted blacks + soft glow --
            def _clamp3(v, lo, hi):
                return [max(lo, min(hi, float(v[i]))) for i in range(3)] if isinstance(v, (list, tuple)) and len(v) >= 3 else None
            sh = _clamp3(_post.get("split_shadow"), 0.55, 1.5)                   # split toning opened up (0.55-1.5); Gemini can tint more boldly
            hl = _clamp3(_post.get("split_highlight"), 0.55, 1.5)
            if sh or hl:
                s.set_editor_property("override_color_correction_shadows_max", True)
                s.set_editor_property("color_correction_shadows_max", 0.35)      # widen the shadow range so split toning is visible
                s.set_editor_property("override_color_correction_highlights_min", True)
                s.set_editor_property("color_correction_highlights_min", 0.5)
            if sh:
                s.set_editor_property("override_color_gain_shadows", True)
                s.set_editor_property("color_gain_shadows", unreal.Vector4(sh[0], sh[1], sh[2], 1.0))   # gain master W=1
            if hl:
                s.set_editor_property("override_color_gain_highlights", True)
                s.set_editor_property("color_gain_highlights", unreal.Vector4(hl[0], hl[1], hl[2], 1.0))
            fade = max(0.0, min(1.0, float(_post.get("fade", 0.0))))
            if fade > 0.01:                                                       # fade / milky blacks (dreamcore): offset master W MUST be 0! else the whole image +1 blows white (tuition paid)
                s.set_editor_property("override_color_offset", True)
                s.set_editor_property("color_offset", unreal.Vector4(fade * 0.026, fade * 0.030, fade * 0.040, 0.0))   # opened-up fade amplitude (more expressive)
            dream = max(0.0, min(1.0, float(_post.get("dreaminess", 0.0))))
            if dream > 0.01:                                                      # soft dream glow: raise bloom moderately + keep threshold positive (only highlights glow; a negative threshold blows the frame white)
                s.set_editor_property("override_bloom_intensity", True)
                s.set_editor_property("bloom_intensity", min(0.95, bloom + dream * 0.4))
                s.set_editor_property("override_bloom_threshold", True)
                s.set_editor_property("bloom_threshold", 0.6 - dream * 0.25)
        except Exception as pe:
            unreal.log_warning("post look set failed: %s" % pe)
        ppv.set_editor_property("settings", s)
        unreal.log("grade: ev=%.2f sat=%.2f con=%.2f bloom=%.2f +AO | post warmth=%.2f vig=%.2f grain=%.2f ca=%.2f"
                   % (ev, sat, con, bloom, float(_post.get("warmth", 0.0)), float(_post.get("vignette", 0.4)),
                      float(_post.get("grain", 0.0)), float(_post.get("chromatic_aberration", 0.0))))
    except Exception as e:
        unreal.log_warning("apply grade failed: %s" % e)


GLOW_MAT_PATH = "/Game/Auto/M_AutoGlow"


def _ensure_glow_material():
    """Visible-glow master material for light sources: unlit emissive, Emissive = GlowColor x GlowBright (>1 triggers
    bloom). Point lights illuminate the scene while the source itself stays black (bulbs/windows/neon); a small
    sphere with this material at each light -> night sources visibly glow."""
    if unreal.EditorAssetLibrary.does_asset_exist(GLOW_MAT_PATH):
        return unreal.load_asset(GLOW_MAT_PATH)
    atools = unreal.AssetToolsHelpers.get_asset_tools()
    mat = atools.create_asset("M_AutoGlow", _apath(""), unreal.Material, unreal.MaterialFactoryNew())
    try:
        mat.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)
    except Exception:
        pass
    mel = unreal.MaterialEditingLibrary
    gc = mel.create_material_expression(mat, unreal.MaterialExpressionVectorParameter, -600, -40)
    gc.set_editor_property("parameter_name", "GlowColor")
    gc.set_editor_property("default_value", unreal.LinearColor(1.0, 0.85, 0.6, 1.0))
    gb = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -600, 180)
    gb.set_editor_property("parameter_name", "GlowBright"); gb.set_editor_property("default_value", 10.0)
    mul = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -360, 40)
    mel.connect_material_expressions(gc, "", mul, "A"); mel.connect_material_expressions(gb, "", mul, "B")
    mel.connect_material_property(mul, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    mel.recompile_material(mat)
    unreal.EditorAssetLibrary.save_asset(GLOW_MAT_PATH)
    return mat


def _kelvin_rgb(k):
    """Color temperature -> RGB approximation (Tanner Helland fit); only for tinting glow spheres (the light itself
    uses use_temperature)."""
    t = max(1000.0, min(12000.0, float(k))) / 100.0
    if t <= 66:
        r = 255.0
        g = 99.47 * math.log(t) - 161.12 if t > 1 else 0.0
        b = 0.0 if t <= 19 else 138.52 * math.log(t - 10) - 305.04
    else:
        r = 329.7 * ((t - 60) ** -0.1332)
        g = 288.12 * ((t - 60) ** -0.0755)
        b = 255.0
    return [max(0.0, min(255.0, v)) / 255.0 for v in (r, g, b)]


def _apply_lights(lights, indoor=False, objs=None, light_map=None, attn_cm=None, room_box=None):
    """Night/dark scenes: place point lights (color/lumens/attenuation) + one emissive glow sphere per light (visible
    source); clears old AutoLight_*/AutoLightGlow_* first.
    Indoor key hierarchy (observed "double shadows / everything fighting"): brightest = key (casts shadows), rest =
    weak practicals (x0.22, no shadows).
    Position fixes (observed "3 light sources bunched together"): projection puts all lights at the same horizontal
    distance D -> snap each to the nearest rebuilt object (lights live on fixtures) + dedupe repeats within 0.8m
    (the same lamp detected twice).
    Full parameter surface (observed "forgot color temp/source radius, light too hard"; docs/UE_LIGHT_PARAMS.md):
    AI gives temperature/source radius/source length; attenuation = attn_cm (indoor room diagonal -- a 528cm
    attenuation in an 8m room = pitch-black far wall; optimization params stay code-owned, never AI)."""
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in sub.get_all_level_actors():
        lbl = a.get_actor_label()
        if isinstance(a, unreal.PointLight) and lbl.startswith("AutoLight_"):
            try:
                sub.destroy_actor(a)
            except Exception:
                pass
        elif isinstance(a, unreal.StaticMeshActor) and lbl.startswith("AutoLightGlow_"):
            try:
                sub.destroy_actor(a)
            except Exception:
                pass
    if not lights:
        return
    try:
        glow_mat = None
        sphere = unreal.load_asset("/Engine/BasicShapes/Sphere")
        try:
            glow_mat = _ensure_glow_material()
        except Exception as e:
            unreal.log_warning("glow material failed: %s" % e)
        # Light homing (AI, light_map): fixture ownership / real lumens / glow-or-not all judged by Gemini against the photo --
        # the old chain of three guessed factors (0-1 strength x formula x scene factor x indoor discount) = "light intensities set at random", scrapped.
        lmap = {int(m.get("id", 0)): m for m in (light_map or [])}
        obj_by_id = {o.get("id"): o for o in (objs or []) if o.get("location")}
        # Multiple lights on one fixture: spread the detected u (image x) along the horizontal axis perpendicular to the camera->fixture ray -- projective geometry,
        # else all stack at the fixture-center XY as a vertical light column (observed "mysterious extra light balls")
        fix_us = {}
        for m in lmap.values():
            if m.get("keep", True) and m.get("fixture_object_id", -1) in obj_by_id:
                fix_us.setdefault(m["fixture_object_id"], []).append(float(m.get("u", 0.5)))
        kept = []
        for l in lights:
            m = lmap.get(int(l.get("id", 0)))
            if m:
                if not m.get("keep", True):
                    continue                                # AI-judged false detection (reflection/poster), drop
                l["intensity_lm"] = float(m.get("lumens", 500.0))   # single source of truth: AI real lumens
                l["_glow"] = bool(m.get("visible_source", False))
                l["_temp_k"] = float(m.get("color_temp_k", 2700.0))
                l["_crgb"] = m.get("colored_rgb")                   # non-None only for neon/strip colored sources
                l["_src_r"] = float(m.get("source_radius_cm", 8.0))
                l["_src_len"] = float(m.get("source_len_cm", 0.0))
                fo = obj_by_id.get(m.get("fixture_object_id"))
                if fo:
                    fl = fo["location"]
                    fh = float((fo.get("scale") or [200])[0])
                    us = fix_us.get(m["fixture_object_id"]) or []
                    dx = dy = 0.0
                    if len(us) > 1:                     # u offsets -> displacement along the horizontal axis perpendicular to the view ray
                        du = float(m.get("u", 0.5)) - sum(us) / len(us)
                        dist = math.hypot(fl[0], fl[1])
                        off = max(-150.0, min(150.0, du * 1.27 * dist))  # 1.27≈2tan(65°/2)
                        nrm = max(1.0, dist)
                        dx, dy = -fl[1] / nrm * off, fl[0] / nrm * off
                    l["location"] = [fl[0] + 20.0 + dx, fl[1] + 20.0 + dy,
                                     fl[2] + fh * float(m.get("height_frac", 0.6))]
                else:
                    l["_glow"] = False              # no fixture anchor = projected position unreliable -> emit light but no glow body (a floating bright ball is worse)
            else:
                l["_glow"] = True                           # no mapping (legacy tasks) keeps old behavior
            if room_box:                            # clamp lights back into the room (spreading/projection can push them beyond walls; observed a bright ball outside the wall)
                l["location"][0] = min(room_box[1] - 20.0, max(room_box[0] + 20.0, l["location"][0]))
                l["location"][1] = min(room_box[3] - 20.0, max(room_box[2] + 20.0, l["location"][1]))
            kept.append(l)
        if len(kept) != len(lights):
            unreal.log("lights: AI dropped %d false detections" % (len(lights) - len(kept)))
        lights = kept
        # Fixture meshes cast no shadows (A/B test: "black wall" = key light inside the shade mesh; an opaque Tripo shell wedges half the room
        # into hard-edged dead shadow; real shades transmit) -- only objects owning a light get cast_shadows off, everything else unchanged
        fixture_ids = {m.get("fixture_object_id") for m in lmap.values()
                       if m.get("keep", True) and m.get("fixture_object_id", -1) in obj_by_id}
        if fixture_ids:
            for a in sub.get_all_level_actors():
                lbl = a.get_actor_label()
                if isinstance(a, unreal.StaticMeshActor) and any(
                        lbl == "OBJ_%02d" % f for f in fixture_ids):
                    try:
                        # note: mesh components use cast_shadow (singular); cast_shadows belongs to light components (been burned)
                        a.static_mesh_component.set_editor_property("cast_shadow", False)
                        unreal.log("lights: fixture %s shadow OFF (lampshade transmits)" % lbl)
                    except Exception as e:
                        unreal.log_warning("fixture shadow off %s: %s" % (lbl, e))
        key_id = None
        if indoor and lights:
            by_lm = sorted(lights, key=lambda x: -float(x.get("intensity_lm", 0.0)))
            # A key light must clearly outshine the runner-up (>=2x) -- photos of only small mood lights (fairy-light rooms) have no key;
            # forcing one washes warm light across the ceiling (observed rainy-night interior)
            if len(by_lm) == 1 or float(by_lm[0].get("intensity_lm", 0.0)) >= \
                    2.0 * float(by_lm[1].get("intensity_lm", 1.0)):
                key_id = by_lm[0].get("id")
        # glow-ball quota (hand-calibrated): at most 2 emissive orbs per scene, highest lumens win -- a room full of bright balls upstages the real fixtures
        glow_ids = {x.get("id") for x in sorted((l_ for l_ in lights if l_.get("_glow")),
                                                key=lambda x: -float(x.get("intensity_lm", 0.0)))[:2]}
        for l in lights:
            L = l["location"]
            lid = l.get("id", 0)
            actor = sub.spawn_actor_from_class(unreal.PointLight,
                                               unreal.Vector(L[0], L[1], L[2]), unreal.Rotator(0, 0, 0))
            actor.set_actor_label("AutoLight_%d" % lid)
            c = actor.get_component_by_class(unreal.PointLightComponent)
            col = l.get("color") or [1.0, 0.85, 0.6]
            if c:
                try:
                    c.set_editor_property("intensity_units", unreal.LightUnits.LUMENS)
                except Exception:
                    pass
                lm = float(l.get("intensity_lm", 3000.0))
                if indoor and key_id is not None and lid != key_id:
                    c.set_editor_property("cast_shadows", False) # double shadows can only come from one key light (lumens no longer discounted -- the AI value is real)
                c.set_editor_property("intensity", lm)
                # Key light = whole-room attenuation (the black-wall fix); mood lights = local pools <=3.2m (whole-room attenuation washes warm light
                # across the ceiling; observed rainy-night interior "big smear on the ceiling") -- photo grammar: fairy lights light their local wall only
                a_r = float(attn_cm or l.get("radius_cm", 2000.0))
                if indoor and lid != key_id:               # when key is None (no-key room) everyone gets a local pool
                    a_r = min(a_r, 320.0)
                c.set_editor_property("attenuation_radius", a_r)
                if l.get("_crgb"):                               # true colored sources (neon/strips): RGB tint
                    cr = l["_crgb"]
                    c.set_light_color(unreal.LinearColor(cr[0], cr[1], cr[2], 1.0))
                    col = cr
                elif l.get("_temp_k"):                           # regular fixtures: blackbody temperature (Color left white)
                    c.set_editor_property("use_temperature", True)
                    c.set_editor_property("temperature", l["_temp_k"])
                    c.set_light_color(unreal.LinearColor(1.0, 1.0, 1.0, 1.0))
                    col = _kelvin_rgb(l["_temp_k"])              # the glow ball still needs a visible warm color
                else:
                    c.set_light_color(unreal.LinearColor(col[0], col[1], col[2], 1.0))
                sr = float(l.get("_src_r", 0.0))
                if sr > 0:                                       # source radius = penumbra width (0 = razor-edged shadows)
                    c.set_editor_property("source_radius", sr)
                    c.set_editor_property("soft_source_radius", sr)
                if float(l.get("_src_len", 0.0)) > 0:            # tubes/strips: capsule light
                    c.set_editor_property("source_length", float(l["_src_len"]))
            if glow_mat and sphere and l.get("_glow", True) and lid in glow_ids:
                # glow balls only when: AI says the source body is visible + has a fixture anchor + within quota (max 2, hand-calibrated)
                try:
                    dia = max(10.0, min(20.0, float(l.get("intensity_lm", 3000.0)) / 60.0))  # smaller (hand-calibrated)
                    g = sub.spawn_actor_from_object(sphere, unreal.Vector(L[0], L[1], L[2]), unreal.Rotator(0, 0, 0))
                    g.set_actor_label("AutoLightGlow_%d" % lid)
                    g.set_actor_scale3d(unreal.Vector(dia / 100.0, dia / 100.0, dia / 100.0))
                    g.set_actor_enable_collision(False)
                    mic, _ = _ensure_mic("MI_LightGlow_%02d" % lid, glow_mat)
                    # In physical brightness range the orb's emissive must be ~= the bulb's real brightness (nits = lm / sphere area / pi),
                    # else it reads as a dark disc against walls lit to hundreds of nits (observed "a black circle on the wall")
                    nits = 1.0
                    if _extended_luminance():
                        r_m = dia / 200.0
                        nits = max(50.0, lm / max(0.02, 4.0 * math.pi * r_m * r_m) / math.pi)
                    unreal.MaterialEditingLibrary.set_material_instance_vector_parameter_value(
                        mic, "GlowColor", unreal.LinearColor(col[0] * nits, col[1] * nits, col[2] * nits, 1.0))
                    g.static_mesh_component.set_material(0, mic)
                except Exception as e:
                    unreal.log_warning("glow sphere %d failed: %s" % (lid, e))
        unreal.log("lights: placed %d point lights (+glow)" % len(lights))
    except Exception as e:
        unreal.log_warning("apply lights failed: %s" % e)


def _apply_camera(cam):
    """Camera: move the editor viewport to the "photo view" and place a CameraActor with the same params."""
    if not cam:
        return
    try:
        h = float(cam.get("height_m", 1.6)) * CM_PER_UNIT
        pitch = float(cam.get("pitch_deg", 0.0))
        fov = float(cam.get("fov_deg", 65.0))
        loc = unreal.Vector(0.0, 0.0, h)
        rot = unreal.Rotator(pitch=pitch, yaw=0.0, roll=0.0)
        try:
            unreal.EditorLevelLibrary.set_level_viewport_camera_info(loc, rot)
        except Exception:
            pass
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        cam_actor = None
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
        unreal.log_warning("apply camera failed: %s" % e)


def _ensure_fly_input():
    """Give the engine DefaultPawn its flight keys (WASD pan / QE up-down / mouse look) -- Enhanced Input projects do
    not bind these legacy axis mappings, so the pawn cannot move. Added to InputSettings on every run
    (InputSettings has no save_config, not persistent across restarts -> this function rebuilds them each time).
    Verified on flat screen + basic VR movement; proper VR teleport/controller motion comes later in the plugin."""
    try:
        ins = unreal.InputSettings.get_default_object()
        pairs = [("MoveForward", "W", 1.0), ("MoveForward", "S", -1.0),
                 ("MoveRight", "D", 1.0), ("MoveRight", "A", -1.0),
                 ("MoveUp", "E", 1.0), ("MoveUp", "Q", -1.0),
                 ("Turn", "MouseX", 1.0), ("LookUp", "MouseY", -1.0)]
        have = set()
        for m in ins.get_editor_property("axis_mappings"):
            try:
                have.add((str(m.get_editor_property("axis_name")),
                          str(m.get_editor_property("key").get_editor_property("key_name"))))
            except Exception:
                pass
        for name, key, scale in pairs:
            if (name, key) in have:
                continue
            m = unreal.InputAxisKeyMapping()
            m.set_editor_property("axis_name", name)
            m.set_editor_property("scale", scale)
            k = unreal.Key(); k.set_editor_property("key_name", key)
            m.set_editor_property("key", k)
            ins.add_axis_mapping(m, False)
        ins.force_rebuild_keymaps()
    except Exception as e:
        unreal.log_warning("ensure fly input: %s" % e)


def _vr_walls(sub, terrain):
    """Invisible collision walls (AutoVRWall_*) around the terrain footprint so a nudged DefaultPawn cannot fly off
    the island into the void. Engine Cube scaled into thin tall walls, BlockAll, hidden (blocks without drawing).
    Auto prefix -> swept automatically by entry cleanup on re-runs."""
    if not (terrain and terrain.get("grid")):
        return
    try:
        cube = unreal.load_asset("/Engine/BasicShapes/Cube")        # 100cm cube
        cx = float(terrain.get("cx_m", 0.0)) * CM_PER_UNIT
        cy = float(terrain.get("cy_m", 0.0)) * CM_PER_UNIT
        hx = max(50.0, float(terrain.get("half_fwd_m", 10.0)) * CM_PER_UNIT)
        hy = max(50.0, float(terrain.get("half_lat_m", 10.0)) * CM_PER_UNIT)
        relief = max(1.0, float(terrain.get("relief_m", 2.0))) * CM_PER_UNIT
        wz = relief * 0.5                                           # wall center height
        wh = (relief + 2000.0) / 100.0                             # wall height (cube=100cm) ~= relief+20m, tall enough to stop fly-overs
        t = 0.4                                                     # wall thickness 40cm
        spanx = max(2.0, 2.0 * hx / 100.0); spany = max(2.0, 2.0 * hy / 100.0)
        for label, wx, wy, sx, sy in [("AutoVRWall_N", cx - hx, cy, t, spany),
                                      ("AutoVRWall_F", cx + hx, cy, t, spany),
                                      ("AutoVRWall_L", cx, cy - hy, spanx, t),
                                      ("AutoVRWall_R", cx, cy + hy, spanx, t)]:
            a = sub.spawn_actor_from_object(cube, unreal.Vector(wx, wy, wz))
            a.set_actor_label(label)
            a.set_actor_scale3d(unreal.Vector(sx, sy, wh))
            comp = a.static_mesh_component
            try:
                comp.set_collision_profile_name("BlockAll")
                comp.set_visibility(False)
            except Exception:
                pass
    except Exception as e:
        unreal.log_warning("VR walls: %s" % e)


def _apply_vr(data):
    """Tier-0 VR playable loop (engine built-in classes only, project-portable): a PlayerStart on the terrain surface
    at eye height (1.6m), facing scene-forward (yaw=0), strictly level (pitch=roll=0, never inheriting photo tilt
    -> no motion sickness), and the level GameMode overridden to the engine DefaultPawn (own camera = headset,
    fly movement, collision). Terrain/object collision was already added in the respective _apply_*.
    After save + restart (loads OpenXR), click 'VR Preview' next to Play for a first-person stereo walkthrough."""
    try:
        _ensure_fly_input()                                         # let DefaultPawn fly/walk with WASD + mouse
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        terr = data.get("terrain")
        cx = float((terr or {}).get("cx_m", 0.0)) * CM_PER_UNIT      # terrain footprint center (world cm)
        cy = float((terr or {}).get("cy_m", 0.0)) * CM_PER_UNIT
        hx = max(50.0, float((terr or {}).get("half_fwd_m", 10.0)) * CM_PER_UNIT)
        # Spawn = "nearest free spot to the terrain center" (generic): ideal is the footprint center (player mid-scene);
        # if occupied, ring-search outward for the first spot clear of every object's bounds (1.5m margin)
        # and >=2m from the invisible walls -- never spawns inside a model, in any layout.
        hy_ = max(50.0, float((terr or {}).get("half_lat_m", 10.0)) * CM_PER_UNIT)
        objs_ps = [a for a in sub.get_all_level_actors() if a.get_actor_label().startswith("OBJ_")]
        obj_bounds = []
        for a in objs_ps:
            try:
                bo, be = a.get_actor_bounds(False)
                obj_bounds.append((bo, be))
            except Exception:
                pass

        wl_sp = data.get("_waterline_cm")

        def _blocked(px_, py_):
            if wl_sp is not None and _terrain_surface_cm(terr, px_, py_) < wl_sp + 10.0:
                return True     # below the waterline = in the lake: not spawnable (observed VR spawning mid-lake wading + audit stance skewed to 99%)
            for bo, be in obj_bounds:
                if abs(px_ - bo.x) <= be.x + 250.0 and abs(py_ - bo.y) <= be.y + 250.0:
                    return True
            return False

        def _standable(px_, py_):
            """Open flat-ground test: 8 directions x two rings (1.5m, 3m); terrain deviation <= ~15 deg (tan15 ~= 0.27).
            Gully floors/crevices/cliff edges inevitably hit a sharp change on the 3m ring -> rejected; gentle
            grassy slopes pass."""
            g0 = _terrain_surface_cm(terr, px_, py_)
            for r_, lim in ((150.0, 40.0), (300.0, 80.0)):
                for k_ in range(8):
                    a_ = math.pi * k_ / 4.0
                    g = _terrain_surface_cm(terr, px_ + r_ * math.cos(a_), py_ + r_ * math.sin(a_))
                    if abs(g - g0) > lim:
                        return False
            return True

        def _search(need_flat):
            """Ring-search outward from the center for the nearest qualifying spot: clear of objects + >=2m from walls (+ flat)."""
            if not _blocked(cx, cy) and (not need_flat or _standable(cx, cy)):
                return cx, cy
            step = max(200.0, 0.08 * min(hx, hy_))
            r = step
            while r <= max(hx, hy_):
                k = max(8, int(2.0 * math.pi * r / step))
                for i in range(k):
                    ang = 2.0 * math.pi * i / k
                    qx = cx + r * math.cos(ang)
                    qy = cy + r * math.sin(ang)
                    if abs(qx - cx) > hx - 200.0 or abs(qy - cy) > hy_ - 200.0:
                        continue                                     # keep 2m from the walls
                    if not _blocked(qx, qy) and (not need_flat or _standable(qx, qy)):
                        return qx, qy
                r += step
            return None

        spot = _search(True) or _search(False)                       # prefer "flat open ground"; only relax if the whole map has none
        px, py = spot if spot else (cx - hx * 0.85, cy)              # extreme fallback: near the edge
        if (px, py) != (cx, cy):
            unreal.log("VR: spawn -> nearest clear&standable spot (%.0f,%.0f)cm" % (px, py))
        gz = _terrain_surface_cm(terr, px, py)
        loc = unreal.Vector(px, py, gz + 160.0)                      # ground + eye height (1.6m)
        # Facing = most open direction (16 rays vs object bounds/walls; farthest visible distance wins);
        # near-ties (>=90% of max) prefer the bearing closest to the object centroid -- no spawning face-to-wall, still likely to see the subject.
        yaw = 0.0
        cen_bearing = None
        if objs_ps:
            ox = sum(a.get_actor_location().x for a in objs_ps) / len(objs_ps)
            oy = sum(a.get_actor_location().y for a in objs_ps) / len(objs_ps)
            if abs(ox - px) + abs(oy - py) > 100.0:
                cen_bearing = math.atan2(oy - py, ox - px)

        def _view_dist(ang_):
            ca, sa = math.cos(ang_), math.sin(ang_)
            d, step_ = 0.0, 100.0
            while d < 6000.0:
                d += step_
                qx, qy = px + d * ca, py + d * sa
                if abs(qx - cx) > hx or abs(qy - cy) > hy_:
                    return d                                         # stop at the perimeter wall
                for bo, be in obj_bounds:
                    if abs(qx - bo.x) <= be.x and abs(qy - bo.y) <= be.y:
                        return d                                     # hit an object
            return d

        dirs = [(2.0 * math.pi * k_ / 16.0, _view_dist(2.0 * math.pi * k_ / 16.0)) for k_ in range(16)]
        dmax = max(d_ for _, d_ in dirs)
        best = [a_ for a_, d_ in dirs if d_ >= 0.9 * dmax]
        if cen_bearing is not None:
            yaw = math.degrees(min(best, key=lambda a_: abs(math.atan2(math.sin(a_ - cen_bearing), math.cos(a_ - cen_bearing)))))
        elif best:
            yaw = math.degrees(best[0])
        rot = unreal.Rotator(pitch=0.0, yaw=yaw, roll=0.0)          # strictly level: in VR pitch/roll belong to the headset only
        starts = [a for a in sub.get_all_level_actors() if isinstance(a, unreal.PlayerStart)]
        if not starts:                                              # create one if missing (Auto prefix -> swept by the next run's cleanup)
            ps = sub.spawn_actor_from_class(unreal.PlayerStart, loc, rot)
            ps.set_actor_label("AutoPlayerStart")
            starts = [ps]
        for a in starts:                                            # move every PlayerStart to the correct spot so any GameMode pick works
            a.set_actor_location_and_rotation(loc, rot, False, False)
        _vr_walls(sub, terr)                                        # invisible collision walls all around, so nobody flies off the island into the void
        try:                                                        # GameMode override -> DefaultPawn (first-person camera; = headset in VR); engine built-in = portable
            ws = unreal.EditorLevelLibrary.get_editor_world().get_world_settings()
            ws.set_editor_property("default_game_mode", unreal.GameModeBase)
        except Exception as e:
            unreal.log_warning("VR gamemode override failed: %s" % e)
        try:                                                        # key: park the editor viewport at the spawn -> Play "current camera position" starts right on the terrain (bypasses PlayerStart scoring/dropdown traps)
            unreal.EditorLevelLibrary.set_level_viewport_camera_info(loc, rot)
        except Exception:
            pass
        unreal.log("VR: spawn @ (%.0f,%.0f,z=%.0f)cm 水平朝前；编辑器视口已停在出生点 → Play『当前相机位置』即从这生成；四周隐形墙。"
                   % (px, py, gz + 160.0))
    except Exception as e:
        unreal.log_warning("apply VR failed: %s" % e)


def _extend_ground_to_objects(env, terrain):
    """Generic mechanism: when objects scatter far beyond the reconstructed terrain footprint (common in flat scenes
    like streets -- monocular depth only recovers a small nearby patch) and the terrain is nearly flat (relief<3m)
    -> lay one big flat ground covering all objects, seat objects/invisible walls/spawn onto that plane, and hide
    the small fake-relief terrain. Scenes with true relief whose footprint already covers the objects (landscapes)
    fail the condition -> untouched. Not a per-photo hack; purely scene-geometry driven."""
    if not (terrain and terrain.get("grid")):
        return
    try:
        eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        acts = eas.get_all_level_actors()
        objs = [a for a in acts if a.get_actor_label().startswith("OBJ_")]
        if not objs:
            return
        xs = [a.get_actor_location().x for a in objs]; ys = [a.get_actor_location().y for a in objs]
        ox0, ox1, oy0, oy1 = min(xs), max(xs), min(ys), max(ys)
        cx = float(terrain.get("cx_m", 0.0)) * CM_PER_UNIT; cy = float(terrain.get("cy_m", 0.0)) * CM_PER_UNIT
        hf = max(50.0, float(terrain.get("half_fwd_m", 10.0)) * CM_PER_UNIT)
        hl = max(50.0, float(terrain.get("half_lat_m", 10.0)) * CM_PER_UNIT)
        relief = float(terrain.get("relief_m", 2.0))
        beyond = (ox0 < cx - 1.3 * hf) or (ox1 > cx + 1.3 * hf) or (oy0 < cy - 1.3 * hl) or (oy1 > cy + 1.3 * hl)
        if not (beyond and relief < 3.0):           # only intervene when "flat and scattered"; bumpy/already-covered scenes untouched
            return
        M = 1300.0
        gx0, gx1 = ox0 - M, ox1 + M; gy0, gy1 = oy0 - M, oy1 + M
        gcx, gcy = (gx0 + gx1) / 2.0, (gy0 + gy1) / 2.0
        GZ = _terrain_surface_cm(terrain, cx, cy)   # flat street level
        t = next((a for a in acts if a.get_actor_label() == "AutoTerrain"), None)
        if t:
            t.static_mesh_component.set_visibility(False)
            t.static_mesh_component.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)
        g = next((a for a in acts if a.get_actor_label() == "AutoGround"), None)
        if g is None:
            g = eas.spawn_actor_from_object(unreal.load_asset("/Engine/BasicShapes/Plane"), unreal.Vector(gcx, gcy, GZ))
            g.set_actor_label("AutoGround")
        g.set_actor_location(unreal.Vector(gcx, gcy, GZ), False, False)
        g.set_actor_scale3d(unreal.Vector((gx1 - gx0) / 100.0, (gy1 - gy0) / 100.0, 1.0))
        gc = g.static_mesh_component
        gc.set_collision_profile_name("BlockAll"); gc.set_collision_enabled(unreal.CollisionEnabled.QUERY_AND_PHYSICS)
        for a in objs:                              # seat objects on the plane (bottoms at GZ)
            loc = a.get_actor_location(); sc = a.get_actor_scale3d()
            mesh = a.static_mesh_component.get_editor_property("static_mesh")
            newz = GZ
            if mesh:
                b = mesh.get_bounds(); newz = GZ - (b.origin.z - b.box_extent.z) * sc.z
            a.set_actor_location(unreal.Vector(loc.x, loc.y, newz), False, False)
        cube = unreal.load_asset("/Engine/BasicShapes/Cube")
        spanx = (gx1 - gx0) / 100.0; spany = (gy1 - gy0) / 100.0
        for lbl, wx, wy, sx, sy in (("AutoVRWall_N", gx0, gcy, 0.5, spany), ("AutoVRWall_F", gx1, gcy, 0.5, spany),
                                    ("AutoVRWall_L", gcx, gy0, spanx, 0.5), ("AutoVRWall_R", gcx, gy1, spanx, 0.5)):
            w = next((a for a in acts if a.get_actor_label() == lbl), None)
            if w is None:
                w = eas.spawn_actor_from_object(cube, unreal.Vector(wx, wy, GZ + 1500.0)); w.set_actor_label(lbl)
            w.set_actor_location(unreal.Vector(wx, wy, GZ + 1500.0), False, False)
            w.set_actor_scale3d(unreal.Vector(sx, sy, 35.0))
            w.static_mesh_component.set_collision_profile_name("BlockAll"); w.static_mesh_component.set_visibility(False)
        psloc = unreal.Vector(0.0, 0.0, GZ + 160.0); psrot = unreal.Rotator(0.0, 0.0, 0.0)
        for a in acts:
            if isinstance(a, unreal.PlayerStart):
                a.set_actor_location_and_rotation(psloc, psrot, False, False)
        try:
            ues.set_level_viewport_camera_info(psloc, psrot)
        except Exception:
            pass
        unreal.log("ground extended to objects: flat %.0fx%.0f m @ z=%.0f (footprint was %.0fx%.0f)"
                   % ((gx1 - gx0) / 100, (gy1 - gy0) / 100, GZ, 2 * hf / 100, 2 * hl / 100))
    except Exception as e:
        unreal.log_warning("extend ground failed: %s" % e)


GROUND_FLAT_MAT = "/Game/Auto/M_GroundFlat"


def _ensure_ground_flat_material(default_tex=None):
    """Flat/distant ground material: photo-matched GroundTex (world-space tiling, ~8m per tile) x Tint -> BaseColor ->
    the ground gets real texture (asphalt/dirt etc.); without a photo ground texture GroundTex stays default and
    Tint supplies the flat color. No radial darkening (distant fade belongs to fog, not material blackening -> no
    black ring). World tiling -> footprint ground and the distant skirt share texel scale, nothing stretches with
    plane scaling."""
    if unreal.EditorAssetLibrary.does_asset_exist(GROUND_FLAT_MAT):
        return unreal.load_asset(GROUND_FLAT_MAT)
    atools = unreal.AssetToolsHelpers.get_asset_tools()
    mat = atools.create_asset("M_GroundFlat", _apath(""), unreal.Material, unreal.MaterialFactoryNew())
    mel = unreal.MaterialEditingLibrary
    wp = mel.create_material_expression(mat, unreal.MaterialExpressionWorldPosition, -900, 0)
    mask = mel.create_material_expression(mat, unreal.MaterialExpressionComponentMask, -720, 0)
    mask.set_editor_property("r", True); mask.set_editor_property("g", True)
    mask.set_editor_property("b", False); mask.set_editor_property("a", False)
    mel.connect_material_expressions(wp, "", mask, "")
    tile = mel.create_material_expression(mat, unreal.MaterialExpressionConstant, -720, 140); tile.set_editor_property("r", 800.0)
    uv = mel.create_material_expression(mat, unreal.MaterialExpressionDivide, -560, 0)
    mel.connect_material_expressions(mask, "", uv, "A"); mel.connect_material_expressions(tile, "", uv, "B")   # world XY / 800cm -> UV
    ts = mel.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, -380, -40)
    ts.set_editor_property("parameter_name", "GroundTex")
    if default_tex:
        try:
            ts.set_editor_property("texture", default_tex)
        except Exception:
            pass
    try:
        ts.set_editor_property("sampler_type", unreal.MaterialSamplerType.SAMPLERTYPE_COLOR)
    except Exception:
        pass
    mel.connect_material_expressions(uv, "", ts, "UVs")
    tint = mel.create_material_expression(mat, unreal.MaterialExpressionVectorParameter, -380, 220)
    tint.set_editor_property("parameter_name", "Tint"); tint.set_editor_property("default_value", unreal.LinearColor(1, 1, 1, 1))
    mul = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -160, 40)
    mel.connect_material_expressions(ts, "RGB", mul, "A"); mel.connect_material_expressions(tint, "", mul, "B")
    mel.connect_material_property(mul, "", unreal.MaterialProperty.MP_BASE_COLOR)
    rg = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -160, 300)
    rg.set_editor_property("parameter_name", "Roughness"); rg.set_editor_property("default_value", 0.9)
    mel.connect_material_property(rg, "", unreal.MaterialProperty.MP_ROUGHNESS)
    mt = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -160, 410)
    mt.set_editor_property("parameter_name", "Metallic"); mt.set_editor_property("default_value", 0.0)
    mel.connect_material_property(mt, "", unreal.MaterialProperty.MP_METALLIC)
    mel.recompile_material(mat)
    unreal.EditorAssetLibrary.save_asset(GROUND_FLAT_MAT)
    return mat


def _vnoise(x, y, seed=7.0):
    """Pure-python value noise (numpy is not guaranteed inside UE): hashed lattice + smoothstep interpolation; callers
    stack two octaves."""
    def h(ix, iy):
        v = math.sin(ix * 127.1 + iy * 311.7 + seed * 13.7) * 43758.5453
        return v - math.floor(v)
    ix, iy = math.floor(x), math.floor(y)
    fx, fy = x - ix, y - iy
    sx, sy = fx * fx * (3.0 - 2.0 * fx), fy * fy * (3.0 - 2.0 * fy)
    a, b = h(ix, iy), h(ix + 1, iy)
    c, d = h(ix, iy + 1), h(ix + 1, iy + 1)
    return (a + (b - a) * sx) * (1.0 - sy) + (c + (d - c) * sx) * sy


_VISTA_SHAPE = {  # distant_terrain -> (relief amplitude m, wavelength m); amplitude further scaled by Gemini distant_ruggedness.
    "flat": (2.0, 220.0), "dunes": (10.0, 90.0), "hills": (35.0, 340.0),
    "mountains": (75.0, 600.0), "water": (0.0, 200.0),    # mountain amp 130 -> 75 (observed: high wall by the platform = black bowl; longer wavelength reads calmer)
}


def _apply_vista(terrain, env):
    """Distant terrain ring (plan A): a low-poly relief ring ~1.5km beyond the playable platform; its shape judged by
    AI (distant_terrain/distant_ruggedness from the photo horizon/biome), ground texture + ground tint; the outer
    rim handed to fog and the HDRI horizon (plan B). Replaces the old bright grey slab (observed "really hurts
    immersion")."""
    try:
        cx = float(terrain.get("cx_m", 0.0)) * CM_PER_UNIT
        cy = float(terrain.get("cy_m", 0.0)) * CM_PER_UNIT
        hx = max(50.0, float(terrain.get("half_fwd_m", 10.0)) * CM_PER_UNIT)
        hy = max(50.0, float(terrain.get("half_lat_m", 10.0)) * CM_PER_UNIT)
        kind = str(env.get("distant_terrain", "")) or {"sand": "dunes", "dirt": "hills", "grass": "hills",
                                                       "snow": "hills"}.get(str(env.get("ground_material", "")), "flat")
        if kind not in _VISTA_SHAPE:
            kind = "flat"
        rug = float(env.get("distant_ruggedness", 0.4))
        # bearing sectors (1b): AI judges the distance per front/right/back/left (photo horizons are often asymmetric); cosine-smooth blend between sectors
        secs = env.get("distant_sectors") or {}

        def _sector_params(ang):
            # world frame: front=+X (0 deg), right=+Y (90), back (180), left (270)
            names = ("front", "right", "back", "left")
            a = (math.degrees(ang) % 360.0) / 90.0
            i0 = int(a) % 4
            i1 = (i0 + 1) % 4
            f = a - int(a)
            f = f * f * (3.0 - 2.0 * f)

            def p(nm):
                s = secs.get(nm) or {}
                t = str(s.get("t", kind))
                t = t if t in _VISTA_SHAPE else kind
                rg = float(s.get("rug", rug))
                am, wm = _VISTA_SHAPE[t]
                return am * (0.5 + rg), wm
            a0, w0 = p(names[i0])
            a1, w1 = p(names[i1])
            return ((a0 * (1 - f) + a1 * f) * CM_PER_UNIT, (w0 * (1 - f) + w1 * f) * CM_PER_UNIT)
        r0 = 0.99 * max(hx, hy)                           # inner rim tucked under the platform (1.12x left a 5.7m gap at square-side midpoints -> sphere bottom showed)
        r1 = 400000.0                                     # default 4km; with a sky sphere extend to just inside the shell (no more blue sphere-bottom showing)
        try:
            sub_p = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
            dome = next((a for a in sub_p.get_all_level_actors()
                         if isinstance(a, unreal.StaticMeshActor) and a.get_actor_label() == "AutoSky"), None)
            if dome:
                org_d, ext_d = dome.get_actor_bounds(False)
                r1 = max(120000.0, 0.93 * max(ext_d.x, ext_d.y))
        except Exception:
            pass
        NA, NR = 48, 14
        _VPATH = _apath("SM_AutoVista")
        eal = unreal.EditorAssetLibrary
        if eal.does_asset_exist(_VPATH):                  # same UE 5.8 crash-safe flow as SM_AutoTerrain
            sub0 = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
            old = unreal.load_asset(_VPATH)
            for a0 in sub0.get_all_level_actors():
                try:
                    c0 = a0.static_mesh_component if isinstance(a0, unreal.StaticMeshActor) else None
                    if c0 and c0.get_editor_property("static_mesh") == old:
                        c0.set_static_mesh(None)
                except Exception:
                    pass
            try:
                unreal.SystemLibrary.collect_garbage()
                eal.delete_asset(_VPATH)
            except Exception:
                pass
        sm = (unreal.load_asset(_VPATH) if eal.does_asset_exist(_VPATH)
              else unreal.AssetToolsHelpers.get_asset_tools().create_asset(
                  "SM_AutoVista", _apath(""), unreal.StaticMesh, None))
        smd = sm.create_static_mesh_description()
        pg = smd.create_polygon_group()

        def _mk(x, y, z, u, vv):
            v = smd.create_vertex(); smd.set_vertex_position(v, unreal.Vector(x, y, z))
            vi = smd.create_vertex_instance(v); smd.set_vertex_instance_uv(vi, unreal.Vector2D(u, vv), 0)
            return vi

        # Height continuity (observed "carpet on a table"): the ring starts at the platform edge's real height and blends into noise hills over 80m
        # -> skirt walls buried, platform edge disappears. Square boundary in polar form: bd(ang) = h/max(|cos|,|sin|).
        grid_ = terrain.get("grid") or [[0.0]]
        gn = len(grid_)
        relief_cm = max(1.0, float(terrain.get("relief_m", 2.0))) * CM_PER_UNIT

        def _grid_z(px_, py_):
            gr = min(gn - 1.0, max(0.0, (px_ - (cx - hx)) / (2.0 * hx) * (gn - 1)))
            gc = min(gn - 1.0, max(0.0, (py_ - (cy - hy)) / (2.0 * hy) * (gn - 1)))
            r0i, c0i = int(gr), int(gc)
            r1i, c1i = min(gn - 1, r0i + 1), min(gn - 1, c0i + 1)
            fr, fc = gr - r0i, gc - c0i
            return (grid_[r0i][c0i] * (1 - fr) * (1 - fc) + grid_[r1i][c0i] * fr * (1 - fc) +
                    grid_[r0i][c1i] * (1 - fr) * fc + grid_[r1i][c1i] * fr * fc) * relief_cm

        def _edge_z(ang):
            bd = max(hx, hy) / max(abs(math.cos(ang)), abs(math.sin(ang)), 1e-6)
            return _grid_z(cx + bd * math.cos(ang), cy + bd * math.sin(ang)), bd

        def _bite(ang):
            # shape-breaking (observed "square structure"): outer soil bites 6-18% into the platform by angular noise, periodically continuous (noise sampled on a circle)
            return 0.06 + 0.12 * _vnoise(math.cos(ang) * 1.7 + 9.0, math.sin(ang) * 1.7 + 9.0, seed=77.0)

        BLEND = 8000.0                                    # platform edge -> hills blend band, 80m
        KB = 3                                            # shape-breaking tongue rings (thin shell biting the platform)
        KS = 6                                            # seam-dense rings: laid linearly by angle within the blend band (geometric spacing
        NR = KB + KS + 12                                 # put only 2 sample rings here -> interpolation created a 40cm residual cliff + radial jaggies)
        VI = [[None] * NA for _ in range(NR)]
        for i in range(NR):
            for j in range(NA):
                ang = 2.0 * math.pi * j / NA
                ez, bd = _edge_z(ang)
                bite = _bite(ang)
                if i < KB:
                    # tongue: outer soil overlaps the platform (+3cm shell, same material/UV) -- irregular tongues break the square outline
                    r = bd * (1.0 - bite * (1.0 - i / float(KB)))
                elif i < KB + KS:
                    r = bd + BLEND * ((i - KB) / (KS - 1.0))   # blend band: ~16m per ring (tracks the square edge by bearing)
                else:
                    t2 = (i - KB - KS + 1.0) / (NR - KB - KS)
                    r = (bd + BLEND) * math.pow(r1 / (bd + BLEND), t2)   # beyond the band: geometric spacing outward
                x = cx + r * math.cos(ang)
                y = cy + r * math.sin(ang)
                amp_s, wl_s = _sector_params(ang)
                n1 = _vnoise(x / wl_s, y / wl_s)
                n2 = _vnoise(x / (wl_s * 0.37), y / (wl_s * 0.37), seed=31.0)
                n3 = _vnoise(x / (wl_s * 3.1), y / (wl_s * 3.1), seed=57.0)       # large-scale relief (multi-scale sum)
                ofade = 1.0 - min(1.0, max(0.0, (r - 0.85 * r1) / (0.15 * r1)))   # flatten the outer rim (meets fog/sphere bottom)
                zn = amp_s * ofade * (((n1 * 0.55 + n2 * 0.2 + n3 * 0.25)) ** 1.3)
                if r <= bd:
                    z = _grid_z(x, y) + 3.0                # tongue shell +3cm above the platform (covers the straight edge)
                else:
                    w = min(1.0, (r - bd) / BLEND)
                    w = w * w * (3.0 - 2.0 * w)
                    z = (ez + 2.0) * (1.0 - w) + zn * w    # platform edge +2cm (tongue continues) -> into the hills
                # UV continues the platform's normalized space (masks tile periodically): the blend pattern crosses the seam continuously
                VI[i][j] = _mk(x, y, z, (y - (cy - hy)) / (2.0 * hy), (x - (cx - hx)) / (2.0 * hx))
        for i in range(NR - 1):
            for j in range(NA):
                j2 = (j + 1) % NA
                a, b = VI[i][j], VI[i + 1][j]
                c, d = VI[i + 1][j2], VI[i][j2]
                smd.create_triangle(pg, [a, c, b])        # normals +Z (same winding as the terrain top)
                smd.create_triangle(pg, [a, d, c])
        sm.build_from_static_mesh_descriptions([smd], False)
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        actor = sub.spawn_actor_from_object(sm, unreal.Vector(0, 0, 0))
        actor.set_actor_label("AutoVista")
        comp = actor.static_mesh_component
        comp.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)
        actor.set_actor_enable_collision(False)
        # Material: single-texture MIC (photo ground) + persistent tint. NOT the platform blend MIC -- engine tests show its layer choice is driven by
        # a height curve (domain = platform 0..relief); the ring's z (-25cm..45m) saturates out of domain into the dark layer
        # (even flat rings darken -- z sits at the domain's low end). Tint calibrated by dev/_tint_match.py against the platform's actual render color, stored in the MIC.
        # Aerial perspective (AI flow): distant hills desaturate and shift toward Gemini's HORIZON HAZE color (horizon_color, falls back to fog_color).
        # High HazeAmt = distant shapes are mostly haze-colored masses -> cures "pebble bowl too dark". Code does the lerp physics, AI judges the haze color.
        haze = env.get("horizon_color") or env.get("fog_color") or [0.6, 0.66, 0.74]
        try:
            haze = [float(haze[0]), float(haze[1]), float(haze[2])]
        except Exception:
            haze = [0.6, 0.66, 0.74]
        mat = None
        for cand in (_aipath("Terrain/T_AutoTerrainA"), _aipath("Terrain/T_AutoTerrainB")):
            tex = unreal.load_asset(cand)
            if tex:
                mat = _ensure_vista_mic(tex, [1.0, 1.0, 1.0], haze=haze, haze_amt=0.12)  # low haze: distant ground = same texture as the foreground (materials must match), only a whisper of atmosphere
                break
        if mat is None:
            mat = _ensure_vista_mic(None, [1.0, 1.0, 1.0], haze=haze, haze_amt=0.12)
        if mat:
            comp.set_material(0, mat)
        unreal.log("vista ring: %s rug=%.2f sectors=%s r=%.0f..%.0fm (AI distant terrain)"
                   % (kind, rug, {k: (v or {}).get("t") for k, v in (secs or {}).items()},
                      r0 / CM_PER_UNIT, r1 / CM_PER_UNIT))
    except Exception as e:
        unreal.log_warning("vista failed: %s" % e)


def _vista_height(terrain, env, x, y):
    """Ring surface height (cm) at a point -- same math as the _apply_vista mesh (change one, sync the other). Used for
    midground placement."""
    cx = float(terrain.get("cx_m", 0.0)) * CM_PER_UNIT
    cy = float(terrain.get("cy_m", 0.0)) * CM_PER_UNIT
    hx = max(50.0, float(terrain.get("half_fwd_m", 10.0)) * CM_PER_UNIT)
    hy = max(50.0, float(terrain.get("half_lat_m", 10.0)) * CM_PER_UNIT)
    kind = str(env.get("distant_terrain", "flat"))
    kind = kind if kind in _VISTA_SHAPE else "flat"
    rug = float(env.get("distant_ruggedness", 0.4))
    secs = env.get("distant_sectors") or {}
    ang = math.atan2(y - cy, x - cx)
    names = ("front", "right", "back", "left")
    a = (math.degrees(ang) % 360.0) / 90.0
    i0 = int(a) % 4; i1 = (i0 + 1) % 4
    f = a - int(a); f = f * f * (3.0 - 2.0 * f)

    def p(nm):
        s = secs.get(nm) or {}
        t = str(s.get("t", kind)); t = t if t in _VISTA_SHAPE else kind
        rg = float(s.get("rug", rug))
        am, wm = _VISTA_SHAPE[t]
        return am * (0.5 + rg), wm
    a0, w0 = p(names[i0]); a1, w1 = p(names[i1])
    amp_s = (a0 * (1 - f) + a1 * f) * CM_PER_UNIT
    wl_s = (w0 * (1 - f) + w1 * f) * CM_PER_UNIT
    grid_ = terrain.get("grid") or [[0.0]]
    gn = len(grid_)
    relief_cm = max(1.0, float(terrain.get("relief_m", 2.0))) * CM_PER_UNIT
    bd = max(hx, hy) / max(abs(math.cos(ang)), abs(math.sin(ang)), 1e-6)
    px_, py_ = cx + bd * math.cos(ang), cy + bd * math.sin(ang)
    gr = min(gn - 1.0, max(0.0, (px_ - (cx - hx)) / (2.0 * hx) * (gn - 1)))
    gc = min(gn - 1.0, max(0.0, (py_ - (cy - hy)) / (2.0 * hy) * (gn - 1)))
    r0i, c0i = int(gr), int(gc); r1i, c1i = min(gn - 1, r0i + 1), min(gn - 1, c0i + 1)
    fr, fc = gr - r0i, gc - c0i
    ez = (grid_[r0i][c0i] * (1 - fr) * (1 - fc) + grid_[r1i][c0i] * fr * (1 - fc) +
          grid_[r0i][c1i] * (1 - fr) * fc + grid_[r1i][c1i] * fr * fc) * relief_cm
    r = math.hypot(x - cx, y - cy)
    r1 = 400000.0
    n1 = _vnoise(x / wl_s, y / wl_s)
    n2 = _vnoise(x / (wl_s * 0.37), y / (wl_s * 0.37), seed=31.0)
    n3 = _vnoise(x / (wl_s * 3.1), y / (wl_s * 3.1), seed=57.0)
    ofade = 1.0 - min(1.0, max(0.0, (r - 0.85 * r1) / (0.15 * r1)))
    zn = amp_s * ofade * (((n1 * 0.55 + n2 * 0.2 + n3 * 0.25)) ** 1.3)
    if r <= bd:
        return ez - 25.0
    w = min(1.0, (r - bd) / 8000.0)
    w = w * w * (3.0 - 2.0 * w)
    return (ez - 8.0) * (1.0 - w) + zn * w


def _apply_midground(data, env):
    """Midground silhouette belt (2b): AI picks types (midground.use_object_ids/count/scale_range); variants of rebuilt
    objects scattered into the 150-600m ring, landed on the ring surface. Zero new assets; the biome naturally
    matches the scene."""
    try:
        mg = data.get("midground") or {}
        ids = mg.get("use_object_ids") or []
        cnt = int(mg.get("count", 0))
        terr = data.get("terrain")
        if not ids or cnt <= 0 or not terr:
            unreal.log("midground: skipped (AI chose none)")
            return
        slo, shi = (mg.get("scale_range") or [0.8, 2.0])[:2]
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        meshes = []
        for a in sub.get_all_level_actors():
            for oid in ids:
                if a.get_actor_label() == "OBJ_%02d" % oid and isinstance(a, unreal.StaticMeshActor):
                    m = a.static_mesh_component.get_editor_property("static_mesh")
                    if m:
                        meshes.append(m)
        if not meshes:
            unreal.log_warning("midground: no source meshes")
            return
        cx = float(terr.get("cx_m", 0.0)) * CM_PER_UNIT
        cy = float(terr.get("cy_m", 0.0)) * CM_PER_UNIT
        rng_ = random.Random(911)
        # The vista mesh is deleted (design decision: flat skirt + fog to the sky), but _vista_height still computed that phantom hill height -> midground trees floated.
        # Fix: ray-probe the real surface (terrain has collision) per tree; no hit (the skirt has no collision) -> fall back to the skirt base z_base.
        _world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        _objz = [a.get_actor_location().z for a in sub.get_all_level_actors() if a.get_actor_label().startswith("OBJ_")]
        _probe = (sum(_objz) / len(_objz)) if _objz else 0.0
        _gact = next((a for a in sub.get_all_level_actors() if a.get_actor_label() == "AutoGround"), None)
        _zbase = _gact.get_actor_location().z if _gact else (_probe - 1500.0)
        n = 0
        for k in range(min(150, max(int(cnt), 90))):       # densify: >=90 trees, hides the terrain-edge hard cut + richer distance (the old cap of 40 was too sparse)
            ang = rng_.uniform(0.0, 2.0 * math.pi)
            r = rng_.uniform(4500.0, 48000.0)              # 45-480m: fill from the terrain edge (~40m) outward, covering the old empty band inside 150m
            x, y = cx + r * math.cos(ang), cy + r * math.sin(ang)
            _zt = _ground_trace_z(_world, x, y, _probe)
            z = _zt if _zt is not None else _zbase         # land on real ground/skirt, not the deleted vista height (the floating root cause)
            m = meshes[k % len(meshes)]
            actor = sub.spawn_actor_from_object(m, unreal.Vector(x, y, z))
            if actor is None:
                continue
            actor.set_actor_label("AutoMid_%02d" % k)
            s = rng_.uniform(float(slo), float(shi))
            actor.set_actor_scale3d(unreal.Vector(s, s, s))
            actor.set_actor_rotation(unreal.Rotator(pitch=0.0, yaw=rng_.uniform(0, 360), roll=0.0), False)
            try:
                org_, ext_ = actor.get_actor_bounds(False)
                actor.set_actor_location(unreal.Vector(x, y, z + (actor.get_actor_location().z - (org_.z - ext_.z)) - 0.08 * ext_.z), False, False)
            except Exception:
                pass
            actor.static_mesh_component.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)
            actor.set_actor_enable_collision(False)
            n += 1
        unreal.log("midground: %d silhouettes from objs %s scale %.1f-%.1f (AI)" % (n, ids, slo, shi))
    except Exception as e:
        unreal.log_warning("midground failed: %s" % e)


def _apply_fogband(data, env):
    """Midground leading-edge fog band (3c): LocalFogVolume local volumetric fog (true volume / any view angle /
    VR-safe); class unavailable -> falls back to mist-particle fog banks (3a). Density/color driven by env (AI)."""
    try:
        terr = data.get("terrain")
        if not terr:
            return
        cx = float(terr.get("cx_m", 0.0)) * CM_PER_UNIT
        cy = float(terr.get("cy_m", 0.0)) * CM_PER_UNIT
        col = env.get("fog_color") or [0.6, 0.7, 0.85]
        # Extinction: target in-fog visibility 150-400m (0.0026-0.007/m). The old x220 formula at fog 0.012 gave
        # 0.156/m = 20m pea-soup visibility (whole screen gold mush, measured)
        dens = max(0.04, min(0.12, float(env.get("fog_density", 0.005)) * 8.0))
        cls = getattr(unreal, "LocalFogVolume", None)
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        rng_ = random.Random(733)
        if cls is None:
            unreal.log_warning("fogband: LocalFogVolume class unavailable; skipped (mist preset covers)")
            return
        n = 0
        for k in range(4):
            ang = rng_.uniform(0.0, 2.0 * math.pi)
            r = rng_.uniform(13000.0, 26000.0)            # 130-260m: midground leading edge
            x, y = cx + r * math.cos(ang), cy + r * math.sin(ang)
            z = _vista_height(terr, env, x, y)
            a = sub.spawn_actor_from_class(cls, unreal.Vector(x, y, z + 500.0))
            if a is None:
                continue
            a.set_actor_label("AutoFogBand_%d" % k)
            a.set_actor_scale3d(unreal.Vector(90.0, 90.0, 14.0))   # ~90m-radius flat fog volume
            try:
                c = a.get_component_by_class(unreal.LocalFogVolumeComponent)
                c.set_editor_property("radial_fog_extinction", 0.06 * dens)
                c.set_editor_property("height_fog_extinction", 0.06 * dens)
                try:
                    c.set_editor_property("fog_albedo", unreal.LinearColor(col[0], col[1], col[2], 1.0))
                except Exception:
                    pass
            except Exception as e:
                unreal.log_warning("fogband params: %s" % e)
            n += 1
        unreal.log("fogband: %d local fog volumes (density x%.1f)" % (n, dens))
    except Exception as e:
        unreal.log_warning("fogband failed: %s" % e)


def _ensure_vista_mic(tex, tint, haze=None, haze_amt=0.7):
    """Distant ring material (aerial perspective): BaseColor = Lerp(texture x Tint, Haze horizon color, HazeAmt),
    Roughness 1. Distant hills desaturate with distance toward Gemini's horizon haze -> no longer a black bowl of
    tiled foreground pebble texture (observed "too dark")."""
    try:
        path = _apath("M_AutoVista")
        mat = unreal.load_asset(path)
        mel = unreal.MaterialEditingLibrary
        has_haze = mat is not None and any(
            isinstance(e, unreal.MaterialExpressionVectorParameter)
            and str(e.get_editor_property("parameter_name")) == "Haze"
            for e in (mel.get_material_expressions(mat) or []))
        if mat is None or not has_haze:                   # newly built or old version without Haze -> rebuild the graph in place (aerial-perspective upgrade)
            if mat is None:
                mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
                    "M_AutoVista", _apath(""), unreal.Material, unreal.MaterialFactoryNew())
            else:
                mel.delete_all_material_expressions(mat)
            ts = mel.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, -700, -100)
            ts.set_editor_property("parameter_name", "Albedo")
            vp = mel.create_material_expression(mat, unreal.MaterialExpressionVectorParameter, -700, 160)
            vp.set_editor_property("parameter_name", "Tint")
            vp.set_editor_property("default_value", unreal.LinearColor(1, 1, 1, 1))
            mul = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -460, 0)
            mel.connect_material_expressions(ts, "RGB", mul, "A")
            mel.connect_material_expressions(vp, "", mul, "B")
            hz = mel.create_material_expression(mat, unreal.MaterialExpressionVectorParameter, -460, 200)
            hz.set_editor_property("parameter_name", "Haze")
            hz.set_editor_property("default_value", unreal.LinearColor(0.55, 0.6, 0.68, 1))
            ha = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -460, 340)
            ha.set_editor_property("parameter_name", "HazeAmt")
            ha.set_editor_property("default_value", 0.7)
            lp = mel.create_material_expression(mat, unreal.MaterialExpressionLinearInterpolate, -220, 60)
            mel.connect_material_expressions(mul, "", lp, "A")
            mel.connect_material_expressions(hz, "", lp, "B")
            mel.connect_material_expressions(ha, "", lp, "Alpha")
            mel.connect_material_property(lp, "", unreal.MaterialProperty.MP_BASE_COLOR)
            rc = mel.create_material_expression(mat, unreal.MaterialExpressionConstant, -220, 260)
            rc.set_editor_property("r", 1.0)
            mel.connect_material_property(rc, "", unreal.MaterialProperty.MP_ROUGHNESS)
            mel.recompile_material(mat)
            unreal.EditorAssetLibrary.save_asset(path)
        mic, mic_path = _ensure_mic("MI_AutoVista", mat)
        if tex:
            mel.set_material_instance_texture_parameter_value(mic, "Albedo", tex)
        if tint is not None:
            mel.set_material_instance_vector_parameter_value(mic, "Tint", unreal.LinearColor(tint[0], tint[1], tint[2], 1.0))
        if haze is not None:                              # Gemini horizon haze color (aerial-perspective target)
            mel.set_material_instance_vector_parameter_value(mic, "Haze", unreal.LinearColor(haze[0], haze[1], haze[2], 1.0))
            mel.set_material_instance_scalar_parameter_value(mic, "HazeAmt", float(haze_amt))
        unreal.EditorAssetLibrary.save_asset(mic_path)
        return mic
    except Exception as e:
        unreal.log_warning("vista material failed: %s" % e)
        return None


def _ensure_room_master():
    """Indoor surface master material: Albedo (UV x UVScale tiling) x Tint -> BaseColor, parametric Roughness.
    Engine Cube faces are UV 0-1; tiling comes from UVScale=(edge length/TU). Kept separate from M_AutoVista
    (that one has no UV params)."""
    path = _apath("M_AutoRoom")
    mat = unreal.load_asset(path)
    if mat:
        return mat
    mel = unreal.MaterialEditingLibrary
    mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        "M_AutoRoom", _apath(""), unreal.Material, unreal.MaterialFactoryNew())
    tc = mel.create_material_expression(mat, unreal.MaterialExpressionTextureCoordinate, -1000, -150)
    uvp = mel.create_material_expression(mat, unreal.MaterialExpressionVectorParameter, -1000, 50)
    uvp.set_editor_property("parameter_name", "UVScale")
    uvp.set_editor_property("default_value", unreal.LinearColor(1, 1, 0, 0))
    msk = mel.create_material_expression(mat, unreal.MaterialExpressionComponentMask, -820, 50)
    msk.set_editor_property("r", True); msk.set_editor_property("g", True)
    msk.set_editor_property("b", False); msk.set_editor_property("a", False)
    mel.connect_material_expressions(uvp, "", msk, "")
    uvm = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -650, -50)
    mel.connect_material_expressions(tc, "", uvm, "A")
    mel.connect_material_expressions(msk, "", uvm, "B")
    ts = mel.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, -480, -120)
    ts.set_editor_property("parameter_name", "Albedo")
    mel.connect_material_expressions(uvm, "", ts, "UVs")
    vp = mel.create_material_expression(mat, unreal.MaterialExpressionVectorParameter, -480, 150)
    vp.set_editor_property("parameter_name", "Tint")
    vp.set_editor_property("default_value", unreal.LinearColor(1, 1, 1, 1))
    mul = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -240, 0)
    mel.connect_material_expressions(ts, "RGB", mul, "A")
    mel.connect_material_expressions(vp, "", mul, "B")
    mel.connect_material_property(mul, "", unreal.MaterialProperty.MP_BASE_COLOR)
    rp = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -240, 220)
    rp.set_editor_property("parameter_name", "Roughness")
    rp.set_editor_property("default_value", 0.9)
    mel.connect_material_property(rp, "", unreal.MaterialProperty.MP_ROUGHNESS)
    mel.recompile_material(mat)
    unreal.EditorAssetLibrary.save_asset(path)
    return mat


def _ensure_window_master():
    """Window-view panel material: ViewTex x ViewTint x Nits -> Emissive, UNLIT.
    ViewTint = AI window color temperature (observed at dusk "outside and inside tones mismatch" -- generated-image
    temperature is uncontrollable; tint the view with the same color-temperature truth as the interior light so
    the two never split)."""
    path = _apath("M_WindowView")
    mel = unreal.MaterialEditingLibrary
    mat = unreal.load_asset(path)
    fresh = mat is None
    if fresh:
        mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            "M_WindowView", _apath(""), unreal.Material, unreal.MaterialFactoryNew())
    else:
        if any(getattr(e, "get_editor_property", None) and isinstance(e, unreal.MaterialExpressionVectorParameter)
               and str(e.get_editor_property("parameter_name")) == "ViewTint"
               for e in (mel.get_material_expressions(mat) or [])):
            return mat                                  # already upgraded
        mel.delete_all_material_expressions(mat)        # rebuild in place (delete+create at the same path in one call = empty shell, old trap)
    mat.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)
    ts = mel.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, -700, -100)
    ts.set_editor_property("parameter_name", "ViewTex")
    tp = mel.create_material_expression(mat, unreal.MaterialExpressionVectorParameter, -700, 150)
    tp.set_editor_property("parameter_name", "ViewTint")
    tp.set_editor_property("default_value", unreal.LinearColor(1, 1, 1, 1))
    sp = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -700, 320)
    sp.set_editor_property("parameter_name", "Nits")
    sp.set_editor_property("default_value", 10.0)
    m1 = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -420, 0)
    mel.connect_material_expressions(ts, "RGB", m1, "A")
    mel.connect_material_expressions(tp, "", m1, "B")
    m2 = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -220, 60)
    mel.connect_material_expressions(m1, "", m2, "A")
    mel.connect_material_expressions(sp, "", m2, "B")
    mel.connect_material_property(m2, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    mel.recompile_material(mat)
    unreal.EditorAssetLibrary.save_asset(path)
    return mat


def _window_view_tint(temp_k):
    """Window view tint = white blended 65% toward the window-temperature RGB (a full tint would eat the image's own
    colors)."""
    kr = _kelvin_rgb(temp_k)
    return [1.0 + (kr[i] - 1.0) * 0.65 for i in range(3)]


def _room_bounds(data):
    """Room box bounds (cm). Prefer the cache made at shell build -- wall-snapped objects would push the elastic
    expansion formula (min(xs)-80) further out; recomputing = wall drift and useless snapping. One run references
    one and the same box."""
    rb = data.get("_room_box")
    if rb:
        return tuple(rb)
    room = data.get("room") or {}
    w_m, d_m, h_m = room.get("size_m", [5.0, 6.0, 2.8])
    cam = room.get("cam", [0.5, 0.15])
    objs = data.get("objects") or []

    def _hw(o):
        return max(40.0, min(220.0, float((o.get("scale") or [160])[0]) / 2.0))
    pts = [(o["location"][0], o["location"][1], _hw(o)) for o in objs if o.get("location")]
    x0 = min(-max(80.0, cam[1] * d_m * 100.0), min((x - hw for x, _, hw in pts), default=220.0) - 40.0)
    x1 = max(x0 + d_m * 100.0, max((x + hw for x, _, hw in pts), default=380.0) + 40.0)
    y0 = min(-w_m * 50.0, min((y - hw for _, y, hw in pts), default=-80.0) - 40.0)
    y1 = max(w_m * 50.0, max((y + hw for _, y, hw in pts), default=80.0) + 40.0)
    return x0, x1, y0, y1


def _resolve_overlaps(data, moved_ids):
    """De-penetration after programmatic moves (critique adjustments / wall snapping): moved objects are AABB-checked
    against the rest, pushed apart along the minimum-penetration axis (+6cm gap), clamped back into the room
    indoors, iterated until clean. Observed: critique moved a chair into the shelf."""
    if not moved_ids:
        return
    try:
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        actors = {}
        for a in sub.get_all_level_actors():
            lbl = a.get_actor_label()
            if isinstance(a, unreal.StaticMeshActor) and lbl.startswith("OBJ_"):
                try:
                    actors[int(lbl[4:])] = a
                except Exception:
                    pass
        rb = None
        if data.get("room"):
            rb = _room_bounds(data)
        by_id = {int(o.get("id", -1)): o for o in (data.get("objects") or [])}
        lay_by_id = {int(o.get("id", -1)): o for o in (data.get("layout") or [])}
        for oid in moved_ids:
            a = actors.get(int(oid))
            if a is None:
                continue
            for _it in range(5):
                org, ext = a.get_actor_bounds(False)
                hit = None
                for oid2, a2 in actors.items():
                    if oid2 == int(oid):
                        continue
                    o2, e2 = a2.get_actor_bounds(False)
                    px = (ext.x + e2.x) - abs(org.x - o2.x)
                    py = (ext.y + e2.y) - abs(org.y - o2.y)
                    pz = (ext.z + e2.z) - abs(org.z - o2.z)
                    if not (px > 20.0 and py > 20.0 and pz > 4.0):
                        continue                           # a light touch is not interpenetration
                    # nesting exemption (observed: a record on the shelf pushed the whole shelf away): a small object whose footprint mostly
                    # lies inside a big object's footprint = legit placement (lamp on table, items on shelves), do not separate
                    small_is_a = ext.x * ext.y <= e2.x * e2.y
                    so, se = (org, ext) if small_is_a else (o2, e2)
                    bo, be = (o2, e2) if small_is_a else (org, ext)
                    inx = min(so.x + se.x, bo.x + be.x) - max(so.x - se.x, bo.x - be.x)
                    iny = min(so.y + se.y, bo.y + be.y) - max(so.y - se.y, bo.y - be.y)
                    cover = max(0.0, inx) * max(0.0, iny) / max(1.0, 4.0 * se.x * se.y)
                    if cover >= 0.7:
                        continue
                    hit = (px, py, o2)
                    break
                if hit is None:
                    break
                px, py, o2 = hit
                if px <= py:
                    dx = (px + 6.0) * (1.0 if org.x >= o2.x else -1.0)
                    dy = 0.0
                else:
                    dx = 0.0
                    dy = (py + 6.0) * (1.0 if org.y >= o2.y else -1.0)
                L0 = a.get_actor_location()
                nx, ny = L0.x + dx, L0.y + dy
                if rb:
                    nx = min(rb[1] - ext.x, max(rb[0] + ext.x, nx))
                    ny = min(rb[3] - ext.y, max(rb[2] + ext.y, ny))
                if abs(nx - L0.x) < 0.5 and abs(ny - L0.y) < 0.5:
                    break                                  # pinned by the room bounds, cannot move -- stop spinning
                a.set_actor_location(unreal.Vector(nx, ny, L0.z), False, False)
                for d_ in (by_id.get(int(oid)), lay_by_id.get(int(oid))):
                    if d_ and d_.get("location"):
                        d_["location"][0] += nx - L0.x
                        d_["location"][1] += ny - L0.y
                unreal.log("overlap: OBJ_%02d pushed (%.0f,%.0f)cm" % (int(oid), nx - L0.x, ny - L0.y))
    except Exception as e:
        unreal.log_warning("overlap resolve failed: %s" % e)


def _clamp_objects_into_room(data):
    """Clamp object AABBs back into the room (observed: a shelf edge poking through the right wall) -- elastic room
    expansion used object centers +/-80, not true half-widths; furniture wider than 80cm clipped the walls. After
    the shell is built, push back by the true bounding boxes."""
    if not data.get("room"):
        return
    try:
        x0, x1, y0, y1 = _room_bounds(data)
        gap = 4.0
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        by_id = {int(o.get("id", -1)): o for o in (data.get("objects") or [])}
        lay_by_id = {int(o.get("id", -1)): o for o in (data.get("layout") or [])}
        clamped = []
        for a in sub.get_all_level_actors():
            lbl = a.get_actor_label()
            if not (isinstance(a, unreal.StaticMeshActor) and lbl.startswith("OBJ_")):
                continue
            org, ext = a.get_actor_bounds(False)
            dx = dy = 0.0
            if org.x - ext.x < x0 + gap:
                dx = (x0 + gap) - (org.x - ext.x)
            elif org.x + ext.x > x1 - gap:
                dx = (x1 - gap) - (org.x + ext.x)
            if org.y - ext.y < y0 + gap:
                dy = (y0 + gap) - (org.y - ext.y)
            elif org.y + ext.y > y1 - gap:
                dy = (y1 - gap) - (org.y + ext.y)
            if abs(dx) < 1.0 and abs(dy) < 1.0:
                continue
            L0 = a.get_actor_location()
            a.set_actor_location(unreal.Vector(L0.x + dx, L0.y + dy, L0.z), False, False)
            try:
                oid = int(lbl[4:])
            except Exception:
                oid = -1
            for d_ in (by_id.get(oid), lay_by_id.get(oid)):
                if d_ and d_.get("location"):
                    d_["location"][0] += dx
                    d_["location"][1] += dy
            clamped.append(oid)
            unreal.log("roomclamp: %s pushed in (%.0f,%.0f)cm" % (lbl, dx, dy))
        if clamped:
            _resolve_overlaps(data, clamped)    # clamping can squeeze furniture together (observed "crowded + clipping") -> de-penetrate immediately
    except Exception as e:
        unreal.log_warning("room clamp failed: %s" % e)


def _audit_dark_fills(data):
    """Dark-zone audit + gentle fill (goal: look from many angles for spots with no light at all, fill lightly):
    a 0.8m grid over the room floor, per point summing every light's illuminance (lm/4*pi*d^2); points below 8% of
    room lux are clustered -> a shadowless big-soft-source small fill at each cluster center, lumens closing the
    illuminance gap to ~25%. Geometric photometry = code's job; the budget (indoor_lux) = the AI's."""
    if not data.get("room"):
        return
    try:
        indoor_lux = float((data.get("environment") or {}).get("indoor_lux_override")
                           or data["room"].get("indoor_lux", 80.0))
        x0, x1, y0, y1 = _room_bounds(data)
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        for a in list(sub.get_all_level_actors()):
            if a.get_actor_label().startswith("AutoLightDark_"):
                sub.destroy_actor(a)
        srcs = []                                       # (x, y, z, lumens, attn_cm)
        for a in sub.get_all_level_actors():
            lbl = a.get_actor_label()
            if lbl.startswith(("AutoLight_", "AutoMomentLamp_", "AutoMomentFill")):
                c = a.get_component_by_class(unreal.PointLightComponent)
                if c:
                    L = a.get_actor_location()
                    srcs.append((L.x, L.y, L.z, float(c.get_editor_property("intensity")),
                                 float(c.get_editor_property("attenuation_radius"))))
            elif lbl.startswith("AutoWinLight_"):
                c = a.get_component_by_class(unreal.RectLightComponent)
                if c:
                    L = a.get_actor_location()
                    srcs.append((L.x, L.y, L.z, float(c.get_editor_property("intensity")) * 0.5,
                                 float(c.get_editor_property("attenuation_radius"))))
        dark = []
        gy = y0 + 60.0
        while gy < y1 - 40.0:
            gx = x0 + 60.0
            while gx < x1 - 40.0:
                lux = 0.0
                for sx, sy, sz, lm, ar in srcs:
                    d2 = (gx - sx) ** 2 + (gy - sy) ** 2 + (120.0 - sz) ** 2
                    if d2 > ar * ar:
                        continue                        # beyond the attenuation radius = effectively zero light (mood-light local-pool cutoff)
                    lux += lm / (4.0 * math.pi * max(0.25, d2 / 10000.0))
                if lux < 0.08 * indoor_lux:
                    dark.append((gx, gy, lux))
                gx += 80.0
            gy += 80.0
        if not dark:
            unreal.log("dark audit: no dead zones (grid fully lit)")
            return
        # clustering (simplified: max 2 clusters = split along the first principal axis, take cluster centers)
        dark.sort(key=lambda p: p[0] + p[1])
        clusters = [dark] if len(dark) <= 4 else [dark[:len(dark) // 2], dark[len(dark) // 2:]]
        temp = 3200.0
        for i, cl in enumerate(clusters):
            cx_ = sum(p[0] for p in cl) / len(cl)
            cy_ = sum(p[1] for p in cl) / len(cl)
            avg = sum(p[2] for p in cl) / len(cl)
            # illuminance closure: top clusters up to ~25% of indoor_lux; lm ~= delta_lux x 4*pi*d^2 (d~1.6m), clamped 60-450
            need = max(0.0, 0.25 * indoor_lux - avg)
            lm = max(60.0, min(450.0, need * 4.0 * math.pi * 2.56))
            fa = sub.spawn_actor_from_class(unreal.PointLight, unreal.Vector(cx_, cy_, 210.0))
            fa.set_actor_label("AutoLightDark_%d" % i)
            c = fa.get_component_by_class(unreal.PointLightComponent)
            if c:
                try:
                    c.set_editor_property("intensity_units", unreal.LightUnits.LUMENS)
                except Exception:
                    pass
                c.set_editor_property("intensity", lm)
                c.set_editor_property("cast_shadows", False)
                c.set_editor_property("use_temperature", True)
                c.set_editor_property("temperature", temp)
                c.set_editor_property("source_radius", 70.0)
                c.set_editor_property("soft_source_radius", 110.0)
                c.set_editor_property("attenuation_radius", 420.0)
            unreal.log("dark fill %d: (%.0f,%.0f) %d dead pts, %.0f lm" % (i, cx_, cy_, len(cl), lm))
    except Exception as e:
        unreal.log_warning("dark audit failed: %s" % e)


def _arrange_furniture_walls(data, movable_ids):
    """Deterministic wall packing (live rounds vetoed AI coordinate output as "randomly placed" -- AI is poor at
    geometry in abstract coordinates; iron division of labor: AI judges semantics/who stays, code computes
    positions). Pack back-to-wall along free wall runs, avoiding window walls/the bed/already-placed items;
    big pieces first."""
    if not (movable_ids and data.get("room")):
        return
    try:
        x0, x1, y0, y1 = _room_bounds(data)
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        actors = {a.get_actor_label(): a for a in sub.get_all_level_actors()
                  if a.get_actor_label().startswith("OBJ_") and isinstance(a, unreal.StaticMeshActor)}
        win_wall = next((str(o.get("wall", "front")) for o in
                         (data["room"].get("openings") or []) if o.get("kind") == "window"), None)
        movable = []
        obstacles = []
        for lbl, a in actors.items():
            oid = int(lbl[4:])
            org, ext = a.get_actor_bounds(False)
            if oid in movable_ids:
                movable.append((oid, a, ext))
            else:
                obstacles.append((org.x - ext.x, org.x + ext.x, org.y - ext.y, org.y + ext.y))
        movable.sort(key=lambda t: -(t[2].x * t[2].y))      # big pieces claim good walls first
        # candidate walls (excluding window walls): (axis, wall line, run range)
        walls = []
        if win_wall != "back":
            walls.append(("x0", x0, (y0 + 60, y1 - 60)))
        if win_wall != "left":
            walls.append(("y0", y0, (x0 + 60, x1 - 60)))
        if win_wall != "right":
            walls.append(("y1", y1, (x0 + 60, x1 - 60)))
        if win_wall != "front":
            walls.append(("x1", x1, (y0 + 60, y1 - 60)))
        by_id = {int(o.get("id", -1)): o for o in (data.get("objects") or [])}
        lay_by_id = {int(o.get("id", -1)): o for o in (data.get("layout") or [])}
        for oid, a, ext in movable:
            placed = False
            for wname, wline, (r0, r1) in walls:
                step = 40.0
                t = r0 + ext.y if wname.startswith("x") else r0 + ext.x
                while not placed:
                    if wname == "x0":
                        cx_, cy_, yaw = x0 + ext.x + 6, t, 0.0
                    elif wname == "x1":
                        cx_, cy_, yaw = x1 - ext.x - 6, t, 180.0
                    elif wname == "y0":
                        cx_, cy_, yaw = t, y0 + ext.y + 6, 90.0
                    else:
                        cx_, cy_, yaw = t, y1 - ext.y - 6, -90.0
                    lim = r1 - (ext.y if wname.startswith("x") else ext.x)
                    if t > lim:
                        break
                    box = (cx_ - ext.x - 12, cx_ + ext.x + 12, cy_ - ext.y - 12, cy_ + ext.y + 12)
                    if all(not (box[0] < bhi and box[1] > blo and box[2] < byh and box[3] > bylo)
                           for blo, bhi, bylo, byh in obstacles):
                        L0 = a.get_actor_location()
                        a.set_actor_rotation(unreal.Rotator(pitch=0, yaw=yaw, roll=0), False)
                        org1, ext1 = a.get_actor_bounds(False)
                        a.set_actor_location(unreal.Vector(
                            cx_, cy_, L0.z + (1.0 - (org1.z - ext1.z))), False, False)
                        obstacles.append((cx_ - ext1.x, cx_ + ext1.x, cy_ - ext1.y, cy_ + ext1.y))
                        for d_ in (by_id.get(oid), lay_by_id.get(oid)):
                            if d_ and d_.get("location"):
                                d_["location"][0], d_["location"][1] = cx_, cy_
                        unreal.log("arrange: OBJ_%02d -> %s @(%.0f,%.0f)" % (oid, wname, cx_, cy_))
                        placed = True
                    t += step
                if placed:
                    break
            if not placed:
                unreal.log_warning("arrange: OBJ_%02d no wall slot (left in place)" % oid)
    except Exception as e:
        unreal.log_warning("arrange failed: %s" % e)


def _reseat_floaters(data):
    """Re-seat floating small items (observed "floating vase"): the projected z comes from the photo's tabletop height,
    but the supporting furniture got re-arranged away, or the rebuilt model's table height differs from the photo
    -> small items hover at the old height. Mechanism: a floating small item (base >8cm above the first surface
    below) finds its tabletop by HEIGHT CLASS -- furniture whose photo support height ~= the projected base
    (+/-40cm), nearest wins (only if the top can fit it); no matching tabletop -> drop to the surface below.
    Writes back objects/layout (light anchors / particle anchors reference them)."""
    if not data.get("room"):
        return
    try:
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        actors = {}
        for a in sub.get_all_level_actors():
            lbl = a.get_actor_label()
            if lbl.startswith("OBJ_") and isinstance(a, unreal.StaticMeshActor):
                try:
                    actors[int(lbl[4:])] = a
                except Exception:
                    pass
        by_id = {int(o.get("id", -1)): o for o in (data.get("objects") or [])}
        lay_by_id = {int(o.get("id", -1)): o for o in (data.get("layout") or [])}
        boxes = {oid: a.get_actor_bounds(False) for oid, a in actors.items()}
        for oid, a in actors.items():
            org, ext = boxes[oid]
            base = org.z - ext.z
            r = max(ext.x, ext.y)
            if ext.z * 2 > 90.0 or r * 2 > 90.0 or base < 8.0:
                continue                                    # only handle small items off the floor
            if min(ext.x, ext.y) < 25.0 and base > 40.0:
                continue                                    # wall-item signature (thin + high off the floor): frames/mirrors are meant to hang; _arrange_wall_items owns them
                                                            # (observed: reseat grabbed a picture frame and set it on a planter/chair top)
            hr = unreal.SystemLibrary.line_trace_single(
                world, unreal.Vector(org.x, org.y, base - 1.0),
                unreal.Vector(org.x, org.y, base - 400.0),
                unreal.TraceTypeQuery.TRACE_TYPE_QUERY1, False, [a], unreal.DrawDebugTrace.NONE, True)
            hit_z = None
            if hr:
                ip = hr.to_dict().get("impact_point")
                if ip:
                    hit_z = float(ip.z)
            gap = base - hit_z if hit_z is not None else base
            if gap <= 8.0:
                continue                                    # already seated
            cand = []
            for sid, sa in actors.items():
                if sid == oid:
                    continue
                so, se = boxes[sid]
                top = so.z + se.z
                if abs(top - base) > 40.0 or min(se.x, se.y) < r + 8.0:
                    continue                                # wrong height class / surface too small
                cand.append((math.hypot(so.x - org.x, so.y - org.y), sid, so, se, top))
            L0 = a.get_actor_location()
            if cand:
                cand.sort()
                _d, sid, so, se, top = cand[0]
                nx = min(so.x + se.x - r - 6.0, max(so.x - se.x + r + 6.0, org.x))
                ny = min(so.y + se.y - r - 6.0, max(so.y - se.y + r + 6.0, org.y))
                a.set_actor_location(unreal.Vector(
                    L0.x + (nx - org.x), L0.y + (ny - org.y), L0.z + (top + 0.5 - base)), False, False)
                unreal.log("reseat: OBJ_%02d -> top of OBJ_%02d z=%.0f (was floating %.0fcm)"
                           % (oid, sid, top, gap))
            elif hit_z is not None:
                a.set_actor_location(unreal.Vector(L0.x, L0.y, L0.z - gap + 0.5), False, False)
                unreal.log("reseat: OBJ_%02d dropped %.0fcm to z=%.0f" % (oid, gap, hit_z))
            else:
                continue
            org2, ext2 = a.get_actor_bounds(False)
            for d_ in (by_id.get(oid), lay_by_id.get(oid)):
                if d_ and d_.get("location"):
                    d_["location"][0], d_["location"][1] = org2.x, org2.y
                    if len(d_["location"]) > 2:
                        d_["location"][2] = org2.z - ext2.z
    except Exception as e:
        unreal.log_warning("reseat failed: %s" % e)


def _snap_objects_to_walls(data):
    """Wall-snap semantics (AI wall_snap; box-feel breaker): depth projection leaves beds/wardrobes/shelves hovering
    mid-room -> snap each to its AI-judged wall by world AABB (4cm gap). Must write positions back to data
    objects/layout -- fixture anchors / particle anchors reference them (else lights stay hung at the old spots)."""
    snaps = data.get("wall_snap") or []
    if not (snaps and data.get("room")):
        return
    try:
        x0, x1, y0, y1 = _room_bounds(data)
        gap = 4.0
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        actors = {a.get_actor_label(): a for a in sub.get_all_level_actors()
                  if isinstance(a, unreal.StaticMeshActor)}
        by_id = {int(o.get("id", -1)): o for o in (data.get("objects") or [])}
        lay_by_id = {int(o.get("id", -1)): o for o in (data.get("layout") or [])}
        n = 0
        moved = []
        for m in snaps:
            oid = int(m.get("id", -1))
            a = actors.get("OBJ_%02d" % oid)
            if a is None:
                continue
            org, ext = a.get_actor_bounds(False)
            wall = str(m.get("wall"))
            dx = dy = 0.0
            if wall == "front":
                dx = (x1 - gap) - (org.x + ext.x)
            elif wall == "back":
                dx = (x0 + gap) - (org.x - ext.x)
            elif wall == "left":
                dy = (y0 + gap) - (org.y - ext.y)
            elif wall == "right":
                dy = (y1 - gap) - (org.y + ext.y)
            if abs(dx) < 2.0 and abs(dy) < 2.0:
                continue                                   # already against the wall, leave it
            L0 = a.get_actor_location()
            a.set_actor_location(unreal.Vector(L0.x + dx, L0.y + dy, L0.z), False, False)
            for d_ in (by_id.get(oid), lay_by_id.get(oid)):
                if d_ and d_.get("location"):
                    d_["location"][0] += dx
                    d_["location"][1] += dy
            unreal.log("wallsnap: OBJ_%02d -> %s (%.0f,%.0f)cm" % (oid, wall, dx, dy))
            moved.append(oid)
            n += 1
        if n:
            unreal.log("wallsnap: %d object(s) against walls" % n)
            _resolve_overlaps(data, moved)                 # wall snapping may have pushed objects into other furniture
    except Exception as e:
        unreal.log_warning("wall snap failed: %s" % e)


def _arrange_wall_items(data):
    """Spread + lift wall items (frames/mirrors) -- must run after ALL indoor re-arrangement (observed: the rule lived
    in _snap_objects_to_walls and ran early -- _arrange_furniture_walls then packed both frames into the same wall
    slot, overwriting the spread). Wall items self-identify geometrically (base >40cm off the floor, thin axis
    <25cm, <35cm from the nearest wall); the wall_snap list is not trusted -- whichever pass hung them, they are
    managed.
      (1) several on one wall -> spread evenly along it (group center as anchor, 18cm gaps, clamped into the wall
          run with 30cm margins)
      (2) base sunk into furniture below -> lift to the furniture top +15cm (XY fixed, stays on the wall), capped
          at ceiling -15cm
    Writes positions back to objects/layout (fixture anchors / particle anchors reference them)."""
    if not data.get("room"):
        return
    try:
        x0, x1, y0, y1 = _room_bounds(data)
        ceil_z = 40.0 + float((data.get("room") or {}).get("size_m", [0, 0, 2.8])[2]) * 100.0
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        actors = {a.get_actor_label(): a for a in sub.get_all_level_actors()
                  if isinstance(a, unreal.StaticMeshActor) and a.get_actor_label().startswith("OBJ_")}
        by_id = {int(o.get("id", -1)): o for o in (data.get("objects") or [])}
        lay_by_id = {int(o.get("id", -1)): o for o in (data.get("layout") or [])}
        witems = {}
        for lbl, a in actors.items():
            org, ext = a.get_actor_bounds(False)
            if org.z - ext.z <= 40.0:
                continue
            # which wall it hugs: the thin axis (<25cm) outer face is <35cm from that wall
            wall = None
            if ext.x < 25.0:
                if abs((org.x + ext.x) - x1) < 35.0:
                    wall = "front"
                elif abs((org.x - ext.x) - x0) < 35.0:
                    wall = "back"
            if wall is None and ext.y < 25.0:
                if abs((org.y - ext.y) - y0) < 35.0:
                    wall = "left"
                elif abs((org.y + ext.y) - y1) < 35.0:
                    wall = "right"
            if wall:
                witems.setdefault(wall, []).append(a)

        def _write_back(a):
            lbl = a.get_actor_label()
            try:
                oid = int(lbl[4:])
            except Exception:
                return
            org2, ext2 = a.get_actor_bounds(False)
            for d_ in (by_id.get(oid), lay_by_id.get(oid)):
                if d_ and d_.get("location"):
                    d_["location"][0], d_["location"][1] = org2.x, org2.y
                    if len(d_["location"]) > 2:
                        d_["location"][2] = org2.z - ext2.z

        for wall, items in witems.items():
            ax_y = wall in ("front", "back")               # slide axis along the wall: front/back walls = Y, side walls = X
            lo, hi = (y0 + 30.0, y1 - 30.0) if ax_y else (x0 + 30.0, x1 - 30.0)
            if len(items) >= 2:                            # (1) spread along the wall
                bs = [(a, a.get_actor_bounds(False)) for a in items]
                bs.sort(key=lambda t: t[1][0].y if ax_y else t[1][0].x)
                ws = [(t[1][1].y if ax_y else t[1][1].x) * 2.0 for t in bs]
                cen = sum((t[1][0].y if ax_y else t[1][0].x) for t in bs) / len(bs)
                need = sum(ws) + 18.0 * (len(bs) - 1)
                pos = min(hi - need, max(lo, cen - need / 2.0))
                for (a, (o_, e_)), w in zip(bs, ws):
                    tgt = pos + w / 2.0
                    pos += w + 18.0
                    L0 = a.get_actor_location()
                    if ax_y:
                        a.set_actor_location(unreal.Vector(L0.x, L0.y + (tgt - o_.y), L0.z), False, False)
                    else:
                        a.set_actor_location(unreal.Vector(L0.x + (tgt - o_.x), L0.y, L0.z), False, False)
                    unreal.log("wallitem: %s spread along %s wall" % (a.get_actor_label(), wall))
            for a in items:                                # (2) lift above furniture tops
                org, ext = a.get_actor_bounds(False)
                lift_to = None
                for l2, a2 in actors.items():
                    if a2 is a:
                        continue
                    o2, e2 = a2.get_actor_bounds(False)
                    if o2.z - e2.z > 40.0 and min(e2.x, e2.y) < 25.0:
                        continue                           # other wall items: already spread in (1)
                    if abs(org.x - o2.x) < ext.x + e2.x and abs(org.y - o2.y) < ext.y + e2.y:
                        top = o2.z + e2.z
                        if org.z - ext.z < top + 8.0:
                            lift_to = max(lift_to or 0.0, top + 15.0)
                if lift_to is not None:
                    dz = min(lift_to - (org.z - ext.z), (ceil_z - 15.0) - (org.z + ext.z))
                    if dz > 0:
                        L0 = a.get_actor_location()
                        a.set_actor_location(unreal.Vector(L0.x, L0.y, L0.z + dz), False, False)
                        unreal.log("wallitem: %s lifted +%.0fcm above furniture" % (a.get_actor_label(), dz))
        for items in witems.values():
            for a in items:
                _write_back(a)
    except Exception as e:
        unreal.log_warning("wall items failed: %s" % e)


def _clamp_spawn_into_room(data):
    """Clamp the spawn into the room -- must be called after _apply_vr (the spawn is only set in the VR stage; clamping
    earlier is a no-op, observed twice)."""
    if not data.get("room"):
        return
    try:
        x0, x1, y0, y1 = _room_bounds(data)
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        for a in sub.get_all_level_actors():
            if isinstance(a, unreal.PlayerStart):
                L0 = a.get_actor_location()
                nx = min(x1 - 80.0, max(x0 + 80.0, L0.x))
                ny = min(y1 - 80.0, max(y0 + 80.0, L0.y))
                if abs(nx - L0.x) > 1 or abs(ny - L0.y) > 1:
                    a.set_actor_location(unreal.Vector(nx, ny, L0.z), False, False)
                    unreal.log("room: PlayerStart clamped into room (%.0f,%.0f) [post-VR]" % (nx, ny))
    except Exception as e:
        unreal.log_warning("spawn clamp failed: %s" % e)


def _apply_room_shell(data):
    """Indoor room shell P0 (docs/INDOOR_SHELL.md): AI room box (wall/floor/ceiling inner faces) + photo-matched wall
    and floor materials. The box auto-expands to object occupancy (projection distances can exceed the AI-estimated
    room depth). P1: openings cut out + outside-window HDRI panels."""
    room = data.get("room")
    if not room:
        return
    try:
        w_m, d_m, h_m = room.get("size_m", [5.0, 6.0, 2.8])
        cam = room.get("cam", [0.5, 0.15])
        objs = data.get("objects") or []
        # Room = elastic container: AI dimensions are a floor; auto-expand four ways + height by object occupancy (a sofa once leaked through the wall /
        # even the "what about 30 models" stress case holds -- the room grows).
        # Expansion uses each object's REAL half-width (scale[0]/2 as longest-axis proxy, clamped 40-220cm) -- a fixed +/-80 underestimates
        # bed/wardrobe half-widths -> clamping squeezed them into each other (observed "crowded + clipping")
        def _hw(o):
            return max(40.0, min(220.0, float((o.get("scale") or [160])[0]) / 2.0))
        pts = [(o["location"][0], o["location"][1], _hw(o)) for o in objs if o.get("location")]
        xs_lo = [x - hw for x, _, hw in pts] or [220.0]
        xs_hi = [x + hw for x, _, hw in pts] or [380.0]
        ys_lo = [y - hw for _, y, hw in pts] or [-80.0]
        ys_hi = [y + hw for _, y, hw in pts] or [80.0]
        tops = [o["location"][2] + float((o.get("scale") or [250])[0]) for o in objs if o.get("location")] or [250.0]
        x0 = min(-max(80.0, cam[1] * d_m * 100.0), min(xs_lo) - 40.0)
        x1 = max(x0 + d_m * 100.0, max(xs_hi) + 40.0)
        y0 = min(-w_m * 50.0, min(ys_lo) - 40.0)
        y1 = max(w_m * 50.0, max(ys_hi) + 40.0)
        z0, z1 = 1.0, max(h_m * 100.0, max(tops) + 30.0)
        data["_room_box"] = [x0, x1, y0, y1]               # freeze the room bounds (wall snap/attenuation/spawn clamp all reference this same box)
        w_m, d_m, h_m = (y1 - y0) / 100.0, (x1 - x0) / 100.0, (z1 - z0) / 100.0
        # Room shell v3: 6 scaled engine Cubes (diagnosis chain: self-built meshes -- thin, thick, or with DF -- receive no Lumen GI bounce;
        # they lack surface-cache cards. Engine Cubes ship with DF + cards + collision; two A/B rounds, fully lit)
        eal = unreal.EditorAssetLibrary
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        for a in sub.get_all_level_actors():                   # clear old: self-built-mesh era pieces + last round's segments/window parts
            lbl = a.get_actor_label()
            if (lbl in ("AutoRoom", "ABTestWall") or lbl.startswith("AutoRoomCol_")
                    or lbl.startswith("AutoRoomSlab_") or lbl.startswith("AutoWin")):
                try:
                    sub.destroy_actor(a)
                except Exception:
                    pass
        if eal.does_asset_exist(_apath("SM_AutoRoom")):
            try:
                unreal.SystemLibrary.collect_garbage()
                eal.delete_asset(_apath("SM_AutoRoom"))
            except Exception:
                pass
        tex_urls = room.get("tex") or {}

        def _room_tex(name, url):
            if not url:
                return None
            try:
                local = os.path.join(tempfile.gettempdir(), name + ".png")
                with open(local, "wb") as f:
                    f.write(_get(SERVER + url))
                task = unreal.AssetImportTask()
                task.filename = local
                task.destination_path = _aipath("Room")
                task.destination_name = "T_" + name
                task.automated = True
                task.replace_existing = True
                task.save = True
                unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
                return unreal.load_asset(_aipath("Room/T_") + name)
            except Exception as e:
                unreal.log_warning("room tex %s: %s" % (name, e))
                return None

        def _room_mic(name, tex, tint, uvx, uvy, rough=0.9):
            master = _ensure_room_master()
            mic, mic_path = _ensure_mic("MI_" + name, master)
            mel = unreal.MaterialEditingLibrary
            if not tex:                          # no texture -> force white: color comes from Tint (observed inheriting the master's default texture = "moon surface")
                tex = unreal.load_asset("/Engine/EngineResources/WhiteSquareTexture")
            if tex:
                mel.set_material_instance_texture_parameter_value(mic, "Albedo", tex)
            mel.set_material_instance_vector_parameter_value(
                mic, "Tint", unreal.LinearColor(tint[0], tint[1], tint[2], 1.0))
            mel.set_material_instance_vector_parameter_value(
                mic, "UVScale", unreal.LinearColor(uvx, uvy, 0.0, 0.0))
            mel.set_material_instance_scalar_parameter_value(mic, "Roughness", rough)
            unreal.EditorAssetLibrary.save_asset(mic_path)
            return mic

        TU = 250.0                                             # one tile per 2.5m
        T = 20.0                                               # panel thickness
        W, D, H = y1 - y0, x1 - x0, z1 - z0
        wall_tex = _room_tex("RoomWall", tex_urls.get("wall"))
        floor_tex = _room_tex("RoomFloor", tex_urls.get("floor"))
        wall_c = room.get("wall_color", [0.85, 0.82, 0.78])
        # panel centering: the inner face lands exactly on the x0/x1/y0/y1/z0/z1 planes
        cube = unreal.load_asset("/Engine/BasicShapes/Cube")   # 100cm basis

        def _slab(nm, pos_, dims, mic):
            b = sub.spawn_actor_from_object(cube, unreal.Vector(*pos_))
            b.set_actor_label("AutoRoomSlab_%s" % nm)
            b.set_actor_scale3d(unreal.Vector(dims[0] / 100.0, dims[1] / 100.0, dims[2] / 100.0))
            b.static_mesh_component.set_material(0, mic)
            b.static_mesh_component.set_collision_profile_name("BlockAll")   # Cube native simple collision, PIE-reliable
            b.static_mesh_component.set_collision_enabled(unreal.CollisionEnabled.QUERY_AND_PHYSICS)
            return b

        _slab("Floor", ((x0 + x1) / 2, (y0 + y1) / 2, z0 - T / 2), (D + 2 * T, W + 2 * T, T),
              _room_mic("RoomFloor", floor_tex, room.get("floor_color", [0.5, 0.4, 0.3]), D / TU, W / TU))
        _slab("Ceil", ((x0 + x1) / 2, (y0 + y1) / 2, z1 + T / 2), (D + 2 * T, W + 2 * T, T),
              _room_mic("RoomCeil", None, room.get("ceiling_color", [0.92, 0.9, 0.88]), D / TU, W / TU))
        # four walls segmented around AI openings (P1): full-height strips beside holes + sill/header strips; u_frac convention = from the left end facing that wall from inside
        wall_geo = {                                   # wall name -> (fixed axis, panel center coord, u-axis range, inward yaw)
            "back": ("x", x0 - T / 2, (y1, y0), 0.0),
            "front": ("x", x1 + T / 2, (y0, y1), 180.0),
            "left": ("y", y0 - T / 2, (x0, x1), 90.0),
            "right": ("y", y1 + T / 2, (x1, x0), -90.0),
        }
        ops_by_wall = {}
        for o in (room.get("openings") or []):
            ops_by_wall.setdefault(str(o.get("wall", "left")), []).append(o)
        trim_mic = _room_mic("RoomTrim", None, room.get("ceiling_color", [0.92, 0.9, 0.88]),
                             1.0, 1.0, rough=0.55)         # skirting = painted woodwork (matches ceiling color), slight sheen
        win_tex = _room_tex("WindowView", room.get("window_view"))
        win_i = 0
        for wname, (axis, plane, (a0, a1), yaw_in) in wall_geo.items():
            lo, hi = min(a0, a1), max(a0, a1)
            ivs = []                                   # opening intervals (absolute axis coords)
            for o in ops_by_wall.get(wname, []):
                hw = min(float(o.get("w_m", 1.2)) * 50.0, (hi - lo) / 2 - 20.0)
                c = a0 + float(o.get("u_frac", 0.5)) * (a1 - a0)
                c = max(lo + 15.0 + hw, min(hi - 15.0 - hw, c))
                zs = z0 + float(o.get("sill_m", 0.9)) * 100.0
                zh = min(z1 - 10.0, zs + float(o.get("h_m", 1.4)) * 100.0)
                zs = min(zs, zh - 30.0)
                ivs.append((c - hw, c + hw, zs, zh, o))
            ivs.sort()

            def _wseg(nm, ca, half, zlo, zhi):
                if half < 1.0 or (zhi - zlo) < 2.0:
                    return
                dims = (2 * half, T, zhi - zlo) if axis == "y" else (T, 2 * half, zhi - zlo)
                pos_ = ((ca, plane, (zlo + zhi) / 2) if axis == "y"
                        else (plane, ca, (zlo + zhi) / 2))
                # each segment gets its own MIC: Cube face UV is always 0-1; tiling density must match this segment's actual size
                _slab(nm, pos_, dims, _room_mic("RoomW_" + nm, wall_tex, wall_c,
                                                2 * half / TU, (zhi - zlo) / TU))

            cur = lo
            for si, (il, ir, zs, zh, o) in enumerate(ivs):
                _wseg("%s_%d" % (wname, si * 3), (cur + il) / 2, (il - cur) / 2, z0, z1)
                _wseg("%s_%d" % (wname, si * 3 + 1), (il + ir) / 2, (ir - il) / 2, z0, zs)   # below window
                _wseg("%s_%d" % (wname, si * 3 + 2), (il + ir) / 2, (ir - il) / 2, zh, z1)   # above window
                cur = ir
                # outside-window panel: emissive backdrop 1.2m beyond the hole (window-view image, nits = AI transmitted lux / pi; door holes = near-black corridor)
                lux_ = float(o.get("lux", 50.0))
                is_win = str(o.get("kind", "window")) == "window"
                nits = max(0.05, lux_ / math.pi) if is_win else 0.05
                bw, bh = (ir - il) * 3.0, (zh - zs) * 2.5
                off = T + 120.0
                bpos = ((il + ir) / 2, plane + (off if plane > (y0 + y1) / 2 else -off), (zs + zh) / 2) \
                    if axis == "y" else (plane + (off if plane > (x0 + x1) / 2 else -off), (il + ir) / 2, (zs + zh) / 2)
                bdims = (bw, 10.0, bh) if axis == "y" else (10.0, bw, bh)
                bb = sub.spawn_actor_from_object(cube, unreal.Vector(*bpos))
                bb.set_actor_label("AutoWinBoard_%d" % win_i)
                bb.set_actor_scale3d(unreal.Vector(bdims[0] / 100.0, bdims[1] / 100.0, bdims[2] / 100.0))
                bb.set_actor_enable_collision(False)
                try:
                    # View panels are look-only, never light: textured emissive bounced by Lumen paints striped ghosts on walls (observed);
                    # window lighting is carried entirely by the window RectLight (lux x area, physical quantity unchanged)
                    bb.static_mesh_component.set_editor_property("affect_dynamic_indirect_lighting", False)
                    bb.static_mesh_component.set_editor_property("affect_distance_field_lighting", False)
                except Exception as e:
                    unreal.log_warning("win board GI off: %s" % e)
                wmic, wpath = _ensure_mic("MI_WinView_%d" % win_i, _ensure_window_master())
                mel_ = unreal.MaterialEditingLibrary
                if win_tex and is_win:
                    mel_.set_material_instance_texture_parameter_value(wmic, "ViewTex", win_tex)
                mel_.set_material_instance_scalar_parameter_value(wmic, "Nits", nits)
                wt = _window_view_tint(float(o.get("color_temp_k", 6500.0)))
                rainy_night = ("rain" in str(_FX_ENV.get("weather", "")).lower()
                               and "night" in str(_FX_ENV.get("time_of_day", "")).lower())
                if rainy_night and is_win:
                    # Rainy-night window = glowing rain curtain (aesthetic pass, hand-tuned): panel floor 35 nits + cool blue cast --
                    # the photo grammar = large cool-blue glowing windows x warm interior accents
                    nits = max(nits, 35.0)
                    mel_.set_material_instance_scalar_parameter_value(wmic, "Nits", nits)
                    wt = [0.55, 0.72, 1.0]
                mel_.set_material_instance_vector_parameter_value(
                    wmic, "ViewTint", unreal.LinearColor(wt[0], wt[1], wt[2], 1.0))
                # window mullions (the soul of the photo's lattice windows, applied to all windows): one vertical + one horizontal dark strip
                fz0 = z0 + float(o.get("sill_m", 0.9)) * 100.0
                fh = float(o.get("h_m", 1.4)) * 100.0
                fc = (il + ir) / 2
                for tag, fpos, fdim in (
                        ("V", (fc, plane + (T / 2 + 7.0) * (1 if plane < (y0 + y1) / 2 else -1), fz0 + fh / 2)
                              if axis == "y" else
                              (plane + (T / 2 + 7.0) * (1 if plane < (x0 + x1) / 2 else -1), fc, fz0 + fh / 2),
                         (0.07, 0.06, fh / 100.0) if axis == "y" else (0.06, 0.07, fh / 100.0)),
                        ("H", (fc, plane + (T / 2 + 7.0) * (1 if plane < (y0 + y1) / 2 else -1), fz0 + fh * 0.62)
                              if axis == "y" else
                              (plane + (T / 2 + 7.0) * (1 if plane < (x0 + x1) / 2 else -1), fc, fz0 + fh * 0.62),
                         ((ir - il) / 100.0, 0.06, 0.07) if axis == "y" else (0.06, (ir - il) / 100.0, 0.07))):
                    fb = sub.spawn_actor_from_object(cube, unreal.Vector(*fpos))
                    fb.set_actor_label("AutoWinFrame_%d_%s" % (win_i, tag))
                    fb.set_actor_scale3d(unreal.Vector(*fdim))
                    fb.set_actor_enable_collision(False)
                if rainy_night and is_win:
                    # Outside glowing rain curtain (the photo's soul): a bright blue rain sheet visible through the window. Box center offset = wall thickness + half box width + 30
                    # (a 90cm offset let the 150cm-half-width box intrude 60cm indoors -> rain inside the room; observed washout)
                    rbox = min(150.0, (ir - il) / 2)
                    roff = T + rbox + 30.0
                    rz = fz0 + fh + 120.0
                    # Offset direction: push toward whichever side of the room centerline the plane sits on (the sign was once flipped ->
                    # the rain curtain 190cm inside the room, raining indoors; the final culprit of three washout rounds)
                    rx, ry = ((fc, plane + roff * (1 if plane > (y0 + y1) / 2 else -1))
                              if axis == "y" else
                              (plane + roff * (1 if plane > (x0 + x1) / 2 else -1), fc))
                    entry = {"name": "window rain curtain", "primitive": "niagara", "blend": "soft",
                             "color": [0.55, 0.72, 1.0], "opacity": 0.85, "emissive": 11.0, "tex_url": "",
                             "niagara": {"preset": "rain", "spots": [[rx, ry, rz]],
                                         "box_xy": rbox, "box_z": 50.0,
                                         "count": 240, "size_cm": 8, "size_cm_lo": 6.0, "size_cm_hi": 10.0}}
                    try:
                        _spawn_fx_niagara(77 + win_i, entry, sub)
                    except Exception as e:
                        unreal.log_warning("window rain curtain: %s" % e)
                unreal.EditorAssetLibrary.save_asset(wpath)
                bb.static_mesh_component.set_material(0, wmic)
                # window area light: flux = AI transmitted lux x hole area (physical), color temp from AI; day casts shadows, night glimmer does not
                if is_win and lux_ > 0.5:
                    la = sub.spawn_actor_from_class(
                        unreal.RectLight,
                        unreal.Vector(*(((il + ir) / 2, plane, (zs + zh) / 2) if axis == "y"
                                        else (plane, (il + ir) / 2, (zs + zh) / 2))),
                        unreal.Rotator(pitch=0.0, yaw=yaw_in, roll=0.0))
                    la.set_actor_label("AutoWinLight_%d" % win_i)
                    lc = la.get_component_by_class(unreal.RectLightComponent)
                    if lc:
                        try:
                            lc.set_editor_property("intensity_units", unreal.LightUnits.LUMENS)
                        except Exception:
                            pass
                        area_m2 = max(0.1, (ir - il) * (zh - zs) / 10000.0)
                        lc.set_editor_property("intensity", lux_ * area_m2)
                        lc.set_editor_property("source_width", ir - il)
                        lc.set_editor_property("source_height", zh - zs)
                        lc.set_editor_property("attenuation_radius",
                                               math.sqrt(D * D + W * W + H * H))
                        lc.set_editor_property("use_temperature", True)
                        lc.set_editor_property("temperature", float(o.get("color_temp_k", 6500.0)))
                        lc.set_editor_property("cast_shadows", lux_ >= 200.0)
                    unreal.log("room: window light %s %.0flux %.0fK" %
                               (wname, lux_, float(o.get("color_temp_k", 6500.0))))
                win_i += 1
            _wseg("%s_end" % wname, (cur + hi) / 2, (hi - cur) / 2, z0, z1)
            # skirting boards (breaks the box feel): 8cm strip along wall bases, skipped at door holes (sill~=0)
            sign = 1.0 if plane < ((y0 + y1) / 2 if axis == "y" else (x0 + x1) / 2) else -1.0
            bb_c = plane + sign * (T / 2 + 1.2)
            doors = sorted((il, ir) for (il, ir, zs, zh, o) in ivs if zs <= z0 + 12.0)
            segs_, cur_ = [], lo
            for dl, dr in doors:
                if dl > cur_ + 2.0:
                    segs_.append((cur_, dl))
                cur_ = dr
            if hi > cur_ + 2.0:
                segs_.append((cur_, hi))
            for bi, (s0_, s1_) in enumerate(segs_):
                dims_ = ((s1_ - s0_, 2.4, 8.0) if axis == "y" else (2.4, s1_ - s0_, 8.0))
                pos_ = (((s0_ + s1_) / 2, bb_c, z0 + 4.0) if axis == "y"
                        else (bb_c, (s0_ + s1_) / 2, z0 + 4.0))
                _slab("BB_%s_%d" % (wname, bi), pos_, dims_, trim_mic)
        # clamp the spawn into the room (spawn search knows nothing about walls; observed landing outside)
        for a in sub.get_all_level_actors():
            if isinstance(a, unreal.PlayerStart):
                L0 = a.get_actor_location()
                nx = min(x1 - 80.0, max(x0 + 80.0, L0.x))
                ny = min(y1 - 80.0, max(y0 + 80.0, L0.y))
                if abs(nx - L0.x) > 1 or abs(ny - L0.y) > 1:
                    a.set_actor_location(unreal.Vector(nx, ny, L0.z), False, False)
                    unreal.log("room: PlayerStart clamped into room (%.0f,%.0f)" % (nx, ny))
        unreal.log("room shell: %.1fx%.1fx%.1fm wall=%s floor=%s (P0, openings P1=%d pending)"
                   % (w_m, d_m, h_m, room.get("wall_material"), room.get("floor_material"),
                      len(room.get("openings") or [])))
    except Exception as e:
        unreal.log_warning("room shell failed: %s" % e)


def _import_sound(url, name):
    """Download and import an audio clip as a looping SoundWave; returns the asset (None on failure)."""
    ext = os.path.splitext(url)[1].lower() or ".wav"
    local = os.path.join(tempfile.gettempdir(), "auto_" + name.lower() + ext)
    with open(local, "wb") as f:
        f.write(_get(SERVER + url))
    task = unreal.AssetImportTask()
    task.filename = local
    task.destination_path = _aipath("Music")
    task.destination_name = name
    task.automated = True
    task.replace_existing = True
    task.save = True
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    snd = unreal.load_asset(_aipath("Music/") + name)
    if snd is not None:
        try:
            snd.set_editor_property("looping", True)
            unreal.EditorAssetLibrary.save_asset(_aipath("Music/") + name)
        except Exception:
            pass
    return snd


def _apply_moment(data, name):
    """Four-moment in-place switching (indoor v1): grading/exposure + window area lights + window view panels + night
    practicals + window sound. All assets reused, parameters only -- the AI decided everything once in make_moments
    (data['moments'])."""
    m = (data.get("moments") or {}).get(name)
    if not m:
        unreal.log_warning("moment '%s' not found (run dev/_moments_gen.py first)" % name)
        return
    try:
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        env = dict(data.get("environment") or {})
        env["indoor_lux_override"] = float(m["indoor_lux"])
        env["exposure_ev"] = float(m["exposure_ev"])
        env["saturation"] = float(m["saturation"])
        env["contrast"] = float(m["contrast"])
        env["bloom"] = float(m["bloom"])
        env["time_of_day"] = {"noon": "noon", "dusk": "dusk", "night": "night",
                              "rain": "overcast"}.get(name, env.get("time_of_day", "day"))  # base = original time of day
        _FX_ENV.clear()                          # clear first: update() does not drop old keys; stale cross-mode/cross-task keys leak (audit H2)
        _FX_ENV.update(env)
        _apply_grade(env)
        # Indoor lighting invariants (double insurance, enforced on every mode switch): sun=0 / skylight muted -- any bypass reviving them
        # brings the ghosts back (grazing VSM triangles / pattern GI); an iron rule paid for with a full review round
        for a in sub.get_all_level_actors():
            if isinstance(a, unreal.DirectionalLight):
                c0 = a.get_component_by_class(unreal.DirectionalLightComponent)
                if c0 and float(c0.get_editor_property("intensity")) > 0.0:
                    c0.set_editor_property("intensity", 0.0)
                    unreal.log_warning("moment: stray sun muted (invariant)")
            elif isinstance(a, unreal.SkyLight):
                c0 = a.get_component_by_class(unreal.SkyLightComponent)
                if c0 and float(c0.get_editor_property("intensity")) > 0.0:
                    c0.set_editor_property("intensity", 0.0)
                    unreal.log_warning("moment: stray skylight muted (invariant)")
        lux_ = float(m["window_lux"])
        for a in sub.get_all_level_actors():
            lbl = a.get_actor_label()
            if lbl.startswith("AutoWinLight_"):
                c = a.get_component_by_class(unreal.RectLightComponent)
                if c:
                    w_ = float(c.get_editor_property("source_width"))
                    h_ = float(c.get_editor_property("source_height"))
                    c.set_editor_property("intensity", lux_ * max(0.1, w_ * h_ / 10000.0))
                    c.set_editor_property("temperature", float(m["window_temp_k"]))
                    c.set_editor_property("cast_shadows", lux_ >= 200.0)
            elif lbl.startswith("AutoWinBoard_"):
                _ensure_window_master()                    # upgrade the old panel material in place to expose ViewTint
                mic = a.static_mesh_component.get_material(0)
                mel = unreal.MaterialEditingLibrary
                if m.get("window_view"):
                    tex = _room_tex_global("WinView_" + name, m["window_view"])
                    if tex:
                        mel.set_material_instance_texture_parameter_value(mic, "ViewTex", tex)
                mel.set_material_instance_scalar_parameter_value(mic, "Nits", max(0.05, lux_ / math.pi))
                wt = _window_view_tint(float(m["window_temp_k"]))   # the window view shares the same color-temperature truth as the interior light
                mel.set_material_instance_vector_parameter_value(
                    mic, "ViewTint", unreal.LinearColor(wt[0], wt[1], wt[2], 1.0))
            elif lbl.startswith("AutoMoment") or lbl.startswith("AutoSfx_"):
                sub.destroy_actor(a)
        # night/dark mode: AI lights the fixtures (real lumens + color temp, same parameter surface as light homing)
        obj_by_id = {o.get("id"): o for o in (data.get("objects") or []) if o.get("location")}
        for i, p in enumerate(m.get("practicals") or []):
            fo = obj_by_id.get(p.get("object_id"))
            if not fo:
                continue
            fh = float((fo.get("scale") or [150])[0])
            la = sub.spawn_actor_from_class(unreal.PointLight, unreal.Vector(
                fo["location"][0] + 15.0, fo["location"][1] + 15.0, fo["location"][2] + 0.7 * fh))
            la.set_actor_label("AutoMomentLamp_%d" % i)
            c = la.get_component_by_class(unreal.PointLightComponent)
            if c:
                try:
                    c.set_editor_property("intensity_units", unreal.LightUnits.LUMENS)
                except Exception:
                    pass
                c.set_editor_property("intensity", float(p.get("lumens", 500.0)))
                c.set_editor_property("use_temperature", True)
                c.set_editor_property("temperature", float(p.get("color_temp_k", 2700.0)))
                c.set_editor_property("source_radius", 12.0)
                c.set_editor_property("soft_source_radius", 12.0)
                c.set_editor_property("cast_shadows", i == 0)
        # Night-mode Lumen denoise (ruling (1): night goes to the viva): at very low light, screen probes undersample = mottled noise --
        # dark modes max out final-gather quality, bright modes reset to save perf
        try:
            for a in sub.get_all_level_actors():
                if isinstance(a, unreal.PostProcessVolume):
                    st = a.get_editor_property("settings")
                    dark = lux_ < 100.0
                    st.set_editor_property("override_lumen_final_gather_quality", True)
                    st.set_editor_property("lumen_final_gather_quality", 4.0 if dark else 1.0)
                    st.set_editor_property("override_lumen_scene_detail", True)
                    st.set_editor_property("lumen_scene_detail", 2.0 if dark else 1.0)
                    a.set_editor_property("settings", st)
                    if dark:
                        unreal.log("moment: Lumen gather quality maxed (dark moment denoise)")
        except Exception as e:
            unreal.log_warning("lumen quality: %s" % e)
        # Illuminance closure (observed night mode "so dark"): AI indoor lux is the perceptual truth; whatever the supply (window flux + fixture lumens)
        # falls short of it = ambient bounce (moon/sky diffuse), topped up by a shadowless fill -- same precedent as the outdoor night sky-fill.
        # Daytime window supply far exceeds demand -> no top-up.
        try:
            rx0, rx1, ry0, ry1 = _room_bounds(data)
            area_m2 = max(4.0, (rx1 - rx0) * (ry1 - ry0) / 10000.0)
            op_area = sum(float(o.get("w_m", 1.2)) * float(o.get("h_m", 1.4))
                          for o in (data["room"].get("openings") or []) if o.get("kind") == "window")
            supply = lux_ * max(0.5, op_area) + sum(float(p.get("lumens", 0.0))
                                                    for p in (m.get("practicals") or []))
            need = float(m["indoor_lux"]) * area_m2
            if need > supply * 1.1:
                fill = min(6000.0, need - supply)
                fa = sub.spawn_actor_from_class(unreal.PointLight, unreal.Vector(
                    (rx0 + rx1) / 2, (ry0 + ry1) / 2, 220.0))
                fa.set_actor_label("AutoMomentFill")
                fc = fa.get_component_by_class(unreal.PointLightComponent)
                if fc:
                    try:
                        fc.set_editor_property("intensity_units", unreal.LightUnits.LUMENS)
                    except Exception:
                        pass
                    fc.set_editor_property("intensity", fill)
                    fc.set_editor_property("cast_shadows", False)
                    fc.set_editor_property("use_temperature", True)
                    fc.set_editor_property("temperature", float(m["window_temp_k"]))
                    fc.set_editor_property("source_radius", 80.0)
                    fc.set_editor_property("soft_source_radius", 120.0)
                    fc.set_editor_property("attenuation_radius",
                                           math.sqrt((rx1 - rx0) ** 2 + (ry1 - ry0) ** 2 + 350.0 ** 2))
                unreal.log("moment fill: %.0flm (need %.0f, supply %.0f)" % (fill, need, supply))
        except Exception as e:
            unreal.log_warning("moment fill: %s" % e)
        # Particles per mode: dark modes (transmitted <100 lux) cleared -- "sunlit dust" must not show at night without a beam (observed white-dot ghosts);
        # bright modes re-scale radiance
        if lux_ < 100.0:
            for a in sub.get_all_level_actors():
                if a.get_actor_label().startswith("AutoFx"):
                    try:
                        sub.destroy_actor(a)
                    except Exception:
                        pass
        elif data.get("effects"):
            _apply_effects(data.get("effects"), data)
        # window sound swaps per mode (rain mode = raindrops)
        if m.get("sfx_url"):
            ss = [{"id": "window", "kind": m["window_sound"], "sound": m["window_sound"],
                   "volume": 0.5, "url": m["sfx_url"]}]
            _apply_sound_sources(dict(data, sound_sources=ss))
        unreal.log("moment '%s': win %.0flux %.0fK, indoor %.0flux, %d lamps, sound=%s" % (
            name, lux_, float(m["window_temp_k"]), float(m["indoor_lux"]),
            len(m.get("practicals") or []), m["window_sound"]))
    except Exception as e:
        unreal.log_warning("moment failed: %s" % e)


def _room_tex_global(name, url):
    """Fetch an image from the server and import it as /Game/AutoImport/Room/T_<name> (idempotent overwrite)."""
    try:
        local = os.path.join(tempfile.gettempdir(), name + ".png")
        with open(local, "wb") as f:
            f.write(_get(SERVER + url))
        task = unreal.AssetImportTask()
        task.filename = local
        task.destination_path = _aipath("Room")
        task.destination_name = "T_" + name
        task.automated = True
        task.replace_existing = True
        task.save = True
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
        return unreal.load_asset(_aipath("Room/T_") + name)
    except Exception as e:
        unreal.log_warning("tex %s: %s" % (name, e))
        return None


def _apply_city_block(data):
    """S1 pipelined: storey-count height correction + AI block-plan execution + corridor clearance + per-spot grounding
    + props + spawn re-clearing. (Ported from the dev/_street_fill.py live prototype; the AI produces the layout,
    this only executes and clamps -- iron rule.)"""
    cb = data.get("city_block") or {}
    if not isinstance(cb, dict) or not cb.get("placements"):
        return
    try:
        cor = cb.get("corridor") or [6.0, 16.0]
        cor_lo, cor_hi = float(cor[0]) * 100.0, float(cor[1]) * 100.0
        prompts = {r["id"]: str(r.get("prompt", "")).lower() for r in (data.get("results") or [])}
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        world = unreal.EditorLevelLibrary.get_editor_world()
        rng = random.Random(7)
        for a in list(sub.get_all_level_actors()):
            if a.get_actor_label().startswith("AutoFill_"):
                sub.destroy_actor(a)

        def ground_z(x, y):
            hr = unreal.SystemLibrary.line_trace_single(
                world, unreal.Vector(x, y, 1500.0), unreal.Vector(x, y, -1500.0),
                unreal.TraceTypeQuery.TRACE_TYPE_QUERY1, False, [], unreal.DrawDebugTrace.NONE, True)
            if hr:
                ip = hr.to_dict().get("impact_point")
                if ip:
                    return float(ip.z)
            return None

        def expected_h(p):
            if "high-rise" in p or "skyscraper" in p:
                return None
            if "two-story" in p or "two story" in p:
                return 7.0
            if "multi-story" in p or "multi story" in p or "apartment" in p:
                return 13.0
            if "building" in p[:70] or "facade" in p[:70]:   # subject window is 70 chars (old 40 cut off long-attribute buildings; same as pipeline_server._is_building_prompt)
                return 8.0
            return None

        actors = {a.get_actor_label(): a for a in sub.get_all_level_actors()
                  if a.get_actor_label().startswith("OBJ_") and isinstance(a, unreal.StaticMeshActor)}
        # 1. storey-count semantic height correction
        for lbl, a in actors.items():
            p = prompts.get(int(lbl[4:]), "")
            eh = expected_h(p)
            org, ext = a.get_actor_bounds(False)
            h = ext.z * 2 / 100.0
            if eh and h < 0.75 * eh:
                k = eh / max(0.5, h)
                s = a.get_actor_scale3d()
                a.set_actor_scale3d(unreal.Vector(s.x * k, s.y * k, s.z * k))
                org2, ext2 = a.get_actor_bounds(False)
                L0 = a.get_actor_location()
                a.set_actor_location(unreal.Vector(
                    L0.x, L0.y, L0.z + ((org.z - ext.z) - (org2.z - ext2.z))), False, False)
                unreal.log("cityblock height-fix %s: %.1fm -> %.1fm" % (lbl, h, eh))
        # 2. corridor clearance for originals
        mid = (cor_lo + cor_hi) / 2
        for lbl, a in actors.items():
            _pb = prompts.get(int(lbl[4:]), "")
            if not ("building" in _pb[:70] or "facade" in _pb[:70]):
                continue
            org, ext = a.get_actor_bounds(False)
            lo, hi = org.y - ext.y, org.y + ext.y
            L0 = a.get_actor_location()
            if org.y <= mid and hi > cor_lo:
                a.set_actor_location(unreal.Vector(L0.x, L0.y - (hi - cor_lo), L0.z), False, False)
            elif org.y > mid and lo < cor_hi:
                a.set_actor_location(unreal.Vector(L0.x, L0.y + (cor_hi - lo), L0.z), False, False)
        # 3. AI layout execution (buildings + props)
        src_by_id = {int(lbl[4:]): a for lbl, a in actors.items()}

        def spawn_dup(a, nx, ny, label, scale_k, yaw):
            org, ext = a.get_actor_bounds(False)
            samples = [(nx, ny), (nx + ext.x * scale_k, ny), (nx - ext.x * scale_k, ny),
                       (nx, ny + ext.y * scale_k), (nx, ny - ext.y * scale_k)]
            gzs = [z for z in (ground_z(sx, sy) for sx, sy in samples) if z is not None]
            gz = (min(gzs) - 8.0) if gzs else (org.z - ext.z)
            dup = sub.spawn_actor_from_object(
                a.static_mesh_component.static_mesh, unreal.Vector(nx, ny, a.get_actor_location().z))
            dup.set_actor_label(label)
            for mi in range(a.static_mesh_component.get_num_materials()):
                m = a.static_mesh_component.get_material(mi)
                if m:
                    dup.static_mesh_component.set_material(mi, m)
            s = a.get_actor_scale3d()
            dup.set_actor_scale3d(unreal.Vector(s.x * scale_k, s.y * scale_k, s.z * scale_k))
            dup.set_actor_rotation(unreal.Rotator(pitch=0, yaw=yaw, roll=0), False)
            o2, e2 = dup.get_actor_bounds(False)
            dup.set_actor_location(unreal.Vector(
                nx, ny, dup.get_actor_location().z + (gz - (o2.z - e2.z))), False, False)
            return dup

        n = 0
        for j, p in enumerate(cb.get("placements") or []):
            a = src_by_id.get(int(p.get("src_id", -1)))
            if a is None:
                continue
            spawn_dup(a, float(p["fwd_m"]) * 100.0, float(p["lat_m"]) * 100.0,
                      "AutoFill_B%02d_%d" % (int(p["src_id"]), j),
                      float(p.get("scale", 1.0)), float(p.get("yaw_deg", 0.0)) + rng.uniform(-3, 3))
            n += 1
        for j, q in enumerate(cb.get("props") or []):
            a = src_by_id.get(int(q.get("src_id", -1)))
            if a is None or "meter" in prompts.get(int(q.get("src_id", -1)), ""):
                continue
            spawn_dup(a, float(q["fwd_m"]) * 100.0, float(q["lat_m"]) * 100.0,
                      "AutoFill_P%02d_%d" % (int(q["src_id"]), j), 1.0, float(q.get("yaw_deg", 0.0)))
            n += 1
        # 4. corridor clearance for cloned buildings too (AABB)
        for a in sub.get_all_level_actors():
            if not a.get_actor_label().startswith("AutoFill_B"):
                continue
            org, ext = a.get_actor_bounds(False)
            lo, hi = org.y - ext.y, org.y + ext.y
            L0 = a.get_actor_location()
            if org.y <= mid and hi > cor_lo:
                a.set_actor_location(unreal.Vector(L0.x, L0.y - (hi - cor_lo), L0.z), False, False)
            elif org.y > mid and lo < cor_hi:
                a.set_actor_location(unreal.Vector(L0.x, L0.y + (cor_hi - lo), L0.z), False, False)
        data["_street_corridor"] = [cor_lo, cor_hi]
        _purge_overlapped_fills()                # 5. de-overlap cloned buildings (clumped clipping / stacked on rooftops, observed)
        unreal.log("city block: %d AI placements executed (corridor %.0f..%.0f)" % (n, cor_lo, cor_hi))
    except Exception as e:
        unreal.log_warning("city block failed: %s" % e)


def _ensure_skyline_glow_mat(glow_k):
    """Distant-building night-glow material (AI-driven): UNLIT, dark building body + scattered lit window cells (window
    temperature = Gemini glow_k). Observed "no material" = buildings were previously flat-painted with white light;
    the window-grid texture makes the distant city = dark towers + lights, no more white blocks."""
    mel = unreal.MaterialEditingLibrary
    # 1. lit-window texture (reuse _nightwin_pattern: warm scatter, temperature=glow_k)
    tdir = os.path.join(tempfile.gettempdir(), "auto_skyline")
    os.makedirs(tdir, exist_ok=True)
    png = os.path.join(tdir, "skyline_win.png")
    _nightwin_pattern(png, 14, 22, {"lit_ratio": 0.45, "warm_ratio": 1.0,
                                    "warm_k": glow_k, "cool_k": glow_k, "cluster_bias": 0.25}, "skyline")
    task = unreal.AssetImportTask()
    task.filename = png; task.destination_path = _apath("Skyline")
    task.destination_name = "T_SkylineWin"; task.automated = True
    task.replace_existing = True; task.save = True
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    tex = unreal.load_asset(_apath("Skyline/T_SkylineWin"))
    # 2. material: UNLIT, Emissive = WinTex x Glow multiplier (windows already carry the glow_k color)
    path = _apath("M_SkylineGlow")
    mat = unreal.load_asset(path)
    has_ws = mat is not None and any(
        isinstance(e, unreal.MaterialExpressionScalarParameter)
        and str(e.get_editor_property("parameter_name")) == "WinScale"
        for e in (mel.get_material_expressions(mat) or []))
    if mat is None or not has_ws:                              # create/upgrade: world-space window projection (Tripo UVs cannot carry window grids -> solid slabs, observed)
        if mat is None:
            mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
                "M_SkylineGlow", _apath(""), unreal.Material, unreal.MaterialFactoryNew())
        else:
            mel.delete_all_material_expressions(mat)
        mat.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)
        wp = mel.create_material_expression(mat, unreal.MaterialExpressionWorldPosition, -1100, 200)
        mz = mel.create_material_expression(mat, unreal.MaterialExpressionComponentMask, -900, 120)
        mz.set_editor_property("r", False); mz.set_editor_property("g", False)
        mz.set_editor_property("b", True); mz.set_editor_property("a", False)            # height z -> V
        mx = mel.create_material_expression(mat, unreal.MaterialExpressionComponentMask, -900, 250)
        mx.set_editor_property("r", True); mx.set_editor_property("g", False)
        mx.set_editor_property("b", False); mx.set_editor_property("a", False)
        myc = mel.create_material_expression(mat, unreal.MaterialExpressionComponentMask, -900, 340)
        myc.set_editor_property("r", False); myc.set_editor_property("g", True)
        myc.set_editor_property("b", False); myc.set_editor_property("a", False)
        axy = mel.create_material_expression(mat, unreal.MaterialExpressionAdd, -760, 300)  # x+y -> around-the-building U
        mel.connect_material_expressions(mx, "", axy, "A")
        mel.connect_material_expressions(myc, "", axy, "B")
        ws = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -900, 470)
        ws.set_editor_property("parameter_name", "WinScale"); ws.set_editor_property("default_value", 0.0011)
        mu = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -600, 300)
        mel.connect_material_expressions(axy, "", mu, "A")
        mel.connect_material_expressions(ws, "", mu, "B")
        mvv = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -600, 120)
        mel.connect_material_expressions(mz, "", mvv, "A")
        mel.connect_material_expressions(ws, "", mvv, "B")
        ap = mel.create_material_expression(mat, unreal.MaterialExpressionAppendVector, -440, 200)  # (U,V)
        mel.connect_material_expressions(mu, "", ap, "A")
        mel.connect_material_expressions(mvv, "", ap, "B")
        ts = mel.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, -260, -40)
        ts.set_editor_property("parameter_name", "WinTex")
        mel.connect_material_expressions(ap, "", ts, "UVs")
        sp = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -260, 220)
        sp.set_editor_property("parameter_name", "Glow"); sp.set_editor_property("default_value", 6.0)
        mul = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -60, 40)
        mel.connect_material_expressions(ts, "RGB", mul, "A")
        mel.connect_material_expressions(sp, "", mul, "B")
        mel.connect_material_property(mul, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
        mel.recompile_material(mat)
        unreal.EditorAssetLibrary.save_asset(path)
    mic, mic_path = _ensure_mic("MI_SkylineGlow", mat)
    if tex:
        mel.set_material_instance_texture_parameter_value(mic, "WinTex", tex)
    mel.set_material_instance_scalar_parameter_value(mic, "Glow", 6.0)
    unreal.EditorAssetLibrary.save_asset(mic_path)
    return mic


def _ground_trace_z(world, x, y, around_z):
    """Ray-probe the ground/terrain surface z at (x,y) (cast straight down from open air; the terrain must have
    collision). Miss -> None. Lets fill trees/pedestrians hug real relief/slopes instead of digging into hillsides
    at a fixed height (observed rain-alley downhill trees sunk into the ground)."""
    try:
        hr = unreal.SystemLibrary.line_trace_single(
            world, unreal.Vector(x, y, around_z + 6000.0), unreal.Vector(x, y, around_z - 6000.0),
            unreal.TraceTypeQuery.TRACE_TYPE_QUERY1, False, [], unreal.DrawDebugTrace.NONE, True)
        if hr:
            ip = hr.to_dict().get("impact_point")
            if ip:
                return float(ip.z)
    except Exception:
        pass
    return None


def _apply_scene_fill(data):
    """Scene fill (generic; observed street feedback 'frame center is empty'): Gemini judges scene_fill
    (species+density) -> real vegetation meshes scattered along the playable-area edge, avoiding the
    camera->subject sight cone (+/-26 deg) so the subject stays clear, adding framing and depth to empty near/mid
    fields. City distance is handled by _apply_distant_skyline (buildings); this adds near/mid vegetation fill
    (street cherries / park woods / wild scrub). It is CONTENT (real meshes with GI), not out-of-scene terrain.
    Idempotently clears AutoFillTree_*. Non-vegetation fill (species not in _VEG_SPECIES or density<=0) -> skip."""
    env = data.get("environment") or {}
    sf = env.get("scene_fill") or {}
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    acts = list(sub.get_all_level_actors())
    for a in acts:                                            # idempotent cleanup
        if a.get_actor_label().startswith("AutoFillTree"):
            try:
                sub.destroy_actor(a)
            except Exception:
                pass
    species = str(sf.get("species", "")).strip()
    density = float(sf.get("density", 0.0) or 0.0)
    if species not in _VEG_SPECIES or density <= 0.0:
        return
    meshes = [unreal.load_asset(VEG_MESH_DIR + n) for n in _VEG_SPECIES[species]]
    meshes = [m for m in meshes if m]
    if not meshes:
        return
    objs = [a for a in acts if a.get_actor_label().startswith("OBJ_")]
    if not objs:
        return
    ox = sum(a.get_actor_location().x for a in objs) / len(objs)
    oy = sum(a.get_actor_location().y for a in objs) / len(objs)
    gz = min(a.get_actor_location().z for a in objs) - 20.0   # ground = just below the objects' lowest point
    cam = next((a for a in acts if a.get_actor_label() == "AutoCam"), None)
    cl = cam.get_actor_location() if cam else unreal.Vector(ox - 2000.0, oy, gz + 150.0)
    span = math.hypot(ox - cl.x, oy - cl.y)                   # camera -> subject-cluster distance
    plaza_r = max(900.0, 0.62 * span)                        # plaza radius
    look = math.degrees(math.atan2(oy - cl.y, ox - cl.x))    # camera-to-subject bearing (keep a sight cone)
    hisms = [_spawn_hism("AutoFillTree_%d" % i, m)[1] for i, m in enumerate(meshes)]
    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()

    def _rnd(k, s):                                          # deterministic jitter (integer-hash decorrelated; see the bird-line lesson)
        h = (k * 374761393 + s * 668265263) & 0xffffffffffffffff
        h = ((h ^ (h >> 13)) * 1274126177) & 0xffffffffffffffff
        return ((h >> 16) & 0xffff) / 65536.0

    def _angdiff(a, b):
        return abs((a - b + 180) % 360 - 180)

    is_tree = species in ("tree_green", "tree_autumn", "tree_dead")
    cone = 26.0 if is_tree else 14.0                         # tall trees keep a wider sight cone; low shrubs may sit closer
    urban = bool((env.get("distant_city") or {}).get("present"))
    placed = 0
    if urban:
        # city context: ring-plaza layout (street cherry-tree ring); the far view is _apply_distant_skyline's buildings
        n_ring = int(round(6 + density * 24))                # density -> count (the old 14+34x read as a forest on city streets; one notch down)
        for k in range(n_ring):
            ang = 360.0 * k / max(1, n_ring) + (_rnd(k, 1) - 0.5) * 9.0
            if _angdiff(ang, look) < cone:                   # keep the camera->subject sight line clear of the subject
                continue
            R = plaza_r * (0.92 + 0.34 * _rnd(k, 2))
            x = ox + R * math.cos(math.radians(ang))
            y = oy + R * math.sin(math.radians(ang))
            s = 0.85 + 0.6 * _rnd(k, 3)
            zt = _ground_trace_z(world, x, y, gz)            # seat each tree on the real surface (slopes/relief), 8cm buried against floating
            zz = (zt - 8.0) if zt is not None else gz
            hisms[k % len(hisms)].add_instance(
                unreal.Transform(unreal.Vector(x, y, zz),
                                 unreal.Rotator(pitch=0.0, yaw=_rnd(k, 4) * 360.0, roll=0.0),  # yaw only (Rotator positional order is roll,pitch,yaw)
                                 unreal.Vector(s, s, s)), True)
            placed += 1
        unreal.log("scene fill: %d %s around plaza r=%d (sightline-clear cone %d deg)" %
                   (placed, species, round(plaza_r), round(cone)))
    else:
        # Natural context (design ruling 2026-07-02): no rings -- scatter across an AREA, sparse near / dense far, closing into a tree wall
        # as the distance; avoid water (carved region + below waterline) and the sight cone. With no distant city, trees ARE the natural horizon.
        wl = data.get("_waterline_cm"); wr = data.get("_water_region")
        terr = data.get("terrain") or {}
        tb = None                                            # terrain footprint (trees only on the textured platform; planting on the bare skirt = floating seams, seen in review)
        if terr.get("grid"):
            tcx = float(terr.get("cx_m", 0.0)) * 100.0; tcy = float(terr.get("cy_m", 0.0)) * 100.0
            thx = float(terr.get("half_fwd_m", 40.0)) * 100.0; thy = float(terr.get("half_lat_m", 40.0)) * 100.0
            tb = (tcx - 0.92 * thx, tcx + 0.92 * thx, tcy - 0.92 * thy, tcy + 0.92 * thy)
        R0, R1 = 0.55 * plaza_r, 3.4 * plaza_r
        if tb:
            # Area fill must reach the platform's farthest corner: the far-shore band needs trees too, breaking the straight water-land line,
            # else the far shore is a bare strip + a distant ring = "floating/disjointed edge" (flagged in review)
            far_corner = max(math.hypot(tx - ox, ty - oy) for tx in (tb[0], tb[1]) for ty in (tb[2], tb[3]))
            R1 = max(R1, far_corner)
        n_area = int(round((6 + density * 24) * 4.0))        # area budget (covers the whole platform; the outer tree wall must be dense)
        for k in range(n_area * 5):                          # oversample: water/sightline/footprint/near-field rejection rate is high
            if placed >= n_area:
                break
            ang = 360.0 * _rnd(k, 11)
            if _angdiff(ang, look) < cone:
                continue
            R = R0 + (R1 - R0) * (_rnd(k, 12) ** 0.5)        # radius biased outward -> sparse near, dense far
            if _rnd(k, 13) > 0.25 + 0.75 * (R - R0) / max(1.0, R1 - R0):
                continue                                     # distance-weighted acceptance: walls far away, clearing up close
            x = ox + R * math.cos(math.radians(ang))
            y = oy + R * math.sin(math.radians(ang))
            if tb and not (tb[0] < x < tb[1] and tb[2] < y < tb[3]):
                continue                                     # off the platform (bare skirt): do not plant
            if wr and (wr[0] - 150.0) < x < (wr[1] + 150.0) and (wr[2] - 150.0) < y < (wr[3] + 150.0):
                continue                                     # lake region (incl. 1.5m shore band): no trees
            zt = _ground_trace_z(world, x, y, gz)
            near_water = bool(wr) and (wr[0] - 600.0) < x < (wr[1] + 600.0) and (wr[2] - 600.0) < y < (wr[3] + 600.0)
            if wl is not None and near_water and zt is not None and zt < wl + 12.0:
                continue                                     # waterline filter only near the lake region (globally it rejects the whole outer fade band; observed tree wall reduced to 1 tree)
            zz = (zt - 8.0) if zt is not None else gz
            s = 0.85 + 0.6 * _rnd(k, 3)
            hisms[k % len(hisms)].add_instance(
                unreal.Transform(unreal.Vector(x, y, zz),
                                 unreal.Rotator(pitch=0.0, yaw=_rnd(k, 4) * 360.0, roll=0.0),
                                 unreal.Vector(s, s, s)), True)
            placed += 1
        unreal.log("scene fill (nature): %d %s over range r=%d..%d (near-sparse far-dense, water-clear)" %
                   (placed, species, round(R0), round(R1)))
        # Distant forest belts (the natural "skyline"; design ruling "the far view is trees"): plant two tree lines with depth on the skirt.
        # The old single sparse ring (~40-100 trees over 360 deg, starting at the platform's farthest-corner radius = a big bare skirt gap facing each edge center) =
        # "lone trees around an island" (still isolated after the fog fix). New: per bearing, start just outside the platform rectangle's edge (ray-rect intersection);
        # a near belt of normal trees hugging the platform seam + a far belt of large layered silhouettes -> forest reads as continuing to the horizon. City counterpart = _apply_distant_skyline's buildings.
        def _edge_r(ang):
            """Distance from the fill origin along a ray at ang (deg) to the platform rectangle's edge; no platform -> R1.
            Tree belts start right at the textured platform's edge, leaving no bare skirt gap."""
            if not tb:
                return R1
            ca = math.cos(math.radians(ang)); sa = math.sin(math.radians(ang))
            tmax = 1e9
            if abs(ca) > 1e-4:
                for bx in (tb[0], tb[1]):
                    t = (bx - ox) / ca
                    if t > 0:
                        yy = oy + t * sa
                        if tb[2] - 1.0 <= yy <= tb[3] + 1.0:
                            tmax = min(tmax, t)
            if abs(sa) > 1e-4:
                for by in (tb[2], tb[3]):
                    t = (by - oy) / sa
                    if t > 0:
                        xx = ox + t * ca
                        if tb[0] - 1.0 <= xx <= tb[1] + 1.0:
                            tmax = min(tmax, t)
            return tmax if tmax < 1e8 else R1
        ring = 0
        for bi, (bn, ro0, ro1, s0, sj, salt) in enumerate((
                (int(round(90 + 120 * density)), 500.0, 6000.0, 1.4, 1.0, 21),
                (int(round(60 + 90 * density)), 6000.0, 20000.0, 2.2, 1.4, 25))):
            done = 0
            for k in range(bn * 2):
                if done >= bn:
                    break
                ang = 360.0 * _rnd(k, salt)
                R = _edge_r(ang) + ro0 + (ro1 - ro0) * (_rnd(k, salt + 1) ** 0.8)
                x = ox + R * math.cos(math.radians(ang))
                y = oy + R * math.sin(math.radians(ang))
                zt = _ground_trace_z(world, x, y, gz)
                zz = (zt - 8.0) if zt is not None else -10.0     # skirt has no collision so the trace misses -> sit on the skirt surface, slightly buried
                s = s0 + sj * _rnd(k, salt + 2)
                hisms[(k + bi) % len(hisms)].add_instance(
                    unreal.Transform(unreal.Vector(x, y, zz),
                                     unreal.Rotator(pitch=0.0, yaw=_rnd(k, salt + 3) * 360.0, roll=0.0),
                                     unreal.Vector(s, s, s)), True)
                done += 1
            ring += done
        unreal.log("distant forest ring: %d trees in 2 depth bands from the terrain edge outward" % ring)
        # Shore-lip shrub line: the stepped ridge at the water-land seam needs low shrubs to break it (photo shores are all low scrub;
        # a bare straight waterline = a black step band, seen in review). More on the near lip, fewer far, a narrow gap dead-center to see the water.
        if wr and "bush" in _VEG_SPECIES:
            bmeshes = [m for m in (unreal.load_asset(VEG_MESH_DIR + n) for n in _VEG_SPECIES["bush"]) if m]
            if bmeshes:
                bh = [_spawn_hism("AutoFillTreeShore_%d" % i, m)[1] for i, m in enumerate(bmeshes)]
                shore_n = 0
                for k in range(160):
                    if shore_n >= 44:
                        break
                    y = wr[2] + (wr[3] - wr[2]) * _rnd(k, 31)
                    edge = wr[0] if _rnd(k, 32) < 0.62 else wr[1]
                    x = edge + (_rnd(k, 33) - 0.5) * 260.0
                    ang_p = math.degrees(math.atan2(y - cl.y, x - cl.x))
                    if _angdiff(ang_p, look) < 9.0:
                        continue                             # leave a narrow center gap in the sight line; do not wall off the lake
                    zt = _ground_trace_z(world, x, y, gz)
                    if zt is None or (wl is not None and zt < wl - 2.0):
                        continue                             # discard any that fall into the water
                    s = 0.7 + 0.5 * _rnd(k, 34)
                    bh[k % len(bh)].add_instance(
                        unreal.Transform(unreal.Vector(x, y, zt - 6.0),
                                         unreal.Rotator(pitch=0.0, yaw=_rnd(k, 35) * 360.0, roll=0.0),
                                         unreal.Vector(s, s, s)), True)
                    shore_n += 1
                unreal.log("shore lip bushes: %d along water edges" % shore_n)


_PED_TEX = (("ped_0.png", 0.335), ("ped_1.png", 0.580), ("ped_2.png", 0.401),
            ("ped_3.png", 0.531), ("ped_4.png", 0.378))   # bundled pedestrian silhouettes (white figure/black bg) + aspect ratios, fetched via the /amblife route
_BIRD_TEX = (("bird_0.png", 3.067), ("bird_1.png", 1.028), ("bird_2.png", 1.424),
             ("bird_3.png", 1.137), ("bird_4.png", 1.201), ("bird_5.png", 1.497))   # bundled bird silhouettes (white bird/black bg) + aspect ratios
_AMBLIFE_DIR = r"C:\Users\Strix\Desktop\毕业设计\python-kana\ue_library\AmbLife"   # local silhouette-texture dir (read locally if the backend is down; fast same-machine path, remote UE still uses /amblife)


def _ensure_ped_silhouette_material():
    """Pedestrian silhouette material: masked (white figure = opaque) + unlit + dark emissive -> dark human silhouettes
    at night. Idempotent reuse."""
    mp = _apath("M_PedSilhouette")
    if unreal.EditorAssetLibrary.does_asset_exist(mp):
        return unreal.load_asset(mp)
    mel = unreal.MaterialEditingLibrary           # mel is local to this function (not module-global; was missing, masked by "material exists, skip create", exposed by per-tid fresh paths)
    mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        "M_PedSilhouette", _apath(""), unreal.Material, unreal.MaterialFactoryNew())
    mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_MASKED)
    mat.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)
    mat.set_editor_property("two_sided", True)
    mat.set_editor_property("opacity_mask_clip_value", 0.33)
    ts = mel.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, -460, 0)
    ts.set_editor_property("parameter_name", "PedTex")
    mel.connect_material_property(ts, "R", unreal.MaterialProperty.MP_OPACITY_MASK)
    em = mel.create_material_expression(mat, unreal.MaterialExpressionVectorParameter, -460, 260)
    em.set_editor_property("parameter_name", "Tint")
    em.set_editor_property("default_value", unreal.LinearColor(0.018, 0.020, 0.030, 1.0))
    mel.connect_material_property(em, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    mel.recompile_material(mat)
    return mat


def _amb_import_texs(tex_list, sub_dir):
    """Import the bundled silhouette textures (pedestrians/birds); returns [(texture, aspect)].
    Already imported -> reuse directly (no source needed, the backend may be down); else LOCAL REPO first, backend
    /amblife as fallback."""
    eal = unreal.EditorAssetLibrary
    out = []
    for fn, asp in tex_list:
        try:
            ap = _apath("%s/T_%s") % (sub_dir, fn.split(".")[0])
            tx = unreal.load_asset(ap) if eal.does_asset_exist(ap) else None
            if tx is None:                                       # not imported yet -> source: local first, backend fallback
                lf = os.path.join(_AMBLIFE_DIR, fn)
                if os.path.exists(lf):
                    src = lf
                else:
                    src = os.path.join(tempfile.gettempdir(), "auto_%s" % fn)
                    with open(src, "wb") as f:
                        f.write(_get(SERVER + "/amblife/" + fn))
                task = unreal.AssetImportTask()
                task.filename = src
                task.destination_path = _apath(sub_dir)
                task.destination_name = "T_" + fn.split(".")[0]
                task.automated = True
                task.replace_existing = True
                task.save = True
                unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
                tx = unreal.load_asset(ap)
            if tx:
                out.append((tx, asp))
        except Exception as e:
            unreal.log_warning("amb tex %s failed: %s" % (fn, e))
    return out


def _amb_hisms(texs, mat, label):
    """One HISM per texture (quad mesh + that texture's MID); returns [(hism, aspect)]."""
    quad = _fx_quad()
    out = []
    for i, (tx, asp) in enumerate(texs):
        h = _spawn_hism("%s_%d" % (label, i), quad)[1]
        mid = h.create_dynamic_material_instance(0, mat)
        mid.set_texture_parameter_value("PedTex", tx)
        out.append((h, asp))
    return out


def _apply_ambient_life(data):
    """Ambient life (generic; brings the scene alive): Gemini judges ambient_life{pedestrians,birds} -> dark pedestrian
    silhouettes scattered midfield facing the camera (silhouetted against the brighter night backdrop, real heights,
    grounded) + a bird flock high up drifting on one heading. Idempotently clears AutoPed*/AutoBird*. Silhouette =
    masked dark unlit material (white-silhouette texture as alpha); textures bundled in ue_library/AmbLife, fetched
    via /amblife (portable). Pedestrians/birds gate on their own densities independently."""
    env = data.get("environment") or {}
    al = env.get("ambient_life") or {}
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in list(sub.get_all_level_actors()):
        if a.get_actor_label().startswith(("AutoPed", "AutoBird")):
            try:
                sub.destroy_actor(a)
            except Exception:
                pass
    peds = float(al.get("pedestrians", 0.0) or 0.0)
    birds = float(al.get("birds", 0.0) or 0.0)
    if peds <= 0.0 and birds <= 0.0:
        return
    objs = [a for a in sub.get_all_level_actors() if a.get_actor_label().startswith("OBJ_")]
    if not objs:
        return
    ox = sum(a.get_actor_location().x for a in objs) / len(objs)
    oy = sum(a.get_actor_location().y for a in objs) / len(objs)
    gz = min(a.get_actor_location().z for a in objs) - 15.0
    cam = next((a for a in sub.get_all_level_actors() if a.get_actor_label() == "AutoCam"), None)
    cl = cam.get_actor_location() if cam else unreal.Vector(ox - 2000.0, oy, gz + 150.0)
    span = max(300.0, math.hypot(ox - cl.x, oy - cl.y))
    fwx = (ox - cl.x) / span
    fwy = (oy - cl.y) / span
    sxv = fwy
    syv = -fwx                                           # lateral unit vector

    def _rnd(k, s):                                      # integer hash: k and s must be truly decorrelated (old s*40503 differed only 0.05 across dims -> points fell on the x=y=z line)
        h = (k * 374761393 + s * 668265263) & 0xffffffffffffffff
        h = ((h ^ (h >> 13)) * 1274126177) & 0xffffffffffffffff
        return ((h >> 16) & 0xffff) / 65536.0

    mat = _ensure_ped_silhouette_material()
    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()

    if peds > 0.0:                                       # pedestrian silhouettes: scattered midfield, facing the camera, grounded, real heights
        ph = _amb_hisms(_amb_import_texs(_PED_TEX, "Ped"), mat, "AutoPedH")
        n = int(round(2 + peds * 20)) if ph else 0
        for k in range(n):
            t = 0.14 + 0.70 * _rnd(k, 1)                 # between camera and subject (pulled closer; near pedestrians read clearly)
            lat = (_rnd(k, 2) - 0.5) * 0.55 * span * t   # lateral spread narrows with t (near narrow, far wide; all stay on the street; the old 1.7x span = +/-52m flung them off-frame)
            px = cl.x + (ox - cl.x) * t + sxv * lat
            py = cl.y + (oy - cl.y) * t + syv * lat
            H = 1.62 + 0.22 * _rnd(k, 3)                 # height 1.62-1.84m
            h, asp = ph[k % len(ph)]
            yaw = math.degrees(math.atan2(cl.y - py, cl.x - px))   # +X normal faces the camera
            zt = _ground_trace_z(world, px, py, gz)      # feet on the real surface (no sinking/floating on slopes)
            pz = (zt - 4.0) if zt is not None else gz
            h.add_instance(unreal.Transform(unreal.Vector(px, py, pz),
                                            unreal.Rotator(pitch=0.0, yaw=yaw, roll=0.0),
                                            unreal.Vector(1.0, H * asp, H)), True)
        if n:
            unreal.log("ambient life: %d pedestrian silhouettes (peds=%.2f)" % (n, peds))

    if birds > 0.0:                                      # birds: one flock high above the subject, drawn into a band along one heading, side profiles to the camera
        bh = _amb_hisms(_amb_import_texs(_BIRD_TEX, "Bird"), mat, "AutoBirdH")
        n = int(round(8 + birds * 34)) if bh else 0
        fcx = cl.x + fwx * span * 1.15 + sxv * span * 0.55   # flock center: high, ahead, off to one side
        fcy = cl.y + fwy * span * 1.15 + syv * span * 0.55
        tops = []                                            # the flock must fly above the tallest thing (rooftops), else birds get swallowed by facades
        for a in objs:
            try:
                o, ext = a.get_actor_bounds(False)
                tops.append(o.z + ext.z)
            except Exception:
                pass
        fcz = max(gz + 2000.0, (max(tops) if tops else gz) + 900.0)
        head = _rnd(7, 9) * 360.0                            # flock heading (differs per photo)
        hx = math.cos(math.radians(head))
        hy = math.sin(math.radians(head))
        rad = max(700.0, span * 0.55)                        # flock scale
        for k in range(n):
            # a 3D cluster (not a line): long along the heading + lateral + real vertical spread, ratio 1.8:1.1:0.95
            along = (_rnd(k, 1) - 0.5) * rad * 1.8
            cross = (_rnd(k, 2) - 0.5) * rad * 1.1
            bx = fcx + hx * along - hy * cross
            by = fcy + hy * along + hx * cross
            bz = fcz + (_rnd(k, 3) - 0.5) * rad * 0.95       # vertical spread (the old +/-700 was too flat -> a line)
            Wm = 0.9 + 0.8 * _rnd(k, 4)                      # wingspan 0.9-1.7m
            h, asp = bh[k % len(bh)]
            yaw = math.degrees(math.atan2(cl.y - by, cl.x - bx))   # side profile to the camera
            h.add_instance(unreal.Transform(unreal.Vector(bx, by, bz),
                                            unreal.Rotator(pitch=0.0, yaw=yaw, roll=0.0),
                                            unreal.Vector(1.0, Wm, Wm / asp)), True)
        if n:
            unreal.log("ambient life: %d birds flock (birds=%.2f)" % (n, birds))


def _apply_distant_skyline(data):
    """Distant city skyline (AI-driven; rule: this too must stay AI-driven): Gemini judges distant_city
    (present/density/height/glow); code reuses the rebuilt building models in a ring beyond the playable area,
    fading into night fog as a glowing skyline. Non-city (present=False) -> skip (forest/lake/wild auto-skip).
    This is CONTENT (buildings -- real Tripo models with GI), not "out-of-scene terrain" (no vista black-bowl
    repeat). Must run before _apply_night_windows (so distant buildings get lit windows too)."""
    env = data.get("environment") or {}
    dc = env.get("distant_city") or {}
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in list(sub.get_all_level_actors()):                # idempotent cleanup (buildings + lights)
        if a.get_actor_label().startswith("AutoSkyline"):
            try:
                sub.destroy_actor(a)
            except Exception:
                pass
    if not dc.get("present"):
        return
    try:
        prompts = {r["id"]: str(r.get("prompt", "")).lower() for r in (data.get("results") or [])}

        def _isb(p):
            return any(k in p[:60] for k in ("building", "high-rise", "skyscraper", "apartment", "tower", "house"))
        builds = []
        for a in sub.get_all_level_actors():
            lbl = a.get_actor_label()
            if lbl.startswith("OBJ_") and isinstance(a, unreal.StaticMeshActor):
                try:
                    if _isb(prompts.get(int(lbl[4:]), "")) and a.static_mesh_component.static_mesh is not None:
                        builds.append(a)
                except Exception:
                    pass
        if not builds:
            return
        terr = data.get("terrain") or {}
        cx = float(terr.get("cx_m", 0.0)) * CM_PER_UNIT
        cy = float(terr.get("cy_m", 0.0)) * CM_PER_UNIT
        hf = max(50.0, float(terr.get("half_fwd_m", 40.0)) * CM_PER_UNIT)
        hl = max(50.0, float(terr.get("half_lat_m", 40.0)) * CM_PER_UNIT)
        gz0 = _terrain_surface_cm(terr, cx, cy)                # street level (distant city at the same height)
        R0 = 2.0 * max(hf, hl)                                 # inner rim = well beyond the playable area (observed: too close = big bright blocks; pushed out to read as distant city glow)
        density = float(dc.get("density", 0.5)); height = float(dc.get("height", 0.5))
        n_total = int(20 + density * 70)                       # AI density -> 20-90 distant buildings
        night = "night" in str(env.get("time_of_day", "")).lower()
        rng = random.Random(421)
        n = 0
        spots = []                                             # night: distant buildings keep real materials (texture); scattered warm lights turn them into a cityscape
        # Distant-city ground: one big flat slab at street level so far buildings have footing (observed "buildings standing over void"); same wet street material,
        # fades into fog, seamless with the near street. No collision (players never get there; must not block the near field).
        far_r = R0 * 4.2
        gpl = unreal.load_asset("/Engine/BasicShapes/Plane")
        if gpl is not None:
            ga = sub.spawn_actor_from_object(gpl, unreal.Vector(cx, cy, gz0 - 3.0))
            ga.set_actor_label("AutoSkylineGround")
            ga.static_mesh_component.set_static_mesh(gpl)
            ga.set_actor_scale3d(unreal.Vector(far_r / 50.0, far_r / 50.0, 1.0))   # Plane=1m -> side length 2 x far_r
            gmat = unreal.load_asset(BLEND_MIC_PATH)           # wet street MIC (not the checkerboard MI_GroundFlat)
            if gmat:
                ga.static_mesh_component.set_material(0, gmat)
            ga.static_mesh_component.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)
            try:
                ga.static_mesh_component.set_editor_property("cast_shadow", False)
            except Exception:
                pass
        for k in range(n_total):
            ang = rng.uniform(0.0, 2.0 * math.pi)
            R = R0 * (1.0 + 2.8 * (rng.random() ** 1.4))       # sparser with distance (exponential)
            x = cx + R * math.cos(ang); y = cy + R * math.sin(ang)
            src = builds[rng.randrange(len(builds))]
            dup = sub.spawn_actor_from_object(src.static_mesh_component.static_mesh, unreal.Vector(x, y, gz0))
            dup.set_actor_label("AutoSkyline_%03d" % k)
            for mi in range(src.static_mesh_component.get_num_materials()):    # real materials (texture, not white blocks)
                m = src.static_mesh_component.get_material(mi)
                if m:
                    dup.static_mesh_component.set_material(mi, m)
            spots.append((x, y))
            s = src.get_actor_scale3d()
            vk = 0.8 + height * 1.9 + rng.uniform(-0.2, 0.5)   # AI height bias -> stretch into towers
            hk = 0.85 + rng.uniform(-0.1, 0.35)
            dup.set_actor_scale3d(unreal.Vector(s.x * hk, s.y * hk, s.z * max(0.5, vk)))
            dup.set_actor_rotation(unreal.Rotator(pitch=0, yaw=rng.uniform(0, 360), roll=0), False)
            o2, e2 = dup.get_actor_bounds(False)
            dup.set_actor_location(unreal.Vector(x, y, dup.get_actor_location().z + (gz0 - (o2.z - e2.z))), False, False)
            try:
                dup.static_mesh_component.set_editor_property("cast_shadow", False)
            except Exception as e:
                unreal.log_warning("skyline shadow off: %s" % e)
            n += 1
        nlt = 0
        if night and spots:                                    # night: dense warm point lights turn the textured distant buildings into a cityscape (temperature/brightness/light density all AI-judged)
            kr = _kelvin_rgb(float(dc.get("glow_k", 3200.0)))
            live = float(dc.get("liveliness", 0.55))           # AI-judged liveliness of the distant city at night (sleepy town vs neon city)
            rng.shuffle(spots)
            ntake = min(len(spots), max(8, int(len(spots) * (0.22 + 0.6 * live))))   # livelier -> more lit buildings
            inten = 110000.0 + 440000.0 * live                 # livelier -> brighter (sleepy town dim, neon city cuts through the fog)
            for (lx, ly) in spots[:ntake]:
                la = sub.spawn_actor_from_class(unreal.PointLight, unreal.Vector(lx, ly, gz0 + 600.0))
                la.set_actor_label("AutoSkylineLight_%02d" % nlt)
                lc = la.point_light_component
                lc.set_intensity(inten)                        # AI liveliness -> intensity
                lc.set_attenuation_radius(22000.0)
                lc.set_light_color(unreal.LinearColor(kr[0], kr[1], kr[2], 1.0))
                try:
                    lc.set_editor_property("use_temperature", False)
                    lc.set_cast_shadows(False)
                except Exception:
                    pass
                nlt += 1
        unreal.log("distant skyline: %d distant buildings (density %.2f height %.2f) + %d glow lights" % (n, density, height, nlt))
    except Exception as e:
        unreal.log_warning("distant skyline failed: %s" % e)


def _clamp_spawn_into_street(data):
    """Spawn re-clearing after block fill (the fill runs after spawn placement; observed respawning inside a building)
    -- called post-VR."""
    cor = data.get("_street_corridor")
    if not cor:
        return
    try:
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        boxes = []
        for a in sub.get_all_level_actors():
            lbl = a.get_actor_label()
            if (lbl.startswith("OBJ_") or lbl.startswith("AutoFill_B")) and isinstance(a, unreal.StaticMeshActor):
                org, ext = a.get_actor_bounds(False)
                boxes.append((org.x - ext.x, org.x + ext.x, org.y - ext.y, org.y + ext.y))
        cy = (cor[0] + cor[1]) / 2
        x = 1200.0
        while x < 18000.0:
            if all(not (lo - 150 < x < hi + 150 and ylo - 150 < cy < yhi + 150)
                   for lo, hi, ylo, yhi in boxes):
                for a in sub.get_all_level_actors():
                    if isinstance(a, unreal.PlayerStart):
                        a.set_actor_location(unreal.Vector(x, cy, 200.0), False, False)
                        unreal.log("street spawn re-cleared -> (%.0f, %.0f)" % (x, cy))
                        ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
                        ues.set_level_viewport_camera_info(
                            unreal.Vector(x, cy, 320.0), unreal.Rotator(pitch=0, yaw=0, roll=0))
                return
            x += 300.0
    except Exception as e:
        unreal.log_warning("street spawn clear: %s" % e)


def _write_png(path, w, h, rgba):
    """stdlib RGBA PNG (no PIL in UE's Python). rgba = bytearray of length w*h*4, row-major top-down."""
    import zlib
    import struct
    raw = b"".join(b"\x00" + bytes(rgba[y * w * 4:(y + 1) * w * 4]) for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(raw)))
        f.write(chunk(b"IEND", b""))


def _ensure_nightwin_master():
    """Night-window material: ViewTex.RGB x Nits -> Emissive, ViewTex.A -> OpacityMask, UNLIT + MASKED, two-sided.
    Masked rather than Opaque: unlit cells are fully cut out and the facade stays normally lit -- an opaque panel
    would stamp a dead-black rectangle onto the building."""
    path = _apath("M_NightWin")
    mel = unreal.MaterialEditingLibrary
    mat = unreal.load_asset(path)
    if mat is not None:
        return mat
    mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        "M_NightWin", _apath(""), unreal.Material, unreal.MaterialFactoryNew())
    mat.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)
    mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_MASKED)
    mat.set_editor_property("two_sided", True)
    ts = mel.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, -700, -100)
    ts.set_editor_property("parameter_name", "ViewTex")
    sp = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -700, 200)
    sp.set_editor_property("parameter_name", "Nits")
    sp.set_editor_property("default_value", 20.0)
    m1 = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -380, 0)
    mel.connect_material_expressions(ts, "RGB", m1, "A")
    mel.connect_material_expressions(sp, "", m1, "B")
    mel.connect_material_property(m1, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    mel.connect_material_property(ts, "A", unreal.MaterialProperty.MP_OPACITY_MASK)
    mel.recompile_material(mat)
    unreal.EditorAssetLibrary.save_asset(path)
    return mat


def _nightwin_pattern(path, cols, rows, nw, seed):
    """Lit-window pattern PNG: 24px grid; window rects = lit cells tinted + opaque, everything else fully transparent.
    Distribution follows the AI params: lit_ratio total / cluster_bias clumping (a lit left neighbor tends to stay
    lit -- whole office floors) / warm_ratio warm-cool split / 0.45-1.0 per-window brightness jitter (rooms differ).
    Deterministic seed = reproducible re-runs."""
    import zlib as _z
    rng = random.Random(_z.crc32(seed.encode()) & 0xffffff)
    warm = _kelvin_rgb(nw["warm_k"])
    cool = _kelvin_rgb(nw["cool_k"])
    lit, cb_ = float(nw["lit_ratio"]), float(nw["cluster_bias"])
    w, h = cols * 24, rows * 24
    buf = bytearray(w * h * 4)
    n_on = 0
    for r in range(rows):
        left_on = False
        for c in range(cols):
            p = lit + (0.92 - lit) * cb_ if left_on else lit * (1.0 - 0.55 * cb_)
            on = rng.random() < p
            left_on = on
            if not on:
                continue
            n_on += 1
            base = warm if rng.random() < float(nw["warm_ratio"]) else cool
            v = 0.45 + 0.55 * rng.random()
            col = [min(255, int(255 * ch * v * (0.94 + 0.12 * rng.random()))) for ch in base]
            for py in range(r * 24 + 6, r * 24 + 19):
                row0 = (py * w + c * 24) * 4
                for px in range(5, 19):
                    o = row0 + px * 4
                    buf[o], buf[o + 1], buf[o + 2], buf[o + 3] = col[0], col[1], col[2], 255
    _write_png(path, w, h, buf)
    return n_on


def _purge_overlapped_fills():
    """De-overlap cloned buildings (observed "obviously unreasonable placement": towers interpenetrating in clumps).
    Originals (photo-pinned) are unconditionally kept; clones are validated in creation order and deleted when XY
    overlap with the accepted set exceeds 25% of their own footprint (dense blocks may lightly touch). A clone
    stacked on another roof overlaps that building 100% -> removed by the same rule."""
    try:
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        acc = []
        fills = []
        for a in sub.get_all_level_actors():
            lbl = a.get_actor_label()
            if not isinstance(a, unreal.StaticMeshActor):
                continue
            if lbl.startswith("OBJ_"):
                org, ext = a.get_actor_bounds(False)
                if ext.z * 2 > 350.0:                      # only building-scale originals claim ground area
                    acc.append((org.x - ext.x, org.x + ext.x, org.y - ext.y, org.y + ext.y))
            elif lbl.startswith("AutoFill_B"):
                fills.append(a)

        def _idx(a):
            try:
                return int(a.get_actor_label().rsplit("_", 1)[1])
            except Exception:
                return 9999
        fills.sort(key=_idx)
        n = 0
        for a in fills:
            org, ext = a.get_actor_bounds(False)
            lo, hi, ylo, yhi = org.x - ext.x, org.x + ext.x, org.y - ext.y, org.y + ext.y
            area = max(1.0, (hi - lo) * (yhi - ylo))
            ov = 0.0
            for (l2, h2, y2, Y2) in acc:
                w = min(hi, h2) - max(lo, l2)
                d = min(yhi, Y2) - max(ylo, y2)
                if w > 0 and d > 0:
                    ov = max(ov, w * d / area)
            if ov > 0.25:
                sub.destroy_actor(a)
                n += 1
            else:
                acc.append((lo, hi, ylo, yhi))
        if n:
            unreal.log("fill purge: %d overlapped duplicate building(s) removed" % n)
    except Exception as e:
        unreal.log_warning("fill purge failed: %s" % e)


def _ground_contact_audit(data):
    """Outdoor grounding audit (observed "a few of them up in the sky"): fill items generated/grounded during the asset
    mix-up got shifted support points after mesh repair -> buildings floating 1-4m, props 10m. Mechanism:
    AutoFill_* floating >30cm are all re-grounded by five-point sampling (prefer slight burial, -8cm); OBJ_
    originals only when >200cm AND not semantically hanging (sign/lamp/lantern/meter/light) -- photo-pinned hanging
    objects are never pulled down. Lower only, never lift (negative gap = the half-buried grounding strategy,
    untouched)."""
    if data.get("room"):
        return
    try:
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        prompts = {r["id"]: str(r.get("prompt", "")).lower() for r in (data.get("results") or [])}

        def gz_at(x, y, top_z, reach, ignore_a):
            # Probe rays must start from open AIR above the object's own roof: half-buried items (the -8cm grounding strategy) probed from near their base
            # start inside the terrain body, and complex collision has no backfaces -> silent miss (v1 sank 83 items underground
            # on garbage values); length = building height + 30m (a 50m tower once failed to reach its own base plane)
            hr = unreal.SystemLibrary.line_trace_single(
                world, unreal.Vector(x, y, top_z + 60.0), unreal.Vector(x, y, top_z - reach),
                unreal.TraceTypeQuery.TRACE_TYPE_QUERY1, False, [ignore_a],
                unreal.DrawDebugTrace.NONE, True)
            if hr:
                ip = hr.to_dict().get("impact_point")
                if ip:
                    return float(ip.z)
            return None
        g0 = None                                    # main ground height (flat street fallback)
        for a0 in sub.get_all_level_actors():
            if a0.get_actor_label() == "AutoGround":
                g0 = a0.get_actor_location().z
                break
        n = 0
        n_skip = 0
        for a in sub.get_all_level_actors():
            lbl = a.get_actor_label()
            if not isinstance(a, unreal.StaticMeshActor):
                continue
            is_fill = lbl.startswith("AutoFill_")
            if not (is_fill or lbl.startswith("OBJ_")):
                continue
            if not is_fill:
                try:
                    p = prompts.get(int(lbl[4:]), "")
                except Exception:
                    continue
                if any(k in p for k in ("sign", "lamp", "lantern", "meter", "light")):
                    continue
            org, ext = a.get_actor_bounds(False)
            base, top = org.z - ext.z, org.z + ext.z
            pts = [(org.x, org.y), (org.x + ext.x, org.y), (org.x - ext.x, org.y),
                   (org.x, org.y + ext.y), (org.x, org.y - ext.y)]
            reach = (top - base) + 3000.0
            gzs = [z for z in (gz_at(px, py, top, reach, a) for px, py in pts) if z is not None]
            # Root fix for "neighbor rooftop mistaken for ground", split by scene (audit H1: the old absolute ceiling buried objects on real relief):
            #  - city scenes (city_block ran = flat + cloned towers): an absolute ceiling filters neighbor rooftops; all filtered -> back to street level
            #    (fixes "56m tower reads the 56m neighbor roof as ground, circular reasoning, never descends")
            #  - terrain scenes (lake/forest/hills, no city_block): objects really stand on relief -> trust the probe, never force down
            if data.get("_street_corridor"):
                ceiling = (g0 + 600.0) if g0 is not None else (base + 150.0)
                gzs = [z for z in gzs if z < ceiling]
                if not gzs:
                    if g0 is not None:
                        gzs = [g0 + 8.0]         # all misses / all neighbor roofs -> back to street level
                    else:
                        n_skip += 1
                        continue
            elif not gzs:
                n_skip += 1                      # terrain-scene probe miss -> leave in place, never bury by an absolute plane
                continue
            delta = base - (min(gzs) - 8.0)          # target base = lowest of 5 probes - 8 (prefer slight burial)
            buried = delta < 0                       # base below ground = buried (relief/slope rose above the object's bottom)
            # Asymmetric thresholds: buried -> low threshold to lift (ground objects belong on the ground; observed "house under the ground" = micro-relief/slope buried it);
            # floating -> high threshold (avoid yanking down photo-pinned objects)
            if abs(delta) < (30.0 if is_fill else (15.0 if buried else 200.0)):
                continue
            L0 = a.get_actor_location()
            a.set_actor_location(unreal.Vector(L0.x, L0.y, L0.z - delta), False, False)
            n += 1
        if n or n_skip:
            unreal.log("ground audit: %d actor(s) re-seated, %d skipped (no traceable surface — far skirt)"
                       % (n, n_skip))
    except Exception as e:
        unreal.log_warning("ground audit failed: %s" % e)


def _apply_night_windows(data):
    """R2 night windows: lit window grids on night buildings. AI judges liveliness (night_windows:
    ratio/warm-cool/temperature/nits/clumping); code does the geometry: pick each building's street-facing facade
    -> 3m storey / 2.2m window-pitch grid -> pattern texture -> UNLIT masked panel on the face. Lit windows'
    emissive feeds Lumen back onto the wet street (free night city glow). Day/indoor/no params -> clear and exit
    (idempotent)."""
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in list(sub.get_all_level_actors()):
        if a.get_actor_label().startswith("AutoNightWin_"):
            try:
                sub.destroy_actor(a)
            except Exception:
                pass
    nw = data.get("night_windows") or {}
    env = data.get("environment") or {}
    if not nw or data.get("room") or "night" not in str(env.get("time_of_day", "")).lower():
        return
    try:
        prompts = {r["id"]: str(r.get("prompt", "")).lower() for r in (data.get("results") or [])}

        def is_b(p):
            return any(k in p[:60] for k in ("building", "high-rise", "skyscraper", "apartment", "tower"))
        cor = data.get("_street_corridor")
        if not cor:
            cb = data.get("city_block") or {}
            c = cb.get("corridor") if isinstance(cb, dict) else None
            cor = [float(c[0]) * 100.0, float(c[1]) * 100.0] if c else None
        mid_y = (cor[0] + cor[1]) / 2.0 if cor else 0.0
        world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        ml = unreal.MathLibrary
        faces = []
        for a in sub.get_all_level_actors():
            lbl = a.get_actor_label()
            if not isinstance(a, unreal.StaticMeshActor):
                continue
            if lbl.startswith("OBJ_"):
                try:
                    if not is_b(prompts.get(int(lbl[4:]), "")):
                        continue
                except Exception:
                    continue
            elif not lbl.startswith("AutoFill_B"):
                continue
            sm = a.static_mesh_component.static_mesh
            if sm is None:
                continue
            bb = sm.get_bounds()
            ext, org_l = bb.box_extent, bb.origin
            T = a.get_actor_transform()
            s = a.get_actor_scale3d()
            best = None
            for ax, sign in ((0, 1), (0, -1), (1, 1), (1, -1)):
                lc = unreal.Vector(org_l.x + (ext.x * sign if ax == 0 else 0.0),
                                   org_l.y + (ext.y * sign if ax == 1 else 0.0), org_l.z)
                wc = ml.transform_location(T, lc)
                wn = ml.transform_direction(T, unreal.Vector(
                    float(sign) if ax == 0 else 0.0, float(sign) if ax == 1 else 0.0, 0.0))
                nl = math.hypot(wn.x, wn.y)
                if nl < 1e-4:
                    continue
                nx, ny = wn.x / nl, wn.y / nl
                tx, ty = (wc.x, mid_y) if cor else (0.0, 0.0)
                dl = math.hypot(tx - wc.x, ty - wc.y)
                if dl < 1e-3:
                    continue
                score = (nx * (tx - wc.x) + ny * (ty - wc.y)) / dl
                w_cm = (ext.y * abs(s.y) if ax == 0 else ext.x * abs(s.x)) * 2.0
                h_cm = ext.z * abs(s.z) * 2.0
                if best is None or score > best[0]:
                    best = (score, wc, nx, ny, w_cm, h_cm, dl)
            if best is None or best[0] < 0.2 or best[4] < 250.0 or best[5] < 400.0:
                continue
            # Snap to the real wall (observed 56/70 panels floating with no wall behind): Tripo buildings are not boxes; the AABB face is often 1-3m outside the true wall --
            # probe horizontally from 2.2m outside toward the building center for 4.8m; attach the panel only if THIS building is hit; miss / neighbor hit = skip
            _sc, wc, nx, ny, w_cm, h_cm, dl = best
            hr = unreal.SystemLibrary.line_trace_single(
                world, unreal.Vector(wc.x + nx * 220.0, wc.y + ny * 220.0, wc.z),
                unreal.Vector(wc.x - nx * 260.0, wc.y - ny * 260.0, wc.z),
                unreal.TraceTypeQuery.TRACE_TYPE_QUERY1, False, [], unreal.DrawDebugTrace.NONE, True)
            if not hr:
                continue
            hd = hr.to_dict()
            ha, ip = hd.get("hit_actor"), hd.get("impact_point")
            if ip is None or ha is None or ha.get_actor_label() != lbl:
                continue
            faces.append((dl, lbl, unreal.Vector(ip.x, ip.y, wc.z), nx, ny, w_cm, h_cm))
        faces.sort(key=lambda f: f[0])
        if len(faces) > 70:
            unreal.log("night windows: %d facades beyond cap dropped (keep nearest 70)" % (len(faces) - 70))
            faces = faces[:70]
        if not faces:
            return
        master = _ensure_nightwin_master()
        mel = unreal.MaterialEditingLibrary
        cube = unreal.load_asset("/Engine/BasicShapes/Cube")
        tdir = os.path.join(tempfile.gettempdir(), "auto_nightwin")
        os.makedirs(tdir, exist_ok=True)
        atools = unreal.AssetToolsHelpers.get_asset_tools()
        n, total_on = 0, 0
        for _, lbl, wc, nx, ny, w_cm, h_cm in faces:
            cols = max(1, min(48, int(w_cm / 220.0)))
            rows = max(1, min(30, int(h_cm / 300.0)))
            safe = "".join(ch if ch.isalnum() else "_" for ch in lbl)
            png = os.path.join(tdir, "nw_%s.png" % safe)
            total_on += _nightwin_pattern(png, cols, rows, nw, lbl)
            task = unreal.AssetImportTask()
            task.filename = png
            task.destination_path = _apath("NightWin")
            task.destination_name = "T_NW_" + safe
            task.automated = True
            task.replace_existing = True
            task.save = True
            atools.import_asset_tasks([task])
            tex = unreal.load_asset(_apath("NightWin/T_NW_") + safe)
            if tex is None:
                continue
            mic, _p = _ensure_mic("MI_NW_" + safe, master)
            mel.set_material_instance_texture_parameter_value(mic, "ViewTex", tex)
            mel.set_material_instance_scalar_parameter_value(mic, "Nits", float(nw["nits"]))
            mel.update_material_instance(mic)
            act = sub.spawn_actor_from_object(
                cube, unreal.Vector(wc.x + nx * 7.0, wc.y + ny * 7.0, wc.z))
            act.set_actor_label("AutoNightWin_" + safe)
            act.set_actor_scale3d(unreal.Vector(w_cm * 0.96 / 100.0, 0.022, h_cm * 0.92 / 100.0))
            act.set_actor_rotation(unreal.Rotator(
                pitch=0, yaw=math.degrees(math.atan2(-nx, ny)), roll=0), False)
            act.static_mesh_component.set_material(0, mic)
            try:
                act.static_mesh_component.set_editor_property("cast_shadow", False)
            except Exception as e:
                unreal.log_warning("night win shadow off: %s" % e)
            n += 1
        unreal.log("night windows: %d facades, %d lit cells (lit %.2f warm %.2f %.0fnits cluster %.2f)"
                   % (n, total_on, nw["lit_ratio"], nw["warm_ratio"], nw["nits"], nw["cluster_bias"]))
    except Exception as e:
        unreal.log_warning("night windows failed: %s" % e)


def _apply_sound_sources(data):
    """Soundscape layer B: AI semantic point sources (fridge hum / fireplace crackle / street sound at the window) ->
    spatialized AmbientSound, louder as you approach. Position: objects = half their top height; window = the hole
    center (same wall mapping as the room shell)."""
    srcs = data.get("sound_sources") or []
    if not srcs:
        return
    try:
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        obj_by_id = {o.get("id"): o for o in (data.get("objects") or []) if o.get("location")}
        indoor = bool(data.get("room"))
        fall = 900.0
        if indoor:
            x0, x1, y0, y1 = _room_bounds(data)
            fall = max(600.0, 0.7 * math.hypot(x1 - x0, y1 - y0))
        for i, s in enumerate(srcs):
            url = s.get("url") or ""
            if not url:
                continue
            pos = None
            if s.get("id") == "window" and indoor:
                op = next((o for o in (data["room"].get("openings") or [])
                           if o.get("kind") == "window"), None)
                if op:
                    geo = {"back": ("x", x0, (y1, y0)), "front": ("x", x1, (y0, y1)),
                           "left": ("y", y0, (x0, x1)), "right": ("y", y1, (x1, x0))}
                    axis, plane, (a0, a1) = geo.get(str(op.get("wall", "left")), geo["left"])
                    c = a0 + float(op.get("u_frac", 0.5)) * (a1 - a0)
                    z = float(op.get("sill_m", 0.9)) * 100.0 + float(op.get("h_m", 1.4)) * 50.0
                    pos = (c, plane, z) if axis == "y" else (plane, c, z)
            else:
                fo = obj_by_id.get(s.get("id"))
                if fo:
                    fh = float((fo.get("scale") or [150])[0])
                    pos = (fo["location"][0], fo["location"][1], fo["location"][2] + 0.5 * fh)
            if pos is None:
                continue
            snd = _import_sound(url, "S_AutoSfx_%d" % i)
            if snd is None:
                continue
            a = sub.spawn_actor_from_class(unreal.AmbientSound, unreal.Vector(*pos))
            a.set_actor_label("AutoSfx_%d" % i)
            ac = a.get_component_by_class(unreal.AudioComponent)
            ac.set_editor_property("sound", snd)
            try:
                # AI volume (0.2-1.0) is relative intent; passed straight through it runs quiet (calibration: birds ran too quiet):
                # map 0.3+1.2x -> 0.4->0.78, 1.0->1.5
                ac.set_editor_property("volume_multiplier", 0.3 + 1.2 * float(s.get("volume", 0.5)))
                ac.set_editor_property("allow_spatialization", True)
                ac.set_editor_property("override_attenuation", True)
                att = unreal.SoundAttenuationSettings()
                att.set_editor_property("falloff_distance", fall)
                # default inner radius 400cm = full volume within 4m; indoors the whole walk stays in the full-volume zone (observed "distance change barely audible")
                att.set_editor_property("attenuation_shape_extents", unreal.Vector(60.0, 0.0, 0.0))
                ac.set_editor_property("attenuation_overrides", att)
            except Exception as e:
                unreal.log_warning("sfx %d attenuation: %s" % (i, e))
            unreal.log("sfx %d: '%s' @ (%.0f,%.0f,%.0f) vol=%.2f fall=%.0f" %
                       (i, s.get("sound", ""), pos[0], pos[1], pos[2], float(s.get("volume", 0.5)), fall))
    except Exception as e:
        unreal.log_warning("sound sources failed: %s" % e)


def _apply_music(data):
    """Soundscape: music (Lyria composition) + ambience bed (field-recording style; soundscape layer A) -- two
    non-spatialized looping tracks."""
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for url, label, asset, vol in ((data.get("music") or "", "AutoMusic", "S_AutoMusic", 0.3),
                                   (data.get("ambience") or "", "AutoAmbience", "S_AutoAmbience", 0.9)):
        # music 0.3 (calibration: 0.55 measured very loud) -- music is the bed, not the lead; the ambience bed itself is very quiet, 0.9 stays
        if not url:
            continue
        try:
            snd = _import_sound(url, asset)
            if snd is None:
                unreal.log_warning("%s: import failed" % label)
                continue
            a = sub.spawn_actor_from_class(unreal.AmbientSound, unreal.Vector(0, 0, 200))
            a.set_actor_label(label)
            ac = a.get_component_by_class(unreal.AudioComponent)
            ac.set_editor_property("sound", snd)
            try:
                ac.set_editor_property("is_ui_sound", False)
                ac.set_editor_property("volume_multiplier", vol)   # ambience sits just under the music in overall loudness
                ac.set_editor_property("allow_spatialization", False)
            except Exception:
                pass
            unreal.log("%s: looping (%s) vol=%.2f" % (label, url.split("/")[-1], vol))
        except Exception as e:
            unreal.log_warning("%s failed: %s" % (label, e))


def _ensure_skirt_sky_material():
    """Distant ground-skirt master material M_SkirtSky4: UNLIT world-space radial-gradient emissive.
    The old flat-color slab = a bare olive board + hard seam against the platform + hard horizon = "the scene looks
    like an island" (seen on forest day, still isolated after the fog-luminance fix).
    New: inner ring GroundTex (photo terrain texture, world-tiled at the same 800cm scale as the walking ground) x
    InnerTint -> the texture continues seamlessly off the platform; blends with distance from Center over R0..R1
    into HorizonCol (fog/sky color matched via _match_fog_luminance_to_sky) -> the far skirt sinks into fog = sky
    color, the horizon dissolves with no hard line. Night sets InnerTint=HorizonCol, degrading to the calibrated
    flat slab (night look unchanged)."""
    skp = _apath("M_SkirtSky4")
    if unreal.EditorAssetLibrary.does_asset_exist(skp):
        return unreal.load_asset(skp)
    mel = unreal.MaterialEditingLibrary
    mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        "M_SkirtSky4", _apath(""), unreal.Material, unreal.MaterialFactoryNew())
    mat.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)
    wp = mel.create_material_expression(mat, unreal.MaterialExpressionWorldPosition, -1240, 40)
    mask = mel.create_material_expression(mat, unreal.MaterialExpressionComponentMask, -1060, 40)
    mask.set_editor_property("r", True); mask.set_editor_property("g", True)
    mask.set_editor_property("b", False); mask.set_editor_property("a", False)
    mel.connect_material_expressions(wp, "", mask, "")
    ctr = mel.create_material_expression(mat, unreal.MaterialExpressionVectorParameter, -1060, 220)
    ctr.set_editor_property("parameter_name", "Center")
    ctr.set_editor_property("default_value", unreal.LinearColor(0.0, 0.0, 0.0, 0.0))
    cmask = mel.create_material_expression(mat, unreal.MaterialExpressionComponentMask, -880, 220)
    cmask.set_editor_property("r", True); cmask.set_editor_property("g", True)
    cmask.set_editor_property("b", False); cmask.set_editor_property("a", False)
    mel.connect_material_expressions(ctr, "", cmask, "")
    dist = mel.create_material_expression(mat, unreal.MaterialExpressionDistance, -700, 140)
    mel.connect_material_expressions(mask, "", dist, "A")
    mel.connect_material_expressions(cmask, "", dist, "B")
    r0 = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -700, 280)
    r0.set_editor_property("parameter_name", "R0"); r0.set_editor_property("default_value", 4000.0)
    r1 = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -700, 380)
    r1.set_editor_property("parameter_name", "R1"); r1.set_editor_property("default_value", 28000.0)
    sub = mel.create_material_expression(mat, unreal.MaterialExpressionSubtract, -540, 160)
    mel.connect_material_expressions(dist, "", sub, "A")
    mel.connect_material_expressions(r0, "", sub, "B")
    rng = mel.create_material_expression(mat, unreal.MaterialExpressionSubtract, -540, 320)
    mel.connect_material_expressions(r1, "", rng, "A")
    mel.connect_material_expressions(r0, "", rng, "B")
    div = mel.create_material_expression(mat, unreal.MaterialExpressionDivide, -400, 220)
    mel.connect_material_expressions(sub, "", div, "A")
    mel.connect_material_expressions(rng, "", div, "B")
    sat = mel.create_material_expression(mat, unreal.MaterialExpressionSaturate, -260, 220)
    mel.connect_material_expressions(div, "", sat, "")
    tile = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -1060, -140)
    tile.set_editor_property("parameter_name", "TileCm"); tile.set_editor_property("default_value", 800.0)
    uv = mel.create_material_expression(mat, unreal.MaterialExpressionDivide, -880, -100)
    mel.connect_material_expressions(mask, "", uv, "A")
    mel.connect_material_expressions(tile, "", uv, "B")
    ts = mel.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, -700, -180)
    ts.set_editor_property("parameter_name", "GroundTex")
    try:
        ts.set_editor_property("texture", unreal.load_asset("/Engine/EngineResources/WhiteSquareTexture"))
        ts.set_editor_property("sampler_type", unreal.MaterialSamplerType.SAMPLERTYPE_COLOR)
    except Exception:
        pass
    mel.connect_material_expressions(uv, "", ts, "UVs")
    it = mel.create_material_expression(mat, unreal.MaterialExpressionVectorParameter, -700, 0)
    it.set_editor_property("parameter_name", "InnerTint")
    it.set_editor_property("default_value", unreal.LinearColor(1.0, 1.0, 1.0, 1.0))
    imul = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -480, -80)
    mel.connect_material_expressions(ts, "RGB", imul, "A")
    mel.connect_material_expressions(it, "", imul, "B")
    hcp = mel.create_material_expression(mat, unreal.MaterialExpressionVectorParameter, -480, 80)
    hcp.set_editor_property("parameter_name", "HorizonCol")
    hcp.set_editor_property("default_value", unreal.LinearColor(0.7, 0.75, 0.8, 1.0))
    lerp = mel.create_material_expression(mat, unreal.MaterialExpressionLinearInterpolate, -240, -20)
    mel.connect_material_expressions(imul, "", lerp, "A")
    mel.connect_material_expressions(hcp, "", lerp, "B")
    mel.connect_material_expressions(sat, "", lerp, "Alpha")
    mel.connect_material_property(lerp, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    mel.recompile_material(mat)
    unreal.EditorAssetLibrary.save_asset(skp)
    return mat


def _apply_ground_skirt(env):
    """Distant edge closure: beyond the walkable ground (+invisible walls) lay a large NO-COLLISION visual ground
    extending ~400m into the atmospheric fog. Height fog + SkyAtmosphere aerial perspective fade the far ground
    toward sky color -> no hard edge; wandering to the wall and looking out shows fogged distance, not void.
    Flat non-fading material (distant fade belongs to fog, not material darkening), color from Gemini ground_color,
    2cm lower against z-fighting. Indoors: skip."""
    try:
        eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        acts = eas.get_all_level_actors()
        g = next((a for a in acts if a.get_actor_label() == "AutoGround"), None)
        if g is None:
            return                                          # no fallback ground (indoor) -> skip
        gl = g.get_actor_location()
        mel = unreal.MaterialEditingLibrary
        # prefer the photo-matched ground texture (terrain albedo) -> flat street scenes get real ground texture too; else fall back to flat color (Tint)
        terr_tex = unreal.load_asset(_aipath("Terrain/T_AutoTerrainA"))
        mic, _p = _ensure_mic("MI_GroundFlat", _ensure_ground_flat_material(terr_tex))
        if terr_tex is not None:
            mel.set_material_instance_texture_parameter_value(mic, "GroundTex", terr_tex)
            mel.set_material_instance_vector_parameter_value(mic, "Tint", unreal.LinearColor(1.0, 1.0, 1.0, 1.0))
        else:
            col = (env or {}).get("ground_color") or [0.18, 0.18, 0.19]
            dark = (1.0 - 0.3 * float((env or {}).get("ground_wetness", 0.0))) * GROUND_DIM
            mel.set_material_instance_vector_parameter_value(
                mic, "Tint", unreal.LinearColor(col[0] * dark, col[1] * dark, col[2] * dark, 1.0))
        unreal.EditorAssetLibrary.save_asset(_p)
        # Walkable flat ground also uses the flat material: M_GroundV2's center->edge darkening drew a black ring at the footprint edge that fog cannot hide (too close);
        # with the distant skirt + fog closing the edge, that radial darkening is obsolete -> walking ground matches the skirt seamlessly. On terrain scenes AutoGround is hidden under the terrain; no effect.
        if g.static_mesh_component.is_visible():
            g.static_mesh_component.set_material(0, mic)
        # Distant ground skirt (800m): M_SkirtSky4 radial gradient (see _ensure_skirt_sky_material) -- inner ring continues the terrain texture, outer melts into fog/sky color.
        # Gain must survive scene exposure (like the sky sphere's SkyBoost, else daytime -10EV crushes the emissive to black) -> read the PPV exposure bias and compute the same way.
        _sbias = -1.2
        try:
            _spv = next((a for a in eas.get_all_level_actors() if isinstance(a, unreal.PostProcessVolume)), None)
            if _spv: _sbias = float(_spv.get_editor_property("settings").get_editor_property("auto_exposure_bias"))
        except Exception: pass
        _hc = (env or {}).get("horizon_color") or (env or {}).get("fog_color") or (env or {}).get("sky_color") or [0.60, 0.66, 0.74]
        _tod = str((env or {}).get("time_of_day", "")).lower()
        _sk_tex = None
        if "night" in _tod or _sbias < -5.0:      # judge night by time_of_day (observed rain-alley night bias only -2.8; exposure alone misses it)
            # Night (rain-alley test): unlit emissive does not dim with the night; high gain makes the skirt glow ~50x brighter than the lit night ground = a stark white slab.
            # Night horizons are not bright -> the skirt goes ground-color dominant and heavily dimmed (0.04, calibrated over two alley rounds); same color inside and out = a fixed flat slab, night look unchanged.
            _gc = (env or {}).get("ground_color") or [0.20, 0.20, 0.22]
            _nb = 0.04 * (2.0 ** (-_sbias))
            _sk_in = [(0.70 * _gc[i] + 0.30 * _hc[i]) * _nb for i in range(3)]
            _sk_far = list(_sk_in)
        else:
            _sboost = 0.75 * (2.0 ** (-_sbias)) * 0.6
            if bool(((env or {}).get("distant_city") or {}).get("present")):
                _sk_in = [_hc[i] * _sboost for i in range(3)]      # city: keep horizon haze color under the distant buildings (the skyline closes the edge itself)
            elif terr_tex is not None:
                _sk_tex = terr_tex                                  # natural daytime: continue the photo terrain texture (the texture carries the ground color -> grey gain)
                _sk_in = [_sboost * 0.6] * 3                        # 0.6 calibrated on forest: the unlit skirt receives no canopy shadow, full gain ran one stop bright
            else:
                _gc = (env or {}).get("ground_color") or [0.30, 0.34, 0.22]
                _sk_in = [(0.45 * _gc[i] + 0.55 * _hc[i]) * _sboost for i in range(3)]
            # The skirt's far color matches the fog standard (_match_fog_luminance_to_sky, same formula: SkyBoost x sky_luminance x 0.6) ->
            # gradient endpoint = fog color = sky color, the horizon dissolves; without an AutoSky HDRI (physical sky) fall back to old horizon color x gain.
            _fb = 1.0
            try:
                _sky = next((a for a in acts if a.get_actor_label() == "AutoSky"), None)
                _smi = _sky.static_mesh_component.get_material(0) if _sky else None
                if _smi is not None and "AutoSkyHDRI" in str(_smi.get_name()):
                    _sb = float(mel.get_material_instance_scalar_parameter_value(_smi, "SkyBoost"))
                    _fb = max(1.0, _sb * max(0.05, float((env or {}).get("sky_luminance", 0.5))) * 0.6)
            except Exception: pass
            _fcol = (env or {}).get("fog_color") or _hc
            _sk_far = [_fcol[i] * max(_fb, _sboost) for i in range(3)]
        _r0 = 3000.0                              # gradient start = platform edge (the inner texture ring only needs to cover the seam area)
        _ctr = (gl.x, gl.y)
        try:
            _terr = next((a for a in acts if a.get_actor_label() == "AutoTerrain"), None)
            if _terr is not None:
                _to, _te = _terr.get_actor_bounds(False)
                _r0 = max(float(_te.x), float(_te.y)) * 0.9
                _ctr = (float(_to.x), float(_to.y))   # gradient center = platform center (the AutoGround origin may be offset from the platform)
        except Exception: pass
        mic_sk, _skp = _ensure_mic("MI_SkirtSky", _ensure_skirt_sky_material())
        mel.set_material_instance_vector_parameter_value(mic_sk, "Center", unreal.LinearColor(_ctr[0], _ctr[1], 0.0, 0.0))
        mel.set_material_instance_scalar_parameter_value(mic_sk, "R0", _r0)
        mel.set_material_instance_scalar_parameter_value(mic_sk, "R1", _r0 + 24000.0)   # pure fog/sky color after a ~240m gradient band
        mel.set_material_instance_vector_parameter_value(mic_sk, "InnerTint", unreal.LinearColor(_sk_in[0], _sk_in[1], _sk_in[2], 1.0))
        mel.set_material_instance_vector_parameter_value(mic_sk, "HorizonCol", unreal.LinearColor(_sk_far[0], _sk_far[1], _sk_far[2], 1.0))
        if _sk_tex is not None:
            mel.set_material_instance_texture_parameter_value(mic_sk, "GroundTex", _sk_tex)
        unreal.EditorAssetLibrary.save_asset(_skp)
        plane = unreal.load_asset("/Engine/BasicShapes/Plane")
        far = next((a for a in acts if a.get_actor_label() == "AutoGroundFar"), None)
        if far is None:
            far = eas.spawn_actor_from_object(plane, unreal.Vector(gl.x, gl.y, gl.z - 2.0))
            far.set_actor_label("AutoGroundFar")
        far.set_actor_location(unreal.Vector(gl.x, gl.y, gl.z - 2.0), False, False)   # just below the walking ground
        far.set_actor_scale3d(unreal.Vector(800.0, 800.0, 1.0))    # ~800m diameter, far beyond the footprint -> distance extends into the fog
        fc = far.static_mesh_component
        fc.set_static_mesh(plane)
        fc.set_material(0, mic_sk)
        try:
            fc.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)   # visual only, does not block the player (invisible walls still guard the footprint edge)
        except Exception:
            pass
        far.set_actor_enable_collision(False)
        unreal.log("ground skirt: ~800m far ground for distance haze (远景收边)")
    except Exception as e:
        unreal.log_warning("ground skirt failed: %s" % e)


SKY_HDRI_MAT = "/Game/Auto/M_SkyHDRI"


def _ensure_sky_hdri_material(default_tex):
    """Unlit two-sided sky material using the SkyTex texture as emissive color (wraps the panorama onto AutoSky's inner
    wall)."""
    if unreal.EditorAssetLibrary.does_asset_exist(SKY_HDRI_MAT):
        return unreal.load_asset(SKY_HDRI_MAT)
    atools = unreal.AssetToolsHelpers.get_asset_tools()
    mat = atools.create_asset("M_SkyHDRI", _apath(""), unreal.Material, unreal.MaterialFactoryNew())
    mat.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)
    mat.set_editor_property("two_sided", True)
    # IsSky: realtime skylight capture only recognizes SkyAtmosphere/VolumetricCloud/IsSky meshes -> unflagged means the skylight captures black -> whole scene black (engine warning observed)
    try:
        mat.set_editor_property("is_sky", True)
    except Exception:
        pass
    mel = unreal.MaterialEditingLibrary
    ts = mel.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, -500, 0)
    ts.set_editor_property("parameter_name", "SkyTex")
    if default_tex:
        try:
            ts.set_editor_property("texture", default_tex)
        except Exception:
            pass
    # Emissive gain SkyBoost: an 8bit LDR panorama's "night sky" face value is dim (~0.19); night physical exposure (-1.2EV) crushes it to black
    # (observed "still no hdri sky" = actually crushed by exposure). Multiply a gain so the dark sky reads under scene exposure; per scene by time of day (see _apply_hdri).
    boost = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -500, 220)
    boost.set_editor_property("parameter_name", "SkyBoost")
    boost.set_editor_property("default_value", 1.0)
    mul = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -250, 0)
    mel.connect_material_expressions(ts, "RGB", mul, "A")
    mel.connect_material_expressions(boost, "", mul, "B")
    mel.connect_material_property(mul, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    mel.recompile_material(mat)
    unreal.EditorAssetLibrary.save_asset(SKY_HDRI_MAT)
    return mat


def _match_fog_luminance_to_sky(env):
    """Match height-fog luminance to the AI sky dome (a generic-judgment fix).
    Fog scatter color is a 0..1 face value while the AI dome (unlit emissive) is boosted to ~c*2^(-bias); by day
    they differ by 3 orders of magnitude -> distant ground/skirt dragged toward near-black, a black gradient band
    on the backlit horizon (observed forest day; the sun side is masked by glare).
    Fog luminance = Gemini fog color x effective dome brightness (SkyBoost x sky_luminance ~= c*2^-bias) x 0.6 (fog
    reads as haze, slightly darker than the sky).
    At night the formula ~= x1 -> the calibrated night-street look is unchanged; without an AutoSky HDRI dome
    (indoor/physical sky) do nothing, keep the 0..1 fog color.
    Also set a 4km fog cutoff: the dome sits at 10km and must never be fogged (fog should only act on in-scene
    geometry; the dome itself already is "the sky behind the fog")."""
    try:
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        sky = next((a for a in sub.get_all_level_actors() if a.get_actor_label() == "AutoSky"), None)
        if sky is None:
            return
        mi = sky.static_mesh_component.get_material(0)
        if mi is None or "AutoSkyHDRI" not in str(mi.get_name()):
            return
        boost = float(unreal.MaterialEditingLibrary.get_material_instance_scalar_parameter_value(mi, "SkyBoost"))
        lum = float((env or {}).get("sky_luminance", 0.5))
        scale = max(1.0, boost * max(0.05, lum) * 0.6)
        col = (env or {}).get("fog_color") or [0.6, 0.7, 0.85]
        fog = next((a for a in sub.get_all_level_actors() if isinstance(a, unreal.ExponentialHeightFog)), None)
        if fog is None:
            return
        fc = fog.get_component_by_class(unreal.ExponentialHeightFogComponent)
        lc = unreal.LinearColor(col[0] * scale, col[1] * scale, col[2] * scale, 1.0)
        for prop in ("fog_inscattering_color", "fog_inscattering_luminance", "inscattering_color"):
            try:
                fc.set_editor_property(prop, lc); break
            except Exception:
                continue
        try:
            fc.set_editor_property("fog_cutoff_distance", 400000.0)
        except Exception:
            pass
        unreal.log("fog inscattering matched to sky (x%.0f)" % scale)
    except Exception as e:
        unreal.log_warning("fog-sky luminance match skipped: %s" % e)


def _apply_hdri(hdri_url, env=None):
    """Wrap the sky panorama onto the AutoSky sphere's inner wall (self-contained, no HDRIBackdrop plugin needed).
    For a real .hdr, rotate the sphere by the AI-reported light azimuth so its moon/sun aligns with Gemini's light
    direction (shadows agree with the moon)."""
    if not hdri_url:
        return
    try:
        ext = os.path.splitext(hdri_url)[1].lower() or ".png"   # a real .hdr must not be saved as .png before import (the factory dispatches by extension)
        local = os.path.join(tempfile.gettempdir(), "auto_hdri" + ext)
        with open(local, "wb") as f:
            f.write(_get(SERVER + hdri_url))
        task = unreal.AssetImportTask()
        task.filename = local
        task.destination_path = _aipath("HDRI")
        task.destination_name = "T_AutoHDRI"
        task.automated = True
        task.replace_existing = True
        task.save = True
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
        tex = unreal.load_asset(_aipath("HDRI/T_AutoHDRI"))
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
        # Emissive gain SkyBoost must track SCENE EXPOSURE: the sky sphere is unlit emissive; night exposure ~-1.2 vs day ~-10.4 differ ~9 stops,
        # so a fixed gain goes all-black in daylight (observed forest day black sky). Read the exposure bias _apply_grade (already run) just set on the PPV,
        # boost = C*2^(-bias): night (bias~-1.2) -> ~8 (locked by test), day (bias~-10.4) -> ~5000. Gemini sky_luminance fine-tunes perceptually.
        _bias = -1.2
        try:
            _sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
            _ppv = next((a for a in _sub.get_all_level_actors() if isinstance(a, unreal.PostProcessVolume)), None)
            if _ppv:
                _bias = float(_ppv.get_editor_property("settings").get_editor_property("auto_exposure_bias"))
        except Exception:
            pass
        # boost = target on-screen brightness / (sky texture face value x 2^bias); face value ~= sky_luminance (night dark ~0.2 / day bright ~0.85).
        # Night (bias -1.2) -> ~8 (locked); day (bias -10.4) with the old constant 0.75 gave ~1160 = grey sky (observed) -> day constant raised to 2.3 for ~3500 = blue sky, night unchanged.
        lum = float((env or {}).get("sky_luminance", 0.5))
        _c = 2.3 if _bias < -5.0 else 0.75    # deep negative day exposure needs the higher gain for a blue sky (0.75 -> grey sky); night keeps 0.75
        sky_boost = max(2.0, min(6000.0, (_c / max(0.05, lum)) * (2.0 ** (-_bias))))
        try:
            unreal.MaterialEditingLibrary.set_material_instance_scalar_parameter_value(mic, "SkyBoost", sky_boost)
        except Exception:
            pass
        sky.static_mesh_component.set_material(0, mic)
        sky.static_mesh_component.set_collision_profile_name("NoCollision")      # belt and braces: after HDRI too, the sky sphere stays zero-collision, never blocks spawn points
        sky.static_mesh_component.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)
        sky.set_actor_enable_collision(False)
        # HDRI (real .hdr or AI-generated png) is the sky -> the visible sphere takes over the dome; _apply_sky_atmosphere sees this material and stops hiding it.
        sky.static_mesh_component.set_visibility(True)
        sky.set_actor_hidden_in_game(False)
        if hdri_url.lower().endswith((".hdr", ".exr")):
            lf = (env or {}).get("hdri_light_frac")
            if lf is not None:                              # real HDRI: rotate the sphere so its own moon/sun aligns with Gemini's light direction
                yaw_ = float((env or {}).get("sun_yaw_deg", 135.0)) + 180.0 - float(lf) * 360.0
                sky.set_actor_rotation(unreal.Rotator(pitch=0.0, yaw=yaw_, roll=0.0), False)
                unreal.log("hdri: dome rotated yaw=%.0f (light_frac %.2f)" % (yaw_, float(lf)))
            unreal.log("hdri: REAL .hdr sky dome VISIBLE (takes over the sky)")
        else:
            unreal.log("hdri: AI panorama sky dome VISIBLE (AI-fabricated sky)")
        unreal.EditorAssetLibrary.save_asset(mic_path)
        unreal.log("hdri: photo sky panorama applied to AutoSky")
        _match_fog_luminance_to_sky(env)         # re-match fog luminance after SkyBoost is set (else the daytime backlit horizon shows a black band)
    except Exception as e:
        unreal.log_error("apply hdri failed: %s" % e)


_LAST_CAP_CAM = None      # last critique-screenshot camera position (reference frame for fill-light 'side' prescriptions)


def _capture_view(data):
    """Auto-render the reconstruction from a high 3/4 angle with SceneCapture2D (framing all objects, avoiding the
    empty low-angle foreground): SceneCapture carries its own post/exposure, bypassing the all-white editor
    viewport screenshots. 8bit RenderTarget -> PNG -> uploaded to the backend."""
    if not CAPTURE_VIEW:
        return
    try:
        task_id = data.get("task_id") or "latest"
        cam = data.get("camera", {})
        env = data.get("environment", {})
        ev = float(env.get("exposure_ev", 0.0))
        asp = float(data.get("img_aspect", 1.333))
        W = int(CAPTURE_W)
        H = int(round(W / max(0.5, asp)))
        world = unreal.EditorLevelLibrary.get_editor_world()
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        if data.get("is_indoor") and data.get("room"):
            # indoors: critique render = same camera as the photo (a 3/4 bird's-eye above the roof is useless indoors) -> lighting is directly A/B-able
            loc = unreal.Vector(0.0, 0.0, max(60.0, float(cam.get("height_m", 1.5)) * 100.0))
            rot = unreal.Rotator(pitch=float(cam.get("pitch_deg", 0.0)), yaw=0.0, roll=0.0)
            fov = float(cam.get("fov_deg", 65.0))
        else:
            # auto 3/4 framing: take all OBJ_ centers + radius, pull back from high-oblique far enough to frame them all
            ps = [a.get_actor_location() for a in sub.get_all_level_actors()
                  if a.get_actor_label().startswith("OBJ_")]
            if not ps:                                   # pure terrain (no objects): frame the AutoTerrain bounds
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
            az, elev = math.radians(38.0), math.radians(18.0)                 # oblique 38 deg, pitch down 18 (flatter view, less dark ground)
            loc = unreal.Vector(cx - fit * math.cos(elev) * math.cos(az),
                                cy - fit * math.cos(elev) * math.sin(az),
                                cz + fit * math.sin(elev))
            dx, dy, dz = cx - loc.x, cy - loc.y, cz - loc.z
            rot = unreal.Rotator(pitch=math.degrees(math.atan2(dz, (dx * dx + dy * dy) ** 0.5)),
                                 yaw=math.degrees(math.atan2(dy, dx)), roll=0.0)
        global _LAST_CAP_CAM
        _LAST_CAP_CAM = (loc.x, loc.y, loc.z)     # reference frame for critique fill prescriptions (front/back/left/right are relative to this camera)
        rt = unreal.RenderingLibrary.create_render_target2d(world, W, H, unreal.TextureRenderTargetFormat.RTF_RGBA8)
        cap = sub.spawn_actor_from_class(unreal.SceneCapture2D, loc, rot)
        cap.set_actor_label("AutoCapture")        # Auto prefix -> swept by the next run's entry cleanup
        comp = cap.get_component_by_class(unreal.SceneCaptureComponent2D)
        comp.set_editor_property("capture_every_frame", False)   # capture on demand only, not every frame
        comp.set_editor_property("capture_on_movement", False)
        try:
            # key: without persisted render state SceneCapture ignores exposure settings (blown white in physical range; isolated repro)
            comp.set_editor_property("always_persist_rendering_state", True)
        except Exception:
            pass
        comp.set_editor_property("capture_source", unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR)
        comp.set_editor_property("texture_target", rt)
        comp.set_editor_property("fov_angle", fov)
        comp.set_editor_property("post_process_blend_weight", 1.0)
        pp = comp.get_editor_property("post_process_settings")
        # render exposure/saturation/contrast all come from Gemini's solved env (exposure_ev/saturation/contrast) -> works for any photo, no magic numbers.
        sat = min(1.15, float(env.get("saturation", 1.0)))   # same convergence cap as _apply_grade
        con = float(env.get("contrast", 1.0))
        if _extended_luminance():
            _set_physical_exposure(pp, env, ev, channel="capture")   # the capture channel has its own night calibration (LE responds differently)
        else:
            pp.set_editor_property("override_auto_exposure_bias", True)
            pp.set_editor_property("auto_exposure_bias", ev)                 # ev = Gemini exposure_ev (matches photo brightness)
            # the legacy-range anti-blowout clamp; in physical range clamping 0.03-1.0 locks daytime at night levels (the dark-capture root cause)
            pp.set_editor_property("override_auto_exposure_min_brightness", True)
            pp.set_editor_property("auto_exposure_min_brightness", 0.03)
            pp.set_editor_property("override_auto_exposure_max_brightness", True)
            pp.set_editor_property("auto_exposure_max_brightness", 1.0)
        pp.set_editor_property("override_auto_exposure_speed_up", True)
        pp.set_editor_property("auto_exposure_speed_up", 100.0)
        pp.set_editor_property("override_auto_exposure_speed_down", True)
        pp.set_editor_property("auto_exposure_speed_down", 100.0)
        pp.set_editor_property("override_color_saturation", True)
        pp.set_editor_property("color_saturation", unreal.Vector4(sat, sat, sat, 1.0))   # Gemini saturation
        pp.set_editor_property("override_color_contrast", True)
        pp.set_editor_property("color_contrast", unreal.Vector4(con, con, con, 1.0))     # Gemini contrast
        comp.set_editor_property("post_process_settings", pp)
        for _ in range(3):                          # grab a few frames so exposure settles
            comp.capture_scene()
        od = unreal.Paths.convert_relative_path_to_full(
            os.path.join(unreal.Paths.project_saved_dir(), "AutoCapture"))
        unreal.RenderingLibrary.export_render_target(world, rt, od, "ue_view.png")   # 8bit → PNG
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


def _capture_png(loc, rot, fov, env, out_dir, fname, W=1280, H=720, every_frame=False):
    """Render a PNG from any camera (reuses the _capture_view recipe: persisted state + physical metering).
    every_frame=True: resident mode -- only places the camera, no export (left capturing every frame); the engine
    ticks freely and the export stage collects later.
    Why (engine-measured): a one-shot capture_scene renders no Niagara particles; a resident camera capturing
    during normal frames includes them."""
    world = unreal.EditorLevelLibrary.get_editor_world()
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    ev = float(env.get("exposure_ev", 0.0))
    rt = unreal.RenderingLibrary.create_render_target2d(world, W, H, unreal.TextureRenderTargetFormat.RTF_RGBA8)
    cap = sub.spawn_actor_from_class(unreal.SceneCapture2D, loc, rot)
    cap.set_actor_label("AutoCaptureFx@" + fname if every_frame else "AutoCaptureFx")
    comp = cap.get_component_by_class(unreal.SceneCaptureComponent2D)
    comp.set_editor_property("capture_every_frame", bool(every_frame))
    comp.set_editor_property("capture_on_movement", False)
    try:
        comp.set_editor_property("always_persist_rendering_state", True)
    except Exception:
        pass
    try:
        # engine-measured: the capture channel's particle show-flag may be off -> no Niagara drawn (particles exist / viewport shows them / capture empty)
        comp.set_editor_property("show_flag_settings", [
            unreal.EngineShowFlagsSetting(show_flag_name="Particles", enabled=True)])
    except Exception as e:
        unreal.log_warning("showflag: %s" % e)
    comp.set_editor_property("capture_source", unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR)
    comp.set_editor_property("texture_target", rt)
    comp.set_editor_property("fov_angle", fov)
    comp.set_editor_property("post_process_blend_weight", 1.0)
    pp = comp.get_editor_property("post_process_settings")
    sat = min(1.15, float(env.get("saturation", 1.0)))
    con = float(env.get("contrast", 1.0))
    if _extended_luminance():
        _set_physical_exposure(pp, env, ev, channel="capture")
    else:
        pp.set_editor_property("override_auto_exposure_bias", True)
        pp.set_editor_property("auto_exposure_bias", ev)
    pp.set_editor_property("override_color_saturation", True)
    pp.set_editor_property("color_saturation", unreal.Vector4(sat, sat, sat, 1.0))
    pp.set_editor_property("override_color_contrast", True)
    pp.set_editor_property("color_contrast", unreal.Vector4(con, con, con, 1.0))
    comp.set_editor_property("post_process_settings", pp)
    if every_frame:
        unreal.log("fx cam placed (persistent): %s" % fname)
        return
    for _ in range(3):
        comp.capture_scene()
    unreal.RenderingLibrary.export_render_target(world, rt, out_dir, fname)
    try:
        sub.destroy_actor(cap)
    except Exception:
        pass
    unreal.log("fx shot: %s/%s" % (out_dir, fname))


def _fx_export_caps(out_dir):
    """Export all resident FX cameras' render targets (they already captured particle-inclusive frames during free
    ticking), then clean them up."""
    world = unreal.EditorLevelLibrary.get_editor_world()
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    n = 0
    for a in list(sub.get_all_level_actors()):
        lbl = a.get_actor_label()
        if not lbl.startswith("AutoCaptureFx@"):
            continue
        fname = lbl.split("@", 1)[1]
        comp = a.get_component_by_class(unreal.SceneCaptureComponent2D)
        rt = comp.get_editor_property("texture_target")
        if rt:
            unreal.RenderingLibrary.export_render_target(world, rt, out_dir, fname)
            unreal.log("fx shot exported: %s/%s" % (out_dir, fname))
            n += 1
        try:
            sub.destroy_actor(a)
        except Exception:
            pass
    unreal.log("fx export: %d shots" % n)


def _fx_shots(data):
    """Eye-level screenshot set for particle tuning. Cameras must sit in OPEN ground (the center is usually occupied by
    objects; a whole set was once shot from under an awning): prefer pocket points (already rejection-sampled away
    from object footprints), else the quadrant corner farthest from objects. Backlit shots offset 30 deg off-axis
    (never staring into the sun disc)."""
    env = data.get("environment", {})
    terr = data.get("terrain") or {}
    cx = float(terr.get("cx_m", 0.0)) * 100.0
    cy = float(terr.get("cy_m", 0.0)) * 100.0
    hf = float(terr.get("half_fwd_m", 30.0)) * 100.0
    hl = float(terr.get("half_lat_m", 30.0)) * 100.0
    objs = [(o.get("location") or [0, 0, 0], max(200.0, 0.6 * float((o.get("scale") or [100])[0])))
            for o in (data.get("objects") or [])]
    def _clear(x, y):
        return all((x - L0[0]) ** 2 + (y - L0[1]) ** 2 > r0 * r0 for L0, r0 in objs)
    cand = []
    spots_all = []
    for L in (data.get("effects") or []):
        if L.get("primitive") == "niagara":
            ss = L.get("niagara", {}).get("spots") or []
            spots_all.extend(ss)
            if "(accent)" in (L.get("name") or ""):
                cand.extend(ss)
    cand.extend(s for s in spots_all if s not in cand)
    cand.extend([[cx + sx * hf * 0.5, cy + sy * hl * 0.5, 0] for sx in (-1, 1) for sy in (-1, 1)])
    px, py = cx, cy
    for s in cand:
        if _clear(s[0], s[1]):
            px, py = float(s[0]), float(s[1])
            break
    eye = _terrain_surface_cm(terr, px, py) + 170.0
    pos = unreal.Vector(px, py, eye)
    sun_yaw = float(env.get("sun_yaw_deg", 135.0))
    out_dir = os.environ.get("UE_FX_SHOT_DIR", "").strip() or unreal.Paths.convert_relative_path_to_full(
        os.path.join(unreal.Paths.project_saved_dir(), "AutoCapture"))
    poses = [("backlit", unreal.Rotator(pitch=6.0, yaw=sun_yaw + 30.0, roll=0.0)),
             ("frontlit", unreal.Rotator(pitch=6.0, yaw=sun_yaw + 180.0, roll=0.0))]
    near = None
    for s in spots_all:
        d2 = (s[0] - px) ** 2 + (s[1] - py) ** 2
        if d2 > 250.0 ** 2 and (near is None or d2 < near[0]):
            near = (d2, s)
    if near:
        yaw = math.degrees(math.atan2(near[1][1] - py, near[1][0] - px))
        poses.append(("accent", unreal.Rotator(pitch=2.0, yaw=yaw, roll=0.0)))
    stage = os.environ.get("UE_FX_STAGE", "place").strip()
    if stage == "export":
        _fx_export_caps(out_dir)
        return
    # place: clear old resident cameras, add the new set (capturing every frame); the engine ticks freely for seconds, then the export stage collects
    sub2 = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in list(sub2.get_all_level_actors()):
        if a.get_actor_label().startswith("AutoCaptureFx@"):
            try:
                sub2.destroy_actor(a)
            except Exception:
                pass
    for name, rot in poses:
        _capture_png(pos, rot, 70.0, env, out_dir, "fx_%s.png" % name, every_frame=True)
    unreal.log("fx cams placed: %d poses @(%.0f,%.0f)" % (len(poses), px, py))


def _snap_actor_to_terrain(actor, terr):
    """Authoritative grounding (same math as placement): five-point sample of the true world bounds, take the lowest;
    base = lowest surface - slight sink."""
    try:
        org_b, ext_b = actor.get_actor_bounds(False)
        pts5 = [(org_b.x, org_b.y)] + [
            (org_b.x + dx_ * ext_b.x * 0.6, org_b.y + dy_ * ext_b.y * 0.6)
            for dx_, dy_ in ((-1, -1), (1, -1), (-1, 1), (1, 1))]
        gz = min(_terrain_surface_cm(terr, px_, py_) for px_, py_ in pts5)
        bottom = org_b.z - ext_b.z
        sink = min(30.0, max(2.0, 0.03 * ext_b.z))
        aloc = actor.get_actor_location()
        actor.set_actor_location(unreal.Vector(aloc.x, aloc.y, aloc.z + (gz - bottom) - sink), False, False)
    except Exception as e:
        unreal.log_warning("snap to terrain: %s" % e)


def _ensure_bulb_mat():
    """Emissive bulb master material (bridge-written once): unlit emissive, Color vector param -> MIDs tinted per
    practical-light color."""
    path = "/Game/Tool/FX/Sprites/M_FxBulb"
    m = unreal.load_asset(path)
    if m:
        return m
    try:
        atools = unreal.AssetToolsHelpers.get_asset_tools()
        m = atools.create_asset("M_FxBulb", "/Game/Tool/FX/Sprites", unreal.Material, unreal.MaterialFactoryNew())
        m.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)
        mel = unreal.MaterialEditingLibrary
        vp = mel.create_material_expression(m, unreal.MaterialExpressionVectorParameter, -400, 0)
        vp.set_editor_property("parameter_name", "Color")
        vp.set_editor_property("default_value", unreal.LinearColor(30.0, 22.0, 12.0, 1.0))
        mel.connect_material_property(vp, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
        mel.recompile_material(m)
        unreal.EditorAssetLibrary.save_asset(path)
        return m
    except Exception as e:
        unreal.log_warning("bulb material failed: %s" % e)
        return None


def _apply_night_lights(env, terrain, objs, practicals):
    """Night lighting (goal: more light sources to fix dark areas + visible glow spots):
    (1) fill light (procedural): one large-radius SHADOWLESS cool fill opposite the moon -> detail on moon-away
        faces without killing the night;
    (2) practicals (AI-inferred): Gemini judges which objects would emit (warm building windows/bulbs/fire) ->
        warm point lights + emissive bulb spheres."""
    tod = str(env.get("time_of_day", "")).lower()
    if "night" not in tod and tod not in ("dusk", "dawn"):
        return
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    locs = [o.get("location") for o in (objs or []) if o.get("location")]
    if not locs:
        return
    cx = sum(p[0] for p in locs) / len(locs)
    cy = sum(p[1] for p in locs) / len(locs)
    # NO procedural fill-light pool (the anti-moon fill / bounce pool was an overreach, deleted). Visible lighting decisions belong to the AI;
    # but the ambient EXECUTOR must actually work: the skylight live-captures a night sky of ~0 radiance, and any multiple of 0 is 0
    # -> shadowed areas dead black, AI ambient_delta moot. Replace with "sky-dome fill": one SHADOWLESS directional light approximating
    # the whole dome's ambient contribution -- uniform, zero pooling, illuminance = 10% of moon lux (precise and controllable),
    # AI ambient_delta scales its intensity (_apply_adjustments mirrors the scaling).
    lux_ = float(env.get("sun_intensity_lux", 150.0))
    moon_yaw_d = float(env.get("sun_yaw_deg", 135.0))
    skyfill = sub.spawn_actor_from_class(
        unreal.DirectionalLight, unreal.Vector(cx, cy, _terrain_surface_cm(terrain, cx, cy) + 3000.0))
    skyfill.set_actor_label("AutoLightSkyFill")
    skyfill.set_actor_rotation(unreal.Rotator(pitch=-55.0, yaw=moon_yaw_d + 180.0, roll=0.0), False)
    sfc = skyfill.get_component_by_class(unreal.DirectionalLightComponent)
    amb_ratio = float(env.get("ambient_ratio", 0.10))           # AI reads "shadow-region relative brightness" off the photo
    # City-glow term (observed rainy night street "dead black beyond the streetlamps"): with moon 0, ambient != 0 -- city night-sky light pollution
    # rides on AI sky_intensity. Coefficient measured: x10 (0.5 fallback -> 5 lux) turned night streets into snowfields;
    # x2 (-> 1 lux) matches the photo -- ground faintly visible, lamp pools distinct (2026-07-02 night-street bisection)
    city_glow = 2.0 * float(env.get("sky_intensity", 0.0))
    fill_lux = max(0.5, min(40.0, max(amb_ratio * lux_, city_glow)))
    sfc.set_editor_property("intensity", fill_lux)
    sfc.set_editor_property("cast_shadows", False)
    try:
        sfc.set_editor_property("atmosphere_sun_light", False)      # do not usurp the atmosphere sun's (moon's) slot
    except Exception:
        pass
    try:
        sfc.set_editor_property("forward_shading_priority", 0)      # key light = moon (10); the fill must not compete for forward/translucency/volumetric fog
        sfc.set_editor_property("volumetric_scattering_intensity", 0.0)   # the fill adds no fog glow (surface fill only)
    except Exception:
        pass
    sfc.set_light_color(unreal.LinearColor(0.55, 0.66, 1.0, 1.0))
    unreal.log("sky-fill directional: %.1f lux (moon %.0fx%.2f vs city-glow %.1f)"
               % (fill_lux, lux_, amb_ratio, city_glow))
    bulb_mat = _ensure_bulb_mat() if practicals else None
    obj_by_id = {o.get("id"): o for o in (objs or [])}
    n = 0
    for p in (practicals or []):
        o = obj_by_id.get(p.get("object_id"))
        if not o or not o.get("location"):
            continue
        loc = o["location"]
        a_ = next((a for a in sub.get_all_level_actors()
                   if a.get_actor_label() == "OBJ_%02d" % o["id"]), None)
        if a_ is None:
            continue
        org_, ext_ = a_.get_actor_bounds(False)
        col = p.get("color") or [1.0, 0.75, 0.45]
        hz = org_.z - ext_.z + 2.0 * ext_.z * float(p.get("height_frac", 0.4))
        # Mount lights on the REAL model surface: horizontal ray from outside the bounds toward the object center; hit point + normal offset = mount point.
        # (axis-aligned bounds inflate under rotation -> face centers float in air, observed twice)
        ang = math.atan2(cy - loc[1], cx - loc[0])
        dx_, dy_ = math.cos(ang), math.sin(ang)
        if abs(dx_) >= abs(dy_):
            px_ = org_.x + (1.0 if dx_ > 0 else -1.0) * (ext_.x + 150.0)
            py_ = org_.y
        else:
            px_ = org_.x
            py_ = org_.y + (1.0 if dy_ > 0 else -1.0) * (ext_.y + 150.0)
        nx_, ny_, nz_ = (px_ - org_.x), (py_ - org_.y), 0.0
        try:
            world_ = unreal.EditorLevelLibrary.get_editor_world()
            hz0 = org_.z - ext_.z + 2.0 * ext_.z * float(p.get("height_frac", 0.4))
            hr = unreal.SystemLibrary.line_trace_single(
                world_, unreal.Vector(px_, py_, hz0), unreal.Vector(org_.x, org_.y, hz0),
                unreal.TraceTypeQuery.TRACE_TYPE_QUERY1, False, [], unreal.DrawDebugTrace.NONE, True)
            if hr:
                d_ = hr.to_dict()                              # HitResult has no attribute access; use to_dict (engine-measured)
                ip = d_.get("impact_point") or d_.get("location")
                inrm = d_.get("impact_normal") or d_.get("normal")
                px_, py_ = ip.x + inrm.x * 28.0, ip.y + inrm.y * 28.0
                nx_, ny_, nz_ = inrm.x, inrm.y, inrm.z
                hz_hit = ip.z
            else:
                hz_hit = None
        except Exception as e:
            unreal.log_warning("practical trace failed: %s" % e)
            hz_hit = None
        if hz_hit is not None:
            hz = hz_hit
        lt = sub.spawn_actor_from_class(unreal.PointLight, unreal.Vector(px_, py_, hz))
        lt.set_actor_label("AutoLightPrac_%02d" % o["id"])
        c_ = lt.get_component_by_class(unreal.PointLightComponent)
        inten = float(p.get("intensity", 0.5))
        c_.set_editor_property("intensity", 250.0 + 1200.0 * inten)     # lumens: window/bulb scale
        c_.set_editor_property("attenuation_radius", 900.0 + 600.0 * inten)
        c_.set_editor_property("cast_shadows", False)
        c_.set_light_color(unreal.LinearColor(col[0], col[1], col[2], 1.0))   # BGRA trap avoided: use the LinearColor API
        if bulb_mat:                                                    # emissive "glow spot" (visible source body)
            # bulb half-sunk into the surface (hit point + 10cm along normal): reads as a wall lamp / window hole, not a floating ball
            b = sub.spawn_actor_from_object(
                unreal.load_asset("/Engine/BasicShapes/Sphere"),
                unreal.Vector(px_ - nx_ * 18.0, py_ - ny_ * 18.0, hz - nz_ * 18.0))
            b.set_actor_label("AutoLightBulb_%02d" % o["id"])
            b.set_actor_scale3d(unreal.Vector(0.18, 0.18, 0.18))
            comp_ = b.static_mesh_component
            mid = comp_.create_dynamic_material_instance(0, bulb_mat)
            if mid:
                glow = 18.0 + 50.0 * inten
                mid.set_vector_parameter_value("Color", unreal.LinearColor(col[0] * glow, col[1] * glow, col[2] * glow, 1.0))
        n += 1
    if practicals is not None:
        unreal.log("night practicals: %d placed (of %d inferred)" % (n, len(practicals or [])))


def _apply_adjustments(adj, data):
    """Execute Gemini's constrained adjustment commands (the server already clamped them; guard the bounds again here):
    re-ground authoritatively after move/rotate/scale; exposure deltas go through _apply_grade (same generic
    mechanism)."""
    terr = data.get("terrain")
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actors = {a.get_actor_label(): a for a in sub.get_all_level_actors()}
    tcx = float((terr or {}).get("cx_m", 0.0)) * CM_PER_UNIT
    tcy = float((terr or {}).get("cy_m", 0.0)) * CM_PER_UNIT
    thx = max(50.0, float((terr or {}).get("half_fwd_m", 10.0)) * CM_PER_UNIT)
    thy = max(50.0, float((terr or {}).get("half_lat_m", 10.0)) * CM_PER_UNIT)
    n = 0
    adj_moved = []
    for o in (adj.get("objects") or []):
        a = actors.get("OBJ_%02d" % int(o.get("id", -1)))
        if a is None:
            continue
        adj_moved.append(int(o.get("id", -1)))
        loc = a.get_actor_location()
        nx = loc.x + 100.0 * max(-10.0, min(10.0, float(o.get("move_fwd_m", 0.0))))
        ny = loc.y + 100.0 * max(-10.0, min(10.0, float(o.get("move_lat_m", 0.0))))
        if terr and terr.get("grid"):
            nx = min(max(nx, tcx - thx + 100.0), tcx + thx - 100.0)
            ny = min(max(ny, tcy - thy + 100.0), tcy + thy - 100.0)
        a.set_actor_location(unreal.Vector(nx, ny, loc.z), False, False)
        dy_ = float(o.get("rotate_yaw_deg", 0.0))
        if abs(dy_) > 0.5:
            r = a.get_actor_rotation()
            a.set_actor_rotation(unreal.Rotator(pitch=r.pitch, yaw=r.yaw + dy_, roll=r.roll), False)
        sm = max(0.8, min(1.25, float(o.get("scale_mul", 1.0))))
        if abs(sm - 1.0) > 0.01:
            ob_, eb_ = a.get_actor_bounds(False)
            bot0_ = ob_.z - eb_.z
            s = a.get_actor_scale3d()
            a.set_actor_scale3d(unreal.Vector(s.x * sm, s.y * sm, s.z * sm))
            if not (terr and terr.get("grid")):
                # no terrain indoors: scaling pivots at the actor pivot, the base sinks/floats (observed: a planter scaled 1.33x sank 10cm) -> re-anchor the original base
                ob2_, eb2_ = a.get_actor_bounds(False)
                L2_ = a.get_actor_location()
                a.set_actor_location(unreal.Vector(
                    L2_.x, L2_.y, L2_.z + (bot0_ - (ob2_.z - eb2_.z))), False, False)
        if terr and terr.get("grid"):
            _snap_actor_to_terrain(a, terr)
        n += 1
    if adj_moved:
        _resolve_overlaps(data, adj_moved)      # critique moves have no interpenetration concept (observed: moved a chair into the shelf)
    # hallucinated-object removal prescription (critique A/B at the photo camera: in render but not in photo -> delete; observed 3 imagined furniture pieces crowding a small bedroom)
    rm_ids = adj.get("remove_object_ids") or []
    if rm_ids:
        rms = set(int(x) for x in rm_ids)
        for a in sub.get_all_level_actors():
            lbl = a.get_actor_label()
            if lbl.startswith("OBJ_") and int(lbl[4:]) in rms:
                try:
                    sub.destroy_actor(a)
                except Exception:
                    pass
        data["objects"] = [o for o in (data.get("objects") or []) if int(o.get("id", -1)) not in rms]
        data["layout"] = [o for o in (data.get("layout") or []) if int(o.get("id", -1)) not in rms]
        unreal.log("critique: removed %d hallucinated object(s) %s" % (len(rms), sorted(rms)))
    # indoor lighting prescription (autonomous light fixing): critique gives per-light lumen multipliers -> update light_map and relight everything (server already persisted it)
    lm_ = adj.get("light_mults") or []
    if lm_ and data.get("light_map"):
        mp_ = {int(x.get("id", 0)): max(0.25, min(4.0, float(x.get("lumens_mult", 1.0)))) for x in lm_}
        for m_ in data["light_map"]:
            mu_ = mp_.get(int(m_.get("id", 0)))
            if mu_:
                m_["lumens"] = max(20.0, min(4000.0, float(m_.get("lumens", 500.0)) * mu_))
        attn_ = None
        rbox_ = None
        if data.get("room"):
            rb0, rb1, rb2, rb3 = _room_bounds(data)
            rbox_ = (rb0, rb1, rb2, rb3)
            attn_ = math.sqrt((rb1 - rb0) ** 2 + (rb3 - rb2) ** 2 + 350.0 ** 2)
        _apply_lights(data.get("lights", []), indoor=bool(data.get("room")), objs=data.get("objects"),
                      light_map=data.get("light_map"), attn_cm=attn_, room_box=rbox_)
        unreal.log("critique: %d light(s) re-leveled by AI prescription" % len(lm_))
    wm_ = float(adj.get("window_mult", 1.0))
    if abs(wm_ - 1.0) > 0.05:
        for a in sub.get_all_level_actors():
            if a.get_actor_label().startswith("AutoWinLight_"):
                c_ = a.get_component_by_class(unreal.RectLightComponent)
                if c_:
                    c_.set_editor_property("intensity",
                                           float(c_.get_editor_property("intensity")) * max(0.25, min(4.0, wm_)))
        unreal.log("critique: window light x%.2f" % wm_)
    # outdoor atmosphere prescription (enrichment): fog density multiplier + sun bearing correction (photo shadow direction = free ground truth)
    env = data.get("environment", {})
    fogm = max(0.3, min(3.0, float(adj.get("fog_mult", 1.0))))
    sund = max(-45.0, min(45.0, float(adj.get("sun_yaw_delta_deg", 0.0))))
    if not env.get("indoor_lux_override") and (abs(fogm - 1.0) > 0.05 or abs(sund) > 2.0):
        if abs(fogm - 1.0) > 0.05:
            env["fog_density"] = max(0.0, min(0.08, float(env.get("fog_density", 0.003)) * fogm))
        if abs(sund) > 2.0:
            env["sun_yaw_deg"] = float(env.get("sun_yaw_deg", 135.0)) + sund
        _apply_env(env)
        _match_fog_luminance_to_sky(env)    # _apply_env reset fog color to 0..1 face values -> re-match against the sky dome
        unreal.log("critique: fog x%.2f sun %+.0f deg" % (fogm, sund))
    ev = max(-1.0, min(1.0, float(adj.get("exposure_ev_delta", 0.0))))
    if "night" in str(env.get("time_of_day", "")).lower() and not env.get("indoor_lux_override"):
        # The night look is an EXPERIENCE calibration (hand-tuned dark-but-readable), not photo replication -- game night != photo night (long exposure/high ISO).
        # Critique compares captures against the photo, keeps crying "too dark" and drags exposure back to daylight -> OUTDOOR night ignores critique exposure deltas;
        # indoor night has its own metering, critique exposure prescriptions stay valid (two rainy-night-interior rounds were wiped for nothing)
        ev = 0.0
    if abs(ev) > 0.05:
        env["exposure_ev"] = float(env.get("exposure_ev", 0.0)) + ev
        _apply_grade(env)
    nf = 0
    for f in (adj.get("fills") or []):
        # AI fill prescriptions (night): critique sees a dead-black face on some object in the render -> one shadowless cool fill sculpts that face.
        # 'side' is relative to the critique screenshot camera (front = the face toward that camera).
        a = actors.get("OBJ_%02d" % int(f.get("id", -1)))
        if a is None:
            continue
        org_, ext_ = a.get_actor_bounds(False)
        cam = _LAST_CAP_CAM or (org_.x - 1500.0, org_.y, org_.z + 500.0)
        vx, vy = cam[0] - org_.x, cam[1] - org_.y
        L_ = max(1.0, math.hypot(vx, vy)); vx, vy = vx / L_, vy / L_
        side = str(f.get("side", "front"))
        if side == "back":
            dx_, dy_ = -vx, -vy
        elif side == "left":
            dx_, dy_ = vy, -vx
        elif side == "right":
            dx_, dy_ = -vy, vx
        else:
            dx_, dy_ = vx, vy
        t_ = min(ext_.x / max(0.05, abs(dx_)), ext_.y / max(0.05, abs(dy_)))
        st = float(f.get("strength", 0.5))
        lt = sub.spawn_actor_from_class(unreal.PointLight,
                                        unreal.Vector(org_.x + dx_ * (t_ + 250.0), org_.y + dy_ * (t_ + 250.0),
                                                      org_.z + 0.25 * ext_.z))
        lt.set_actor_label("AutoLightFix_%02d_%s" % (int(f.get("id")), side))
        c_ = lt.get_component_by_class(unreal.PointLightComponent)
        # inverse square: the light sits ~2.5m + radius from the face; to give it ~10 lux, I ~= 10 x 4pi x d^2 (bigger object = farther, brighter light)
        d2_ = (2.5 + max(ext_.x, ext_.y) / 100.0)
        c_.set_editor_property("intensity", min(40000.0, (10.0 + 14.0 * st) * 12.57 * d2_ * d2_))
        c_.set_editor_property("attenuation_radius", 2.5 * max(ext_.x, ext_.y, ext_.z) + 600.0)
        c_.set_editor_property("cast_shadows", False)
        c_.set_light_color(unreal.LinearColor(0.7, 0.75, 1.0, 1.0))     # cool neutral; does not break the night language
        nf += 1
    ad = float(adj.get("ambient_delta", 0.0) or 0.0)
    if abs(ad) > 0.05:
        # AI ambient tweak: executor = the sky-dome fill directional (the skylight captures a ~0 night sky, tweaking it does nothing) -> intensity x (1+delta)
        for a in sub.get_all_level_actors():
            if a.get_actor_label() == "AutoLightSkyFill":
                c2 = a.get_component_by_class(unreal.DirectionalLightComponent)
                cur = float(c2.get_editor_property("intensity"))
                c2.set_editor_property("intensity", max(0.5, min(60.0, cur * (1.0 + ad))))
                unreal.log("ambient (sky-fill) %.1f -> %.1f lux (AI %+.2f)" % (
                    cur, max(0.5, min(60.0, cur * (1.0 + ad))), ad))
                break
    unreal.log("critique applied: %d objects, ev%+.2f, %d fills, amb%+.2f — %s" % (n, ev, nf, ad, adj.get("notes", "")))


def run():
    # Task pinning + fast modes (all via env vars, persistent across remote-execs within one editor session):
    #   UE_PLACE_TASK=<tid> -> pull /result/<tid> instead of /latest (no takeover by newly finished tasks)
    #   UE_PLACE_MODE=fx    -> only clear and respawn AutoFx* (fast FX iteration; terrain/objects/lights untouched)
    tid_pin = os.environ.get("UE_PLACE_TASK", "").strip()
    global _TASK_TID
    _TASK_TID = tid_pin
    # module-level path constants were evaluated at import time (tid unknown) -> re-point them here per tid, so each scene's generated assets isolate under /Game/Auto/<tid>/...
    global GROUND_MAT_PATH, TERRAIN_VAR_MAT_PATH, BLEND_MIC_PATH, DRESS_MAT_PATH, DRESS_CARD_MAT_PATH, FX_WATER_MAT_PATH, FX_CARD_MAT_PATH, SKY_MAT_PATH, GLOW_MAT_PATH, GROUND_FLAT_MAT, SKY_HDRI_MAT, DEST
    GROUND_MAT_PATH = _apath("M_GroundV2")
    TERRAIN_VAR_MAT_PATH = _apath("M_AutoTerrainVar")
    BLEND_MIC_PATH = _apath("MI_AutoBlendGround")
    DRESS_MAT_PATH = _apath("M_Dress")
    DRESS_CARD_MAT_PATH = _apath("M_DressCard")
    FX_WATER_MAT_PATH = _apath("M_FxWater")
    FX_CARD_MAT_PATH = _apath("M_FxCard")
    SKY_MAT_PATH = _apath("M_Sky")
    GLOW_MAT_PATH = _apath("M_AutoGlow")
    GROUND_FLAT_MAT = _apath("M_GroundFlat")
    SKY_HDRI_MAT = _apath("M_SkyHDRI")
    DEST = _aipath("")
    data = json.loads(_get(SERVER + ("/result/" + tid_pin if tid_pin else "/latest")))
    if os.environ.get("UE_PLACE_MODE", "").strip() == "fx":
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        for a in sub.get_all_level_actors():
            if a.get_actor_label().startswith("AutoFx"):
                try:
                    sub.destroy_actor(a)
                except Exception:
                    pass
        _FX_ENV.clear()                          # audit H2: same as above; guard against stale env keys from the previous task
        _FX_ENV.update(data.get("environment") or {})
        _apply_effects(data.get("effects"), data)
        unreal.log("FX-only respawn done (task=%s)" % data.get("task_id"))
        return
    if os.environ.get("UE_PLACE_MODE", "").strip() == "fxshots":
        _fx_shots(data)
        return
    objs = data.get("objects", [])
    if data.get("is_indoor") and data.get("room"):
        (data.get("environment") or {})["indoor_lux_override"] = float(
            (data.get("room") or {}).get("indoor_lux", 150.0))   # indoor metering switches to AI room illuminance
    unreal.log("layout: %d objects, task=%s" % (len(objs), data.get("task_id")))
    if not objs and not data.get("terrain"):     # pure terrain/landscape (no discrete objects) still builds terrain + sky
        unreal.log_warning("No layout/terrain. Upload once on the web first (and ENABLE_3D=True).")
        return

    # enable realtime viewport refresh: otherwise the editor viewport/screenshots may read a stale buffer (hit a full-white screen on 5.6)
    try:
        unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).editor_set_viewport_realtime(True)
    except Exception:
        pass

    # Entry cleanup: only remove this pipeline's products (OBJ_ / ECHO_ / Auto*); user and template content is never touched.
    # Each _apply_* rebuilds AutoSky/AutoGround/AutoCam etc. as needed, so re-runs neither stack up nor leave stale actors.
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in sub.get_all_level_actors():
        lbl = a.get_actor_label()
        if lbl.startswith("OBJ_") or lbl.startswith("ECHO_") or lbl.startswith("Auto"):
            try:
                sub.destroy_actor(a)
            except Exception:
                pass

    atools = unreal.AssetToolsHelpers.get_asset_tools()
    tmp = tempfile.gettempdir()

    # Per-task asset isolation (observed level 1 turning into "bed/wall/planter": shared OBJ_xx paths + replace_existing let a later task
    # overwrite an earlier level's meshes in place -- one level = one scene, so assets get one folder per task)
    tid_seg = "".join(ch for ch in str(data.get("task_id") or "latest") if ch.isalnum())
    res3 = {int(r.get("id", -1)): (r.get("size3_m"), bool(r.get("imagined")))
            for r in (data.get("results") or []) if r.get("size3_m")}
    for o in objs:
        oid = o["id"]
        dest_obj = "%s/%s/OBJ_%02d" % (DEST, tid_seg, oid)
        glb = (o.get("glb") or "").strip()
        meshes = []; mats = []
        if glb:
            try:
                local = os.path.join(tmp, "obj_%d.glb" % oid)
                with open(local, "wb") as f:
                    f.write(_get(SERVER + glb))
                task = unreal.AssetImportTask()        # default Interchange import brings mesh + materials + textures
                task.filename = local
                task.destination_path = dest_obj
                task.automated = True
                task.replace_existing = True
                task.save = True
                atools.import_asset_tasks([task])
                assets = _assets_in(dest_obj)
                meshes = [a for a in assets if isinstance(a, unreal.StaticMesh)]
                mats = [a for a in assets if isinstance(a, unreal.MaterialInterface)]
                texs = [a for a in assets if isinstance(a, unreal.Texture)]
                unreal.log("  OBJ_%02d imported: mesh=%d material=%d texture=%d"
                           % (oid, len(meshes), len(mats), len(texs)))
            except Exception as e:
                unreal.log_warning("  OBJ_%02d import failed: %s" % (oid, e))
        if meshes:
            mesh = meshes[0]; placeholder = False
            if ENABLE_VR:                              # Tripo meshes often import without collision -> complex-as-simple uses render triangles as collision: players collide and steer around
                try:
                    bs = mesh.get_editor_property("body_setup")
                    if bs:
                        bs.set_editor_property("collision_trace_flag", unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE)
                except Exception as e:
                    unreal.log_warning("  OBJ_%02d collision setup: %s" % (oid, e))
        else:                                          # no GLB / import failed -> placeholder box: unmodeled objects still show their position and sit on the terrain
            mesh = unreal.load_asset("/Engine/BasicShapes/Cube"); placeholder = True; mats = []
            unreal.log("  OBJ_%02d: placeholder box" % oid)

        # place by world transform; ground=true seats the bottom on the ground, else it hangs at the projected height.
        # (1) clamp object XY into the terrain footprint (margin = 0.6 x object half-size, min 1m, max 25% of footprint) -- depth-derived
        #     positions outside the terrain no longer float beyond the edge (there is no ground there).
        # (2) grounding height five-point-samples the terrain mesh about to be built (center + 0.6x support corners), lowest wins -- big objects
        #     spanning slopes never float at any corner (the uphill side may embed, like real construction). Center-only sampling left a corner floating 11m over a 19m drop.
        L, R, S = o["location"], o["rotation"], o["scale"]
        sx, sy, sz = S[0] / CM_PER_UNIT, S[1] / CM_PER_UNIT, S[2] / CM_PER_UNIT
        # Per-axis aspect correction (size3_m): scalar size_m scales uniformly, so a mesh's own wrong proportions pass through with nobody in charge
        # (observed indoors: Tripo made a rug a 1.3m-tall slab / a "tall bookcase" a 2.4m cube jammed into the armchair).
        # AI reports [w,d,h] meters; compare per axis against the uniformly scaled mesh -- XY paired big-to-big (immune to yaw /
        # 90-degree modeling-orientation ambiguity); intervene only if deviation >1.35x (good meshes/plants untouched); compression is texture-safe and unlimited,
        # stretching clamped <=4x (against texture smearing).
        s3, s3_imag = res3.get(oid) or (None, False)
        if s3 and not placeholder:
            try:
                b3 = mesh.get_bounds()
                rx = max(1.0, 2.0 * b3.box_extent.x * sx)
                ry = max(1.0, 2.0 * b3.box_extent.y * sy)
                rz = max(1.0, 2.0 * b3.box_extent.z * sz)
                tw, td, th = (max(2.0, float(v) * 100.0) for v in s3[:3])
                txc, tyc = (max(tw, td), min(tw, td)) if rx >= ry else (min(tw, td), max(tw, td))
                kx, ky, kz = txc / rx, tyc / ry, th / rz
                # Intervention gates: aspect distortion (axis deviation >1.25x) -- or imagined objects with overall size off >1.5x (size_m is pure text guesswork,
                # image-grounded size3 is more trustworthy; real objects keep photo-anchored absolute scale, aspect fixed only).
                # 1.35 -> 1.25 from a live round: real/imagined "twin" armchairs took two sizing paths 15% apart (imagined chair size3-corrected to 100cm,
                # real chair dev=1.32 untriggered stayed at 85cm); the lower gate lets both paths converge on the same size3 fact.
                dev = max(kx, ky, kz) / min(kx, ky, kz)
                off = max(kx, ky, kz, 1.0 / kx, 1.0 / ky, 1.0 / kz)
                if dev > 1.25 or (s3_imag and off > 1.5):
                    kx, ky, kz = (min(4.0, k) for k in (kx, ky, kz))
                    sx, sy, sz = sx * kx, sy * ky, sz * kz
                    unreal.log("  OBJ_%02d aspect-fix %.0fx%.0fx%.0f -> %.0fx%.0fx%.0fcm (k=%.2f/%.2f/%.2f)"
                               % (oid, rx, ry, rz, rx * kx, ry * ky, rz * kz, kx, ky, kz))
            except Exception as e:
                unreal.log_warning("  OBJ_%02d aspect fix: %s" % (oid, e))
        z = L[2]
        terr = data.get("terrain")
        has_terr = bool(terr and terr.get("grid"))
        if o.get("ground", True):
            try:
                b = mesh.get_bounds()
                if has_terr:
                    tcx = float(terr.get("cx_m", 0.0)) * CM_PER_UNIT
                    tcy = float(terr.get("cy_m", 0.0)) * CM_PER_UNIT
                    thx = max(50.0, float(terr.get("half_fwd_m", 10.0)) * CM_PER_UNIT)
                    thy = max(50.0, float(terr.get("half_lat_m", 10.0)) * CM_PER_UNIT)
                    mx = min(0.25 * thx, max(100.0, 0.6 * b.box_extent.x * sx))
                    my = min(0.25 * thy, max(100.0, 0.6 * b.box_extent.y * sy))
                    L[0] = min(max(L[0], tcx - thx + mx), tcx + thx - mx)
                    L[1] = min(max(L[1], tcy - thy + my), tcy + thy - my)
                    # Landing-slope check: objects pinned to >~18 deg slopes/cliff faces look wrong -> anchored at the photo position,
                    # ring-search (radius <= 25% of footprint) for the nearest standable flat; none found -> stay put (big items like hills exempt).
                    rr_ = max(150.0, 0.6 * max(b.box_extent.x * sx, b.box_extent.y * sy))
                    lim_ = 0.32 * rr_

                    def _flat_at(px_, py_):
                        g0_ = _terrain_surface_cm(terr, px_, py_)
                        for k_ in range(8):
                            a_ = math.pi * k_ / 4.0
                            g_ = _terrain_surface_cm(terr, px_ + rr_ * math.cos(a_), py_ + rr_ * math.sin(a_))
                            if abs(g_ - g0_) > lim_:
                                return False
                        return True

                    if not _flat_at(L[0], L[1]):
                        rmax_ = 0.25 * 2.0 * min(thx, thy)
                        step_ = max(150.0, 0.5 * rr_)
                        emx_ = max(mx, 0.16 * thx)               # relocation candidates also stay >= 8% of the footprint from edges (objects avoid the rim)
                        emy_ = max(my, 0.16 * thy)
                        rq_ = step_
                        moved = False
                        while rq_ <= rmax_ and not moved:
                            kk_ = max(8, int(2.0 * math.pi * rq_ / step_))
                            for i_ in range(kk_):
                                a_ = 2.0 * math.pi * i_ / kk_
                                qx_ = min(max(L[0] + rq_ * math.cos(a_), tcx - thx + emx_), tcx + thx - emx_)
                                qy_ = min(max(L[1] + rq_ * math.sin(a_), tcy - thy + emy_), tcy + thy - emy_)
                                if _flat_at(qx_, qy_):
                                    L[0], L[1] = qx_, qy_
                                    moved = True
                                    break
                            rq_ += step_
                        if moved:
                            unreal.log("  OBJ_%02d on steep slope -> relocated to flat (%.0f,%.0f)cm" % (oid, L[0], L[1]))
                    exx = b.box_extent.x * sx * 0.6
                    eyy = b.box_extent.y * sy * 0.6
                    gz = min(_terrain_surface_cm(terr, px_, py_) for px_, py_ in (
                        (L[0], L[1]), (L[0] - exx, L[1] - eyy), (L[0] + exx, L[1] - eyy),
                        (L[0] - exx, L[1] + eyy), (L[0] + exx, L[1] + eyy)))
                else:
                    gz = L[2]
                z = gz - (b.origin.z - b.box_extent.z) * sz     # bottom sits on the (lowest) terrain surface
                h_cm = 2.0 * b.box_extent.z * sz
                if h_cm < 10.0:
                    z += 0.8    # flat items (rug/mat, height <10cm after aspect fix): sinking hides them in the floor; raise 0.8cm against coplanar flicker
                else:
                    z -= min(30.0, max(2.0, 0.015 * h_cm))      # slight sink (the man_made split comes in a later round)
            except Exception:
                pass
        loc = unreal.Vector(L[0], L[1], z)
        rot = unreal.Rotator(pitch=R[0], yaw=R[1], roll=R[2])   # [pitch, yaw, roll]
        scale = unreal.Vector(sx, sy, sz)
        actor = _spawn(mesh, loc, rot, scale)
        actor.set_actor_label("OBJ_%02d" % oid)          # label for cleanup on re-runs
        # Authoritative grounding (post-placement): pre-placement used the unrotated mesh bounds, so yawed objects had misplaced support corners ->
        # five-point-sample the true world bounds (rotation/scale included), position the base = lowest surface - slight sink.
        if o.get("ground", True) and has_terr:
            try:
                org_b, ext_b = actor.get_actor_bounds(False)
                pts5 = [(org_b.x, org_b.y)] + [
                    (org_b.x + dx_ * ext_b.x * 0.6, org_b.y + dy_ * ext_b.y * 0.6)
                    for dx_, dy_ in ((-1, -1), (1, -1), (-1, 1), (1, 1))]
                gz2 = min(_terrain_surface_cm(terr, px_, py_) for px_, py_ in pts5)
                bottom = org_b.z - ext_b.z
                sink = -0.8 if ext_b.z * 2.0 < 10.0 else min(30.0, max(2.0, 0.03 * ext_b.z))  # flat items raise instead of sink
                aloc = actor.get_actor_location()
                actor.set_actor_location(
                    unreal.Vector(aloc.x, aloc.y, aloc.z + (gz2 - bottom) - sink), False, False)
            except Exception as e:
                unreal.log_warning("  OBJ_%02d ground snap: %s" % (oid, e))
        if ENABLE_VR:                                    # VR: objects block the player and can be walked around (with the mesh's complex-as-simple collision)
            try:
                oc = actor.static_mesh_component
                oc.set_collision_profile_name("BlockAll")
                oc.set_collision_enabled(unreal.CollisionEnabled.QUERY_AND_PHYSICS)
            except Exception:
                pass
        # No more manual material re-stuffing: Interchange import already binds each material to the right StaticMesh slot BY NAME,
        # and spawned actors use those slot materials by default. The old set_material(i, mats[i]) overwrote in arbitrary order -> mismatches (grey/wrong textures).
        unreal.log("  placed OBJ_%02d @ %s%s" % (oid, L, " (placeholder)" if placeholder else ""))

    env = data.get("environment", {})
    S = data.get("scene_scale", 10.0)
    indoor = bool(data.get("is_indoor"))
    _apply_sky(env, indoor)                     # sky color (skipped indoors)
    if not indoor:
        _apply_micro_relief(data)               # light terrain undulation (even cities are not dead flat); must precede carve (water areas get flattened after)
        _carve_water_basin(data)                # water-depth fix: carve the lake basin (flatten bed + set waterline + re-seat objects); must precede the terrain mesh build
    _apply_terrain(data.get("terrain"), env, S)  # depth-derived height grid -> real relief terrain; else flat ground fallback
    _apply_dressing((data.get("terrain") or {}).get("dressing"),  # ground dressing (Gemini decides what to scatter; HISM instancing)
                    data.get("terrain"), data.get("_waterline_cm"))   # re-seat on post-carve terrain + drop underwater pebbles
    _FX_ENV.clear()                             # audit H2: clear stale task keys (leftover indoor_lux_override/weather made outdoor particles invisible)
    _FX_ENV.update(env or {})                   # for particle radiance conversion (additive particles need scene-nit scaling under physical lighting)
    _apply_effects(data.get("effects"), data)   # particle/fluid FX (water/waterfall/rain/fog/dust/birds; Gemini decides, static instancing)
    _apply_env(env)                             # sync lighting/atmosphere
    _apply_grade(env)                           # sync post grading
    attn = None
    rbox = None
    if indoor:
        _apply_room_shell(data)                 # build the shell first to fix room bounds (wall snap/attenuation reference the same _room_box cache)
        _clamp_objects_into_room(data)          # clamp true bounding boxes back into the room (the center +/-80 expansion formula cannot see half-widths)
        _snap_objects_to_walls(data)            # wall-snap semantics must precede lighting (fixture anchors reference object positions)
        ra = data.get("room_arrange") or {}     # AI only says WHO may move; coordinates = deterministic packing (AI free-form layout vetoed as random)
        _arrange_furniture_walls(data, [int(m.get("id")) for m in (ra.get("moves") or [])
                                        if m.get("id") is not None])
        _reseat_floaters(data)                  # re-seat small floaters (observed "floating vase": its support got moved away)
        _arrange_wall_items(data)               # wall items spread/lift: must run after ALL re-arrangement (running early = overwritten by arrange, observed)
        bx0, bx1, by0, by1 = _room_bounds(data)
        rbox = (bx0, bx1, by0, by1)
        attn = math.sqrt((bx1 - bx0) ** 2 + (by1 - by0) ** 2 + 350.0 ** 2)  # attenuation must cover the room (528cm in an 8m room = black walls)
    _apply_lights(data.get("lights", []), indoor=indoor, objs=objs,
                  light_map=data.get("light_map"), attn_cm=attn, room_box=rbox)  # artificial lights (AI light homing)
    if indoor:
        _audit_dark_fills(data)                 # dark-zone audit + gentle fill (grid photometry; shadowless small fills at dead-zone cluster centers)
    # outdoor night practicals moved below, after block fill / grounding (lights anchor buildings; buildings move first; audit H3)
    _apply_music(data)                          # soundscape: music + ambience bed (two non-spatialized loops)
    _apply_sound_sources(data)                  # soundscape B: AI semantic point sources (spatialized, louder as you approach)
    _apply_hdri(data.get("hdri", ""), env)      # panorama -> AutoSky inner wall; a real .hdr also rotates to align moon/shadows
    if ENABLE_SKY_ATMOSPHERE and not indoor:
        _apply_sky_atmosphere(env)              # physical sky (Gemini sun-driven) -> replaces the emissive dome; yields to an HDRI sphere when present
        if not data.get("hdri"):                # an HDRI dome already is the sky (with its own clouds) -> no procedural volumetric clouds in front (avoid double clouds)
            _apply_clouds(env)                  # generic volumetric clouds (Gemini cloud_coverage/weather driven): clear = none, overcast = many, thunderstorm = storm clouds
    _apply_reflection(env)                      # reflection capture: must come after sun+atmosphere, else the bake captures an unlit black scene -> dark dirty speculars (iron-rule bug fix)
    _apply_camera(data.get("camera", {}))       # sync the camera
    _capture_view(data)                          # SceneCapture2D renders the "photo view" back to the backend (reliable output)
    # visual critique loop: Gemini looks (photo + render + layout) -> constrained adjustment commands -> execute -> re-review. AI decides, code clamps.
    tid = data.get("task_id") or "latest"
    for rnd in range(1, CRITIQUE_ROUNDS + 1):
        try:
            adj = json.loads(_get(SERVER + "/adjustments/%s?round=%d" % (tid, rnd)))
        except Exception as e:
            unreal.log_warning("critique fetch failed: %s" % e)
            break
        has_ops = (bool(adj.get("objects")) or abs(float(adj.get("exposure_ev_delta", 0.0))) > 0.05
                   or bool(adj.get("light_mults")) or abs(float(adj.get("window_mult", 1.0)) - 1.0) > 0.05
                   or abs(float(adj.get("fog_mult", 1.0)) - 1.0) > 0.05
                   or abs(float(adj.get("sun_yaw_delta_deg", 0.0))) > 2.0
                   or bool(adj.get("remove_object_ids"))
                   or bool(adj.get("fills")) or abs(float(adj.get("ambient_delta", 0.0) or 0.0)) > 0.05)  # audit M3: _apply_adjustments implements these two but the gate omitted them -> pure fill prescriptions were silently dropped
        if adj.get("verdict") != "adjust" or not has_ops:
            unreal.log("critique r%d: ok — %s" % (rnd, adj.get("notes", "")))
            break
        _apply_adjustments(adj, data)
        _capture_view(data)                      # fresh render after adjustments, for the next round / archive
    if ENABLE_VR:
        _apply_vr(data)                          # Tier-0 VR last: spawn/invisible walls/GameMode + park the editor viewport at the spawn (Play "current camera position" starts here)
        _extend_ground_to_objects(data.get("environment", {}), data.get("terrain"))  # generic: flat-and-scattered scenes (streets) auto-extend one big flat ground covering all objects
        if indoor:
            _clamp_spawn_into_room(data)         # must follow _apply_vr (the spawn exists only then; clamping earlier is a no-op)
    if not indoor:
        terr_ = data.get("terrain")
        cb_ = data.get("city_block")
        is_city = isinstance(cb_, dict) and cb_.get("placements")
        # No more "out-of-scene relief / distant mountain ring" (design decision: vista is a runtime mesh with no Lumen GI = a black bowl, not worth it).
        # Open natural scenes (lake/wild) close the edge with the flat distant ground reaching the horizon; city scenes = buildings + fog, no flat skirt
        # (a skirt behind city towers exposed MI_GroundFlat checkerboard / water mirror; street regression).
        if not is_city:
            _apply_ground_skirt(env)
        if terr_ and terr_.get("grid"):
            # _apply_midground(data, env)        # midground silhouette belt -- design decision (2026-06): no such distant assets (sparse/odd/prone to floating)
            _apply_fogband(data, env)            # 3c: local volumetric fog band at the midground leading edge (spatial separation)
        _apply_city_block(data)                  # S1 pipelined: AI street-block planning (automatic for city photos)
        _clamp_spawn_into_street(data)           # spawn re-clearing after block fill (must follow _apply_vr; this runs later)
        _ground_contact_audit(data)              # grounding audit: re-seat floating fill items (must precede the night window panels)
        _apply_distant_skyline(data)             # distant city skyline (AI distant_city; before night windows so far buildings get lit windows too)
        _apply_scene_fill(data)                  # scene fill (AI scene_fill species+density; trees along the playable edge, avoiding the subject sight line, fixes the 'empty center')
        _apply_ambient_life(data)                # ambient life (AI pedestrians/birds; dark pedestrian silhouettes midfield facing the camera -- brings the scene alive)
        _apply_night_lights(env, data.get("terrain"), objs, data.get("practicals"))  # night practicals (anchored to final building positions; audit H3)
        _apply_night_windows(data)               # R2 night windows (lit window grids on night buildings; AI-judged liveliness)
    unreal.log("Done: %d objects imported and placed." % len(objs))


if __name__ == "__main__":     # exec() gives __name__=="__main__" -> full build; import does not trigger (handy for remote single-function calls)
    run()
