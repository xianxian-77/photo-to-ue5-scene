# Pipeline adapter for the Niagara trio (NS_Particles/NS_Leaves/NS_Mesh).
# The systems are hand-built in the UE editor per docs/NIAGARA_SETUP.md (Python cannot author Niagara; verified on both 5.7 and 5.8).
# This module maps the contract's semantic params onto the hand-built systems' real parameter names/types:
#   naming (verified): a "User_" prefix was typed at creation and UE adds its namespace -> runtime name = "User.User_<name>";
#   packing (verified): BoxXY/BoxZ are merged into vec3 "User.User_Box" (x,y,z radii); VelZ is the z component of vec3 "User.User_VelZ".
# Usage (editor Python / ue_remote_bridge / ue_scene_builder):
#   import ue_fx_presets as fx
#   fx.spawn_fx("fountain", (x, y, z))                      # spawn a preset as-is
#   fx.spawn_fx("leaves", loc, overrides={"SpawnRate": 30}) # preset + overrides
#   fx.apply_params(comp, {"Color": (1,0.4,0.1,1), "Size": 30})   # retune an existing component
import unreal

SYSTEMS = {
    "soft": "/Game/Tool/FX/NS_Particles",   # soft sprites: water/rain/snow/dust/embers/mist
    "leaf": "/Game/Tool/FX/NS_Leaves",      # card sprites + tumble + wind: falling leaves/petals/confetti
    "mesh": "/Game/Tool/FX/NS_Mesh",        # mesh particles: debris/gravel/splashes
}

# Semantic name -> real variable name (floats). The two vec3-packed special keys are handled separately.
_FLOATS = {
    "SpawnRate": "User.User_SpawnRate",
    "Size": "User.User_Size",
    "Lifetime": "User.User_Lifetime",
    "GravityZ": "User.User_GravityZ",
    "VelSpread": "User.User_VelSpread",
    "RotRate": "User.User_RotRate",      # leaf/mesh only; missing elsewhere = silently ignored (harmless)
    "WindXY": "User.User_WindXY",
}
_COLOR = ("Color", "User.User_Color")
_VEC_VELZ = "User.User_VelZ"             # vec3; semantic VelZ goes into z
_VEC_BOX = "User.User_Box"               # vec3; semantic BoxXY -> x=y, BoxZ -> z

