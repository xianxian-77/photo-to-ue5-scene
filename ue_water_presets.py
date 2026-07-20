# MA_YX_Blend water-body preset module (import inside UE editor Python, or call via ue_remote_bridge).
# Sources: vendor manual 13 "Water Refinement" step-by-step course + WaterLab in-engine scans (output/mat_test/exp/wl*.png,
# docs/MATERIAL_WATER.md). Core mechanism (verified): water = two switches (Use Puddle Layer master +
# Use_Water_ImageAlpha area mask, white = water) + Water_curve = 0 (falloff curve, 0 = full strength).
# Usage:
#   import ue_water_presets as wp
#   wp.apply_water(mi, "lake_calm")                          # enable water on an existing instance
#   wp.make_water_lake("MyLake", (0, -60000, 200), 30, "deep_dark")   # one-call lake
import unreal

# Switch baseline shared by all presets (verified-safe combination)
_BASE_SWITCHES = {
    "Use Puddle Layer": True,        # master switch of the water look chain (without it the whole water branch is statically compiled out)
    "Use_Water_ImageAlpha": True,    # area-mask switch (white = water)
    "Refraction?": True,             # fake refraction, one of the main sources of the water feel
    "Foam?": False,                  # RadialFoam strobes by default; lake presets may enable it at low opacity
    "Moss?": False, "Leaves?": False, "Rain?": False,
}
_BASE_SCALARS = {
    "Water_curve": 0.0,              # curve sample position: wateralpha falls off, 0 = water at full strength (raising it kills the water -- do not touch)
    "base_curve": 1.0, "top_curve": 4.0,
}

# Preset values all come from in-engine scans: Wave_Size = world size of ripple patches (512 choppy / 1024 medium / 2048 open),
# Ripples_Intensity = micro-ripple sparkle (0 smooth .. 4 glittery), Water_Roughness = reflection sharpness (0.02 mirror .. 0.3 matte),
# Metallic = surface reflection trait (misleading name, NOT a 0-1 metalness; 0/1024/2048 visibly differ, default 1024),
# Water_Depth = bottom-visibility falloff (60 = see the bottom .. 1000 = ink black), Gerstner Height = wave amplitude (0.2 still .. 6 storm),
# Foam_Opacity <= 0.3 tames the temporal variance (0.25 measured 4.65; default 1.0 strobes at 15+).
WATER_PRESETS = {
    "lake_calm": {       # calm lake: open ripples + near-mirror + medium depth
        "scalars": {"Wave_Size": 2048.0, "Ripples_Intensity": 1.0, "Water_Roughness": 0.05,
                    "Metallic": 1024.0, "Water_Depth": 400.0,
                    "1_GerstnerWave_Height": 0.3, "2_GerstnerWave_Height": 0.2},
        "vectors": {"Water_Color_01": (0.02, 0.10, 0.16), "Water_Color_02": (0.05, 0.20, 0.25)},
        "mask": "/Game/Tool/Masks/T_Alpha_05",
    },
    "lake_windy": {      # windy lake: medium ripples + glittery + slightly matte
        "scalars": {"Wave_Size": 1024.0, "Ripples_Intensity": 2.5, "Water_Roughness": 0.12,
                    "Metallic": 1024.0, "Water_Depth": 400.0,
                    "1_GerstnerWave_Height": 2.0, "2_GerstnerWave_Height": 1.5},
        "vectors": {"Water_Color_01": (0.02, 0.09, 0.14), "Water_Color_02": (0.05, 0.17, 0.22)},
        "mask": "/Game/Tool/Masks/T_Alpha_05",
        "switches": {"Foam?": True}, "extra_scalars": {"Foam_Opacity": 0.25, "Foam_Fade": 10.0},
    },
    "pond_shallow": {    # shallow pond: visible bottom + fine ripples
        "scalars": {"Wave_Size": 768.0, "Ripples_Intensity": 1.5, "Water_Roughness": 0.08,
                    "Metallic": 1024.0, "Water_Depth": 60.0,
                    "1_GerstnerWave_Height": 0.4, "2_GerstnerWave_Height": 0.3},
        "vectors": {"Water_Color_01": (0.04, 0.12, 0.14), "Water_Color_02": (0.08, 0.20, 0.22)},
        "mask": "/Game/Tool/Masks/T_Alpha_05",
    },
    "deep_dark": {       # deep dark water: ink blue + no visible bottom
        "scalars": {"Wave_Size": 1536.0, "Ripples_Intensity": 1.2, "Water_Roughness": 0.06,
                    "Metallic": 1024.0, "Water_Depth": 1000.0,
                    "1_GerstnerWave_Height": 0.8, "2_GerstnerWave_Height": 0.5},
        "vectors": {"Water_Color_01": (0.01, 0.04, 0.10), "Water_Color_02": (0.03, 0.10, 0.20)},
        "mask": "/Game/Tool/Masks/T_Alpha_05",
    },
    "swamp_mossy": {     # swamp: murky green + algae + matte (algae = Moss? switch, verified working)
        "scalars": {"Wave_Size": 1024.0, "Ripples_Intensity": 0.8, "Water_Roughness": 0.2,
                    "Metallic": 512.0, "Water_Depth": 150.0,
                    "1_GerstnerWave_Height": 0.2, "2_GerstnerWave_Height": 0.15},
        "vectors": {"Water_Color_01": (0.03, 0.10, 0.05), "Water_Color_02": (0.08, 0.18, 0.08)},
        "mask": "/Game/Tool/Masks/T_Alpha_05",
        "switches": {"Moss?": True},
    },
    "puddles_cracks": {  # puddle water in ground cracks (for blended ground, not a standalone water surface): wet sidewalks / dirt roads
        "scalars": {"Wave_Size": 512.0, "Ripples_Intensity": 1.0, "Water_Roughness": 0.08,
                    "Metallic": 1024.0, "Water_Depth": 260.0,
                    "1_GerstnerWave_Height": 0.1, "2_GerstnerWave_Height": 0.1},
        "vectors": {},
        "mask": "/Game/Tool/Masks/T_Alpha_01",   # white = water: 01 = crack puddles (28%), 04 = large areas (71%), 05 = fully flooded
    },
}