# Preset table (values carried over from the NIAGARA_SETUP.md effect matrix)
PRESETS = {
    # soft-sprite family
    "fountain":  ("soft", {"SpawnRate": 300, "Size": 15, "VelZ": 400, "VelSpread": 120, "GravityZ": -600, "BoxXY": 40, "BoxZ": 10, "Lifetime": 2.0, "Color": (0.6, 0.85, 1.0, 1.0)}),
    "waterfall": ("soft", {"SpawnRate": 400, "Size": 10, "VelZ": -300, "VelSpread": 40, "GravityZ": -800, "BoxXY": 60, "BoxZ": 10, "Lifetime": 2.0, "Color": (0.7, 0.85, 1.0, 1.0)}),
    "rain":      ("soft", {"SpawnRate": 600, "Size": 5, "VelZ": -600, "VelSpread": 20, "GravityZ": -1000, "BoxXY": 600, "BoxZ": 20, "Lifetime": 1.6, "Color": (0.8, 0.85, 0.9, 0.7)}),
    "snow":      ("soft", {"SpawnRate": 200, "Size": 8, "VelZ": -80, "VelSpread": 60, "GravityZ": -150, "BoxXY": 600, "BoxZ": 20, "Lifetime": 6.0, "Color": (1.0, 1.0, 1.0, 0.9)}),
    "dust":      ("soft", {"SpawnRate": 60, "Size": 4, "VelZ": 5, "VelSpread": 20, "GravityZ": 0, "BoxXY": 300, "BoxZ": 120, "Lifetime": 5.0, "Color": (1.0, 0.95, 0.8, 0.35)}),
    "embers":    ("soft", {"SpawnRate": 80, "Size": 6, "VelZ": 200, "VelSpread": 80, "GravityZ": -400, "BoxXY": 30, "BoxZ": 10, "Lifetime": 2.5, "Color": (1.0, 0.5, 0.1, 1.0)}),
    # card family
    "leaves":    ("leaf", {"SpawnRate": 40, "Size": 22, "VelZ": -25, "VelSpread": 60, "GravityZ": -150, "BoxXY": 250, "BoxZ": 30, "Lifetime": 7.0, "RotRate": 120, "WindXY": 200, "Color": (0.95, 0.55, 0.2, 1.0)}),
    "petals":    ("leaf", {"SpawnRate": 30, "Size": 14, "VelZ": -15, "VelSpread": 40, "GravityZ": -100, "BoxXY": 220, "BoxZ": 30, "Lifetime": 8.0, "RotRate": 90, "WindXY": 150, "Color": (1.0, 0.75, 0.85, 1.0)}),
    "ash":       ("leaf", {"SpawnRate": 50, "Size": 8, "VelZ": -10, "VelSpread": 30, "GravityZ": -60, "BoxXY": 300, "BoxZ": 60, "Lifetime": 9.0, "RotRate": 60, "WindXY": 120, "Color": (0.45, 0.43, 0.4, 0.9)}),
    # mesh family
    "debris":    ("mesh", {"SpawnRate": 100, "Size": 6, "VelZ": 280, "VelSpread": 160, "GravityZ": -900, "BoxXY": 30, "BoxZ": 10, "Lifetime": 3.0, "RotRate": 200, "Color": (0.6, 0.55, 0.5, 1.0)}),
    "gravel":    ("mesh", {"SpawnRate": 60, "Size": 4, "VelZ": 150, "VelSpread": 120, "GravityZ": -900, "BoxXY": 40, "BoxZ": 10, "Lifetime": 2.5, "RotRate": 240, "Color": (0.55, 0.5, 0.45, 1.0)}),
}


def apply_params(comp, params):
    """Write a semantic-param dict onto a NiagaraComponent (auto name/type adaptation; missing params are silently skipped)."""
    velz = params.get("VelZ")
    if velz is not None:
        comp.set_niagara_variable_vec3(_VEC_VELZ, unreal.Vector(0.0, 0.0, float(velz)))
    bxy, bz = params.get("BoxXY"), params.get("BoxZ")
    if bxy is not None or bz is not None:
        comp.set_niagara_variable_vec3(_VEC_BOX, unreal.Vector(float(bxy or 30.0), float(bxy or 30.0), float(bz or 10.0)))
    for sem, real in _FLOATS.items():
        if sem in params:
            comp.set_niagara_variable_float(real, float(params[sem]))
    col = params.get(_COLOR[0])
    if col is not None:
        c = list(col) + [1.0] * (4 - len(col))
        comp.set_niagara_variable_linear_color(_COLOR[1], unreal.LinearColor(c[0], c[1], c[2], c[3]))
    try:
        comp.reinitialize_system()
    except Exception:
        pass
    comp.activate(True)
    return comp


def spawn_fx(preset, location, label=None, overrides=None, prewarm_s=0.0):
    """Spawn a particle FX from a preset. preset = PRESETS key; overrides override semantic params; prewarm_s > 0 advances the sim offline (CPU sim)."""
    kind, base = PRESETS[preset]
    params = dict(base)
    params.update(overrides or {})
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    a = sub.spawn_actor_from_class(unreal.NiagaraActor, unreal.Vector(*location))
    a.set_actor_label(label or ("AutoFxNS_" + preset))
    comp = a.get_component_by_class(unreal.NiagaraComponent)
    comp.set_asset(unreal.load_asset(SYSTEMS[kind]))
    apply_params(comp, params)
    if prewarm_s > 0:
        comp.advance_simulation(int(prewarm_s * 30), 1.0 / 30.0)
    unreal.log("fx '%s' (%s) @ %s" % (preset, kind, list(location)))
    return a