def apply_water(mi, preset="lake_calm", water_mask=None):
    """Enable and configure the water layer on a MA_YX_Blend instance. mi = MaterialInstanceConstant,
    preset = WATER_PRESETS key, water_mask = override for the preset's mask (asset path, white = water)."""
    mel = unreal.MaterialEditingLibrary
    p = WATER_PRESETS[preset]
    sw = dict(_BASE_SWITCHES); sw.update(p.get("switches", {}))
    for n, v in sw.items():
        mel.set_material_instance_static_switch_parameter_value(mi, n, v)
    sc = dict(_BASE_SCALARS); sc.update(p["scalars"]); sc.update(p.get("extra_scalars", {}))
    for n, v in sc.items():
        mel.set_material_instance_scalar_parameter_value(mi, n, float(v))
    for n, (r, g, b) in p.get("vectors", {}).items():
        mel.set_material_instance_vector_parameter_value(mi, n, unreal.LinearColor(r, g, b, 1.0))
    mask = water_mask or p.get("mask")
    if mask:
        t = unreal.load_asset(mask)
        if t:
            mel.set_material_instance_texture_parameter_value(mi, "water_imagealpha", t)
    mel.update_material_instance(mi)
    unreal.log("water preset '%s' applied to %s" % (preset, mi.get_name()))
    return mi


def make_water_lake(label, location, size_m=30.0, preset="lake_calm",
                    bottom_albedos=None, mic_path=None):
    """One-call lake: plane + instance duplicated from the template (all slots filled to avoid NULL compile
    failures) + 3 bottom water-bed layers + water preset. bottom_albedos = up to 3 bottom-albedo asset
    paths (defaults to the library Soil/Leaves). Returns the actor."""
    eal = unreal.EditorAssetLibrary
    mel = unreal.MaterialEditingLibrary
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    mic_path = mic_path or ("/Game/Auto/MI_Water_%s" % label)
    if not eal.does_asset_exist(mic_path):
        eal.duplicate_asset("/Game/Tool/MA_Blend/matellic/MA_YX_Blend_Inst", mic_path)
    mi = unreal.load_asset(mic_path)
    mel.set_material_instance_parent(mi, unreal.load_asset("/Game/Tool/MA_Blend/MA_YX_Blend"))
    bots = list(bottom_albedos or ["/Game/Tool/MA_Blend/CoreContent/textures/Soil_D",
                                   "/Game/Tool/MA_Blend/CoreContent/textures/T_Leaves2_D",
                                   "/Game/Tool/MA_Blend/CoreContent/textures/Soil_D"])
    while len(bots) < 3:
        bots.append(bots[-1])
    for slot, pth in zip(("Base Layer Albedo Map", "Middle Layer Albedo Map", "Top Layer Albedo Map"), bots):
        t = unreal.load_asset(pth)
        if t:
            mel.set_material_instance_texture_parameter_value(mi, slot, t)
    for n in ("Use_Base_ImageAlpha", "Use_Top_ImageAlpha",
              "Use Base Layer Adjustments", "Use Middle Layer Adjustments", "Use Top Layer Adjustments"):
        mel.set_material_instance_static_switch_parameter_value(mi, n, True)
    apply_water(mi, preset)
    eal.save_asset(mic_path)
    plane = unreal.load_asset("/Engine/BasicShapes/Plane")
    actor = next((a for a in sub.get_all_level_actors() if a.get_actor_label() == label), None)
    if actor is None:
        actor = sub.spawn_actor_from_object(plane, unreal.Vector(*location))
        actor.set_actor_label(label)
    actor.set_actor_location(unreal.Vector(*location), False, False)
    actor.set_actor_scale3d(unreal.Vector(size_m, size_m, 1.0))
    actor.static_mesh_component.set_material(0, mi)
    unreal.log("water lake '%s' ready (%.0fm, preset=%s)" % (label, size_m, preset))
    return actor
