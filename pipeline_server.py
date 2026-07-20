"""
Flask web service -- object extraction and display from a photo.
Object-extraction core logic with a polished web UI on top.
"""

import io
import os
import sys
import json
import math
import shutil
import uuid
import time
import threading
import functools

# Windows console/redirected streams default to GBK -> printing emoji raises UnicodeEncodeError and kills the run
# (seen live: the 🚀 startup banner + worker-thread ✓/✗ threw exceptions on already-generated, already-billed successes). Reconfigure both streams once, error-tolerant.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import requests as http_requests

from flask import (
    Flask, request, jsonify, render_template,
    send_from_directory, url_for, redirect
)
from google import genai
from google.genai import types as genai_types
from google.genai.errors import ServerError
from PIL import Image
from werkzeug.utils import secure_filename

# ── Config ──────────────────────────────────────────────────
# Key safety: env var > apikeys.local.json (gitignored, never committed) > empty. No plaintext keys in code, ever.
def _local_key(name):
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "apikeys.local.json"), encoding="utf-8") as f:
            return (json.load(f) or {}).get(name, "")
    except Exception:
        return ""


API_KEY = os.environ.get("GEMINI_API_KEY") or _local_key("GEMINI_API_KEY")
TEXT_MODEL = "gemini-3.5-flash"        # parsing/understanding: current-gen Flash, stronger structured output + vision
FALLBACK_TEXT_MODEL = "gemini-2.5-flash"  # 503-storm fallback (2026-06-11 triage: 3.5 alone overloaded, 2.5 healthy)
IMAGE_MODELS = [
    "gemini-3.1-flash-image",          # Nano Banana 2: preferred image generation
    "gemini-2.5-flash-image",          # Nano Banana: fallback image generation
]
IMAGE_SIZE = "1K"                      # resolution of regular object images (1K/2K/4K; higher costs more tokens)
# Scene hero: Nano Banana Pro for max fidelity + higher resolution (tiered like Tripo)
HERO_IMAGE_MODEL = "gemini-3-pro-image"
HERO_IMAGE_SIZE = "2K"

TRIPO3D_API_KEY = os.environ.get("TRIPO3D_API_KEY") or _local_key("TRIPO3D_API_KEY")

# Whether to generate 3D models. True = full Tripo 3D run (costs credits, GLB downloadable);
# False = object images + prompts only; done in seconds, costs no credits.
ENABLE_3D = True

# Monocular depth (Depth Anything V2): real per-pixel depth instead of LLM guesses; improves ordering and scale.
# Off = fall back to the LLM's depth estimate. First run downloads model weights (~100MB).
ENABLE_DEPTH = True
DEPTH_MODEL = "depth-anything/Depth-Anything-V2-Small-hf"

# Imagine beyond the frame: AI guesses what lies outside the photo (left/right/behind), N objects per direction (0 = off)
IMAGINE_OFFSCREEN = True
OFFSCREEN_PER_DIR = 1

# Environment analysis: AI solves the photo's light/atmosphere (sun direction, colour temperature, weather, haze)
# to drive UE's directional light (sun) / skylight / exponential height fog / exposure. Off = UE default lighting.
ENABLE_ENV = True

# Camera match: solve capture height + pitch; UE puts the viewport/camera at "the photo's eye".
ENABLE_CAMERA = True

# Artificial lights: in night/dark/indoor scenes the AI finds street lamps/signs/neon/windows; UE places point lights.
ENABLE_LIGHTS = True
LIGHT_MAX = 12            # max light sources per scene

# HDRI backdrop: out-paint the photo into a 360° panorama; UE feeds it to HDRIBackdrop (black-box fabrication of the unseen sky).
ENABLE_HDRI = True
PREFER_AI_HDRI = True       # True = AI paints the sky from the photo (thesis stance: the whole world is AI-fabricated); False = prefer real Poly Haven HDRIs

# Terrain: pinhole back-project the visible ground into a top-down height grid from the Depth Anything depth map + solved camera
# (theory-driven: flat stays flat, real relief comes through -- NOT the "ask Gemini to paint a top view" approach). Auto-skipped indoors.
ENABLE_TERRAIN = True
ENABLE_FX = True                  # particle/fluid FX (water/waterfall/rain/fog/dust/birds): Gemini decides, instanced statically
TERRAIN_GRID_N = 64                # height grid resolution (N x N)
TERRAIN_MIN_RELIEF_M = 0.6         # below this reconstructed relief -> treat as flat ground (don't turn noise into terrain)
TERRAIN_MAX_RELIEF_M = 80.0        # absolute relief cap (m): stops DA noise/bad geometry from raising fake mountains tens/hundreds of metres tall
TERRAIN_MAX_FOOTPRINT_M = 400.0    # playable-extent cap (m): vista/long-shot photos put kilometres of background into the terrain
                                   # while objects occupy the nearest tens of metres -> huge empty scene, textures stretched to 200x tiling, unwalkable in VR.
                                   # Crop to the nearest 400m; the distance goes to ground skirt + atmospheric fog (generic mechanism; near-field photos unaffected).
TERRAIN_MIN_HALF_M = 40.0          # minimum playable footprint half-size (m): content-centred crop keeps at least 80m square walkable.

# ── Tripo 3D params (tiered quality: scene hero gets best, the rest fast) ──
TRIPO_TEXTURE = True                       # generate textures + PBR
# hero: highest detail
TRIPO_HERO_MODEL = "v3.0-20250812"
TRIPO_HERO_GEOM = "detailed"               # geometry_quality (v3.0+ only)
TRIPO_HERO_TEX = "detailed"                # texture_quality (HD)
# the rest: fast
TRIPO_FAST_MODEL = "Turbo-v1.0-20250506"
TRIPO_FAST_TEX = "standard"

# ── Final-build all-hero switch (env KANA_ALL_HERO=1): every object uses hero quality
#    (Pro images + v3.0 detailed 3D). Only the quality tier is raised; hero SEMANTICS unchanged --
#    camera/ground-pinning/critique protection still follow the Gemini-designated hero. ──
ALL_HERO_QUALITY = os.environ.get("KANA_ALL_HERO", "") == "1"

# ── Tunable: max object count per extraction (homepage slider) ──
MIN_OBJECTS = 2
MAX_OBJECTS = 30
DEFAULT_OBJECTS = 6

# ── Budget / fast mode: one switch downgrades all the expensive settings (for testing; False = back to high quality) ──
#   hero uses flash + Turbo too, monocular depth off, no off-frame objects, fewer objects by default.
FAST_MODE = False
if FAST_MODE:
    ENABLE_DEPTH = False                          # skip CPU depth inference (saves time)
    IMAGINE_OFFSCREEN = False                     # no off-frame objects (cheaper: fewer objects)
    DEFAULT_OBJECTS = 3                           # fewer objects by default
    HERO_IMAGE_MODEL = "gemini-3.1-flash-image"   # hero images use flash instead of Pro
    HERO_IMAGE_SIZE = "1K"
    TRIPO_HERO_MODEL = TRIPO_FAST_MODEL           # hero 3D also uses the fast Turbo tier
    TRIPO_HERO_GEOM = "standard"
    TRIPO_HERO_TEX = TRIPO_FAST_TEX

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
OUTPUT_FOLDER = os.path.join(os.path.dirname(__file__), "output")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "bmp"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["OUTPUT_FOLDER"] = OUTPUT_FOLDER

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# ── Task state ─────────────────────────────────────────────
tasks: dict[str, dict] = {}
_lock = threading.Lock()


def _update_task(task_id: str, **kwargs):
    with _lock:
        if task_id in tasks:
            tasks[task_id].update(kwargs)


# ── Retry decorator ─────────────────────────────────────────
def retry_with_backoff(max_retries=3, base_delay=2):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1 + max_retries):
                try:
                    return func(*args, **kwargs)
                except ServerError as e:
                    last_exc = e
                    if attempt < max_retries:
                        delay = base_delay * (2 ** attempt)
                        print(f"   ⏳ ServerError ({e.code}), retry in {delay}s ({attempt+1}/{max_retries})...")
                        time.sleep(delay)
            raise last_exc
        return wrapper
    return decorator


# ── Gemini structured parsing: forced JSON output + low temperature (replaces brittle manual extraction) ──
def _gen_json(client, image, instruction: str, temperature: float = 0.3, seed: int | None = None):
    """Call Gemini to parse an image into JSON. responseMimeType guarantees legal JSON (no markdown wrapper);
    low temperature + fixed seed make results reproducible (same image -> same output).
    Returns the parsed dict/list; raises on total failure."""
    kwargs = {"temperature": temperature, "responseMimeType": "application/json"}
    if seed is not None:
        kwargs["seed"] = seed
    cfg = genai_types.GenerateContentConfig(**kwargs)
    contents = [image, instruction] if image is not None else [instruction]
    try:
        resp = client.models.generate_content(model=TEXT_MODEL, contents=contents, config=cfg)
    except ServerError as e:
        # 503-storm fallback: single-model overload (triaged live: 3.5-flash 503s while 2.5-flash works with the same key) -> downgrade to the older model,
        # same seed/temperature. Only server-side errors trigger this; our own errors (4xx) still raise.
        if getattr(e, "code", None) != 503:
            raise
        print(f"   ⛑️ {TEXT_MODEL} 503 → fallback {FALLBACK_TEXT_MODEL}")
        resp = client.models.generate_content(model=FALLBACK_TEXT_MODEL, contents=contents, config=cfg)
    raw = (resp.text or "").strip()
    try:
        return json.loads(raw)
    except Exception:
        for a, b in (("{", "}"), ("[", "]")):          # tolerant: slice out the first object/array
            if a in raw and b in raw:
                try:
                    return json.loads(raw[raw.index(a): raw.rindex(b) + 1])
                except Exception:
                    continue
        raise


def _gen_image_content(client, contents, cfg):
    """Single entry point for image generation: iterate IMAGE_MODELS in order; a 503 (single-model overload) moves to the next.
    The fallback list existed all along but one call site used only [0] -- a night task lost its whole terrain-texture set to one 503 spike."""
    last = None
    for mi_, m in enumerate(IMAGE_MODELS):
        try:
            return client.models.generate_content(model=m, contents=contents, config=cfg)
        except ServerError as e:
            last = e
            if getattr(e, "code", None) != 503 or mi_ == len(IMAGE_MODELS) - 1:
                raise
            print(f"   ⛑️ {m} 503 → fallback {IMAGE_MODELS[mi_ + 1]}")
    raise last


def _box2d_to_xywh(box_2d, legacy_bbox=None):
    """Gemini native detection box [ymin,xmin,ymax,xmax] (0-1000) -> internal [x,y,w,h] (0-1).
    Legacy-compatible: with no box_2d but an [x,y,w,h] bbox, use it directly. On failure fall back to a centred default box."""
    try:
        if box_2d and len(box_2d) >= 4:
            ymin, xmin, ymax, xmax = (float(v) for v in box_2d[:4])
            x, y = min(xmin, xmax) / 1000.0, min(ymin, ymax) / 1000.0
            w, h = abs(xmax - xmin) / 1000.0, abs(ymax - ymin) / 1000.0
            return [max(0.0, min(1.0, x)), max(0.0, min(1.0, y)),
                    max(0.01, min(1.0, w)), max(0.01, min(1.0, h))]
        if legacy_bbox and len(legacy_bbox) >= 4:
            b = [float(v) for v in legacy_bbox[:4]]
            return [max(0.0, min(1.0, b[0])), max(0.0, min(1.0, b[1])),
                    max(0.01, min(1.0, b[2])), max(0.01, min(1.0, b[3]))]
    except Exception:
        pass
    return [0.25, 0.25, 0.5, 0.5]


# Real-size priors per object class (metres, longest dimension): clamp the VLM's occasionally absurd size_m. The key job is keeping
# same-class objects (especially vehicles) mutually consistent, since size_m drives both UE scale and inverse-distance solving -> a car given 2m is both half-sized and placed too near. More specific classes precede plain 'car'.
CLASS_SIZE_PRIORS = [
    (("bus", "coach"), 9.0, 14.0),
    (("truck", "lorry", " van"), 4.8, 8.0),
    (("suv", "pickup", "jeep"), 4.4, 5.2),
    (("sedan", "hatchback", "car", "vehicle", "taxi", "automobile"), 4.0, 5.0),
    (("motorcycle", "scooter", "moped", "bicycle"), 1.6, 2.3),
    (("lamp post", "lamppost", "street lamp", "street light", "light pole"), 4.0, 6.0),
    (("pedestrian", "person", "human"), 1.6, 1.9),
    (("bench",), 1.5, 2.2),
    (("kiosk", "phone booth", "morris column"), 2.5, 5.0),
]


def _class_size_prior(prompt: str):
    """Match the prompt text to an object class; return its (min_m, max_m) real-size prior, or None. More specific classes match first.
    Single words match on word boundaries (avoids 'car' in 'carved', 'bus' in 'business'); multi-word phrases use substring match."""
    p = (prompt or "").lower()
    words = set("".join(c if c.isalpha() else " " for c in p).split())   # punctuation-stripped word set, for exact word matches
    for keys, lo, hi in CLASS_SIZE_PRIORS:
        for k in keys:
            k = k.strip()
            if (k in p) if " " in k else (k in words):
                return lo, hi
    return None


# ── Step 1: generate prompts ────────────────────────────────
@retry_with_backoff()
def extract_objects(client, image: Image.Image, max_objects: int = DEFAULT_OBJECTS) -> tuple[list[dict], float]:
    """Extract objects: returns (objects, fov_deg). Each object carries prompt/bbox/depth/size_m/hero/facing_deg;
    returns an empty list when JSON parsing fails."""
    instruction = (
        "Task: reconstruct a COHESIVE 3D SCENE from this photo — NOT a pile of isolated fragments. "
        "Prioritise the big scene-defining structures, fold small details into them, and ignore clutter.\n\n"
        "Rules:\n"
        f"1. PRIORITISE BY SIZE, AIM FOR THE TARGET COUNT. TARGET: {max_objects} objects, ORDERED from "
        "the LARGEST / most scene-defining to the smallest. ALWAYS start with the dominant structures "
        "(mosque, large buildings, monuments), then work DOWN the size ranking until you REACH the "
        f"target. Output fewer than {max_objects} ONLY if the photo genuinely does not contain that many "
        "distinct, whole, placeable objects satisfying the rules below — NEVER pad with fragments, "
        "clutter or duplicates just to hit the number. A few big coherent objects beat many tiny "
        "FRAGMENTS, but when the budget allows, MORE genuinely separate whole structures (each distinct "
        "building, each significant free-standing fixture) make a RICHER rebuilt scene — do not stop "
        "early out of minimalism.\n"
        "1b. URBAN SCENES (street / alley / city block with several visible buildings): buildings ARE "
        "the scene — spend MOST of the object budget (aim for at least ~60% of the slots) on DISTINCT "
        "BUILDINGS before spending any on cars, poles or props. Every physically separate building that "
        "shows enough of itself deserves its OWN slot: the blocks on BOTH sides of the street, the corner "
        "building, the taller one behind. Without enough building volumes a rebuilt street feels empty. "
        "This does NOT override rule 5c — a single receding ROW is still represented by its nearest "
        "compact unit — but genuinely SEPARATE buildings along the street each count, near AND far.\n"
        "2. ABSORB ATTACHMENTS — key for scene cohesion. Any sign, balcony, awning, banner, shopfront, "
        "window, lamp or ornament that is ATTACHED TO a building MUST be described as PART of that "
        "building's prompt (one combined model). Do NOT extract attached details as separate objects.\n"
        "3. Extract a small thing on its OWN only if it is a SIGNIFICANT FREE-STANDING object (statue, "
        "fountain, tree, car, kiosk, free-standing street lamp).\n"
        "4. IGNORE CLUTTER, MERGE MINOR REPEATS: skip tiny flags, small loose signs, people, and anything "
        "roughly under ~2m that is not visually important — leave it out entirely. MINOR repeated objects "
        "(poles, bollards, near-identical parked cars) get at most ONE or TWO representative slots between "
        "them. BUT large or scene-defining repeats (distinct trees, distinct buildings, distinct boulders) "
        "are NOT clutter: while slots remain before the target count, physically separate instances that "
        "visibly differ in shape / size / appearance may EACH take their own slot — pick the most "
        "distinct-looking ones. Never invent instances that are not actually in the photo.\n"
        "5. WHOLE OBJECTS, NO DUPLICATES, NO FRAGMENTS. Each object is one entire, self-contained thing; "
        "never a fragment/part, and never the same real structure twice (a different angle/wording is "
        "still a duplicate, forbidden). If an object is CUT OFF by the image border or only PARTIALLY "
        "visible (e.g. just the corner of a car in the foreground, half a building at the edge), either "
        "describe it as a COMPLETE whole object (mentally completing the unseen part) or LEAVE IT OUT — "
        "never output a cropped/partial piece.\n"
        "5b. ONE PHYSICAL STRUCTURE = ONE OBJECT. Objects must be PHYSICALLY SEPARATE, DETACHED "
        "structures. NEVER split ONE continuous structure into several objects — this includes a building "
        "and its own tower / dome / wing / facade, AND a BRIDGE together with its towers, pylons, lifting "
        "mechanism, railings and span (a drawbridge / lift bridge is ONE object = deck + lifting gear + "
        "towers combined), AND any single continuous span, wall, colonnade, pier or roof. THEORY — why "
        "this is critical: every object is placed in 3D by ITS OWN apparent size and screen position, so "
        "splitting one real structure scatters its parts to different distances and sides and wrecks the "
        "layout. Therefore, when one structure shows distinct visual parts, emit ONE object whose prompt "
        "describes the WHOLE structure (naming all its parts) and fold the parts in. Only emit a second "
        "object if it is unmistakably a DIFFERENT, physically detached structure.\n"
        "5c. PLACEABLE, COMPACT OBJECTS ONLY — the engine places each object at ONE point in space, so a "
        "single position must represent it well. Prefer objects that are reasonably COMPACT (roughly as "
        "wide/tall as they are deep). If an element RECEDES FAR INTO DEPTH away from the camera (a ROW of "
        "houses, a wall, fence, railing, colonnade or quay trailing down a street/canal toward the "
        "horizon), it has NO single correct position — do NOT output the whole receding run as one object. "
        "Instead extract ONLY its single NEAREST, most prominent COMPACT unit (one building / one house) "
        "as a stand-in, and let the rest be background (leave it out). A structure that spans ACROSS your "
        "view fronto-parallel (a bridge, one wide facade) is fine as one object; it is the "
        "receding-INTO-DEPTH runs you must not place as a single piece.\n"
        "6. SOLID TANGIBLE THINGS ONLY (buildings, structures, monuments, trees, vehicles, statues…). "
        "NEVER sky, clouds, fog, smoke, haze, light, water, reflections, shadows, terrain, ground, road "
        "or background scenery. The asset goes into UE5 with its own HDR sky. In INDOOR photos, NEVER "
        "extract walls, wall panels/sections, windows, window frames, doors, floors or ceilings as "
        "objects — the engine builds the room architecture itself (a 'wall with window' object would "
        "stand as a dead slab in front of the real window).\n"
        "7. Each prompt is fed to an image model with the original photo: describe that ONE object "
        "(WITH its absorbed attachments) — appearance, colour, texture, shape — never the surrounding "
        "scene, sky or other objects. Stay FAITHFUL to how the object ACTUALLY looks in THIS photo: its "
        "real colours, materials, age and weathering (if it is old, dirty, painted, rusted or worn, keep "
        "that) — do NOT idealise, clean up or restyle it. For any BUILDING or STRUCTURE, explicitly render "
        "it as a COMPLETE, FREE-STANDING VOLUME seen in 3/4 perspective (its front AND one side both "
        "visible, with real depth and a roof) — NOT a flat front facade — so it reconstructs as a solid 3D "
        "building instead of a thin slab. Reasonably infer the unseen sides from the visible architecture.\n"
        "8. Composition every prompt MUST enforce: the object rendered ALONE on a pure solid black "
        "background (#000000), nothing else in frame; the WHOLE object complete and never cropped; "
        "centered with a comfortable empty black margin (~15%) on all sides; no part touching any border.\n\n"
        "OUTPUT — return ONLY a raw JSON object (no markdown, no code fences, no extra text):\n"
        '  {"fov_deg": <number>, "objects": [ {"prompt": "...", "box_2d": [ymin,xmin,ymax,xmax], '
        '"depth": <number>, "size_m": <number>, "hero": <true/false>, "facing_deg": <number>, '
        '"man_made": <true/false>} ]}\n'
        "- man_made: true for a MAN-MADE / manufactured object that rests ON the ground and must NOT sink "
        "into it (vehicle, bicycle, bench, lamp post, kiosk, statue, furniture, building); false for a "
        "NATURAL object that may partially embed / take root (rock, boulder, tree, log, bush).\n"
        "- hero: mark EXACTLY ONE object — the single most important CENTERPIECE / main subject of the "
        "whole scene (usually the dominant front structure) — as true; every other object false.\n"
        "- facing_deg: the yaw the object's FRONT (the face you can see) is turned, viewed from above, in "
        "degrees. 0 = its front squarely faces the camera (flat frontal view). +90 = front turned to the "
        "RIGHT (you mostly see its LEFT side). -90 = front turned LEFT. ±180 = you see its back. Most "
        "facades are frontal or oblique (typically -60..+60). Judge from the perspective of its edges.\n"
        "- fov_deg: estimate the camera's HORIZONTAL field of view in degrees (typical phone ~60-70, "
        "wide ~80, telephoto ~35). Reason from the perspective/compression of the scene.\n"
        "- box_2d: the object's TIGHT bounding box in the photo as [ymin, xmin, ymax, xmax], each value "
        "normalized to 0-1000 (this is your standard object-detection format — be precise).\n"
        "- depth: 0..1 — 0 = nearest, 1 = farthest.\n"
        "- size_m: act as an ARCHITECTURAL SURVEYOR sizing the object FOR PERSPECTIVE DISTANCING. Report, "
        "in METRES, the real-world size of the extent that the object's BOUNDING BOX actually frames and "
        "that runs ACROSS your view (fronto-parallel — its visible width or height as seen), NOT a "
        "dimension that recedes INTO the distance away from the camera. THEORY: the pipeline divides this "
        "size by the box's apparent angular size to recover the camera distance, so size_m MUST match what "
        "the box spans across-view, or the object lands at the wrong depth. Hence: a building seen 3/4-on "
        "→ the width of its visible facade (not a diagonal, not its depth); a ROW of houses / wall / "
        "colonnade / fence RECEDING along a street or canal → only the real cross-view width of the few "
        "units the box actually covers (often ~8-25m), NOT the full length of the whole receding row; a "
        "bridge spanning across the view → its visible span; a tower → its height. ANCHOR to well-known "
        "real sizes — outdoor: one storey ~3m, door ~2m, person ~1.7m; EVERY road vehicle "
        "(car/sedan/hatchback/SUV/taxi) MUST be 4.0-5.0m long (an SUV/pickup 4.4-5.2m, a bus ~12m) and ALL "
        "vehicles in the scene MUST share a consistent length — never output one car at half the size of "
        "another; INDOOR FURNITURE (use "
        "these so indoor objects do NOT come out oversized): interior door ~2m, person ~1.7m, sofa "
        "~1.8-2.2m wide, armchair ~0.8m, floor cushion / bean bag / pouf ~0.7-1.0m, coffee table ~1.0-1.2m "
        "wide & ~0.4m high, sideboard / shelf unit ~0.7-0.9m high, dining chair ~0.45m seat, floor lamp "
        "~1.5-1.8m tall, desk ~1.2m, rug ~2-3m. This also drives the 3D scale. Keep all objects MUTUALLY "
        "CONSISTENT in relative size and depth (a car is smaller than the building behind it; a nearer "
        "object has a smaller depth).\n"
        f"TARGET COUNT: {max_objects} objects — reach it unless the photo truly lacks that many valid "
        "whole objects (never pad with junk to reach it). Output ONLY the JSON object, nothing else."
    )
    objects, fov = [], 65.0
    try:
        data = _gen_json(client, image, instruction, temperature=0.3, seed=7)
        fov = float(data.get("fov_deg", 65.0))
        for o in data.get("objects", []):
            p = (o.get("prompt") or "").strip()
            if not p:
                continue
            bbox = _box2d_to_xywh(o.get("box_2d"), o.get("bbox"))
            sm = float(o.get("size_m", 1.0))
            prior = _class_size_prior(p)              # class-prior clamp: keeps same-class objects (esp. vehicles) consistent, fixes occasional absurd size_m
            if prior:
                lo, hi = prior
                sm2 = max(lo, min(hi, sm))
                if abs(sm2 - sm) > 1e-3:
                    print(f"[Step1] size prior clamp: '{p[:32]}' {sm:.1f}m -> {sm2:.1f}m")
                sm = sm2
            objects.append({"prompt": p, "bbox": bbox,
                            "depth": float(o.get("depth", 0.5)),
                            "size_m": sm,
                            "hero": bool(o.get("hero", False)),
                            "man_made": bool(o.get("man_made", False)),
                            "facing_deg": float(o.get("facing_deg", 0.0))})
    except ServerError:
        raise                    # let @retry_with_backoff retry 503s
    except Exception as e:
        print(f"[Step1] extract parse failed: {e}", file=sys.stderr)
    objects = objects[:max_objects]
    # Ensure exactly one hero (none marked -> take the first largest; several -> keep only the first)
    heroes = [o for o in objects if o.get("hero")]
    if not heroes and objects:
        objects[0]["hero"] = True
    elif len(heroes) > 1:
        kept = False
        for o in objects:
            if o.get("hero"):
                o["hero"] = not kept
                kept = True
    fov = max(25.0, min(100.0, fov))
    print(f"[Step1] Extracted {len(objects)} objects, fov≈{fov:.0f}°")
    return objects, fov


# ── Step 1b: imagine off-frame objects (black-box fabrication of the unseen world) ──
@retry_with_backoff()
def imagine_offscreen(client, image: Image.Image, per_dir: int = 1) -> list[dict]:
    """Ask the AI to guess objects beyond the photo (left/right/behind); returns imagined objects tagged with direction."""
    instruction = (
        "You are a rigorous 3D scene-continuation inference system. Examine this UNKNOWN photo and, using "
        "the universal physical/statistical method below, infer what most likely exists JUST OUTSIDE the "
        "frame in EACH direction: 'left', 'right', and 'behind' the camera. This must work for ANY scene "
        "(a city corner, a museum corridor, an alley, an interior, a landscape…).\n\n"
        "STEP 1 — ENVIRONMENT BASELINE (analyse first):\n"
        "  a) Semantic zoning: classify the scene's functional type (e.g. dense commercial street, "
        "run-down residential alley, modern indoor exhibition hall, grand museum corridor, natural "
        "landscape).\n"
        "  b) Edge materiality: inspect the ~10% strip of the image nearest EACH target direction and "
        "note the dominant material there (e.g. red brick, peeling concrete, reflective glass, wood, "
        "stone, marble, vegetation).\n"
        "  c) Lighting origin: from shadows and highlights, infer the main light direction.\n\n"
        f"STEP 2 — GROUNDED FABRICATION: for EACH direction invent {per_dir} object(s)/structure(s) that "
        "STRICTLY satisfy:\n"
        "  - Probability: it is the statistically most typical fixture of that functional zone.\n"
        "  - Material continuation: its face toward the photo carries and continues the edge material you "
        "noted for that direction.\n"
        "  - Lighting imprint: its description states a lighting direction consistent with the original.\n"
        "  - Form: a COMPLETE free-standing object/building in 3/4 perspective (front + one side, real "
        "depth and a roof), ALONE on a pure solid black (#000000) background, centered with ~15% margin, "
        "nothing else. SOLID tangible things only — no sky, ground, people. Do NOT repeat visible "
        "objects, and AVOID inventing a type that already appears several times on-screen (e.g. if the "
        "photo is full of cars, do not add another car) — prefer variety that enriches the scene.\n\n"
        "OUTPUT — return ONLY a raw JSON array (no markdown, no extra text):\n"
        '  [ {"prompt":"<rich English 3D-model prompt with the zoning/material/lighting constraints baked '
        'in>", "size_m":<number>, "direction":"left|right|behind"} ]\n'
        "- size_m: realistic real-world size in metres (largest dimension), and it MUST be consistent with "
        "the scale of the objects visible in the photo — an indoor scene gets furniture-scale things "
        "(~0.5-3m), a street gets building-scale things; never put a building-sized object into a small "
        "room or a tiny prop into a vast cityscape.\n"
        "Output ONLY the JSON array, nothing else."
    )
    out = []
    try:
        arr = _gen_json(client, image, instruction, temperature=0.6)
        if isinstance(arr, dict):                       # tolerant: may arrive wrapped as {"objects":[...]}
            arr = arr.get("objects") or arr.get("items") or []
        for o in (arr or []):
            p = (o.get("prompt") or "").strip()
            if not p:
                continue
            d = (o.get("direction") or "left").strip().lower()
            if d not in ("left", "right", "behind", "front"):
                d = "left"
            out.append({"prompt": p, "bbox": [0.3, 0.3, 0.4, 0.4], "depth": 0.7,
                        "size_m": float(o.get("size_m", 10.0)),
                        "imagined": True, "direction": d})
    except ServerError:
        raise
    except Exception as e:
        print(f"[Imagine] parse failed: {e}", file=sys.stderr)
    print(f"[Imagine] {len(out)} off-screen objects")
    return out


# ── Step 1c: environment analysis (solve light/atmosphere -> drive UE sun/skylight/fog/exposure) ──
# UE world-yaw convention matches objects: 0=+X (forward, away from camera), 90=+Y (right), 180=-X (towards camera), -90=-Y (left).
ENV_DEFAULT = {
    "time_of_day": "midday", "weather": "clear",
    "sun_yaw_deg": 135.0, "sun_pitch_deg": -45.0,
    "sun_intensity_lux": 75000.0, "color_temp_k": 6500.0,
    "sky_intensity": 1.0, "fog_density": 0.003, "fog_color": [0.6, 0.7, 0.85],
    "cloud_coverage": 0.2, "exposure_ev": 0.0, "sky_luminance": 0.5,
    "saturation": 1.0, "contrast": 1.0, "bloom": 0.5, "mood": "",
    # Cinematic post (Gemini judges the photo's look; code maps + clamps): white balance/vignette/grain/chromatic aberration/grade + dreamcore (split-toning/fade/soft glow)
    "post": {"warmth": 0.0, "tint": 0.0, "vignette": 0.4, "grain": 0.0,
             "chromatic_aberration": 0.0, "color_cast": [1.0, 1.0, 1.0],
             "split_shadow": [1.0, 1.0, 1.0], "split_highlight": [1.0, 1.0, 1.0],
             "fade": 0.0, "dreaminess": 0.0},
    "ground_material": "asphalt", "ground_color": [0.18, 0.18, 0.19],
    "ground_roughness": 0.85, "ground_wetness": 0.0,
    "sky_color": [0.35, 0.55, 0.85], "horizon_color": [0.70, 0.80, 0.90],
    # Scene fill (fills an empty frame centre): what vegetation should fill this scene's near/mid empty ground (trees/shrubs/grass) + how dense; code scatters along the playable-area edge, keeping the subject sightline clear
    "scene_fill": {"species": "none", "density": 0.0},
    # Ambient life (makes the scene feel alive): pedestrian silhouettes + birds; code places silhouette billboards / bird flocks; at night pedestrians stick to light pools
    "ambient_life": {"pedestrians": 0.0, "birds": 0.0},
}


def _clampf(v, lo, hi, default):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


@retry_with_backoff()
def analyze_environment(client, image: Image.Image) -> dict:
    """Have the VLM solve this photo's light and atmosphere, returning values UE can consume directly."""
    instruction = (
        "You are a rigorous cinematography / lighting analyst. Study this photo and SOLVE its real "
        "lighting and atmosphere, outputting concrete values an Unreal Engine 5 scene can use directly. "
        "Reason from the visible CUES: shadow directions and lengths, highlight positions, sky colour and "
        "brightness, haze/contrast falloff with distance, white balance of surfaces.\n"
        "Coordinate convention (top-down): yaw 0 = +X (forward, away from the camera), 90 = +Y (camera's "
        "right), 180 = -X (toward the camera), -90 = -Y (left).\n\n"
        "Return ONLY a raw JSON object (no markdown):\n"
        "{\n"
        '  "time_of_day": "sunrise|golden hour|midday|afternoon|dusk|night|overcast",\n'
        '  "weather": "clear|partly cloudy|overcast|foggy|rainy|snowy",\n'
        '  "sun_yaw_deg": <0-360, the compass yaw the SUNLIGHT COMES FROM, inferred from shadow direction>,\n'
        '  "sun_pitch_deg": <-90..0, sun elevation as a downward pitch: -10 = low/long shadows near sunset, '
        "-75 = high midday sun>,\n"
        '  "sun_intensity_lux": <UE DirectionalLight lux: ~110000 bright midday, ~75000 hazy sun, ~20000 '
        "golden hour, ~3000 overcast, ~50 dusk, ~0 night>,\n"
        '  "color_temp_k": <2000-12000 white balance: ~2800 warm dusk, ~5000 afternoon, ~6500 midday, '
        "~7500 overcast, ~9000 blue shade>,\n"
        '  "sky_intensity": <0-3 relative SkyLight ambient strength: ~1 normal, ~1.6 bright overcast, '
        "~0.3 night>,\n"
        '  "sun_soft_angle_deg": <0.2-8 light source angular size, READ FROM SHADOW EDGES: razor-sharp '
        "shadows ~0.5 (clear sun), slightly soft ~1.5 (thin haze), mushy/diffuse ~4-8 (overcast)>,\n"
        '  "godray_strength": <0-1 volumetric light-shaft strength: 0 none, ~0.3 subtle atmosphere, '
        "~0.8 visible god-rays through haze/trees>,\n"
        '  "ambient_ratio": <0.03-0.35 how bright SHADOWED areas are vs sunlit areas IN THIS PHOTO: '
        "deep black shadows ~0.05, normal day ~0.15, flat overcast ~0.3>,\n"
        '  "distant_terrain": "flat|dunes|hills|mountains|water",   // what the land BEYOND the visible '
        "area most plausibly looks like (read the horizon / biome): desert->dunes, meadow->hills, "
        "alpine->mountains, coast->water, city/plaza->flat,\n"
        '  "distant_ruggedness": <0-1 how dramatic that distant relief is>,\n'
        '  "distant_sectors": {   // OPTIONAL per-direction refinement (real horizons are asymmetric):\n'
        '    "front": {"t": "<flat|dunes|hills|mountains|water>", "rug": <0-1>},  // what the PHOTO shows at its horizon\n'
        '    "left":  {"t": "...", "rug": <0-1>},   // plausible biome continuation to the camera-left\n'
        '    "right": {"t": "...", "rug": <0-1>},\n'
        '    "back":  {"t": "...", "rug": <0-1>}    // the unseen world behind the camera\n'
        "  },\n"
        '  "distant_city": {   // does a CITY/TOWN continue into the distance beyond the visible buildings?\n'
        '     "present": <true|false>,    // true ONLY for urban/town scenes (false for nature/forest/lake/desert/lone building)\n'
        '     "density": <0-1>,           // how packed the distant skyline is: sparse town 0.2, dense city 0.6, megacity 0.95\n'
        '     "height": <0-1>,            // distant building height bias: low-rise 0.2, mid-rise 0.5, towers/high-rise 0.9\n'
        '     "glow_k": <2200-6000>,      // night colour temp of the distant city glow (warm sodium 2500, mixed 3500, cool LED 5000)\n'
        '     "liveliness": <0-1>         // how LIT-UP/alive the distant city is at night: sleepy quiet town 0.2, ordinary city 0.55, never-sleeps bright metropolis 0.95\n'
        "  },\n"
        '  "scene_fill": {   // what VEGETATION should fill the empty near/mid ground AROUND the scene (frames + adds depth; placed at the playable edge, NOT over the subject)\n'
        '     "species": "<none|tree_green|tree_autumn|tree_dead|bush|fern|grass_tall|grass_short|grass_moor|weed>",  '
        "// the dominant plant the photo IMPLIES around this place: leafy/blossom street trees & parks -> tree_green, autumn foliage -> tree_autumn, "
        "bare winter/dead -> tree_dead, hedged yard/shrubland -> bush, meadow/field -> grass_tall, lawn -> grass_short, moor/scrub -> grass_moor; "
        'pure paved plaza / desert / indoor / open water with NO plants -> "none"\n'
        '     "density": <0-1>           // how much to fill: a couple of accent trees 0.2, a tree-lined street/park edge 0.5, dense surrounding woods 0.9 (0 = none)\n'
        "  },\n"
        '  "ambient_life": {   // LIVING activity to bring the scene alive (people + birds), judged from place + time\n'
        '     "pedestrians": <0-1>,  // how many PEOPLE would be out: empty alley / wilderness 0.0, quiet residential street 0.2, normal sidewalk 0.5, busy plaza/market 0.9 (lower at deep night)\n'
        '     "birds": <0-1>         // bird activity: none/night 0.0, a few 0.3, a wheeling flock at dusk/over water/fields 0.8\n'
        "  },\n"
        '  "fog_density": <0-0.08 UE ExponentialHeightFog density: ~0.002 crisp clear, ~0.01 city haze, '
        "~0.04 foggy>,\n"
        '  "sky_luminance": <0-1 how BRIGHT/luminous the SKY ITSELF looks in the photo (drives the AI sky dome '
        "brightness): deep dark storm/overcast night 0.2, clear starry or moonlit night 0.35, dim overcast dusk/dawn "
        "0.5, bright overcast day 0.7, clear blue sky / glowing sunset 0.9>,\n"
        '  "fog_color": [r,g,b] each 0-1, the colour of the distant haze/atmosphere,\n'
        '  "cloud_coverage": <0-1>,\n'
        '  "exposure_ev": <-2..2 exposure compensation so the render matches the photo brightness>,\n'
        '  "saturation": <0.4-1.8 overall colour saturation vs neutral 1.0: vivid sunny ~1.2, muted '
        "overcast/foggy ~0.8>,\n"
        '  "contrast": <0.6-1.5 tonal contrast vs neutral 1.0: harsh sun ~1.25, flat overcast ~0.9>,\n'
        '  "bloom": <0-1 glow/bloom strength for bright sources: dim flat daylight ~0.3, glowing neon '
        "night / lit signs ~1.0>,\n"
        '  "post": {        // cinematic FILM-LOOK grade. Be EXPRESSIVE & BOLD — strong split-toning, decisive '
        "colour casts, a stylised dreamlike grade (this is an oneiric MEMORY reconstruction, NOT a flat copy). "
        "BUT keep overall SATURATION on the low / desaturated / faded side (muted memory, never candy-bright):\n"
        '     "warmth": <-1..1 overall colour temperature MOOD: -1 cold blue (moonlight/clinical/dread), 0 neutral, '
        "+1 warm amber (golden hour / sodium street-lamp / cozy interior)>,\n"
        '     "tint": <-1..1 green(-) to magenta(+) cast, usually -0.3..0.3>,\n'
        '     "vignette": <0-1 darkening toward the frame edges: 0 flat/open, 0.4 gentle cinematic, 0.8 heavy / '
        "intimate / closing-in>,\n"
        '     "grain": <0-1 film/sensor GRAIN texture: 0 crisp clean digital, 0.3 subtle filmic, 0.7 grainy '
        "low-light phone / analog>,\n"
        '     "chromatic_aberration": <0-1 lens colour-fringing at edges: 0 clean pro lens, 0.3 subtle, 0.8 '
        "cheap-lens / dreamy>,\n"
        '     "color_cast": [r,g,b],  // subtle overall colour-grade tint, each 0.7-1.3, [1,1,1]=neutral (e.g. teal '
        "shadows [0.95,1.0,1.05], warm [1.05,1.0,0.95])\n"
        "     // —— DREAMY / liminal / dreamcore grade (this is a half-remembered MEMORY reconstruction — lean "
        "soft & dreamlike, but pick the actual colours/amounts FROM the photo) ——\n"
        '     "split_shadow": [r,g,b],     // SPLIT-TONING colour pushed into the SHADOWS, each 0.7-1.3 (1=neutral); '
        "dreamy = cool teal/blue/lilac e.g. [0.88,1.0,1.15]\n"
        '     "split_highlight": [r,g,b],  // colour pushed into the HIGHLIGHTS, each 0.7-1.3; dreamy = warm '
        "peach/pink/gold e.g. [1.12,1.0,0.93]\n"
        '     "fade": <0-1 faded/milky lifted blacks (old-photo / dream haze): 0 true blacks, 0.4 soft faded, '
        "0.8 washed-out dreamy>,\n"
        '     "dreaminess": <0-1 soft hazy GLOW/halation (dreamcore bloom-halo): 0 crisp, 0.4 gentle glow, '
        "0.8 heavy dream-haze>\n"
        "  },\n"
        '  "ground_material": "asphalt|concrete|cobblestone|brick|dirt|sand|grass|marble|wood|water|snow",\n'
        '  "ground_color": [r,g,b] each 0-1, the dominant colour of the ground / floor underfoot,\n'
        '  "ground_roughness": <0-1 surface roughness: polished marble ~0.1, wet asphalt ~0.3, dry asphalt '
        "~0.85, grass/dirt ~1.0>,\n"
        '  "ground_wetness": <0-1 how wet/rainy the ground looks (puddles, sheen, reflections): dry ~0, '
        "damp ~0.4, rainy/puddled ~0.85>,\n"
        '  "sky_color": [r,g,b] each 0-1, the dominant colour of the SKY high up (zenith); if the sky is '
        "not visible, infer it from the weather/time,\n"
        '  "horizon_color": [r,g,b] each 0-1, the sky colour low near the horizon,\n'
        '  "mood": "<a few words: the lighting mood>"\n'
        "}\n"
        "Output ONLY the JSON object, nothing else."
    )
    env = dict(ENV_DEFAULT)
    try:
        d = _gen_json(client, image, instruction, temperature=0.0, seed=7)
        env["time_of_day"] = str(d.get("time_of_day", env["time_of_day"]))[:24]
        env["weather"] = str(d.get("weather", env["weather"]))[:24]
        env["mood"] = str(d.get("mood", ""))[:60]
        env["sun_yaw_deg"] = _clampf(d.get("sun_yaw_deg"), 0.0, 360.0, env["sun_yaw_deg"])
        env["sun_pitch_deg"] = _clampf(d.get("sun_pitch_deg"), -90.0, 0.0, env["sun_pitch_deg"])
        env["sun_intensity_lux"] = _clampf(d.get("sun_intensity_lux"), 0.0, 130000.0, env["sun_intensity_lux"])
        env["color_temp_k"] = _clampf(d.get("color_temp_k"), 1800.0, 12000.0, env["color_temp_k"])
        env["sky_intensity"] = _clampf(d.get("sky_intensity"), 0.0, 4.0, env["sky_intensity"])
        env["sun_soft_angle_deg"] = _clampf(d.get("sun_soft_angle_deg"), 0.2, 8.0, 1.0)
        env["godray_strength"] = _clampf(d.get("godray_strength"), 0.0, 1.0, 0.4)
        env["ambient_ratio"] = _clampf(d.get("ambient_ratio"), 0.03, 0.35, 0.10)
        dt = str(d.get("distant_terrain", "flat")).lower().strip()
        env["distant_terrain"] = dt if dt in ("flat", "dunes", "hills", "mountains", "water") else "flat"
        env["distant_ruggedness"] = _clampf(d.get("distant_ruggedness"), 0.0, 1.0, 0.4)
        secs = d.get("distant_sectors") or {}
        out_secs = {}
        for k in ("front", "left", "right", "back"):
            s = secs.get(k) or {}
            t = str(s.get("t", env["distant_terrain"])).lower().strip()
            out_secs[k] = {"t": t if t in ("flat", "dunes", "hills", "mountains", "water") else env["distant_terrain"],
                           "rug": _clampf(s.get("rug"), 0.0, 1.0, env["distant_ruggedness"])}
        env["distant_sectors"] = out_secs
        dc = d.get("distant_city") or {}
        env["distant_city"] = {"present": bool(dc.get("present", False)),
                               "density": _clampf(dc.get("density"), 0.0, 1.0, 0.5),
                               "height": _clampf(dc.get("height"), 0.0, 1.0, 0.5),
                               "glow_k": _clampf(dc.get("glow_k"), 2200.0, 6000.0, 3200.0),
                               "liveliness": _clampf(dc.get("liveliness"), 0.0, 1.0, 0.55)}
        sfl = d.get("scene_fill") or {}                  # scene fill: species must be legal (else none), density clamped 0-1
        sf_sp = str(sfl.get("species", "none")).lower().strip()
        env["scene_fill"] = {"species": sf_sp if sf_sp in VEG_SPECIES else "none",
                             "density": _clampf(sfl.get("density"), 0.0, 1.0, 0.0)}
        alf = d.get("ambient_life") or {}                # ambient life: pedestrian/bird density clamped 0-1
        env["ambient_life"] = {"pedestrians": _clampf(alf.get("pedestrians"), 0.0, 1.0, 0.0),
                               "birds": _clampf(alf.get("birds"), 0.0, 1.0, 0.0)}
        env["fog_density"] = _clampf(d.get("fog_density"), 0.0, 0.1, env["fog_density"])
        env["cloud_coverage"] = _clampf(d.get("cloud_coverage"), 0.0, 1.0, env["cloud_coverage"])
        env["sky_luminance"] = _clampf(d.get("sky_luminance"), 0.0, 1.0, env.get("sky_luminance", 0.5))
        pj = d.get("post")                              # cinematic post (fresh dict; never mutate the shared ENV_DEFAULT reference)
        if isinstance(pj, dict):
            def _rgb(key, lo=0.6, hi=1.4):
                v = pj.get(key)
                return ([_clampf(v[i], lo, hi, 1.0) for i in range(3)]
                        if isinstance(v, (list, tuple)) and len(v) >= 3 else [1.0, 1.0, 1.0])
            env["post"] = {
                "warmth": _clampf(pj.get("warmth"), -1.0, 1.0, 0.0),
                "tint": _clampf(pj.get("tint"), -1.0, 1.0, 0.0),
                "vignette": _clampf(pj.get("vignette"), 0.0, 1.0, 0.4),
                "grain": _clampf(pj.get("grain"), 0.0, 1.0, 0.0),
                "chromatic_aberration": _clampf(pj.get("chromatic_aberration"), 0.0, 1.0, 0.0),
                "color_cast": _rgb("color_cast", 0.5, 1.5),
                "split_shadow": _rgb("split_shadow", 0.5, 1.5),
                "split_highlight": _rgb("split_highlight", 0.5, 1.5),
                "fade": _clampf(pj.get("fade"), 0.0, 1.0, 0.0),
                "dreaminess": _clampf(pj.get("dreaminess"), 0.0, 1.0, 0.0),
            }
        env["exposure_ev"] = _clampf(d.get("exposure_ev"), -3.0, 3.0, env["exposure_ev"])
        env["saturation"] = _clampf(d.get("saturation"), 0.2, 2.0, env["saturation"])
        env["contrast"] = _clampf(d.get("contrast"), 0.4, 2.0, env["contrast"])
        env["bloom"] = _clampf(d.get("bloom"), 0.0, 1.0, env["bloom"])
        env["ground_material"] = str(d.get("ground_material", env["ground_material"]))[:24]
        env["ground_roughness"] = _clampf(d.get("ground_roughness"), 0.0, 1.0, env["ground_roughness"])
        env["ground_wetness"] = _clampf(d.get("ground_wetness"), 0.0, 1.0, env["ground_wetness"])
        fc = d.get("fog_color")
        if isinstance(fc, (list, tuple)) and len(fc) >= 3:
            env["fog_color"] = [_clampf(fc[i], 0.0, 1.0, env["fog_color"][i]) for i in range(3)]
        gc = d.get("ground_color")
        if isinstance(gc, (list, tuple)) and len(gc) >= 3:
            env["ground_color"] = [_clampf(gc[i], 0.0, 1.0, env["ground_color"][i]) for i in range(3)]
        for key in ("sky_color", "horizon_color"):
            v = d.get(key)
            if isinstance(v, (list, tuple)) and len(v) >= 3:
                env[key] = [_clampf(v[i], 0.0, 1.0, env[key][i]) for i in range(3)]
    except ServerError:
        raise
    except Exception as e:
        print(f"[Env] parse failed, using defaults: {e}", file=sys.stderr)
    print(f"[Env] {env['time_of_day']} / {env['weather']} / sun {env['sun_yaw_deg']:.0f}°,{env['sun_pitch_deg']:.0f}° "
          f"/ {env['color_temp_k']:.0f}K / fog {env['fog_density']:.3f}")
    return env


# ── Step 1c2: ground-dressing analysis (Gemini decides what to scatter / density / size / colour / clumping; we execute verbatim) ──
DRESSING_SHAPES = ("rock", "card", "plant")
DRESSING_PLACES = ("ground", "near_objects", "everywhere")
# Real vegetation species (mapped to UE VegetationPack meshes); when shape==plant Gemini picks one
VEG_SPECIES = ("tree_green", "tree_autumn", "tree_dead", "bush", "ivy", "plant",
               "fern", "grass_tall", "grass_short", "grass_moor", "weed")


@retry_with_backoff()
def analyze_ground_dressing(client, image: Image.Image, environment: dict | None = None) -> dict:
    """Let the VLM look at the photo and decide what ground dressing to scatter, emitting a GENERIC dressing plan the executor follows verbatim (no hardcoded styles).
    Each layer = one dressing kind, carried by generic shape primitives (rock = solid chunk / card = crossed cards) -- these two cover nearly all natural ground dressing;
    appearance (count/size/colour/clumping/slope/placement/companion) is entirely Gemini's call per photo. Returns {should_dress, biome, layers:[...]}."""
    env_hint = ""
    if environment:
        env_hint = ("\nContext from lighting analysis: ground reads as '%s', dominant ground colour ~%s, "
                    "weather '%s' — stay consistent with this.\n" % (
                        environment.get("ground_material", "?"),
                        [round(c, 2) for c in (environment.get("ground_color") or [])],
                        environment.get("weather", "?")))
    instruction = (
        "You are an environment artist dressing a 3D scene to match THIS photo's GROUND. Look at what "
        "actually litters/covers the ground and decide what small SCATTER props make it a faithful, richer "
        "surface, and HOW they sit. Decide EVERYTHING yourself from the image — do not assume a fixed style. "
        "Reasoning examples (do not copy, infer from the actual photo): rocky chalk meadow -> scattered "
        "stones with smaller pebbles clustered around them, tufts of grass, a few wildflowers; arid desert "
        "dirt -> pebbles, dry shrubs, sparse dry grass; manicured lawn -> dense short grass only; bare "
        "pavement / open water / snow sheet / indoor floor -> nothing.\n"
        + env_hint +
        "Each prop is built from ONE generic SHAPE primitive the engine can make:\n"
        "  - \"rock\": a solid chunky blob (stones, pebbles, boulders, dirt clods).\n"
        "  - \"card\": thin / leafy things from crossed flat cards (grass tufts, weeds, small plants, "
        "flowers, twigs).\n"
        "Return ONLY a raw JSON object (no markdown):\n"
        "{\n"
        '  "should_dress": <true|false — false for bare paved squares, open water, snow sheet, indoor floors>,\n'
        '  "biome": "<a few words for the ground type, e.g. alpine chalk meadow / arid desert dirt>",\n'
        '  "layers": [        // 0-6 scatter layers, densest/most-important first\n'
        '    {\n'
        '      "name": "<what it is in the photo, e.g. mossy stone / dry grass tuft / wildflower>",\n'
        '      "shape": "rock|card|plant",  // rock=solid stone; plant=REAL 3D foliage mesh (PREFER for any '
        "tree/bush/grass/fern); card=flat billboard (only for tiny flowers/props with no matching plant species)\n"
        '      "species": "<REQUIRED when shape==plant — ONE of: tree_green (leafy green tree), tree_autumn '
        "(red/orange autumn tree), tree_dead (bare dead tree), bush, ivy (low ground/climbing vine), plant "
        "(leafy shrub/weed), fern, grass_tall, grass_short, grass_moor (wispy meadow grass), weed — pick what "
        'truly fits THIS biome; ignore for rock/card>",\n'
        '      "density_per_100m2": <instances per 100 m^2: sparse trees ~2-6, bushes/ferns ~10-40, '
        "scattered stones ~30, pebbles ~150, grass tufts ~1500, lush meadow grass ~3000; pick what THIS photo "
        "actually shows — go dense for thick living turf, sparse for trees / dry patchy ground>,\n"
        '      "size_m": [<min>, <max>],   // real-world size in metres (height for cards, diameter for rocks)\n'
        '      "color": [r,g,b],           // 0-1 base colour sampled from the photo for this prop\n'
        '      "roughness": <0-1>,\n'
        '      "clump": <0-1 clustering: 0 even, 1 strong patches>,\n'
        '      "companion_of": "<name of a LARGER layer this scatters around (e.g. pebbles around stones), '
        "or empty string>\",\n"
        '      "max_slope_deg": <0-90, skip ground steeper than this>,\n'
        '      "place": "ground|near_objects|everywhere"\n'
        '    }\n'
        '  ]\n'
        "}\n"
        "Match the photo's colours, pick realistic densities, include only props that truly belong on this "
        "ground. Output ONLY the JSON object."
    )
    plan = {"should_dress": False, "biome": "", "layers": []}
    try:
        d = _gen_json(client, image, instruction, temperature=0.2, seed=11)
        plan["should_dress"] = bool(d.get("should_dress", False))
        plan["biome"] = str(d.get("biome", ""))[:48]
        layers = []
        for L in (d.get("layers") or [])[:6]:
            if not isinstance(L, dict):
                continue
            shape = str(L.get("shape", "rock")).lower().strip()
            species = str(L.get("species", "")).lower().strip()
            if shape not in DRESSING_SHAPES:                       # tolerant: map unknown shapes onto a primitive
                shape = "card" if shape in ("grass", "weed", "flower", "leaf", "twig") else "rock"
            if shape == "plant" and species not in VEG_SPECIES:    # plant needs a legal species, else fall back to card
                shape = "card"
            place = str(L.get("place", "ground")).lower().strip()
            if place not in DRESSING_PLACES:
                place = "ground"
            sz = L.get("size_m")
            if not (isinstance(sz, (list, tuple)) and len(sz) >= 2):
                sz = [0.1, 0.3]
            smin = _clampf(sz[0], 0.01, 20.0, 0.1); smax = _clampf(sz[1], 0.01, 30.0, max(0.2, smin))
            if smax < smin:
                smin, smax = smax, smin
            col = L.get("color")
            if not (isinstance(col, (list, tuple)) and len(col) >= 3):
                col = [0.5, 0.5, 0.5]
            col = [_clampf(col[i], 0.0, 1.0, 0.5) for i in range(3)]
            layers.append({
                "name": str(L.get("name", shape))[:40],
                "shape": shape,
                "species": species if shape == "plant" else "",
                "density_per_100m2": _clampf(L.get("density_per_100m2"), 0.0, 8000.0, 20.0),
                "size_m": [round(smin, 3), round(smax, 3)],
                "color": [round(c, 3) for c in col],
                "roughness": _clampf(L.get("roughness"), 0.0, 1.0, 0.9),
                "clump": _clampf(L.get("clump"), 0.0, 1.0, 0.4),
                "companion_of": str(L.get("companion_of", ""))[:40],
                "max_slope_deg": _clampf(L.get("max_slope_deg"), 0.0, 90.0, 35.0),
                "place": place,
            })
        plan["layers"] = layers
    except ServerError:
        raise
    except Exception as e:
        print(f"[Dressing] parse failed: {e}", file=sys.stderr)
    print("[Dressing] dress=%s biome='%s' layers=%d: %s" % (
        plan["should_dress"], plan["biome"], len(plan["layers"]),
        ", ".join("%s %s×%.0f/100m²" % (L["name"], L["shape"], L["density_per_100m2"]) for L in plan["layers"])))
    return plan


@retry_with_backoff()
def generate_plant_textures(client, image: Image.Image, plan: dict, output_dir: str, task_id: str):
    """For each card layer (grass/weeds/shrub/cactus...) generate one single-plant side view on PURE BLACK; UE uses its luminance as an opacity mask
    to make alpha cards (black -> transparent, plant -> opaque) -> cards read as real plants, not flat colour quads. Adds tex_url to the plan's card layers in place."""
    if not plan or not plan.get("layers"):
        return
    biome = plan.get("biome", "")
    for i, L in enumerate(plan["layers"]):
        if L.get("shape") != "card":
            continue
        existing = os.path.join(output_dir, "dress_card%d.png" % i)
        if os.path.exists(existing):                 # idempotent: cards already on disk are reused, so @retry reruns don't re-bill
            L["tex_url"] = "/output/%s/dress_card%d.png" % (task_id, i)
            continue
        name = L.get("name", "plant")
        prompt = (
            "A single %s (as found in a '%s' setting), upright, viewed straight from the SIDE, filling the "
            "frame vertically with its BASE touching the BOTTOM edge of the frame, on a PURE SOLID BLACK "
            "(#000000) background. ONLY the one plant / tuft — no ground line, no shadow, no pot, no other "
            "objects, no text, nothing else. Evenly lit, true-to-life colour and shape." % (name, biome)
        )
        for ar, size in (("1:1", "1K"),):
            try:
                cfg = genai_types.GenerateContentConfig(
                    responseModalities=["IMAGE"],
                    imageConfig=genai_types.ImageConfig(aspectRatio=ar, imageSize=size),
                )
                resp = _gen_image_content(client, [prompt, image], cfg)
                for p in resp.parts:
                    if getattr(p, "thought", False):
                        continue
                    if p.inline_data is not None:
                        raw = os.path.join(output_dir, "_card_raw%d.png" % i)
                        p.as_image().save(raw)
                        Image.open(raw).convert("RGB").save(os.path.join(output_dir, "dress_card%d.png" % i))
                        try:
                            os.remove(raw)
                        except Exception:
                            pass
                        L["tex_url"] = "/output/%s/dress_card%d.png" % (task_id, i)
                        print("[Dressing] card texture %d (%s) saved" % (i, name))
                        break
                if L.get("tex_url"):
                    break
            except ServerError:
                raise
            except Exception as e:
                print("[Dressing] card tex %d failed: %s" % (i, e), file=sys.stderr)
                continue


# ── Step 1c3: particle/fluid analysis (Gemini decides which dynamic/atmospheric elements to add: water/waterfall/rain/fog/dust/birds...; we execute verbatim) ──
FX_PRIMS = ("card", "water", "fog")
WATER_BODY_PRESETS = ("lake_calm", "lake_windy", "pond_shallow", "deep_dark", "swamp_mossy")  # real water-body material presets (AI picks per photo; ue_water_presets/MA_YX_Blend)
# Live-particle presets (Gemini picks; executor = hand-built NS_Particles/NS_Leaves/NS_Mesh + the ue_fx_presets adapter layer)
NS_PRESET_KINDS = ("none", "rain", "snow", "dust", "embers", "leaves", "petals", "ash",
                   "waterfall", "fountain", "debris", "gravel",
                   "smoke", "steam", "fireflies", "mist")
FX_REGIONS = ("water_surface", "fall_line", "air", "sky", "ground", "over_object")
FX_BLENDS = ("soft", "additive", "masked")


@retry_with_backoff()
def analyze_effects(client, image: Image.Image, environment: dict | None = None, objects: list | None = None,
                    indoor: bool = False) -> dict:
    """Let the VLM decide which moving/airborne elements would make the single frame read truer and more alive, emitting a GENERIC FX plan the executor follows verbatim.
    Single-frame render -> no real animation needed: water = translucent plane; waterfall/rain/spray/fog/smoke/dust/snow/birds = static alpha cards in the right places.
    Only three generic primitives (card group / water plane / fog); what/where/how many/how big/what colour is entirely Gemini's call per photo."""
    env_hint = ""
    if environment:
        env_hint = ("\nContext: weather '%s', time '%s'. Stay consistent.\n" % (
            environment.get("weather", "?"), environment.get("time_of_day", "?")))
    if objects:
        # Object table: makes near_object_id actually usable (without it Gemini can only give -1 and effects land on the ground projection in front of objects)
        rows = "; ".join("id=%d %s" % (o.get("id", -1), str(o.get("prompt", ""))[:50])
                         for o in objects if not o.get("imagined"))
        env_hint += ("\nReconstructed objects in the scene: " + rows +
                     "\nIf an effect hangs OVER or clings to one of these (dust above a vehicle, mist around "
                     "a building, embers off a lamp), SET near_object_id to that id — the engine will anchor "
                     "the emitter to that object's true 3D position. Use -1 only for free-air effects.\n")
    instruction = (
        "You are a VFX environment artist studying THIS photo to add the moving / atmospheric elements a 3D "
        "engine must place so a SINGLE still looks alive and faithful. Decide EVERYTHING from the image — do "
        "not assume a fixed scene type. Identify what the photo implies: water surfaces, waterfalls, rain, "
        "spray, mist / haze / fog, smoke, dust or pollen in light, falling snow, birds / insects, embers. If "
        "the photo implies NONE (a dry indoor room, a clear static street with nothing in the air, a plain "
        "studio), set should_fx=false and return no layers.\n"
        + env_hint +
        "Each effect maps to ONE of three engine primitives you choose:\n"
        "  - \"water\": a flat standing/flowing WATER surface (lake, river, sea, pond, puddle). One per body.\n"
        "  - \"card\": anything made of many small sprites — waterfall/rain/spray streaks, mist/smoke/dust/"
        "pollen/snow motes, birds/insects, embers. (it becomes many alpha billboards)\n"
        "  - \"fog\": broad atmospheric haze done by the engine's height fog (no sprites).\n"
        "Return ONLY a raw JSON object (no markdown):\n"
        "{\n"
        '  "should_fx": <true|false>,\n'
        '  "summary": "<one line, e.g. misty waterfall plunging into a pool under light rain>",\n'
        '  "layers": [        // 0-8 effects, most dominant first\n'
        '    {\n'
        '      "name": "<what it is, e.g. waterfall / light rain / mist over river / flock of birds / dust in sunbeam>",\n'
        '      "primitive": "card|water|fog",\n'
        '      "region": "water_surface|fall_line|air|sky|ground|over_object",\n'
        '      "blend": "soft|additive|masked",   // card look: additive=bright water/light/embers, soft=haze/smoke/dust, masked=hard silhouettes (birds)\n'
        '      "anchor_uv": [u, v],               // 0-1 image position of the feature center\n'
        '      "extent_uv": [w, h],               // 0-1 image size of the feature\n'
        '      "height_m": <number>,              // water: surface height above ground; air: base altitude above ground; fall: top height\n'
        '      "thickness_m": <number>,           // air-volume depth / waterfall fall length / fog height band\n'
        '      "count": <int sprites for card: downpour ~600, waterfall ~250, mist ~40, dust ~400, flock ~12>,\n'
        '      "size_m": [<min>, <max>],          // card world size (height); water ignored\n'
        '      "color": [r,g,b],                  // 0-1 sampled colour (white water ~[.9,.93,1], dust warm, birds dark, smoke grey)\n'
        '      "opacity": <0-1>,\n'
        '      "emissive": <0-1>,                 // glow for bright water / embers / lit motes\n'
        '      "water_preset": "lake_calm|lake_windy|pond_shallow|deep_dark|swamp_mossy",\n'
        '          // ONLY for primitive=water + a real water BODY: which kind — calm open lake /\n'
        '          // wind-rippled lake / shallow see-through pond / deep dark water / green mossy swamp.\n'
        '      "water_color": [r,g,b],            // ONLY water: the water body\'s OWN colour sampled from THIS photo\n'
        '          // (deep alpine=dark blue-grey, tropical=turquoise, glacial=milky cyan, muddy=green-brown,\n'
        '          //  reflective dusk=warm-tinted). Sample the actual pixels, do not default.\n'
        '      "water_clarity": <0-1>,            // ONLY water: 0=opaque/murky (cannot see bed), 1=crystal clear (see the bottom)\n'
        '      "water_calm": <0-1>,               // ONLY water: 1=glassy mirror-still, 0=wind-chopped & rippled\n'
        '      "water_depth_feel": "shallow|medium|deep",  // ONLY water: shallow pond / normal lake / deep water body\n'
        '      "near_object_id": <int object id this hangs on, or -1>,\n'
        '      "attach": "none|top|canopy|around|base",\n'
        '          // WHERE on that object the particles originate (only with near_object_id):\n'
        '          // top=its summit (chimney smoke, roof steam), canopy=inside its upper volume\n'
        '          // (leaves/spores falling within a tree crown), around=ring around it (fireflies\n'
        '          // by a lamp), base=at its foot (ground mist, splash). Use "none" for free-air.\n'
        '      "niagara": "none|rain|snow|dust|embers|leaves|petals|ash|waterfall|fountain|debris'
        '|smoke|steam|fireflies|mist"\n'
        '          // LIVE particle preset for the navigable scene (engine spawns a real moving particle\n'
        '          // system instead of static sprites): falling rain->rain, drifting snow->snow, dust/pollen\n'
        '          // motes in light->dust, fire sparks->embers, falling leaves->leaves, flower petals->petals,\n'
        '          // drifting ash->ash, waterfall spray->waterfall, splashing fountain->fountain,\n'
        '          // chimney/fire smoke->smoke, vents/hot water->steam, blinking night insects->fireflies,\n'
        '          // low pooling ground fog->mist.\n'
        '          // Use "none" for things that are NOT particle-like (birds/insect flocks, broad haze).\n'
        '    }\n'
        '  ]\n'
        "}\n"
        "Use realistic counts and photo-sampled colours. "
        + ("RICHNESS (INDOOR): this is an INTERIOR — indoor air is CALM, restraint is realism. The only "
           "presets that exist indoors: dust (ambient motes drifting in lamp/window light — almost always "
           "justified, modest count ~80-200), steam (ONLY with a visible hot source: cup, kettle, pot, "
           "bathtub — near_object_id + attach=top), smoke (ONLY incense / cigarette / fireplace — "
           "attach=top), embers (ONLY open flame). NEVER use rain/snow/leaves/petals/waterfall/fountain/"
           "mist/debris/ash/fireflies indoors. 1-2 layers typical, 3 max. " if indoor else
           "RICHNESS: you are dressing a navigable GAME LEVEL, not captioning the photo. Think like a level "
           "designer: given this biome/time/weather/mood, SPECULATE what ambient particles would make the "
           "place feel alive — aim for 3-5 DISTINCT particle layers, each its OWN layer with a DIFFERENT "
           "niagara preset; never duplicate a preset. Evidence in the photo is welcome but NOT required: "
           "propose what belongs (street trees -> drifting leaves attach=canopy; paved streets -> wind-blown "
           "litter (debris); lamps/dusk -> fireflies attach=around; chimneys -> smoke attach=top; vents/hot "
           "water -> steam; terrain hollows/water edges at dawn or night -> mist attach=base; flowers -> "
           "petals; arid ground -> dust + pollen). Mix scales: at least one fine atmospheric layer "
           "(motes/pollen) AND one chunky readable layer (leaves/petals/debris/embers). ")
        + "The only hard rule: "
        "never contradict the photo (no rain under a clear sky, no snow in summer foliage). "
        "size_m MUST be a TRUE spread [min,max] with max >= 2x min (nature is never uniform). "
        "Output ONLY the JSON."
    )
    plan = {"should_fx": False, "summary": "", "layers": []}
    try:
        d = _gen_json(client, image, instruction, temperature=0.2, seed=13)
        plan["should_fx"] = bool(d.get("should_fx", False))
        plan["summary"] = str(d.get("summary", ""))[:80]
        out = []
        for L in (d.get("layers") or [])[:8]:
            if not isinstance(L, dict):
                continue
            prim = str(L.get("primitive", "card")).lower().strip()
            if prim not in FX_PRIMS:
                prim = "card"
            region = str(L.get("region", "air")).lower().strip()
            if region not in FX_REGIONS:
                region = "air"
            blend = str(L.get("blend", "soft")).lower().strip()
            if blend not in FX_BLENDS:
                blend = "soft"
            au = L.get("anchor_uv"); eu = L.get("extent_uv")
            au = [_clampf(au[0], 0.0, 1.0, 0.5), _clampf(au[1], 0.0, 1.0, 0.5)] if isinstance(au, (list, tuple)) and len(au) >= 2 else [0.5, 0.5]
            eu = [_clampf(eu[0], 0.02, 1.0, 0.5), _clampf(eu[1], 0.02, 1.0, 0.4)] if isinstance(eu, (list, tuple)) and len(eu) >= 2 else [0.5, 0.4]
            sz = L.get("size_m")
            if not (isinstance(sz, (list, tuple)) and len(sz) >= 2):
                sz = [0.3, 1.0]
            smin = _clampf(sz[0], 0.01, 200.0, 0.3); smax = _clampf(sz[1], 0.01, 300.0, max(0.5, smin))
            if smax < smin:
                smin, smax = smax, smin
            col = L.get("color")
            col = [_clampf(col[i], 0.0, 1.0, 0.85) for i in range(3)] if isinstance(col, (list, tuple)) and len(col) >= 3 else [0.85, 0.9, 0.95]
            nia = str(L.get("niagara", "none")).lower().strip()
            if nia not in NS_PRESET_KINDS:
                nia = "none"
            if indoor and nia not in ("none", "dust", "steam", "smoke", "embers"):
                nia = "none"                       # indoor vocabulary gate (code-level; the prompt alone is not batch-safe)
            att = str(L.get("attach", "none")).lower().strip()
            if att not in ("none", "top", "canopy", "around", "base"):
                att = "none"
            wp = str(L.get("water_preset", "lake_calm")).lower().strip()
            if wp not in WATER_BODY_PRESETS:
                wp = "lake_calm"
            wcol = L.get("water_color")
            wcol = ([_clampf(wcol[i], 0.0, 1.0, 0.1) for i in range(3)]
                    if isinstance(wcol, (list, tuple)) and len(wcol) >= 3 else None)   # None = executor falls back to the preset colour
            wdf = str(L.get("water_depth_feel", "medium")).lower().strip()
            if wdf not in ("shallow", "medium", "deep"):
                wdf = "medium"
            out.append({
                "name": str(L.get("name", prim))[:40], "primitive": prim, "region": region, "blend": blend,
                "niagara": nia, "attach": att, "water_preset": wp,
                "water_color": wcol, "water_clarity": _clampf(L.get("water_clarity"), 0.0, 1.0, 0.5),
                "water_calm": _clampf(L.get("water_calm"), 0.0, 1.0, 0.7), "water_depth_feel": wdf,
                "anchor_uv": [round(au[0], 3), round(au[1], 3)], "extent_uv": [round(eu[0], 3), round(eu[1], 3)],
                "height_m": _clampf(L.get("height_m"), -5.0, 500.0, 0.0),
                "thickness_m": _clampf(L.get("thickness_m"), 0.1, 500.0, 6.0),
                "count": int(_clampf(L.get("count"), 0.0, 8000.0, 200.0)),
                "size_m": [round(smin, 3), round(smax, 3)], "color": [round(c, 3) for c in col],
                "opacity": _clampf(L.get("opacity"), 0.0, 1.0, 0.6),
                "emissive": _clampf(L.get("emissive"), 0.0, 1.0, 0.0),
                "near_object_id": int(_clampf(L.get("near_object_id"), -1.0, 999.0, -1.0)),
            })
        plan["layers"] = out
    except ServerError:
        raise
    except Exception as e:
        print("[FX] parse failed: %s" % e, file=sys.stderr)
    print("[FX] should_fx=%s '%s' layers=%d: %s" % (
        plan["should_fx"], plan["summary"], len(plan["layers"]),
        ", ".join("%s(%s/%s×%d)" % (L["name"], L["primitive"], L["region"], L["count"]) for L in plan["layers"])))
    return plan



# ── Step 1d: camera solve (capture height + pitch -> UE viewport/camera) ──
@retry_with_backoff()
def analyze_camera(client, image: Image.Image, fov_deg: float = 65.0) -> dict:
    """Solve this photo's capture position: camera height above ground (m) + pitch (deg) + dominant vanishing direction + absolute-scale reference.
    FOV reuses the extraction-stage estimate."""
    instruction = (
        "You are a camera-tracking AND scale analyst. From this single photo, SOLVE the camera's viewpoint "
        "so a 3D engine can reproduce the exact vantage, and anchor the absolute real-world scale. Reason "
        "from: where the horizon line sits, perspective convergence, how much we look down onto / up at "
        "surfaces, and the eye-level relative to people, doors, floors.\n"
        "For SCALE: find the most reliable real-world size reference visible (a person, a standard door, a "
        "car, a single building storey, a traffic light) whose true size is well known, and judge whether "
        "the scene's apparent sizes are consistent with it. For INDOOR scenes where no person or door is "
        "clearly visible, DO NOT leave the scale unanchored — fall back to standard FURNITURE dimensions "
        "(an interior door ~2m, a sofa ~2m wide, an armchair ~0.8m, a coffee table ~0.4m high / ~1.1m "
        "wide, a dining chair ~0.45m seat, a kitchen counter ~0.9m high) as your reference, and set "
        "scale_correction so the furniture comes out at those true sizes (push it <1 if the room's "
        "furniture currently looks oversized).\n"
        "Return ONLY a raw JSON object (no markdown):\n"
        "{\n"
        '  "height_m": <camera height above the ground in METERS: handheld eye-level ~1.5-1.7, raised ~2-3, '
        "balcony/upper floor ~4-15, drone/aerial ~20-120>,\n"
        '  "pitch_deg": <camera tilt in degrees: 0 = looking straight at the horizon (horizon centered), '
        "NEGATIVE = tilted DOWN (looking down at the ground / from above), POSITIVE = tilted UP (looking "
        "up at tall buildings). Range -90..90>,\n"
        '  "is_indoor": <true if this is an INTERIOR scene (inside a room, hall, vehicle — walls/ceiling '
        "enclose the space); false for outdoors or open sky>,\n"
        '  "depth_axis_deg": <-80..80, the dominant direction the scene RECEDES into depth (its vanishing '
        "direction) relative to straight ahead: 0 = depth runs straight away from the camera, negative = "
        "the scene recedes toward the LEFT, positive = toward the RIGHT. Read it from the vanishing point "
        "/ converging lines. For a flat, face-on scene with no strong perspective, use 0.>,\n"
        '  "reference": "person|door|car|storey|traffic_light|sofa|table|chair|counter|none",\n'
        '  "reference_true_m": <the canonical true size of that reference in meters: person ~1.7, door '
        "~2.0, car ~4.5, storey ~3.0, traffic_light ~3.0, sofa ~2.0 (width), table ~1.1 (width), chair "
        "~0.9 (height), counter ~0.9 (height); 0 if none>,\n"
        '  "scale_correction": <0.5-2.0 — a single multiplier to apply to ALL object sizes so the scene '
        "matches the reference's true size. 1.0 if the sizes already look correct or there is no reliable "
        "reference; <1 if everything currently looks too big; >1 if too small.>\n"
        "}\n"
        "Output ONLY the JSON object, nothing else."
    )
    cam = {"height_m": 1.6, "pitch_deg": 0.0, "fov_deg": _clampf(fov_deg, 25.0, 100.0, 65.0),
           "is_indoor": False, "depth_axis_deg": 0.0,
           "reference": "none", "reference_true_m": 0.0, "scale_correction": 1.0,
           "solved": False}      # solved=False means no real solve (503/parse failure fell back to defaults); terrain skips based on it
    try:
        d = _gen_json(client, image, instruction, temperature=0.0, seed=7)
        cam["height_m"] = _clampf(d.get("height_m"), 0.2, 200.0, cam["height_m"])
        cam["pitch_deg"] = _clampf(d.get("pitch_deg"), -90.0, 90.0, cam["pitch_deg"])
        cam["is_indoor"] = bool(d.get("is_indoor", False))
        cam["depth_axis_deg"] = _clampf(d.get("depth_axis_deg"), -80.0, 80.0, 0.0)
        cam["reference"] = str(d.get("reference", "none"))[:24]
        cam["reference_true_m"] = _clampf(d.get("reference_true_m"), 0.0, 200.0, 0.0)
        cam["scale_correction"] = _clampf(d.get("scale_correction"), 0.5, 2.0, 1.0)
        cam["solved"] = True
    except ServerError:
        raise                    # let @retry_with_backoff retry the 503 instead of swallowing it here with a default camera
    except Exception as e:
        print(f"[Cam] parse failed, using defaults: {e}", file=sys.stderr)
    print(f"[Cam] height {cam['height_m']:.1f}m / pitch {cam['pitch_deg']:.0f}° / fov {cam['fov_deg']:.0f}° "
          f"/ indoor={cam['is_indoor']} / depth_axis={cam['depth_axis_deg']:.0f}° "
          f"/ ref={cam['reference']}×{cam['scale_correction']:.2f}")
    return cam


# ── Step 1e: artificial light solve (night/dark/indoor: street lamps/signs/neon/windows -> UE point lights) ──
@retry_with_backoff()
def analyze_lights(client, image: Image.Image) -> list[dict]:
    """Find the artificial light sources in the frame; returns normalized position + colour + intensity. Returns an empty list in adequate daylight."""
    instruction = (
        "You are a lighting-placement analyst. Find the ARTIFICIAL light sources visible in this photo so "
        "a 3D engine can place matching lights. If the scene is in BRIGHT DAYLIGHT and well-lit by the "
        "sun, return an empty list. Otherwise (night, dusk, indoor, dim, overcast-with-lit-signs), list "
        "the most important glowing sources.\n"
        "Return ONLY a raw JSON object (no markdown):\n"
        "{\n"
        '  "lights": [ {\n'
        '     "u": <0-1 horizontal position of the light in the image (0 left, 1 right)>,\n'
        '     "v": <0-1 vertical position (0 top, 1 bottom)>,\n'
        '     "color": [r,g,b] each 0-1, the emitted light colour (warm tungsten ~[1,0.8,0.5], neon can '
        "be any hue, cool white ~[0.9,0.95,1]),\n"
        '     "intensity": <0-1 how bright/dominant this source is>\n'
        "  } ]\n"
        "}\n"
        f"List at most {LIGHT_MAX} sources, the brightest/most defining first. Output ONLY the JSON."
    )
    out = []
    try:
        d = _gen_json(client, image, instruction, temperature=0.3, seed=7)
        if isinstance(d, list):
            d = {"lights": d}
        for l in (d.get("lights") or [])[:LIGHT_MAX]:
            col = l.get("color") or [1.0, 0.85, 0.6]
            if not (isinstance(col, (list, tuple)) and len(col) >= 3):
                col = [1.0, 0.85, 0.6]
            out.append({
                "u": _clampf(l.get("u"), 0.0, 1.0, 0.5),
                "v": _clampf(l.get("v"), 0.0, 1.0, 0.5),
                "color": [_clampf(col[i], 0.0, 1.0, 1.0) for i in range(3)],
                "intensity": _clampf(l.get("intensity"), 0.0, 1.0, 0.6),
            })
    except ServerError:
        raise
    except Exception as e:
        print(f"[Lights] parse failed: {e}", file=sys.stderr)
    print(f"[Lights] {len(out)} artificial light sources")
    return out


# ── Step 2: generate an image per prompt ──────────────────────
# Hard composition suffix: whatever the upstream prompt says, force black background + fully in frame + margin on all sides
FRAMING_SUFFIX = (
    " CRITICAL RENDERING CONSTRAINTS (these OVERRIDE anything above that conflicts):\n"
    "(1) ONE OBJECT ONLY. Render EXACTLY the single object described above and ABSOLUTELY NOTHING else: "
    "no second building, no adjacent or attached landmark, no other tower, no lamp post, no fence, no "
    "bench, no street furniture, no signage, no vehicles, no people — even if such things sit right next "
    "to it in the reference photo. If the described object is physically attached to a famous landmark "
    "(e.g. a clock tower), show ONLY the described part and exclude the rest of the landmark entirely.\n"
    "(2) PURE BLACK BACKGROUND. Every pixel not covered by the object must be 100% solid black (#000000) "
    "— NEVER white, grey, a gradient or a studio backdrop, and NO sky, clouds, fog, ground, terrain, "
    "water, reflections, cast shadows or scenery. This holds EVEN IF the object itself is white, pale "
    "marble or light-coloured: a white/light object must STILL sit on a solid pure-black background, "
    "never on a white or light studio backdrop.\n"
    "(3) WHOLE & CENTERED. Show the ENTIRE object, complete and never cropped by the frame; scale it DOWN "
    "and center it with a generous empty black margin on all four sides so no part touches any edge.\n"
    "(4) CLEAN LIGHTING. Light it with bright, even, neutral studio lighting that clearly reveals its "
    "full form and every visible side, with no harsh shadows or blown-out highlights baked into the "
    "texture — it will be relit inside a 3D engine, so it must read as a clean, evenly-lit, full-colour "
    "3D model."
)


def _image_config(model: str, size: str = IMAGE_SIZE):
    """Image generation config: images only + 1:1 square; Gemini 3.x additionally takes a resolution."""
    kw = {"aspectRatio": "1:1"}
    if model.startswith("gemini-3"):
        kw["imageSize"] = size
    return genai_types.GenerateContentConfig(
        responseModalities=["IMAGE"],
        imageConfig=genai_types.ImageConfig(**kw),
    )


def _generate_single_image_with_fallback(client, prompt: str, image: Image.Image, hero: bool = False):
    """Try each model in turn, returning on first success; raise the last exception if all fail.
    hero=True: try Nano Banana Pro first (max fidelity, high resolution), then fall back to the flash models."""
    # Black-background instruction goes first (models weigh the beginning more): even a white subject must sit on pure black
    lead = "A single isolated 3D asset photographed on a PURE SOLID BLACK (#000000) background. "
    full_prompt = lead + prompt + FRAMING_SUFFIX
    models = ([HERO_IMAGE_MODEL] + IMAGE_MODELS) if hero else IMAGE_MODELS
    size = HERO_IMAGE_SIZE if hero else IMAGE_SIZE
    last_exc = None
    for model in models:
        try:
            print(f"   → 尝试模型: {model}{' (hero)' if hero and model == HERO_IMAGE_MODEL else ''}")
            response = client.models.generate_content(
                model=model,
                contents=[full_prompt, image],
                config=_image_config(model, size),
            )
            # Check whether any image came back (thinking-stage intermediates also count as model-usable)
            for part in response.parts:
                if part.inline_data is not None:
                    print(f"   ✓ 模型 {model} 成功")
                    return response
            # Text reply but no image is not success; try the next model
            text = (getattr(response, "text", None) or "").strip()
            print(f"   ⚠ 模型 {model} 未返回图片 (文本: {text[:60]})")
        except ServerError as e:
            last_exc = e
            print(f"   ✗ 模型 {model} ServerError ({e.code}): {e.message[:80]}")
            time.sleep(1)  # brief pause before switching models
        except Exception as e:
            last_exc = e
            print(f"   ✗ 模型 {model} 异常: {e}")
            time.sleep(1)
    raise last_exc or RuntimeError("所有模型均不可用")


@retry_with_backoff()
def _polish_hdri_panorama(img):
    """Clean an AI equirectangular panorama: (1) pole pinch -- the equirect top/bottom rows are really single points, and the AI squeezes cloud/rain streaks
    into radial spokes at the zenith -> average the top/bottom bands row-wise, more uniform towards the poles -> smooth zenith/nadir; (2) left/right seam --
    AI 'seamless' is approximate, the ends don't truly meet -> cross-fade the edge bands with the opposite side, so no vertical seam shows on the sphere.
    Makes a pure-AI sky clean even when mapped onto a dome."""
    import numpy as np
    a = np.asarray(img.convert("RGB"), dtype="float32")
    h, w = a.shape[:2]
    pole = max(4, int(h * 0.18))
    for i in range(pole):
        t = (1.0 - i / float(pole)) ** 1.6                    # 1 = very top (fully uniform) -> 0 = band bottom (untouched)
        a[i] = a[i] * (1.0 - t) + a[i].mean(axis=0) * t       # top band
        j = h - 1 - i
        a[j] = a[j] * (1.0 - t) + a[j].mean(axis=0) * t       # bottom band
    bw = max(4, int(w * 0.05))                                # left/right seam cross-fade width
    for k in range(bw):
        f = 0.5 * (1.0 - k / float(bw))
        l = a[:, k].copy(); r = a[:, w - 1 - k].copy()
        a[:, k] = l * (1.0 - f) + r * f
        a[:, w - 1 - k] = r * (1.0 - f) + l * f
    return Image.fromarray(np.clip(a, 0, 255).astype("uint8"))


def generate_hdri(client, image: Image.Image, output_dir: str, task_id: str) -> str:
    """Photo -> 360-degree equirectangular sky panorama: sky + distant skyline only, no foreground.
    Generated at the widest 8:1 + high resolution, then force-squashed to 2:1 -- deliberately exposing the AI's lateral seams/pole distortion
    ('exquisite errors', on the 'broken memory' theme). Fed to UE as the HDRIBackdrop."""
    prompt = (
        "An equirectangular projection of a seamless 360-degree spherical panorama (latitude-longitude "
        "sky dome / HDRI), to be used as the background of a 3D skysphere. Based on THIS photo, recreate "
        "ONLY its SKY and the FAR-DISTANT background wrapped all the way around the viewer: keep the same "
        "time of day, sun direction, cloud state, colours and weather, with a low DISTANT skyline "
        "silhouette sitting far on the horizon. Keep the horizon line perfectly straight and strictly "
        "centred on the vertical middle of the image. The far LEFT and far RIGHT edges must match for "
        "seamless horizontal wrapping. Show ONLY distant landscape and sky — absolutely NO foreground "
        "objects, NO close-up buildings, NO trees cutting the sky, nothing crossing the top or bottom "
        "edge. There is EXACTLY ONE sun in the entire panorama — a single sun disc; NEVER draw a second "
        "sun, a mirrored sun, or repeated bright sun glows. Natural sky gradient. A flat 2D "
        "equirectangular map — not a fisheye, not a perspective shot."
    )
    for ar, size in (("21:9", "4K"), ("21:9", "2K"), ("16:9", "2K")):
        try:
            cfg = genai_types.GenerateContentConfig(
                responseModalities=["IMAGE"],
                imageConfig=genai_types.ImageConfig(aspectRatio=ar, imageSize=size),
            )
            resp = _gen_image_content(client, [prompt, image], cfg)
            for p in resp.parts:
                if getattr(p, "thought", False):
                    continue
                if p.inline_data is not None:
                    wide = os.path.join(output_dir, "_hdri_wide.png")
                    p.as_image().save(wide)
                    # map to 2:1 equirect + remove pole pinch/seam (pure-AI skies stay clean on the sphere)
                    pano = Image.open(wide).convert("RGB").resize((4096, 2048))
                    _polish_hdri_panorama(pano).save(os.path.join(output_dir, "hdri.png"))
                    try:
                        os.remove(wide)
                    except Exception:
                        pass
                    print(f"[HDRI] sky panorama {ar}@{size} → squashed to 2:1")
                    return f"/output/{task_id}/hdri.png"
        except Exception as e:
            print(f"[HDRI] {ar}@{size} failed: {e}", file=sys.stderr)
            continue
    return ""


@retry_with_backoff()
def _make_tileable(img: Image.Image, band_frac: float = 0.14) -> Image.Image:
    """Turn an 'approximately seamless' texture into a mathematically tileable one: cross-fade with a half-period rolled copy inside edge bands.
    Generative 'seamless' is only approximate -- edges never truly wrap, so every tile boundary leaks a seam,
    forming a regular grid across the ground (in practice: 6x6 tiling on a street = a 16m grid). Deterministic de-seaming, works on any texture."""
    import numpy as np
    a = np.asarray(img.convert("RGB"), dtype="float32")
    h, w = a.shape[:2]
    rolled = np.roll(a, (h // 2, w // 2), (0, 1))            # roll half a period: original edges move to the centre (continuous), original centre (continuous) to the edges
    bx = max(2, int(w * band_frac)); by = max(2, int(h * band_frac))
    wx = np.clip(np.minimum(np.arange(w), w - 1 - np.arange(w)) / float(bx), 0.0, 1.0)
    wy = np.clip(np.minimum(np.arange(h), h - 1 - np.arange(h)) / float(by), 0.0, 1.0)
    wgt = np.minimum(wx[None, :], wy[:, None])
    wgt = (wgt * wgt * (3.0 - 2.0 * wgt))[..., None]         # smoothstep: interior = original, edge band fades to the rolled copy
    out = a * wgt + rolled * (1.0 - wgt)
    return Image.fromarray(out.clip(0, 255).astype("uint8"))


def generate_terrain_textures(client, image: Image.Image, output_dir: str, task_id: str, n_var: int = 3) -> list:
    """Generate 2-3 DECORRELATED seamless tileable top-down ground textures from the same source photo (same material, different patch layout).
    A single oblique photo only captures nearby ground: copying pixels smears, and tiling one image repeats visibly; instead generate several patterns
    matched to the source material, and UE blends them with world-aligned low-frequency noise -> no repeating grid at distance. Per-variant wording
    ('different layout/clumps') keeps layouts different while the material stays consistent. Returns ['/output/<id>/terrain_tex0.png', ...] (may be empty)."""
    base_prompt = (
        "Generate a SEAMLESS TILEABLE top-down ground TEXTURE swatch that MATCHES the ground surface "
        "material seen in this photo (look at its ground — e.g. grass, soil, dry earth, snow, rock, "
        "sand, gravel, paving, terraced field — and reproduce that same material, colour and grain). "
        "Orthographic overhead view looking straight DOWN, perfectly flat, lit by even neutral light "
        "with NO baked shadows, NO directional highlights, NO vignette. Fill the ENTIRE frame edge to "
        "edge with ONLY the repeating ground material: absolutely NO objects, NO buildings, NO plants "
        "standing up, NO people, NO horizon, NO sky, NO text. All four edges must wrap and tile "
        "seamlessly (left matches right, top matches bottom). A uniform repeating material swatch, "
        "like a game-engine PBR albedo/base-color texture. CRITICAL: keep it UNIFORM and NON-DIRECTIONAL "
        "— NO large stripes, bands, layers, terraces, cliffs, ridges, veins or big shapes, NO strong "
        "directional pattern or gradient; ONLY fine, evenly-distributed surface grain that looks the same "
        "in every direction, so it tiles with no obvious repeat or seam. "
        "ALSO CRITICAL — NO painted ROAD MARKINGS of any kind: no lane lines, centre lines, edge lines, "
        "arrows, crosswalk stripes, kerb paint, parking bays, manhole covers or drain grates. If the "
        "photo's ground is a road/pavement, reproduce ONLY the bare asphalt/concrete grain BETWEEN the "
        "markings (markings are unique features, not a repeating material — they tile into fake-looking "
        "stripe grids)."
    )
    variant_hints = [
        " A representative average patch of this ground.",
        " SAME material and colour but a DIFFERENT random arrangement of the grain — shift the clumps, "
        "cracks and pebbles to a new layout so it does NOT match the other swatches.",
        " SAME material and colour, yet ANOTHER distinct layout — redistribute the surface detail and "
        "slightly vary the local lightness, while staying the same surface type and seamlessly tileable.",
    ]
    urls = []
    for i in range(max(1, min(3, n_var))):
        existing = os.path.join(output_dir, "terrain_tex%d.png" % i)
        if os.path.exists(existing):                 # idempotent: variants already on disk are reused, so @retry reruns don't re-bill
            urls.append(f"/output/{task_id}/terrain_tex{i}.png")
            continue
        prompt = base_prompt + variant_hints[i]
        saved = ""
        for ar, size in (("1:1", "2K"), ("1:1", "1K")):
            try:
                cfg = genai_types.GenerateContentConfig(
                    responseModalities=["IMAGE"],
                    imageConfig=genai_types.ImageConfig(aspectRatio=ar, imageSize=size),
                )
                resp = _gen_image_content(client, [prompt, image], cfg)
                for p in resp.parts:
                    if getattr(p, "thought", False):
                        continue
                    if p.inline_data is not None:
                        raw = os.path.join(output_dir, "_tex_raw%d.png" % i)
                        p.as_image().save(raw)
                        out = os.path.join(output_dir, "terrain_tex%d.png" % i)
                        _make_tileable(Image.open(raw)).save(out)   # force seamless: generative 'seamless' is approximate; tiling shows grid seams
                        try:
                            os.remove(raw)
                        except Exception:
                            pass
                        saved = f"/output/{task_id}/terrain_tex{i}.png"
                        print(f"[Terrain] surface texture {i} saved ({ar}@{size})")
                        break
                if saved:
                    break
            except ServerError:
                raise
            except Exception as e:
                print(f"[Terrain] texture {i} {ar}@{size} failed: {e}", file=sys.stderr)
                continue
        if saved:
            urls.append(saved)
    return urls


def reconstruct_terrain(image, results, camera, fov_deg=65.0, img_aspect=1.333, n=TERRAIN_GRID_N,
                        force_water=False):
    """Pinhole back-project the visible ground into a top-down height grid using the Depth Anything depth map + solved camera (validated algorithm).
    Pipeline: DA depth -> mask objects/sky -> anchor metric scale on flat ground as reference -> back-project each ground pixel to (fore-aft, lateral, height)
    -> rasterize to N x N + light smoothing. Returns {n, grid(0..1), relief_m, world position/extent} or None (near-flat/unreliable -> UE uses a plane)."""
    try:
        import numpy as np
        from PIL import ImageFilter
        out = _get_depth_pipe()(image)
        D = np.asarray(out["depth"], dtype="float32")                  # DA affine-invariant disparity: D ~ a/Z + b (a,b unknown)
        Hh, Ww = D.shape[:2]
        Hcam = _clampf(camera.get("height_m", 1.6), 0.2, 200.0, 1.6)
        pitch = math.radians(_clampf(camera.get("pitch_deg", -10.0), -89.0, 5.0, -10.0))
        hfov = math.radians(max(25.0, min(100.0, fov_deg)))
        th = math.tan(hfov / 2.0); tv = th / max(0.1, img_aspect); vfov = 2.0 * math.atan(tv)
        yy, xx = np.mgrid[0:Hh, 0:Ww]; vv = yy / Hh; uu = xx / Ww
        elev = pitch - (vv - 0.5) * vfov                               # per-pixel ray elevation angle (rad)
        mask = elev < -0.01                                            # below the horizon = ground
        for r in (results or []):
            if r.get("imagined"):
                continue
            b = r.get("bbox") or [0, 0, 0, 0]
            x0, y0, w, h = b
            mask &= ~((uu >= x0) & (uu <= x0 + w) & (vv >= y0) & (vv <= y0 + h))
        sin_e = np.sin(elev)
        inv_flat = np.where(sin_e < -1e-3, (-sin_e) / Hcam, np.nan)    # flat-ground true inverse distance 1/Z_flat (varies with row only)
        gm = mask & np.isfinite(inv_flat)
        if int(gm.sum()) < 400:                                        # too little ground (upward shot / pure sky) -> flat
            print("[Terrain] too few ground pixels → flat"); return None
        # Affine fit (ground pixels only): DA disparity D ~ A*inv_flat + B, solving scale A and offset B together.
        # -- a single multiplicative anchor cannot cancel the additive offset B and turns heights into biased residuals; both A and B must be fitted.
        xf = inv_flat[gm].astype("float64"); yf = D[gm].astype("float64")
        A, B = np.polyfit(xf, yf, 1)
        rr = np.abs(yf - (A * xf + B))                                 # robust: drop the ~20% largest residuals, fit again
        kp = rr <= np.percentile(rr, 80)
        if int(kp.sum()) > 50:
            A, B = np.polyfit(xf[kp], yf[kp], 1)
        if A <= 1e-9:
            print("[Terrain] degenerate depth fit → flat"); return None
        invZ = np.clip((D - B) / A, 1e-4, None)                        # recover true inverse distance 1/Z
        d_da = 1.0 / invZ                                             # metric line-of-sight distance
        horiz = d_da * np.cos(elev)                                   # horizontal radial distance of the ray
        tan_az = (uu - 0.5) * 2.0 * th                               # horizontal azimuth tan
        az = np.arctan(tan_az)
        fwd = horiz * np.cos(az)                                     # along the optical axis (fore/aft) -- not horiz directly
        lat = horiz * np.sin(az)                                     # lateral -- not horiz*tan(az)
        z = Hcam + d_da * sin_e                                      # height (flat ground = 0)
        fwd_, lat_, z_ = fwd[gm], lat[gm], z[gm]
        f0, f1 = np.percentile(fwd_, 2), np.percentile(fwd_, 98)
        l0, l1 = np.percentile(lat_, 2), np.percentile(lat_, 98)
        # -- content-centred square playable footprint --
        # The object cluster is the subject: centre the footprint on the objects' distance band, same radius on both axes (square),
        # radius = max(min playable radius, 0.6 x object-band span + 30m), capped at TERRAIN_MAX_FOOTPRINT_M/2.
        # Objects no longer pile up in the near corner and the 64-cell grid density rises accordingly (platforms/steps become resolvable);
        # pure landscape photos (no objects) fall back to 'start at the near field'. Distant background always goes to ground skirt + atmospheric fog.
        cap_half = TERRAIN_MAX_FOOTPRINT_M / 2.0
        # The footprint is sized by the object cluster's bounding geometry (position + true radius), ratio-bounded:
        #   cluster extent ext = object spread + object sizes -> half = ext/2 + buffer (>=15m or half the cluster width)
        #   -> the cluster fills at most ~50% of the playable area; big objects (mountains) automatically get a big area;
        #   the near edge may reach slightly past the data start (diffusion fill + edge fade cover it), so even the nearest object isn't flush with the edge.
        objs_geo = []
        try:
            dists = _object_distances(results or [], fov_deg, img_aspect)
            core_d = sorted(dists[r["id"]] for r in (results or [])
                            if not r.get("imagined") and r["id"] in dists)
            med_d = core_d[len(core_d) // 2] if core_d else 10.0
            for r in (results or []):
                if r.get("imagined") or r["id"] not in dists:
                    continue
                d_ = dists[r["id"]]
                if d_ > max(40.0, 3.0 * med_d):
                    continue    # background band (distance outliers, e.g. far buildings in the photo): excluded from footprint sizing -- the note above
                                # says 'distant background goes to skirt/fog' but the geometry didn't exclude it (seen: narrow alley + 320m background block -> 400m
                                # platform -> the centroid shift dragged the whole street 77m from the camera; the spawn point stood inside a canyon of pure cloned buildings)
                b_ = r.get("bbox") or [0.25, 0.25, 0.5, 0.5]
                ang_ = math.atan(((b_[0] + b_[2] / 2.0) - 0.5) * 2.0 * th)
                rad_ = max(0.5, min(SIZE_MAX, float(r.get("size_m", 1.0))) / 2.0)
                objs_geo.append((d_ * math.cos(ang_), d_ * math.sin(ang_), rad_))
        except Exception:
            objs_geo = []
        if objs_geo:
            fmin = min(o[0] - o[2] for o in objs_geo); fmax = max(o[0] + o[2] for o in objs_geo)
            lmin = min(o[1] - o[2] for o in objs_geo); lmax = max(o[1] + o[2] for o in objs_geo)
            ext_ = max(5.0, fmax - fmin, lmax - lmin)            # object-cluster bounding extent (incl. radius)
            half = min(cap_half, max(TERRAIN_MIN_HALF_M, ext_ / 2.0 + max(15.0, 0.5 * ext_)))
            f_lo = max(0.0, min(float(f0), fmin - max(10.0, 0.15 * half)))   # nearest object >=10m from the near edge
            f_lo = max(f_lo, (fmin + fmax) / 2.0 - half)
            lm = (lmin + lmax) / 2.0                             # laterally centred on the object cluster
        else:
            half = min(cap_half, max(TERRAIN_MIN_HALF_M, (float(f1) - float(f0)) / 2.0))
            f_lo = float(f0)
            lm = float(np.median(lat_))
        f_hi = f_lo + 2.0 * half
        keepf = (fwd_ >= f_lo) & (fwd_ <= f_hi)
        if int(keepf.sum()) > 400:
            fwd_, lat_, z_ = fwd_[keepf], lat_[keepf], z_[keepf]
            f0, f1 = f_lo, f_hi
            print(f"[Terrain] footprint sized by objects: fwd {f_lo:.0f}..{f_hi:.0f}m (half={half:.0f}m)")
        keepl = (lat_ >= lm - half) & (lat_ <= lm + half)
        if int(keepl.sum()) > 400:                              # square: lateral radius = fore/aft radius
            fwd_, lat_, z_ = fwd_[keepl], lat_[keepl], z_[keepl]
            l0, l1 = lm - half, lm + half
        else:
            l0, l1 = np.percentile(lat_, 2), np.percentile(lat_, 98)
        if (f1 - f0) < 1.0 or (l1 - l0) < 1.0:
            return None
        gi = np.clip(((fwd_ - f0) / (f1 - f0) * (n - 1)).astype(int), 0, n - 1)
        gj = np.clip(((lat_ - l0) / (l1 - l0) * (n - 1)).astype(int), 0, n - 1)
        gsum = np.zeros((n, n)); gcnt = np.zeros((n, n))
        np.add.at(gsum, (gi, gj), z_); np.add.at(gcnt, (gi, gj), 1)
        grid = np.where(gcnt > 0, gsum / np.maximum(gcnt, 1.0), np.nan)
        # Hole filling: cells left empty after object bboxes are masked out grow in from their neighbours by iterative neighbourhood-mean diffusion --
        # a global median fill digs a pit/moat under every object (after the content-centred crop, bboxes cover a lot; pits everywhere).
        import warnings
        for _ in range(2 * n):
            nanm = np.isnan(grid)
            if not nanm.any():
                break
            gp = np.pad(grid, 1, constant_values=np.nan)
            nb = np.stack([gp[1 + dr:1 + dr + n, 1 + dc:1 + dc + n]
                           for dr in (-1, 0, 1) for dc in (-1, 0, 1) if not (dr == 0 and dc == 0)])
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")                    # nanmean warnings on all-NaN neighbourhoods are harmless
                nbm = np.nanmean(nb, axis=0)
            fill = nanm & ~np.isnan(nbm)
            grid[fill] = nbm[fill]
        grid = np.nan_to_num(grid, nan=float(np.nanmedian(grid)))
        # Light smoothing: pure-float separable Gaussian (sigma~1.2). The old path quantized to 8-bit then used PIL blur --
        # quantization (256 levels; a few cm per level at low relief) stacked on DA's own banding -> square terraces on the mesh surface (clear in grazing light / the asset editor).
        k = np.array([0.0545, 0.2442, 0.4026, 0.2442, 0.0545], dtype="float64")   # σ≈1.2, 5 taps
        pad = np.pad(grid, 2, mode="edge")
        grid = np.apply_along_axis(lambda r_: np.convolve(r_, k, mode="valid"), 1, pad)[2:-2, :]
        grid = np.apply_along_axis(lambda c_: np.convolve(c_, k, mode="valid"), 0,
                                   np.pad(grid, ((2, 2), (0, 0)), mode="edge"))
        p2 = float(np.percentile(grid, 2)); p98 = float(np.percentile(grid, 98))
        band = max(1e-6, p98 - p2)
        relief = max(0.0, min(band, 0.5 * float(f1 - f0), TERRAIN_MAX_RELIEF_M))     # absolute cap + footprint cap
        if relief < TERRAIN_MIN_RELIEF_M:                             # nearly flat -> flat ground
            if not force_water:
                print(f"[Terrain] relief {relief:.2f}m < {TERRAIN_MIN_RELIEF_M}m → flat"); return None
            # Water present (generic test): keep the terrain even when gentle -- lake-basin carving lives in the terrain path;
            # flattening demotes the lake to a puddle (seen on the forest lake: relief 0.36m judged flat -> lake shrank to a 7m pool)
            print(f"[Terrain] relief {relief:.2f}m < {TERRAIN_MIN_RELIEF_M}m but WATER present → keep terrain (lake needs a basin)")
        gn = np.clip((grid - p2) / band, 0.0, 1.0)                   # normalize 0..1; base matches relief at p2..p98 (else outliers flatten the terrain)
        # Edge fade: the footprint is cropped out of larger terrain, so the border often cuts mid-slope -> tens-of-metres artificial
        # cut faces ('striped walls') on the perimeter. Smoothstep the outer ~12% down to the perimeter low -> natural island-edge slope; interior untouched.
        eb = max(3, int(0.12 * n))
        idx = np.arange(n, dtype="float32")
        dd = np.minimum(np.minimum(idx[:, None], idx[None, :]),
                        np.minimum((n - 1) - idx[:, None], (n - 1) - idx[None, :]))
        w = np.clip(dd / float(eb), 0.0, 1.0)
        w = w * w * (3.0 - 2.0 * w)
        border = np.concatenate([gn[0, :], gn[-1, :], gn[:, 0], gn[:, -1]])
        edge_floor = float(np.percentile(border, 10))
        gn = gn * w + edge_floor * (1.0 - w)
        print(f"[Terrain] reconstructed: relief {relief:.1f}m, footprint {f1 - f0:.0f}×{l1 - l0:.0f}m, {n}x{n}")
        return {
            "n": n,
            "grid": [[round(float(gn[i, j]), 4) for j in range(n)] for i in range(n)],
            "relief_m": round(relief, 2),
            "cx_m": round(float((f0 + f1) / 2.0), 1), "cy_m": round(float((l0 + l1) / 2.0), 1),
            "half_fwd_m": round(float((f1 - f0) / 2.0), 1), "half_lat_m": round(float((l1 - l0) / 2.0), 1),
        }
    except Exception as e:
        print(f"[Terrain] reconstruct failed: {e}", file=sys.stderr)
        return None


# ── Ground-dressing executor: deterministically compute each layer's instance transforms on the terrain from Gemini's plan (UE only instances) ──
DRESS_MAX_PER_LAYER = 40000        # cap raised so Gemini's dense grass isn't cut (20000 truncated a lush lawn to ~half); not unlimited, leaving VR-performance headroom
DRESS_MAX_TOTAL = 90000            # HISM/GPU instancing can take it; density is still driven by density_per_100m2, so sparse photos stay sparse
DRESS_MAX_CANDIDATES = 130000      # total candidate budget (= Python inner-loop iterations): under high rejection (steep slopes / many object footprints) a keep-count cap doesn't converge, so this hard top stops ~240k iterations


def _terrain_sample(terrain, x_m, y_m):
    """Bilinearly sample the terrain at world (x_m, y_m): returns (height z_m, slope deg); None when out of bounds.
    Mirrors _build_terrain_mesh: row r -> X (fore-aft), column c -> Y (lateral), height = grid(0..1) * relief."""
    if not terrain or not terrain.get("grid") or "cx_m" not in terrain:   # legacy/incomplete terrain (missing footprint keys) -> treat as no terrain instead of KeyError-crashing /latest
        return None
    grid = terrain["grid"]; n = int(terrain.get("n", 0))
    if n < 2:
        return None
    cx = float(terrain.get("cx_m", 0.0)); cy = float(terrain.get("cy_m", 0.0))
    hf = float(terrain.get("half_fwd_m", 0.0)); hl = float(terrain.get("half_lat_m", 0.0)); relief = float(terrain.get("relief_m", 0.0))
    if hf <= 0.0 or hl <= 0.0:
        return None
    fr = ((x_m - cx) / (2.0 * hf) + 0.5) * (n - 1)
    fc = ((y_m - cy) / (2.0 * hl) + 0.5) * (n - 1)
    if fr < 0 or fr > n - 1 or fc < 0 or fc > n - 1:
        return None
    r0 = int(fr); c0 = int(fc); r1 = min(r0 + 1, n - 1); c1 = min(c0 + 1, n - 1)
    tr = fr - r0; tc = fc - c0

    def g(r, c):
        return float(grid[r][c])
    z = ((g(r0, c0) * (1 - tr) + g(r1, c0) * tr) * (1 - tc)
         + (g(r0, c1) * (1 - tr) + g(r1, c1) * tr) * tc)
    dx_m = 2.0 * hf / (n - 1); dy_m = 2.0 * hl / (n - 1)
    dzdx = (g(r1, c0) - g(r0, c0)) * relief / max(0.01, dx_m)
    dzdy = (g(r0, c1) - g(r0, c0)) * relief / max(0.01, dy_m)
    slope_deg = math.degrees(math.atan(math.hypot(dzdx, dzdy)))
    return z * relief, slope_deg


def compute_dressing(plan, terrain, seed=123, exclude=None):
    """Compute each dressing layer's instance transforms on the terrain from Gemini's plan (deterministic; UE only instances).
    clump controls clustering; companion layers scatter around their parent ('small stones by the rock'); reject by slope; snap to terrain height with a slight sink;
    exclude=[(x_m,y_m,r_m)] removes the circles under objects (no grass/rocks under houses or bridges).
    Returns [{name, shape, color, roughness, tex_url, instances:[[x_cm,y_cm,z_cm,yaw,scale_m], ...]}, ...] or None."""
    if not plan or not plan.get("should_dress") or not terrain:
        return None
    ex = exclude or []
    try:
        import numpy as np
        rng = np.random.default_rng(seed)
        cx = float(terrain["cx_m"]); cy = float(terrain["cy_m"])
        hf = float(terrain["half_fwd_m"]); hl = float(terrain["half_lat_m"])
        foot_m2 = max(1.0, (2 * hf) * (2 * hl))
        layers_out = []; points_by_name = {}; total = 0; cand_used = 0
        order = sorted(plan["layers"], key=lambda L: 1 if (L.get("companion_of") or "").strip() else 0)
        for L in order:
            if total >= DRESS_MAX_TOTAL or cand_used >= DRESS_MAX_CANDIDATES:
                break
            target = int(L["density_per_100m2"] * foot_m2 / 100.0)
            target = max(0, min(target, DRESS_MAX_PER_LAYER, DRESS_MAX_TOTAL - total, DRESS_MAX_CANDIDATES - cand_used))
            if target <= 0:
                continue
            cand_used += target                                  # candidate budget counts generated candidates, independent of rejection rate -> bounds the inner-loop iterations
            comp = (L.get("companion_of") or "").strip()
            if comp and comp in points_by_name and len(points_by_name[comp]):
                parents = points_by_name[comp]
                pick = parents[rng.integers(0, len(parents), size=target)]
                rad = max(0.1, float(L["size_m"][1]) * 3.0)          # cluster within ~3x own size around the parent layer
                cand = pick + rng.normal(0, rad, size=(target, 2))
            else:
                clump = float(L["clump"]); n_uni = int(target * (1 - clump))
                cand = np.column_stack([rng.uniform(cx - hf, cx + hf, size=target),
                                        rng.uniform(cy - hl, cy + hl, size=target)])
                n_clu = target - n_uni
                if n_clu > 0:
                    n_seeds = max(3, int(target / 80))
                    seeds = np.column_stack([rng.uniform(cx - hf, cx + hf, size=n_seeds),
                                             rng.uniform(cy - hl, cy + hl, size=n_seeds)])
                    crad = max(1.0, 0.06 * min(2 * hf, 2 * hl))
                    cand[n_uni:] = seeds[rng.integers(0, n_seeds, size=n_clu)] + rng.normal(0, crad, size=(n_clu, 2))
            mx_slope = float(L["max_slope_deg"]); smin, smax = L["size_m"]
            if L.get("shape") == "card":                       # grass/plants: extra +-15% jitter on Gemini's size range -> varied sizes within a clump look livelier (density unchanged)
                smin, smax = smin * 0.85, smax * 1.15
            inst = []; kept = []
            for x_m, y_m in cand:
                xm = float(x_m); ym = float(y_m)
                s = _terrain_sample(terrain, xm, ym)
                if s is None or s[1] > mx_slope:
                    continue
                if any((xm - ox) ** 2 + (ym - oy) ** 2 < orr * orr for ox, oy, orr in ex):
                    continue                                    # don't scatter under objects
                scale = float(rng.uniform(smin, smax)); yaw = float(rng.uniform(0, 360))
                inst.append([round(float(x_m) * 100, 1), round(float(y_m) * 100, 1),
                             round((s[0] - 0.25 * scale) * 100, 1), round(yaw, 1), round(scale, 3)])
                kept.append((float(x_m), float(y_m)))
            if not inst:
                continue
            points_by_name[L["name"]] = np.array(kept)
            total += len(inst)
            layers_out.append({"name": L["name"], "shape": L["shape"], "species": L.get("species", ""),
                               "color": L["color"], "roughness": L["roughness"],
                               "tex_url": L.get("tex_url", ""), "instances": inst})
        print("[Dressing] computed %d instances across %d layers" % (total, len(layers_out)))
        return layers_out or None
    except Exception as e:
        print("[Dressing] compute failed: %s" % e, file=sys.stderr)
        return None


# ── Particle/fluid executor: resolve Gemini's FX plan into world coordinates (water plane / card groups / fog); UE instances statically (a single frame needs no animation) ──
FX_MAX_TOTAL = 18000
# Live-particle density table: ALIVE particles per m2 by intensity tier (sparse/medium/dense). Gemini's in-frame count is only a tier signal;
# scene budget = playable area x rate (theory anchor: game ambient particles are commonly 0.5-5/m2; rain denser than dust; leaves/petals/debris use pocket-local density).
NS_DENSITY = {
    "rain": (1.5, 3.0, 6.0), "snow": (1.0, 2.0, 4.0),
    "dust": (0.9, 1.8, 3.6), "ash": (0.8, 1.5, 3.0),
    "petals": (0.5, 1.0, 2.0), "leaves": (0.5, 1.0, 2.0),
    "debris": (0.4, 0.8, 1.6), "embers": (0.8, 1.6, 3.2),
    "smoke": (0.4, 0.8, 1.5), "steam": (0.5, 1.0, 2.0),
    "fireflies": (0.4, 0.8, 1.5), "mist": (0.03, 0.06, 0.12),
}
NS_BUDGET_TOTAL = 24000          # whole-scene alive-particle cap (safe CPU Niagara budget)
NS_PER_EMITTER_CAP = 4000        # per-emitter cap (excess spread across quadrants/pockets)
_FX_TEX_TMPL = {
    "waterfall": "a tall WIDE CURTAIN of bright falling white water made of many fine vertical streaks (a full waterfall sheet), soft feathered edges, filling the frame top to bottom",
    "fall": "a tall WIDE CURTAIN of bright falling white water made of many fine vertical streaks (a full waterfall sheet), soft feathered edges",
    "water": "a wide curtain of bright falling white water streaks",
    "rain": "a few thin vertical white rain streaks",
    "spray": "a soft burst of fine white water spray droplets",
    "splash": "a soft burst of white water splash droplets",
    "mist": "a soft diffuse round puff of pale white mist with faded soft edges",
    "fog": "a soft diffuse pale grey fog puff with soft faded edges",
    "haze": "a soft diffuse pale haze puff with faded edges",
    "smoke": "a soft diffuse puff of grey smoke with soft faded edges",
    "steam": "a soft diffuse puff of white steam with soft edges",
    "dust": "a few tiny soft round out-of-focus warm light dust motes",
    "pollen": "a few tiny soft round glowing pollen motes",
    "snow": "a few soft round white snowflake dots",
    "bird": "a solid WHITE bird silhouette shape with spread wings (white shape, the engine tints it dark)",
    "insect": "a small solid WHITE insect silhouette shape",
    "ember": "a few small glowing orange ember sparks",
    "spark": "a few small glowing sparks",
}


@retry_with_backoff()
def generate_fx_textures(client, image: Image.Image, plan: dict, output_dir: str, task_id: str):
    """Generate one single-element texture on PURE BLACK per card FX layer (white water streak / rain threads / fog puff / dust motes / bird silhouettes...);
    UE uses its luminance as opacity. Adds tex_url to the card layers in place. water/fog layers need no texture (procedural / engine fog)."""
    if not plan or not plan.get("layers"):
        return
    for i, L in enumerate(plan["layers"]):
        if L.get("primitive") != "card":
            continue
        existing = os.path.join(output_dir, "fx_card%d.png" % i)
        if os.path.exists(existing):                              # retry-idempotent: layers already on disk are reused, so @retry reruns don't re-bill
            L["tex_url"] = "/output/%s/fx_card%d.png" % (task_id, i)
            continue
        nm = str(L.get("name", "")).lower(); region = L.get("region", "air")
        if region == "fall_line":                                 # only the fall_line layer uses the 'water curtain' texture
            key = "waterfall"
        else:                                                     # other layers must not use the curtain template (else fog/spray become big white strips)
            key = next((k for k in _FX_TEX_TMPL if k in nm and k not in ("waterfall", "fall", "water")), None)
            if key is None:
                key = "bird" if L.get("blend") == "masked" else "mist"
        prompt = ("%s, centered, on a PURE SOLID BLACK (#000000) background, nothing else at all, no ground, "
                  "no horizon, evenly lit, true colour." % _FX_TEX_TMPL[key])
        saved = ""
        for ar, size in (("1:1", "1K"),):
            try:
                cfg = genai_types.GenerateContentConfig(
                    responseModalities=["IMAGE"], imageConfig=genai_types.ImageConfig(aspectRatio=ar, imageSize=size))
                resp = _gen_image_content(client, [prompt, image], cfg)
                for p in resp.parts:
                    if getattr(p, "thought", False):
                        continue
                    if p.inline_data is not None:
                        raw = os.path.join(output_dir, "_fx_raw%d.png" % i)
                        p.as_image().save(raw)
                        Image.open(raw).convert("RGB").save(os.path.join(output_dir, "fx_card%d.png" % i))
                        try:
                            os.remove(raw)
                        except Exception:
                            pass
                        saved = "/output/%s/fx_card%d.png" % (task_id, i)
                        print("[FX] card texture %d (%s) saved" % (i, key))
                        break
                if saved:
                    break
            except ServerError:
                raise
            except Exception as e:
                print("[FX] card tex %d failed: %s" % (i, e), file=sys.stderr)
                continue
        L["tex_url"] = saved


def _fx_project_ground(u, v, camera, th, vfov_deg, terrain, scene_scale):
    """Image (u,v) -> ground world point (fwd_m, lat_m, ground_z_m). Below the horizon use the flat-ground intersection + terrain height; sky/near-horizon
    points are clamped to the terrain footprint's far bound (otherwise near-horizon anchors project tens of metres away, outside the visible scene)."""
    Hcam = _clampf(camera.get("height_m", 1.6), 0.2, 200.0, 1.6)
    pitch = math.radians(_clampf(camera.get("pitch_deg", -10.0), -89.0, 30.0, -10.0))
    e = pitch - (v - 0.5) * math.radians(vfov_deg)
    az = math.atan((u - 0.5) * 2.0 * th)
    if terrain:                                              # clamp near the terrain patch so FX land inside the visible scene
        hf = float(terrain["half_fwd_m"]); hl = float(terrain["half_lat_m"])
        cdist = math.hypot(float(terrain["cx_m"]), float(terrain["cy_m"]))
        far = max(6.0, cdist + 1.6 * max(hf, hl))
    else:
        far = 4.0 * max(5.0, scene_scale)
    if e < math.radians(-0.5):
        horiz = min(max(Hcam / math.tan(-e), 1.0), far)
    else:
        horiz = min(2.5 * max(5.0, scene_scale), far)
    fwd = horiz * math.cos(az); lat = horiz * math.sin(az)
    return fwd, lat, _terrain_ground(terrain, fwd, lat)


FX_FACE_YAW0 = 0.0   # calibration from single-card normal +X to camera-facing yaw (tweak after render)


def _fx_capture_cam(terrain, layout):
    """Replicate _capture_view's capture-camera horizontal position (cm) so cards face the shooting camera (kills crossing/side-slicing). Returns (camx_cm, camy_cm) or None."""
    pts = [it["location"] for it in (layout or []) if not it.get("imagined") and "location" in it]
    if pts:
        cx = sum(p[0] for p in pts) / len(pts); cy = sum(p[1] for p in pts) / len(pts)
        rad = max(300.0, max(((p[0] - cx) ** 2 + (p[1] - cy) ** 2) ** 0.5 for p in pts))
    elif terrain:
        cx = float(terrain["cx_m"]) * 100.0; cy = float(terrain["cy_m"]) * 100.0
        rad = max(300.0, 100.0 * max(float(terrain["half_fwd_m"]), float(terrain["half_lat_m"])))
    else:
        return None
    fit = (rad * 1.25 + 1500.0) / math.tan(math.radians(26.0))
    az = math.radians(38.0); el = math.radians(18.0)
    return (cx - fit * math.cos(el) * math.cos(az), cy - fit * math.cos(el) * math.sin(az))


def _fx_face_yaw(x_cm, y_cm, cam):
    """Yaw (deg) for a single card (normal +X) to face the capture camera."""
    if not cam:
        return 0.0
    return math.degrees(math.atan2(cam[1] - y_cm, cam[0] - x_cm)) + FX_FACE_YAW0


def _room_bounds_cm(room, layout):
    """Room box bounds (cm) -- the same expansion math as ue_scene_builder._room_bounds (changing one means changing the other).
    Returns (x0, x1, y0, y1, z1)."""
    w_m, d_m, h_m = (room or {}).get("size_m", [5.0, 6.0, 2.8])
    cam = (room or {}).get("cam", [0.5, 0.15])

    def _hw(it):
        return max(40.0, min(220.0, float((it.get("scale") or [160])[0]) / 2.0))
    pts = [(it["location"][0], it["location"][1], _hw(it))
           for it in (layout or []) if it.get("location")]
    tops = [it["location"][2] + float((it.get("scale") or [250])[0])
            for it in (layout or []) if it.get("location")] or [250.0]
    x0 = min(-max(80.0, cam[1] * d_m * 100.0), min((x - hw for x, _, hw in pts), default=220.0) - 40.0)
    x1 = max(x0 + d_m * 100.0, max((x + hw for x, _, hw in pts), default=380.0) + 40.0)
    y0 = min(-w_m * 50.0, min((y - hw for _, y, hw in pts), default=-80.0) - 40.0)
    y1 = max(w_m * 50.0, max((y + hw for _, y, hw in pts), default=80.0) + 40.0)
    z1 = max(h_m * 100.0, max(tops) + 30.0)
    return x0, x1, y0, y1, z1


def compute_effects(plan, terrain, layout, camera, fov_deg=65.0, img_aspect=1.333, scene_scale=10.0, seed=137,
                    room_bounds=None, room=None):
    """Resolve Gemini's FX plan into world coordinates (deterministic): water -> translucent plane, card -> card-group instances, fog -> fog params.
    Regions (anchor_uv/extent_uv/region/height/thickness) resolve to world via pinhole projection; reuses _terrain_ground for grounding.
    Returns [{name, primitive, blend, color, opacity, emissive, tex_url, plane?, instances?, fog?}, ...] or None."""
    if not plan or not plan.get("should_fx"):
        return None
    try:
        import numpy as np
        rng = np.random.default_rng(seed)
        hfov = math.radians(max(25.0, min(100.0, fov_deg)))
        th = math.tan(hfov / 2.0); tv = th / max(0.1, img_aspect); vfov_deg = math.degrees(2.0 * math.atan(tv))
        S = max(5.0, scene_scale)
        # Cap Gemini's absolute heights/thicknesses by scene scale: no 60m waterfalls / 90m-high birds in a small reconstruction, out of proportion with small terrain
        hcap = (max(2.0 * float(terrain["relief_m"]), 0.8 * 2.0 * float(terrain["half_fwd_m"])) if terrain else 2.0 * S)
        obj_loc = {it["id"]: it["location"] for it in (layout or []) if "id" in it}
        cam = _fx_capture_cam(terrain, layout)                # capture-camera horizontal position (cm); cards face it
        out = []; total = 0; ns_total = 0
        # Fair share: diversity beats single-layer density -- no layer (e.g. dense-tier dust) may eat the whole budget and starve later layers
        n_nl = sum(1 for _L in plan["layers"] if str(_L.get("niagara", "none") or "none") != "none")
        ns_layer_cap = max(6000, int(NS_BUDGET_TOTAL / max(1, n_nl)))
        for L in plan["layers"]:
            L = dict(L)
            L["height_m"] = min(float(L.get("height_m", 0.0)), hcap)
            L["thickness_m"] = min(float(L.get("thickness_m", 6.0)), hcap)
            prim = L["primitive"]
            emi = 1.0 + L["emissive"]    # in unlit, emissive = texture brightness multiplier: 1.0 baseline (texture's own colour) + glow bonus
            entry = {"name": L["name"], "primitive": prim, "blend": L["blend"], "color": L["color"],
                     "opacity": L["opacity"], "emissive": emi, "tex_url": L.get("tex_url", ""),
                     "water_preset": L.get("water_preset", "lake_calm"),         # real water-body material preset (AI-picked)
                     "water_color": L.get("water_color"), "water_clarity": L.get("water_clarity", 0.5),
                     "water_calm": L.get("water_calm", 0.7), "water_depth_feel": L.get("water_depth_feel", "medium")}
            au = L["anchor_uv"]; eu = L["extent_uv"]
            if prim == "water":
                # The water's bottom edge in frame (0..1, larger = nearer the camera) -> ue_scene_builder basin carving uses it to decide
                # which photo objects 'stand in front of the water' (object bbox bottom below it = on shore; the lake must start behind them)
                entry["water_bottom_v"] = round(float(au[1]) + float(eu[1]) * 0.5, 4)
            fwd, lat, gz = _fx_project_ground(au[0], au[1], camera, th, vfov_deg, terrain, S)
            dist = max(2.0, math.hypot(fwd, lat))
            rad = max(1.0, eu[0] * 2.0 * th * dist)               # lateral radius (m)
            if prim == "fog":
                entry["fog"] = {"density": round(0.004 + 0.05 * L["opacity"], 4),
                                "color": L["color"], "height_m": L["thickness_m"]}
                out.append(entry); continue
            if prim == "water":
                sx = max(2.0, eu[0] * 2.0 * th * dist); sy = max(2.0, eu[1] * 2.0 * th * dist)
                if terrain:
                    hl = float(terrain["half_lat_m"]); hf = float(terrain["half_fwd_m"])
                    if L["region"] == "water_surface":
                        # plane semantics = [X centre (depth), Y centre (lateral), z, X span, Y span] (matches the ue_scene_builder water/basin consumers;
                        # old code swapped the axes + 'shift centre back to cover the foreground', flooding the near grass bank too -- seen: the photo's camera position was in the lake).
                        # New rule: the NEAR edge is faithful to the AI's call (water starts where the photo says); only the FAR edge
                        # is pulled to the terrain's far end (the lake recedes to the horizon, keeping 'big lake' from shrinking to a puddle); laterally it spans most of the width.
                        near = max(0.0, fwd - sy / 2.0)                     # AI near water boundary (m)
                        far = float(terrain.get("cx_m", 0.0)) + hf          # terrain far end
                        depth_span = max(4.0, far - near)
                        lat_span = max(sx, (0.6 + 0.8 * min(1.0, eu[0])) * hl)
                        fwd = near + depth_span / 2.0
                        sx = min(depth_span, 2.0 * hf)                      # out param [3] = depth span
                        sy = min(lat_span, 2.0 * hl)                        # out param [4] = lateral span
                    else:
                        sx = min(sx, 2.0 * hl); sy = min(sy, 2.0 * hf)
                cz = gz + L["height_m"]
                entry["plane"] = [round(fwd * 100, 1), round(lat * 100, 1), round(cz * 100, 1),
                                  round(sx * 100, 1), round(sy * 100, 1)]
                out.append(entry); continue
            # card
            if room_bounds:
                # No static cards indoors (seen: 45 white 'heavy rain outside' cards scattered through the room by projection = a room full of white strips;
                # indoor FX use the live-particle vocabulary only + the outside rain curtain is the room shell's job)
                continue
            n = min(int(L["count"]), 8000, max(0, FX_MAX_TOTAL - total))
            if n <= 0:
                continue
            smin, smax = L["size_m"]; region = L["region"]
            oid = L.get("near_object_id", -1)
            if oid in obj_loc:
                base = obj_loc[oid]; bx, by, bz = base[0] / 100.0, base[1] / 100.0, base[2] / 100.0
            elif region == "fall_line":                          # fall_line: project the feature's BOTTOM edge for the landing point (nearer; lands on water/ground, rises vertically)
                bx, by, bz = _fx_project_ground(au[0], min(1.0, au[1] + eu[1] * 0.5), camera, th, vfov_deg, terrain, S)
            else:
                bx, by, bz = fwd, lat, gz
            # Live particles: layers where Gemini picked a niagara preset -> emit emitter placement entries (instead of static cards).
            # Split of labour: Gemini decides type/anchor/intensity tier/colour/size; code guarantees walkable-scale presence --
            # the in-frame count is only a tier signal (sparse<80 / medium<300 / dense); scene budget = playable area x NS_DENSITY rate;
            # coverage types (rain/snow/dust/ash) = 4 quadrant emitters over the whole field + denser pockets in open areas; pocket types (leaves/petals/embers/debris) = anchor rings / K pockets in open areas.
            nia = L.get("niagara", "none")
            if nia and nia != "none":
                thick = min(L["thickness_m"], 8.0)
                cnt = float(L["count"])
                tier = 0 if cnt < 80 else (1 if cnt < 300 else 2)
                rate = NS_DENSITY.get(nia, (0.5, 1.0, 2.0))[tier]
                # size = true range (Gemini size_m [min,max]); the executor samples per emitter -> same-layer particles vary in size
                s_lo = min(100.0, max(4.0, 100.0 * float(L["size_m"][0])))
                s_hi = min(140.0, max(max(10.0, 1.6 * s_lo), 100.0 * float(L["size_m"][1])))
                if nia in ("dust", "ash", "embers", "fireflies"):
                    # game-visibility floor for fine particles: physical (millimetre) sizes are sub-pixel beyond 10m -> invisible in test screenshots.
                    # a soft-glow sprite stands for 'a pinch' of dust (near-camera bokeh); measured 10-22cm reads as snow -> 5-12cm
                    s_lo = max(s_lo, 5.0); s_hi = min(max(s_hi, 12.0), 14.0)
                elif nia == "rain":
                    # rain = small droplet sprites (seen: Gemini size_m 0.4-0.8m -> 40-80cm randomly-oriented cards
                    # = 'rocking snowflakes'); round drops don't care about card rotation, and fast fall reads as rain
                    s_lo = max(6.0, min(s_lo, 10.0)); s_hi = min(max(s_hi, 12.0), 16.0)
                elif nia == "mist":
                    s_lo = max(s_lo, 140.0); s_hi = min(max(s_hi, 260.0), 300.0)   # small and dense (a single big disc read as 'flat sheets')
                elif nia in ("smoke", "steam"):
                    s_lo = max(s_lo, 25.0); s_hi = max(s_hi, 65.0)     # smoke/steam are 'puffs', not 'grains'
                size_cm = round(0.5 * (s_lo + s_hi), 1)            # mean kept for compatibility (old executor)
                if room_bounds:
                    # Indoor deployment: volume = room box, no quadrants/terrain. Dust = one room-wide blanket emitter (indoor air is calm,
                    # naturally low density); steam/smoke/embers MUST have an object anchor (sourceless smoke floating mid-room is a giveaway -> drop the layer)
                    rx0, rx1, ry0, ry1, rz1 = room_bounds
                    rdm, rwm, rhm = (rx1 - rx0) / 100.0, (ry1 - ry0) / 100.0, rz1 / 100.0
                    if nia == "dust":
                        # dust semantics = motes in light beams: only valid with strong daylight windows (>=2000lux) -- white dots in a rainy-night/dark room
                        # have no optical explanation (review said 'particles in the room' while the source photo was clean)
                        max_win = max((float(o.get("lux", 0.0)) for o in
                                       ((room or {}).get("openings") or [])), default=0.0)
                        if max_win < 2000.0:
                            continue
                        per = int(min(float(NS_PER_EMITTER_CAP), max(40.0, rate * rdm * rwm),
                                      max(0.0, float(NS_BUDGET_TOTAL - ns_total))))
                        if per >= 30:
                            ns_total += per
                            entry["primitive"] = "niagara"
                            entry["niagara"] = {
                                "preset": nia,
                                "spots": [[round((rx0 + rx1) / 2, 1), round((ry0 + ry1) / 2, 1),
                                           round(rz1 / 2, 1)]],
                                "box_xy": round(max(rdm, rwm) * 50.0, 1),
                                "box_z": round(max(60.0, rz1 / 2 - 40.0), 1),
                                "count": per, "size_cm": size_cm,
                                "size_cm_lo": round(s_lo, 1), "size_cm_hi": round(s_hi, 1)}
                            out.append(entry)
                    elif oid in obj_loc:
                        attach = str(L.get("attach", "none"))
                        osz = max(0.5, float((next((it.get("scale") or [150] for it in (layout or [])
                                                    if it.get("id") == oid), [150]))[0]) / 100.0)
                        pcount = (30, 60, 110)[tier]
                        if attach == "around":                  # ember-like around a fire: three-point ring
                            spots = [[round((bx + 0.5 * osz * math.cos(a_)) * 100, 1),
                                      round((by + 0.5 * osz * math.sin(a_)) * 100, 1),
                                      round((bz + 0.6 * osz) * 100, 1)] for a_ in (0.0, 2.1, 4.2)]
                            pb = max(0.6, 0.4 * osz)
                        else:                                   # top/default: top point source (cup steam / incense smoke)
                            spots = [[round(bx * 100, 1), round(by * 100, 1),
                                      round((bz + 0.95 * osz) * 100, 1)]]
                            pb = min(1.5, max(0.5, 0.3 * osz))
                        if ns_total + pcount * len(spots) <= NS_BUDGET_TOTAL:
                            ns_total += pcount * len(spots)
                            entry["primitive"] = "niagara"
                            entry["niagara"] = {"preset": nia, "spots": spots,
                                                "box_xy": round(pb * 100, 1), "box_z": 60.0,
                                                "count": pcount, "size_cm": size_cm,
                                                "size_cm_lo": round(s_lo, 1), "size_cm_hi": round(s_hi, 1)}
                            out.append(entry)
                    continue
                if nia in ("rain", "snow", "dust", "ash") and terrain:
                    # Coverage type: a base layer wherever you walk. One big box exceeds the per-emitter budget -> 4 quadrants, 1/4 field each
                    cxm, cym = float(terrain["cx_m"]), float(terrain["cy_m"])
                    hfm, hlm = float(terrain["half_fwd_m"]), float(terrain["half_lat_m"])
                    area = (2.0 * hfm) * (2.0 * hlm)
                    # blanket takes 78% of the layer share, denser pockets 22% (pockets used to take extra for free -> with many layers they ate the budget and starved pocket-type layers)
                    want = min(rate * area, 0.78 * float(ns_layer_cap), max(0.0, float(NS_BUDGET_TOTAL - ns_total)))
                    if nia in ("rain", "snow"):
                        # band top <=12m (seen: a 25m band dilutes the falling column; 0.006/m3 = rain is falling but invisible)
                        band_z = min(L["height_m"] + L["thickness_m"], hcap, 12.0)
                        bz_box = min(thick, 6.0) * 50.0
                        # precipitation IS the weather; exempt from inter-layer fair share (the share once squeezed heavy rain to 4680 particles)
                        want = min(rate * area, 0.6 * float(NS_BUDGET_TOTAL),
                                   max(0.0, float(NS_BUDGET_TOTAL - ns_total)))
                        # only cover the playable core (object cluster), not the whole terrain -- spread field-wide it's 0.17/m3, 'rain is falling
                        # but does not read' (measured); 0.4+/m3 in the core is what reads as heavy rain
                        pts = [(it["location"][0] / 100.0, it["location"][1] / 100.0)
                               for it in (layout or []) if it.get("location")]
                        ccx = sum(p[0] for p in pts) / len(pts) if pts else cxm
                        ccy = sum(p[1] for p in pts) / len(pts) if pts else cym
                        crad = max((math.hypot(p[0] - ccx, p[1] - ccy) for p in pts), default=10.0)
                        box_r = max(15.0, min(32.0, 1.2 * crad + 8.0))
                        spots = []
                        for dx_ in (-box_r * 0.7, box_r * 0.7):
                            qx, qy = ccx + dx_, ccy
                            spots.append([round(qx * 100, 1), round(qy * 100, 1),
                                          round((_terrain_ground(terrain, qx, qy) + band_z) * 100, 1)])
                        per = int(min(float(NS_PER_EMITTER_CAP), want / 2.0))
                        if per >= 30:
                            ns_total += per * 2
                            entry["primitive"] = "niagara"
                            entry["niagara"] = {"preset": nia, "spots": spots,
                                                "box_xy": round(box_r * 100, 1),
                                                "box_z": round(bz_box, 1), "count": per, "size_cm": size_cm,
                                                "size_cm_lo": round(s_lo, 1), "size_cm_hi": round(s_hi, 1)}
                            out.append(entry)
                        continue
                    else:
                        # hover band narrowed and anchored at eye height: the same budget concentrated at sightline (with a 4-5m band ~0.3/m3 around the eyes = invisible)
                        band_z = min(max(1.5, L["height_m"]), 2.5)
                        bz_box = min(thick, 4.0) * 50.0
                    spots = []
                    for sx_ in (-0.5, 0.5):
                        for sy_ in (-0.5, 0.5):
                            qx, qy = cxm + sx_ * hfm, cym + sy_ * hlm
                            spots.append([round(qx * 100, 1), round(qy * 100, 1),
                                          round((_terrain_ground(terrain, qx, qy) + band_z) * 100, 1)])
                    per = int(min(float(NS_PER_EMITTER_CAP), want / 4.0))
                    if per >= 30:
                        ns_total += per * 4
                        entry["primitive"] = "niagara"
                        entry["niagara"] = {"preset": nia, "spots": spots,
                                            "box_xy": round(min(60.0, max(hfm, hlm)) * 100, 1),
                                            "box_z": round(bz_box, 1), "count": per, "size_cm": size_cm,
                                            "size_cm_lo": round(s_lo, 1), "size_cm_hi": round(s_hi, 1)}
                        out.append(entry)
                    if nia in ("dust", "ash"):
                        # hover types also get denser open-area POCKETS (3x local density, rejection-sampled away from object footprints): visible dense patches on top of the base
                        circles = [(it["location"][0] / 100.0, it["location"][1] / 100.0,
                                    max(2.0, 0.55 * float((it.get("scale") or [100])[0]) / 100.0))
                                   for it in (layout or []) if it.get("location")]
                        pb = 12.0
                        pcount = int(min(1200.0, 3.0 * rate * pb * pb))
                        # stratified sampling: one fixed patch per quadrant (pure random once put 3 of 4 into the same half -> players felt 'particles all bunched in one spot')
                        spots2 = []
                        for sx_, sy_ in ((-0.5, -0.5), (-0.5, 0.5), (0.5, -0.5), (0.5, 0.5)):
                            px_ = py_ = None
                            for _try in range(40):
                                qx = cxm + sx_ * hfm + float(rng.uniform(-hfm * 0.4, hfm * 0.4))
                                qy = cym + sy_ * hlm + float(rng.uniform(-hlm * 0.4, hlm * 0.4))
                                if all((qx - c0) ** 2 + (qy - c1) ** 2 > cr * cr for c0, c1, cr in circles):
                                    px_, py_ = qx, qy
                                    break
                            if px_ is None:
                                px_, py_ = cxm + sx_ * hfm, cym + sy_ * hlm
                            spots2.append([round(px_ * 100, 1), round(py_ * 100, 1),
                                           round((_terrain_ground(terrain, px_, py_) + band_z) * 100, 1)])
                        K = len(spots2)
                        room = NS_BUDGET_TOTAL - ns_total
                        layer_room = max(0, int((float(ns_layer_cap) - per * 4.0) / K))   # remaining share of this layer
                        if room >= 30 * K and layer_room >= 30:
                            pcount = min(pcount, int(room / K), layer_room)   # shrink rather than drop the layer
                            ns_total += pcount * K
                            e2 = dict(entry)
                            e2["name"] = (L["name"] + " (accent)")[:40]
                            e2["primitive"] = "niagara"
                            e2["niagara"] = {"preset": nia, "spots": spots2, "box_xy": round(pb * 100, 1),
                                             "box_z": round(min(thick, 4.0) * 50.0, 1),
                                             "count": pcount, "size_cm": size_cm,
                                             "size_cm_lo": round(s_lo, 1), "size_cm_hi": round(s_hi, 1)}
                            out.append(e2)
                    continue
                if nia in ("petals", "leaves", "debris", "embers", "gravel",
                           "smoke", "steam", "fireflies", "mist") and terrain:
                    # Pocket type: attach anchors at the semantic part (AI names which part of the object emits), else ring/quadrant scatter
                    cxm, cym = float(terrain["cx_m"]), float(terrain["cy_m"])
                    hfm, hlm = float(terrain["half_fwd_m"]), float(terrain["half_lat_m"])
                    anchored = (oid in obj_loc) or (region == "over_object")
                    attach = str(L.get("attach", "none"))
                    osz = 3.0
                    if oid in obj_loc:
                        osz = max(1.0, float((next((it.get("scale") or [300] for it in (layout or [])
                                                    if it.get("id") == oid), [300]))[0]) / 100.0)
                    K = 4
                    pb = min(16.0, max(8.0, rad))
                    pcount = int(min(1500.0, max(40.0, rate * pb * pb)))
                    band_z = min(max(1.5, L["height_m"]) + 0.5 * thick, hcap)
                    r0 = min(0.5 * max(hfm, hlm), max(3.0, rad))
                    spots = []
                    if anchored and attach == "top":
                        # top point source (chimney smoke / roof steam): single emitter, small box, count by tier
                        K = 1
                        pb = min(3.0, max(1.0, 0.35 * osz))
                        pcount = (30, 60, 110)[tier]
                        spots = [[round(bx * 100, 1), round(by * 100, 1),
                                  round((bz + 0.95 * osz) * 100, 1)]]
                    elif anchored and attach == "canopy":
                        # inside the canopy volume (in-crown falling leaves/spores): K=3 points scattered in the canopy band (0.45-0.75; 0.9 read as 'too high')
                        K = 3
                        pb = min(10.0, max(2.0, 0.45 * osz))
                        for _ in range(K):
                            qx = bx + float(rng.uniform(-0.25, 0.25)) * osz
                            qy = by + float(rng.uniform(-0.25, 0.25)) * osz
                            qz = bz + osz * float(rng.uniform(0.45, 0.75))
                            spots.append([round(qx * 100, 1), round(qy * 100, 1), round(qz * 100, 1)])
                    elif anchored and attach == "base":
                        # at the feet (base ground-fog / splashes): K=2, thin ground-hugging band
                        K = 2
                        pb = min(12.0, max(2.0, 0.8 * osz))
                        for j_ in range(K):
                            ang = float(rng.uniform(0.0, 2.0 * math.pi))
                            qx, qy = bx + 0.5 * osz * math.cos(ang), by + 0.5 * osz * math.sin(ang)
                            spots.append([round(qx * 100, 1), round(qy * 100, 1),
                                          round((_terrain_ground(terrain, qx, qy) + 0.4) * 100, 1)])
                    else:
                        for j_, (sx_, sy_) in enumerate(((-0.5, -0.5), (-0.5, 0.5), (0.5, -0.5), (0.5, 0.5))):
                            if anchored:
                                # anchored (around/default): ring scatter around the source object (spores under a tree / fireflies by a lamp)
                                ang = float(rng.uniform(0.0, 2.0 * math.pi))
                                rr = r0 * float(rng.uniform(0.35, 1.1))
                                qx = min(cxm + hfm * 0.95, max(cxm - hfm * 0.95, bx + rr * math.cos(ang)))
                                qy = min(cym + hlm * 0.95, max(cym - hlm * 0.95, by + rr * math.sin(ang)))
                            else:
                                # unanchored: one pocket per quadrant (pure random bunches into the same half)
                                qx = cxm + sx_ * hfm + float(rng.uniform(-hfm * 0.4, hfm * 0.4))
                                qy = cym + sy_ * hlm + float(rng.uniform(-hlm * 0.4, hlm * 0.4))
                            spots.append([round(qx * 100, 1), round(qy * 100, 1),
                                          round((_terrain_ground(terrain, qx, qy) + band_z) * 100, 1)])
                    if nia == "mist":
                        pcount = max(20, min(70, pcount * 2))      # small and dense, overlapping into a fog bank (one big disc reads as a 'flat sheet')
                        if not anchored:
                            # unanchored fog: 5 big overlapping pockets (quadrants + centre), all ground-hugging (quadrants alone = corners only, a gap in the middle; seen as 'disjointed')
                            pb = max(pb, 14.0)
                            spots = []
                            for sx_, sy_ in ((-0.5, -0.5), (-0.5, 0.5), (0.5, -0.5), (0.5, 0.5), (0.0, 0.0)):
                                qx, qy = cxm + sx_ * hfm, cym + sy_ * hlm
                                spots.append([round(qx * 100, 1), round(qy * 100, 1),
                                              round((_terrain_ground(terrain, qx, qy) + 0.6) * 100, 1)])
                            K = 5
                    room = NS_BUDGET_TOTAL - ns_total
                    if room >= 40 * K:
                        pcount = min(pcount, int(room / K))        # shrink rather than drop the layer: diversity first
                        ns_total += pcount * K
                        entry["primitive"] = "niagara"
                        entry["niagara"] = {"preset": nia, "spots": spots, "box_xy": round(pb * 100, 1),
                                            "box_z": round(min(thick, 6.0) * 50.0, 1),
                                            "count": pcount, "size_cm": size_cm,
                                            "size_cm_lo": round(s_lo, 1), "size_cm_hi": round(s_hi, 1)}
                        out.append(entry)
                    continue
                # The rest (waterfall/fountain/no-terrain fallback): a single emitter anchored at the photo-projected point
                if region == "fall_line":
                    span = min(L["height_m"] + L["thickness_m"], hcap)
                    pos = (bx, by, bz + span)                     # falling water starts from the top
                    bxy = min(20.0, max(1.5, 0.65 * rad))
                else:
                    pos = (bx, by, bz + min(L["height_m"] + 0.5 * thick, hcap))   # anchor + band centre
                    bxy = min(20.0, max(1.0, rad))
                entry["primitive"] = "niagara"
                entry["niagara"] = {
                    "preset": nia,
                    "pos": [round(pos[0] * 100, 1), round(pos[1] * 100, 1), round(pos[2] * 100, 1)],
                    "box_xy": round(bxy * 100, 1),
                    "box_z": round(thick * 50.0, 1),
                    "count": int(min(cnt, 2500)),                 # steady-state alive count (executor SpawnRate = count/lifetime)
                    "size_cm": size_cm, "size_cm_lo": round(s_lo, 1), "size_cm_hi": round(s_hi, 1),
                }
                out.append(entry)
                continue
            if region == "fall_line":                            # waterfall/fall_line: 1-2 LARGE vertical water curtains, the whole group facing the camera with one yaw (not per-card -> doesn't scatter)
                span = min(L["height_m"] + L["thickness_m"], hcap)
                width = max(1.5, 1.3 * rad)                      # curtain width
                fyaw = _fx_face_yaw(bx * 100, by * 100, cam)     # one yaw for the whole group
                cz = bz + span * 0.5
                sheets = [[round(bx * 100, 1), round(by * 100, 1), round(cz * 100, 1),
                           round(width * 100, 1), round(span * 100, 1), round(fyaw, 1)],
                          [round((bx + 0.2) * 100, 1), round((by + 0.2) * 100, 1), round(cz * 100, 1),
                           round(width * 0.8 * 100, 1), round(span * 100, 1), round(fyaw, 1)]]
                entry["sheets"] = sheets; out.append(entry)
            else:                                                # air/sky/ground/over_object: rain/dust/fog puffs/birds/snow, each card faces the camera
                inst = []
                nair = min(n, 120 if region in ("sky",) else 45)  # spray/fog must not densify into a column hiding the subject
                for _ in range(nair):
                    jx = bx + float(rng.uniform(-rad, rad)); jy = by + float(rng.uniform(-rad, rad))
                    gz2 = _terrain_ground(terrain, jx, jy)
                    zc = gz2 + L["height_m"] + float(rng.uniform(0, L["thickness_m"]))
                    yaw = _fx_face_yaw(jx * 100, jy * 100, cam)
                    inst.append([round(jx * 100, 1), round(jy * 100, 1), round(zc * 100, 1),
                                 round(yaw, 1), 0.0, round(float(rng.uniform(smin, smax)), 3)])
                entry["instances"] = inst; total += len(inst); out.append(entry)
        print("[FX] computed %d card instances, %d plane/fog, %d layers" % (
            total, sum(1 for e in out if "plane" in e or "fog" in e), len(out)))
        return out or None
    except Exception as e:
        print("[FX] compute failed: %s" % e, file=sys.stderr)
        return None


def generate_images(client, image: Image.Image, prompts: list[str], output_dir: str, task_id: str,
                    heroes: list[bool] | None = None):
    os.makedirs(output_dir, exist_ok=True)
    results = []
    errors = []
    for idx, prompt in enumerate(prompts, 1):
        is_hero = bool(heroes[idx - 1]) if heroes and idx - 1 < len(heroes) else False
        if ALL_HERO_QUALITY:
            is_hero = True
        tier = " (hero/Pro)" if is_hero else ""
        print(f"\n[Step2] Generating image {idx}/{len(prompts)}{tier}...")
        _update_task(task_id, progress=f"Object {idx}/{len(prompts)}: generating image{tier}…")
        try:
            response = _generate_single_image_with_fallback(client, prompt, image, hero=is_hero)
            # Skip Thinking intermediates (part.thought=True); take the final render (last non-thought image)
            final_img = None
            for part in response.parts:
                if getattr(part, "thought", False):
                    continue
                if part.inline_data is not None:
                    final_img = part.as_image()
            if final_img is not None:
                filename = f"obj{idx}.png"
                out_path = os.path.join(output_dir, filename)
                final_img.save(out_path)
                results.append({
                    "id": idx,
                    "prompt": prompt,
                    "filename": filename,
                    "url": f"/output/{task_id}/{filename}",
                })
                print(f"   ✓ 已保存: {filename}")
            else:
                msg = f"物体 {idx}: 模型未生成图片内容"
                errors.append(msg)
                print(f"   ⚠ {msg}")
        except Exception as e:
            msg = f"物体 {idx}: {type(e).__name__} — {e}"
            errors.append(msg)
            print(f"   ✗ {msg}")
    return results, errors


# ── Tripo3D model generation ────────────────────────────────
def tripo3d_upload_image(image_path: str) -> dict:
    """Upload an image to Tripo3D; returns upload_data (contains image_token)."""
    url = "https://api.tripo3d.ai/v2/openapi/upload"
    headers = {"Authorization": f"Bearer {TRIPO3D_API_KEY}"}
    with open(image_path, 'rb') as f:
        files = {'file': f}
        response = http_requests.post(url, headers=headers, files=files)
    return response.json()


def tripo3d_create_task(file_token: str, image_type: str = 'png', hero: bool = False) -> dict:
    """Create an image_to_model task; hero=True uses the best model, otherwise the fast one."""
    url = "https://api.tripo3d.ai/v2/openapi/task"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TRIPO3D_API_KEY}",
    }
    model = TRIPO_HERO_MODEL if hero else TRIPO_FAST_MODEL
    tex_q = TRIPO_HERO_TEX if hero else TRIPO_FAST_TEX
    data = {
        "type": "image_to_model",
        "model_version": model,
        "file": {
            "type": image_type,
            "file_token": file_token,
        },
        "texture": TRIPO_TEXTURE,
        "pbr": TRIPO_TEXTURE,
    }
    if TRIPO_TEXTURE:
        data["texture_quality"] = tex_q
    if hero and model.startswith("v3"):            # geometry_quality is v3.0+ only
        data["geometry_quality"] = TRIPO_HERO_GEOM
    response = http_requests.post(url, headers=headers, json=data)
    return response.json()


def tripo3d_poll_result(task_id: str, timeout: int = 360) -> dict | None:
    """Poll a Tripo3D task until success or failure. Returns task_data or None."""
    url = f"https://api.tripo3d.ai/v2/openapi/task/{task_id}"
    headers = {"Authorization": f"Bearer {TRIPO3D_API_KEY}"}
    start = time.time()

    while time.time() - start < timeout:
        response = http_requests.get(url, headers=headers)
        res_json = response.json()

        if res_json.get('code') == 0:
            task_data = res_json.get('data', {})
            status = task_data.get('status')
            print(f"   ⏳ Tripo3D 任务 {task_id} 状态: {status}")

            if status == "success":
                return task_data
            elif status == "failed":
                print(f"   ✗ Tripo3D 任务 {task_id} 失败")
                return None
        else:
            print(f"   ⚠ Tripo3D 请求异常: {res_json}")
            return None

        time.sleep(3)

    print(f"   ⏰ Tripo3D 任务 {task_id} 超时 ({timeout}s)")
    return None


# ── Monocular depth estimation (Depth Anything) ───────────────────
_depth_pipe = None


def _get_depth_pipe():
    global _depth_pipe
    if _depth_pipe is None:
        from transformers import pipeline
        # Force CPU: RTX 5090 (sm_120) is newer than current PyTorch supports; GPU inference raises kernel errors
        _depth_pipe = pipeline("depth-estimation", model=DEPTH_MODEL, device=-1)
    return _depth_pipe


def estimate_depths(image, results):
    """Run the depth model on the source photo, sample real depth inside each object bbox, write back to r['depth'] (0 near, 1 far)."""
    try:
        import numpy as np
        out = _get_depth_pipe()(image)
        arr = np.asarray(out["depth"], dtype="float32")
        mn, mx = float(arr.min()), float(arr.max())
        norm = (arr - mn) / (mx - mn + 1e-6)        # DA: 1=near, 0=far
        H, W = arr.shape[:2]
        for r in results:
            b = r.get("bbox") or [0.25, 0.25, 0.5, 0.5]
            x0 = max(0, min(int(b[0] * W), W - 1)); x1 = max(x0 + 1, min(int((b[0] + b[2]) * W), W))
            y0 = max(0, min(int(b[1] * H), H - 1)); y1 = max(y0 + 1, min(int((b[1] + b[3]) * H), H))
            region = norm[y0:y1, x0:x1]
            near = float(np.median(region)) if region.size else 0.5
            r["depth"] = round(1.0 - near, 3)        # convert to 0=near, 1=far
        print(f"[Depth] estimated real depth for {len(results)} objects")
    except Exception as e:
        print(f"[Depth] failed, keep LLM depth: {e}", file=sys.stderr)


def _estimate_size3(client, image, results, image_dir=None):
    """Per-object 3D size [width, depth, height] (m), written to r["size3_m"] -- for mesh proportion correction. The scalar size_m only fixes distance;
    nothing fixes a mesh whose own proportions are wrong (Tripo turned a rug into a 1.3m-tall block).
    ★ Measure the GENERATED images (attached to the call), not the text descriptions: meshes are built from the generated images, and text labels
    drift identity (seen: the pipeline asked for a 'tall mahogany bookcase', Gemini drew a rattan armchair, downstream trusted the label -> the chair
    was squeezed into bookcase proportions = a stretched freak chair). When text and image conflict, trust the image. Real objects keep their absolute
    scale anchored to the photo (size_m); an imagined object's size_m is pure text speculation, unanchored -- report common-sense sizes from the
    generated image content. On failure skip the correction; never block."""
    todo = [r for r in results if not r.get("size3_m")]
    if not todo:
        return
    header = []
    obj_parts = []
    for r in todo:
        header.append("OBJECT %d: %s | %s | extractor size_m=%.2f" % (
            r["id"], "IN the scene photo" if not r.get("imagined") else "imagined (NOT in the photo)",
            str(r.get("prompt", ""))[:90], float(r.get("size_m", 1.0))))
        p = os.path.join(image_dir, "obj%d.png" % r["id"]) if image_dir else None
        if p and os.path.exists(p):
            try:
                im = Image.open(p).convert("RGB")
                im.thumbnail((384, 384))
                obj_parts += ["Render of OBJECT %d:" % r["id"], im]
            except Exception:
                pass
    instruction = (
        "You are an architectural surveyor. Each OBJECT below will become a 3D mesh built EXACTLY from "
        "its attached render image. For EACH object report the real-world 3D bounding size in metres as "
        "[width, depth, height] (width = left-right, depth = front-back, height = vertical, in its natural "
        "standing pose) that this mesh should have when placed in the scene.\n"
        "RULES:\n"
        "- The render image is the ground truth for WHAT the object is and its PROPORTIONS. If the render "
        "contradicts the text description (e.g. text says bookcase but the render shows an armchair), "
        "TRUST THE RENDER and size what you actually see.\n"
        "- Absolute scale: objects marked 'IN the scene photo' - match their size in the scene photo "
        "(first image). Imagined objects - use typical real-world size for what the render shows.\n"
        "- Class sanity: rug/carpet lies flat (height ~0.02), picture frame is thin, armchair ~1.0-1.1 "
        "tall, bookcase tall and shallow.\n"
        "- Consistency: if two objects are clearly the same kind of furniture (e.g. two matching "
        "armchairs, twin lamps), report the SAME size3 for both.\n"
        "Objects:\n" + "\n".join(header) + "\n"
        'Return ONLY raw JSON: {"sizes": [ {"id": <id>, "size3_m": [<w>, <d>, <h>]} ]} - one entry per object.'
    )
    try:
        cfg = genai_types.GenerateContentConfig(
            **{"temperature": 0.2, "seed": 11, "responseMimeType": "application/json"})
        contents = [instruction, "Scene photo:", image] + obj_parts
        try:
            resp = client.models.generate_content(model=TEXT_MODEL, contents=contents, config=cfg)
        except ServerError as e:
            if getattr(e, "code", None) != 503:
                raise
            resp = client.models.generate_content(model=FALLBACK_TEXT_MODEL, contents=contents, config=cfg)
        raw = (resp.text or "").strip()
        try:
            d = json.loads(raw)
        except Exception:
            d = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        got = {}
        for s in (d.get("sizes") or []):
            v = s.get("size3_m")
            if isinstance(v, (list, tuple)) and len(v) >= 3:
                got[int(s.get("id", -1))] = [float(v[0]), float(v[1]), float(v[2])]
        n = 0
        for r in todo:
            s3 = got.get(r["id"])
            if not s3 or min(s3) <= 0:
                continue
            if not r.get("imagined"):               # real objects: absolute scale anchors to the photo-measured size_m (distance solving relies on it)
                sm = max(0.05, float(r.get("size_m", 1.0)))
                mx = max(s3)
                if mx > sm * 2.5 or mx < sm / 2.5:
                    s3 = [v * sm / mx for v in s3]
            r["size3_m"] = [round(max(0.02, min(30.0, v)), 3) for v in s3]
            n += 1
        print(f"[Size3] 3D dims for {n}/{len(todo)} objects (image-grounded)")
    except Exception as e:
        print(f"[Size3] failed (aspect fix skipped): {e}", file=sys.stderr)


# ── Background processing task ───────────────────────────────
def _objects_from_results(results):
    """Rebuild objects from stored results (for reuse-reruns): prompt/bbox/depth etc. are already in results."""
    out = []
    for r in sorted(results or [], key=lambda x: x.get("id", 0)):
        out.append({"prompt": r.get("prompt", ""), "bbox": r.get("bbox"), "depth": r.get("depth", 0.5),
                    "size_m": r.get("size_m", 1.0), "imagined": r.get("imagined", False),
                    "direction": r.get("direction"), "hero": r.get("hero", False),
                    "facing_deg": r.get("facing_deg", 0.0), "man_made": r.get("man_made", False)})
    return out


def process_task(task_id: str, image_path: str, max_objects: int = DEFAULT_OBJECTS, reuse_tid=None):
    """Background thread: generate prompts -> generate images. reuse_tid: re-run REUSING objects + 3D models (skip extraction + Tripo, redo all analysis)."""
    try:
        _update_task(task_id, status="processing", progress="Analysing your photograph…")
        image = Image.open(image_path)

        client = genai.Client(api_key=API_KEY)
        reuse = _load_task_result(reuse_tid) if reuse_tid else None
        if reuse and not (reuse.get("results") and reuse.get("models_3d") is not None):
            reuse = None                                   # incomplete old data -> fall back to a full run

        # Step 1
        if reuse:
            _update_task(task_id, progress="Reusing existing objects & 3D models (re-running all AI analysis)…")
            objects = _objects_from_results(reuse["results"])
            fov_deg = reuse.get("fov_deg", 65.0)
        else:
            _update_task(task_id, progress="Extracting objects from the image…")
            # count = total model budget: off-frame imagination reserves 3*PER_DIR slots, photo extraction gets the rest
            extract_budget = max_objects
            if IMAGINE_OFFSCREEN and OFFSCREEN_PER_DIR > 0:
                extract_budget = max(MIN_OBJECTS, max_objects - 3 * OFFSCREEN_PER_DIR)
            objects, fov_deg = extract_objects(client, image, extract_budget)
        if not objects:
            _update_task(task_id, status="error", error="No objects could be identified in the image.")
            return
        # Solve environment light/atmosphere (drives UE sun/skylight/fog/exposure)
        environment = {}
        if ENABLE_ENV:
            _update_task(task_id, progress="Solving the scene's light & atmosphere…")
            environment = analyze_environment(client, image)
            # Night/dark: when the sun is ~0, give the skylight a floor so UE isn't pitch black
            if environment.get("sun_intensity_lux", 0.0) < 1000.0:
                environment["sky_intensity"] = max(environment.get("sky_intensity", 0.0), 0.5)
        # Solve the camera position (height + pitch -> UE viewport/camera)
        camera = {}
        if ENABLE_CAMERA:
            _update_task(task_id, progress="Solving the camera viewpoint…")
            camera = analyze_camera(client, image, fov_deg)
        # Solve artificial lights (night/dark/indoor: street lamps/signs/windows -> UE point lights)
        lights = []
        if ENABLE_LIGHTS:
            _update_task(task_id, progress="Finding artificial light sources…")
            lights = analyze_lights(client, image)
        # Append the off-frame imagined objects (black-box fabrication of the unseen world)
        if IMAGINE_OFFSCREEN and OFFSCREEN_PER_DIR > 0 and not reuse:
            _update_task(task_id, progress="Imagining what's outside the frame…")
            objects = objects + imagine_offscreen(client, image, OFFSCREEN_PER_DIR)
        prompts = [o["prompt"] for o in objects]
        img_aspect = reuse.get("img_aspect", 1.333) if reuse else ((image.width / image.height) if image.height else 1.333)

        _update_task(task_id, prompts=prompts,
                     progress=f"Found {len(prompts)} objects — generating images…")

        # Step 2 (scene hero uses Nano Banana Pro, the rest flash) -- reuse keeps the old results as-is
        image_output_dir = os.path.join(app.config["OUTPUT_FOLDER"], task_id)
        if reuse:
            results, errors = reuse["results"], []
        else:
            heroes = [bool(o.get("hero")) for o in objects]
            results, errors = generate_images(client, image, prompts, image_output_dir, task_id, heroes=heroes)
            # Merge each object's 2D bbox + depth into results (for UE auto-placement)
            for r in results:
                o = objects[r["id"] - 1] if 0 <= r["id"] - 1 < len(objects) else {}
                r["bbox"] = o.get("bbox", [0.25, 0.25, 0.5, 0.5])
                r["depth"] = o.get("depth", 0.5)
                r["size_m"] = o.get("size_m", 1.0)
                r["imagined"] = o.get("imagined", False)
                r["direction"] = o.get("direction")
                r["hero"] = o.get("hero", False)
                r["facing_deg"] = o.get("facing_deg", 0.0)
                r["man_made"] = o.get("man_made", False)   # passed to _build_layout: man-made objects sit firm; only natural ones sink slightly to root
            # Measure real depth on the source photo with the monocular model, overriding the LLM's rough guess
            if ENABLE_DEPTH:
                _update_task(task_id, progress="Estimating real depth…")
                estimate_depths(image, results)

        if not results:
            detail = "; ".join(errors) if errors else "unknown error"
            _update_task(task_id, status="error", error=f"All image generations failed: {detail}")
            return
        # Per-object 3D size (proportion correction): runs on both fresh and reuse -- old reused results lack size3_m, fill it here
        _update_task(task_id, progress="Surveying object proportions…")
        _estimate_size3(client, image, results, image_output_dir)

        # ── Step 3: generate a 3D model per object (gated by ENABLE_3D; off skips the whole block) ──
        models_3d: list[dict] = []
        model_errors: list[str] = []
        if reuse:
            models_3d = reuse.get("models_3d") or []       # reuse old GLBs, no Tripo run (zero credits)
            _update_task(task_id, progress="Reusing %d existing 3D models…" % len(models_3d))
        elif ENABLE_3D:
            _update_task(task_id, progress="Reconstructing 3D models…")

        for r in (results if (ENABLE_3D and not reuse) else []):
            obj_path = os.path.join(image_output_dir, r["filename"])
            if not os.path.exists(obj_path):
                model_errors.append(f"Object {r['id']}: image file missing")
                continue

            try:
                _update_task(task_id, progress=f"Object {r['id']}/{len(results)}: uploading…")
                # 3a. upload the image
                upload_data = tripo3d_upload_image(obj_path)
                if 'data' not in upload_data or 'image_token' not in upload_data['data']:
                    model_errors.append(f"Object {r['id']}: upload failed — {upload_data}")
                    continue
                file_token = upload_data['data']['image_token']
                image_type = r["filename"].rsplit(".", 1)[-1].lower()

                hero_tier = bool(r.get("hero", False)) or ALL_HERO_QUALITY
                tier = "best" if hero_tier else "fast"
                _update_task(task_id, progress=f"Object {r['id']}/{len(results)}: submitting ({tier})…")
                # 3b. create the conversion task (hero uses the best model, the rest fast; the all-hero switch forces best)
                task_res = tripo3d_create_task(file_token, image_type, hero=hero_tier)
                if 'data' not in task_res or 'task_id' not in task_res['data']:
                    model_errors.append(f"Object {r['id']}: task creation failed — {task_res}")
                    continue
                tripo_task_id = task_res['data']['task_id']
                print(f"   → 物体 {r['id']} Tripo3D 任务: {tripo_task_id}")

                _update_task(task_id, progress=f"Object {r['id']}/{len(results)}: reconstructing in 3D…")
                # 3c. poll until done (best-tier geometry is slower; relaxed timeout)
                final = tripo3d_poll_result(tripo_task_id, timeout=600 if hero_tier else 360)
                if final and final.get("output"):
                    pbr_url = final["output"].get("pbr_model", "")
                    render_url = final["output"].get("rendered_image", "")
                    # Download the GLB locally so Tripo's signed URL can't expire; web page and UE both use the stable local link
                    local_url = pbr_url
                    if pbr_url:
                        try:
                            gg = http_requests.get(pbr_url, timeout=90)
                            with open(os.path.join(image_output_dir, f"model{r['id']}.glb"), "wb") as fh:
                                fh.write(gg.content)
                            local_url = f"/output/{task_id}/model{r['id']}.glb"
                        except Exception as e:
                            print(f"   ⚠ 物体 {r['id']} GLB 下载失败，沿用远程链接: {e}")
                    models_3d.append({
                        "id": r["id"],
                        "pbr_model": local_url,
                        "pbr_remote": pbr_url,
                        "rendered_image": render_url,
                    })
                    print(f"   ✓ 物体 {r['id']} 3D 模型生成成功")
                else:
                    model_errors.append(f"Object {r['id']}: 3D generation failed or no output")
            except Exception as e:
                model_errors.append(f"Object {r['id']}: {type(e).__name__} — {e}")
                print(f"   ✗ Object {r['id']} 3D error: {e}")

        # ── Final state update ──
        status_text = f"Done — {len(results)}/{len(prompts)} objects reconstructed"
        if errors:
            status_text += f", {len(errors)} image failed"
        if models_3d:
            status_text += f", {len(models_3d)} in 3D"
        if model_errors:
            status_text += f", {len(model_errors)} 3D failed"

        # Indoor scenes: skip every outdoor-only step (sky/HDRI/terrain) -- saves a generation and is meaningless indoors
        is_indoor = bool(camera.get("is_indoor"))
        # Out-paint the photo into a 360-degree panorama for UE's HDRI backdrop (indoor has no sky -> skip)
        hdri_url = ""
        if ENABLE_HDRI and not is_indoor:
            _update_task(task_id, progress="Fabricating a 360° panorama (HDRI)…")
            if PREFER_AI_HDRI:                               # thesis stance: the sky too is AI-fabricated from the photo (no Poly Haven stock image)
                hdri_url = generate_hdri(client, image, image_output_dir, task_id)
                if not hdri_url:                             # generation failed -> fall back to a real HDRI
                    _h = fetch_hdri_online(client, image, environment, image_output_dir, task_id)
                    if _h:
                        hdri_url = _h["url"]; environment["hdri_light_frac"] = _h["light_frac"]
            else:
                _h = fetch_hdri_online(client, image, environment, image_output_dir, task_id)  # real HDRI preferred (AI picks visually)
                if _h:
                    hdri_url = _h["url"]; environment["hdri_light_frac"] = _h["light_frac"]   # light's horizontal position -> sky-sphere rotation
                else:
                    hdri_url = generate_hdri(client, image, image_output_dir, task_id)
        # Terrain: back-project depth into a top-down height grid. Needs a reliable camera (solved) -- unsolved/indoor/near-flat -> None, UE uses flat ground.
        practicals = infer_night_practicals(client, results, environment)   # night practical lights (results are ready by here)
        midground = infer_midground(client, results, environment)           # midground silhouette selection
        light_map = match_lights_to_fixtures(client, image, lights, results)  # light placement (AI decides fixture/lumens/glow)
        wall_snap = infer_wall_snap(client, image, results) if is_indoor else []  # wall-snap semantics (breaks the box feel)
        _update_task(task_id, progress="Composing the soundtrack…")
        music_url = generate_music(client, environment, image_output_dir, task_id)  # Lyria generative score
        room = None
        if is_indoor:                                        # indoor room shell P0 (docs/INDOOR_SHELL.md)
            _update_task(task_id, progress="Solving the room box…")
            room = analyze_room(client, image)
            if room:
                room["tex"] = generate_room_textures(client, image, room, image_output_dir, task_id)
                wv = generate_window_view(client, image, room, image_output_dir, task_id)
                if wv:
                    room["window_view"] = wv
        room_arrange = {}                                    # note: the real assignment happens after layout is built (below) --
        # infer_room_arrange needs layout, which doesn't exist yet here; calling early = UnboundLocalError kills the whole task (health-check H2 fix)
        sound_sources = infer_sound_sources(client, results, room, environment,
                                            image_output_dir, task_id)   # soundscape B: semantic point sources
        ambience_url = generate_ambience(client, environment, is_indoor, image_output_dir, task_id,
                                         window_owned=any(s.get("id") == "window"
                                                          for s in sound_sources))  # ambience bed (after semantic dedup)
        terrain = None
        fxplan = None
        if ENABLE_TERRAIN and ENABLE_CAMERA and camera.get("solved") and not is_indoor:
            _update_task(task_id, progress="Reconstructing the terrain from depth…")
            # FX plan moved ahead of terrain (it only needs photo/env/objects): the water-body verdict must join the 'flatten or not' decision --
            # lake-basin carving lives in the terrain path; a lake scene demoted by 'low relief -> flat' shrinks the lake to a puddle (generic test, not a per-photo patch)
            if ENABLE_FX:
                try:
                    fxplan = analyze_effects(client, image, environment, results, indoor=is_indoor)
                except Exception as e:
                    print(f"[FX] early plan failed: {e}", file=sys.stderr); fxplan = None
            has_water = any(str(L.get("primitive", "")).lower() == "water"
                            for L in ((fxplan or {}).get("layers") or []))
            terrain = reconstruct_terrain(image, results, camera, fov_deg, img_aspect,
                                          force_water=has_water)
            if terrain:                                          # ground material: decorrelated seamless tiles from one source; UE blends them with noise to break repetition
                _update_task(task_id, progress="Matching the ground texture from the photo…")
                try:
                    terrain["albedo_urls"] = generate_terrain_textures(client, image, image_output_dir, task_id)
                    terrain["albedo_url"] = terrain["albedo_urls"][0] if terrain["albedo_urls"] else ""
                except Exception as e:
                    print(f"[Terrain] texture gen failed: {e}", file=sys.stderr)
                    terrain["albedo_urls"] = []; terrain["albedo_url"] = ""

        original_url = f"/uploads/{os.path.basename(image_path)}"
        scene_scale = _scene_scale(results, fov_deg, img_aspect)                  # scene scale (m)
        layout = _build_layout(results, models_3d, fov_deg, img_aspect, camera, scene_scale, terrain)  # with terrain, objects snap to terrain height
        _recenter_layout(layout, None, terrain)              # rigid-shift the cluster to the platform centre (dressing/FX anchors share the same datum)

        if is_indoor and room:
            # AI indoor arranger (design decision: inferred furniture is kept, not deleted -- expand the room + re-seat along walls; photo-visible objects pinned).
            # Must run after layout is built (infer_room_arrange reads layout); calling early = UnboundLocalError (H2)
            hero_id = next((o.get("id") for o in layout if o.get("hero")), None)
            room_arrange = infer_room_arrange(client, image, room, results, layout, hero_id=hero_id)
            if room_arrange:
                room["size_m"][0] = room_arrange.get("room_w_m", room["size_m"][0])
                room["size_m"][1] = room_arrange.get("room_d_m", room["size_m"][1])
                # Never accept AI coordinates (vetoed live as 'random placement'; iron-rule split): moves only takes the 'who may move' semantics;
                # positions come deterministically from the UE-side wall packer _arrange_furniture_walls + _reseat_floaters

        # Ground dressing (after layout: Gemini decides what to scatter; object footprints exclude spots under houses/bridges)
        if terrain:
            _update_task(task_id, progress="Planning ground dressing…")
            try:
                dplan = analyze_ground_dressing(client, image, environment)
                generate_plant_textures(client, image, dplan, image_output_dir, task_id)
                terrain["dressing"] = compute_dressing(dplan, terrain, exclude=_layout_footprints(layout))
            except Exception as e:
                print(f"[Dressing] failed: {e}", file=sys.stderr)
                terrain["dressing"] = None

        # Particle/fluid FX (after layout, may bind to objects): Gemini decides what to add (water/waterfall/rain/fog/dust/birds); the executor computes world coordinates; UE instances statically
        city_block = []
        night_windows = None
        if not is_indoor and terrain:
            cor = _street_corridor_m(results, layout)
            if cor:                                      # city photo (>=2 buildings in a row) -> AI plans the whole block (S1 pipelined)
                fwd_r = (terrain["cx_m"] - terrain["half_fwd_m"] + 8.0,
                         terrain["cx_m"] + terrain["half_fwd_m"] - 5.0)
                lat_r = (terrain["cy_m"] - terrain["half_lat_m"] + 5.0,
                         terrain["cy_m"] + terrain["half_lat_m"] - 5.0)
                try:
                    city_block = infer_city_block(client, image, results, layout,
                                                  corridor_lat=cor, fwd_range=fwd_r, lat_range=lat_r)
                    if city_block:
                        city_block["corridor"] = list(cor)
                except Exception as e:
                    print(f"[CityBlock] pipeline: {e}", file=sys.stderr)
        if (not is_indoor and "night" in str(environment.get("time_of_day", "")).lower()
                and any("building" in str(r.get("prompt", ""))[:60].lower() for r in results)):
            night_windows = infer_night_windows(client, image, environment)   # R2 night window lights
        effects = None
        if ENABLE_FX and ENABLE_CAMERA and camera.get("solved"):
            _update_task(task_id, progress="Planning atmospheric effects…")
            try:
                if fxplan is None:      # reuse the plan already computed in the terrain step (saves a call and keeps both decisions consistent)
                    fxplan = analyze_effects(client, image, environment, results, indoor=is_indoor)
                generate_fx_textures(client, image, fxplan, image_output_dir, task_id)
                rb = _room_bounds_cm(room, layout) if (is_indoor and room) else None
                effects = compute_effects(fxplan, terrain, layout, camera, fov_deg, img_aspect, scene_scale,
                                          room_bounds=rb, room=room)
            except Exception as e:
                print(f"[FX] failed: {e}", file=sys.stderr)
                effects = None

        _update_task(
            task_id,
            status="done",
            progress=status_text,
            results=results,
            errors=errors if errors else None,
            models_3d=models_3d,
            model_errors=model_errors if model_errors else None,
            original_url=original_url,
            layout=layout,
            environment=environment,
            camera=camera,
            lights=lights,                # store raw lights (with u/v); routes recompute live via _build_lights, so the in-memory copy can't pile lights at the centre
            scene_scale=scene_scale,
            is_indoor=is_indoor,
            hdri=hdri_url,
            terrain=terrain,
            effects=effects,
            fov_deg=fov_deg,              # in-memory tasks carry this too (matching result.json): otherwise /latest and /layout fall back to the default 65°/1.333 and compute a wrong layout
            img_aspect=img_aspect,
        )
        # Persist to disk so /canvas, /layout, /latest can still read it after a reload
        _save_task_result(task_id, {
            "prompts": prompts, "results": results, "models_3d": models_3d,
            "original_url": original_url, "layout": layout,
            "fov_deg": fov_deg, "img_aspect": img_aspect,
            "environment": environment, "camera": camera, "lights": lights,
            "scene_scale": scene_scale, "is_indoor": is_indoor, "hdri": hdri_url,
            "terrain": terrain, "effects": effects, "practicals": practicals,
            "midground": midground, "room": room, "music": music_url,
            "ambience": ambience_url, "sound_sources": sound_sources,
            "light_map": light_map, "wall_snap": wall_snap, "city_block": city_block,
            "night_windows": night_windows, "room_arrange": room_arrange,
        })

    except Exception as e:
        print(f"[Task {task_id}] Unhandled error: {e}", file=sys.stderr)
        _update_task(task_id, status="error", error=str(e))


# ── Visual-critique loop v1: Gemini reviews the render -> bounded adjustment commands (AI decides, code clamps and executes) ──
# After ue_scene_builder builds the scene and posts ue_view.png back, it pulls /adjustments/<tid>, applies them, re-renders and re-reviews (max 2 rounds).
# Gemini is the only runtime link; every value is clamped by the bounds below -> the AI can only nudge, never break the deterministic layout.
CRITIQUE_MAX_MOVE_M = 10.0      # max per-object move per round
CRITIQUE_SCALE_RANGE = (0.8, 1.25)
CRITIQUE_MAX_EV = 1.0
_critique_cache = {}


def critique_scene(client, task_id, rnd):
    """Original photo + current render + layout table -> Gemini flags OBVIOUS problems (floating/intersections/gross misplacement/exposure failure),
    emitting bounded JSON adjustment commands. Default verdict=ok (leave alone when unsure); every field is clamped before returning."""
    out = {"verdict": "ok", "notes": "", "objects": [], "exposure_ev_delta": 0.0}
    data = _load_task_result(task_id)
    render_p = os.path.join(OUTPUT_FOLDER, task_id, "ue_view.png")
    if not data or not os.path.exists(render_p):
        return out
    try:
        photo = Image.open(os.path.join(UPLOAD_FOLDER, os.path.basename(data.get("original_url", "")))).convert("RGB")
        render = Image.open(render_p).convert("RGB")
    except Exception:
        return out
    rows = []
    for o in (data.get("layout") or [])[:12]:
        L = o.get("location") or [0, 0, 0]
        rows.append("id=%d fwd=%.1fm lat=%.1fm size=%.1fm %s" % (
            o.get("id", -1), L[0] / 100.0, L[1] / 100.0,
            (o.get("scale") or [100])[0] / 100.0, str(o.get("prompt", ""))[:60]))
    night = "night" in str((data.get("environment") or {}).get("time_of_day", "")).lower()
    indoor = bool(data.get("is_indoor")) and bool(data.get("room"))
    lights_rows = ""
    lights_schema = ""
    if indoor:
        lmap = {int(m.get("id", 0)): m for m in (data.get("light_map") or [])}
        lr = ["id=%d lumens=%.0f fixture_obj=%s%s" % (
            int(l.get("id", 0)), float(lmap.get(int(l.get("id", 0)), {}).get("lumens", l.get("intensity_lm", 0) or 0)),
            lmap.get(int(l.get("id", 0)), {}).get("fixture_object_id", -1),
            " (key)" if i == 0 else "") for i, l in enumerate(data.get("lights") or [])]
        if lr:
            lights_rows = "\nPlaced interior lights:\n" + "\n".join(lr)
        lights_schema = (
            '  "lights": [ { "id": <light id>, "lumens_mult": <0.25..4> } ],\n'
            "             // per-light brightness fix vs the photo: a lamp pool clearly stronger than the\n"
            "             // photo -> <1; a region the photo shows lit but the render leaves dark -> >1\n"
            '  "window_mult": <0.25..4>,   // light through the window vs the photo\n'
            '  "remove_object_ids": [<ids>],   // objects visible in the RENDER that do NOT exist anywhere\n'
            "             // in the photo (extraction hallucinations crowding the room); be strict —\n"
            "             // only list ids you are CERTAIN the photo does not contain\n")
    fills_schema = (
        '  "fills": [ { "id": <object id>, "side": "front|back|left|right",   // side AS SEEN IN THE RENDER\n'
        '              "strength": <0.2..1> } ],   // prescribe a fill light ONLY where an object face is\n'
        "                                          // DEAD BLACK (zero readable detail); night look must stay dark\n"
        '  "ambient_delta": <-0.5..1.5>,   // nudge the night ambient (sky fill): positive lifts ALL dark areas\n'
        "                                  // gently; use when shadows overall lack readable detail\n"
    ) if night else ""
    fills_rule = (
        " At most 3 fills, only for truly unreadable black faces — do NOT brighten the whole scene (the night "
        "exposure itself is calibrated and off-limits)." if night else "")
    outdoor_schema = ("" if indoor else (
        '  "fog_mult": <0.3..3>,   // atmospheric haze vs the photo: render too hazy -> <1, photo clearly\n'
        "                          // hazier/rainier than render -> >1; 1 = leave alone\n"
        '  "sun_yaw_delta_deg": <-45..45>,   // ONLY if shadows in the render clearly point a different\n'
        "                          // direction than in the photo; 0 = leave alone\n"))
    instruction = (
        "You are reviewing a 3D scene RECONSTRUCTION. Image 1 = the ORIGINAL photo (ground truth). "
        + ("Image 2 = the CURRENT engine render FROM THE SAME VIEWPOINT as the photo — a direct A/B; "
           "your MAIN job indoors is the LIGHTING: per-light strength, window light, overall exposure. "
           if indoor else
           "Image 2 = the CURRENT engine render of the rebuilt scene (3/4 aerial view, not the photo's viewpoint). ")
        + "Object table (world frame: fwd = metres away from the photo camera, lat = metres to its right):\n"
        + "\n".join(rows) + lights_rows +
        "\nReview round %d. ONLY flag CLEAR, OBVIOUS problems you can see in the render: an object floating "
        "in the air or buried, two objects interpenetrating, an object in a clearly implausible spot vs the "
        "photo, or the whole render badly over/under-exposed. Exposure check: if LARGE areas are washed-out "
        "near-pure WHITE (blown highlights on ground/objects, not just sky) the render is over-exposed -> "
        "negative exposure_ev_delta; if detail is crushed into near-black it is under-exposed -> positive. "
        "Aesthetic nitpicks are NOT problems. If unsure, do NOT touch it.\n"
        "Return ONLY raw JSON:\n"
        "{\n"
        '  "verdict": "ok|adjust",   // ok = nothing clearly wrong (the default)\n'
        '  "notes": "<one short sentence on what you changed and why, or why ok>",\n'
        '  "objects": [ { "id": <id from table>, "move_fwd_m": <-10..10>, "move_lat_m": <-10..10>,\n'
        '                 "rotate_yaw_deg": <-180..180>, "scale_mul": <0.8..1.25> } ],\n'
        + lights_schema + fills_schema + outdoor_schema +
        '  "exposure_ev_delta": <-1..1>\n'
        "}\n"
        "Include at most 4 objects, only the clearly-wrong ones, with the SMALLEST fix that helps."
        + fills_rule
    ) % rnd
    try:
        cfg = genai_types.GenerateContentConfig(temperature=0.2, seed=29 + rnd,
                                                responseMimeType="application/json")
        try:
            resp = client.models.generate_content(model=TEXT_MODEL, contents=[photo, render, instruction], config=cfg)
        except ServerError as e:
            if getattr(e, "code", None) != 503:
                raise
            resp = client.models.generate_content(model=FALLBACK_TEXT_MODEL,
                                                  contents=[photo, render, instruction], config=cfg)
        d = json.loads((resp.text or "").strip())
    except Exception as e:
        print(f"[Critique] {task_id} r{rnd} failed: {e}", file=sys.stderr)
        return out
    valid_ids = {o.get("id") for o in (data.get("layout") or [])}
    objs = []
    for o in (d.get("objects") or [])[:4]:
        try:
            oid = int(o.get("id"))
        except Exception:
            continue
        if oid not in valid_ids:
            continue
        objs.append({
            "id": oid,
            "move_fwd_m": _clampf(o.get("move_fwd_m"), -CRITIQUE_MAX_MOVE_M, CRITIQUE_MAX_MOVE_M, 0.0),
            "move_lat_m": _clampf(o.get("move_lat_m"), -CRITIQUE_MAX_MOVE_M, CRITIQUE_MAX_MOVE_M, 0.0),
            "rotate_yaw_deg": _clampf(o.get("rotate_yaw_deg"), -180.0, 180.0, 0.0),
            "scale_mul": _clampf(o.get("scale_mul"), CRITIQUE_SCALE_RANGE[0], CRITIQUE_SCALE_RANGE[1], 1.0),
        })
    out["verdict"] = "adjust" if str(d.get("verdict", "ok")).lower() == "adjust" else "ok"
    out["notes"] = str(d.get("notes", ""))[:200]
    out["objects"] = objs
    out["exposure_ev_delta"] = _clampf(d.get("exposure_ev_delta"), -CRITIQUE_MAX_EV, CRITIQUE_MAX_EV, 0.0)
    fills = []
    for f in (d.get("fills") or [])[:3]:                  # AI fill-light prescriptions (night): which face of which object is dead black -> add a small shadowless fill light
        try:
            fid = int(f.get("id"))
            side = str(f.get("side", "front")).lower()
            if fid in valid_ids and side in ("front", "back", "left", "right"):
                fills.append({"id": fid, "side": side,
                              "strength": _clampf(f.get("strength"), 0.2, 1.0, 0.5)})
        except Exception:
            continue
    out["fills"] = fills
    out["ambient_delta"] = _clampf(d.get("ambient_delta"), -0.5, 1.5, 0.0)
    lmults = []
    if indoor:
        valid_lids = {int(l.get("id", 0)) for l in (data.get("lights") or [])}
        for f in (d.get("lights") or [])[:8]:
            try:
                lid = int(f.get("id"))
            except Exception:
                continue
            mu = _clampf(f.get("lumens_mult"), 0.25, 4.0, 1.0)
            if lid in valid_lids and abs(mu - 1.0) > 0.05:
                lmults.append({"id": lid, "lumens_mult": round(mu, 2)})
        out["window_mult"] = _clampf(d.get("window_mult"), 0.25, 4.0, 1.0)
        rm = []
        hero = next((o.get("id") for o in (data.get("layout") or []) if o.get("hero")), None)
        for x in (d.get("remove_object_ids") or [])[:4]:
            try:
                xid = int(x)
            except Exception:
                continue
            if xid in valid_ids and xid != hero:
                rm.append(xid)
        out["remove_object_ids"] = rm
        if lmults and data.get("light_map"):
            # Prescription persisted: corrected lumens written back to light_map (the single source of truth); the engine applies the same multiplier -> both sides agree
            mp = {m["id"]: m["lumens_mult"] for m in lmults}
            for m in data["light_map"]:
                mu = mp.get(int(m.get("id", 0)))
                if mu:
                    m["lumens"] = max(20.0, min(4000.0, float(m.get("lumens", 500.0)) * mu))
            try:
                rj = os.path.join(OUTPUT_FOLDER, task_id, "result.json")
                with open(rj, "w", encoding="utf-8") as f_:
                    json.dump(data, f_, ensure_ascii=False)
            except Exception as e:
                print(f"[Critique] light_map persist failed: {e}", file=sys.stderr)
    out["light_mults"] = lmults
    if not indoor:
        out["fog_mult"] = _clampf(d.get("fog_mult"), 0.3, 3.0, 1.0)
        out["sun_yaw_delta_deg"] = _clampf(d.get("sun_yaw_delta_deg"), -45.0, 45.0, 0.0)
    print("[Critique] %s r%d: %s — %d obj, ev%+.2f, %d fills, amb%+.2f, %d lights, win×%.2f, "
          "fog×%.2f, sunΔ%+.0f — %s" % (
              task_id, rnd, out["verdict"], len(objs), out["exposure_ev_delta"], len(fills),
              out["ambient_delta"], len(lmults), out.get("window_mult", 1.0),
              out.get("fog_mult", 1.0), out.get("sun_yaw_delta_deg", 0.0), out["notes"]))
    return out


# ── Flask routes ─────────────────────────────────────────────
@app.route("/")
def index():
    return render_template(
        "index.html",
        min_objects=MIN_OBJECTS, max_objects=MAX_OBJECTS, default_objects=DEFAULT_OBJECTS,
    )


# Preview placeholder data (shared by /preview and /canvas, zero API)
SAMPLE_GLB = "https://modelviewer.dev/shared-assets/models/Astronaut.glb"
SAMPLE_PROMPTS = [
    "a grand, multi-tiered Chinese pagoda-style building, complete and centered, "
    "with red walls and intricate green-and-gold eaves, on a pure black background",
    "a classical Chinese white stone arched bridge with multiple distinct arches and "
    "an ornate balustrade, isolated and complete on a pure black background",
    "an ornate wooden multi-tiered tower structure, dark beams and red panels, "
    "fully visible and centered on a pure black background",
]


def _save_task_result(task_id, data):
    """Write the task result to output/<id>/result.json (still readable after a reload)."""
    try:
        with open(os.path.join(OUTPUT_FOLDER, task_id, "result.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"[{task_id}] save result.json failed: {e}", file=sys.stderr)


def _load_task_result(task_id):
    """Load a task result from memory or output/<id>/result.json; None if neither exists."""
    # Disk first: result.json is the authoritative full set (room/music/light_map and other late fields present);
    # the in-memory snapshot lacks the late fields (seen: rainy-night indoor built with no room shell / missing audio actors, same root cause)
    path = os.path.join(OUTPUT_FOLDER, task_id, "result.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    if task_id in tasks and tasks[task_id].get("results"):
        return tasks[task_id]
    return None


def _find_local_sample():
    """Pick the most recent output/ task containing objN.png; returns (tid, obj_files, source_url)."""
    if not os.path.isdir(OUTPUT_FOLDER):
        return None, [], None
    for d in sorted(os.listdir(OUTPUT_FOLDER), reverse=True):
        dpath = os.path.join(OUTPUT_FOLDER, d)
        if not os.path.isdir(dpath):
            continue
        objs = sorted(f for f in os.listdir(dpath)
                      if f.startswith("obj") and f.endswith(".png"))
        if not objs:
            continue
        source_url = None
        if os.path.isdir(UPLOAD_FOLDER):
            for f in sorted(os.listdir(UPLOAD_FOLDER)):
                if f.startswith(d):
                    source_url = f"/uploads/{f}"
                    break
        return d, objs, source_url
    return None, [], None


# ── UE auto-placement: perspective-projection positioning ──
# Solve each object's metric camera distance from its real size (VLM), then pinhole-project via the camera FOV into world coordinates (cm).
SIZE_MIN, SIZE_MAX = 0.2, 300.0     # real-size clamp (m): generous ceiling to fit towers/megastructures; assumes no scene type
Z_MIN, Z_MAX = 0.5, 5000.0          # distance clamp (m): up to ~5km so aerial/vista shots aren't squashed
SCALE_MULT = 100.0                   # metres -> centimetres (UE)
ELEVATE_SIZE_MAX = 3.0              # only objects <3m may float via back-projection (signs/wall lamps/hangings); large structures always ground (stops towers floating)
ELEVATE_CAP = 12.0                  # hard cap on float height (m)
DEPTH_FUSE_W = 0.6                   # fusion weight: how strongly Depth Anything's relative depth reorders distances (0=pure size, 1=pure DA order)
ENABLE_DEPTH_COMPRESS = False       # off: no post-hoc far compression (layout sanity is now guaranteed by prompt theory; pure pinhole projection)
COMPRESS_KNEE_FRAC = 0.3            # compression knee = this fraction x the 75th-percentile distance; smaller pulls harder


# Default facing calibration for imported models: the world yaw (deg) a Tripo model's front faces after UE import.
# World-yaw convention: 0=+X (forward/away from camera), 90=+Y (right), 180=-X (towards camera), -90=-Y (left).
# If imported objects all face the wrong way, changing this one number (+-90/180) recalibrates everything at once.
MODEL_FRONT_YAW = 0.0


def _face_yaw(facing_deg: float) -> float:
    """Real objects: AI-estimated facing_deg -> the object's world yaw.
    facing_deg=0 -> front towards the camera (yaw 180); +90 -> front to the right (yaw 90); -90 -> to the left (yaw 270)."""
    return (180.0 - float(facing_deg)) + MODEL_FRONT_YAW


def _face_origin_yaw(X: float, Y: float) -> float:
    """Imagined objects: front faces the scene centre (camera/origin)."""
    return math.degrees(math.atan2(-Y, -X)) + MODEL_FRONT_YAW


def _compress_far(z: float, knee: float) -> float:
    """Far compression: below the knee unchanged; beyond the knee pulled in logarithmically (order-preserving, monotonic)."""
    return z if (knee <= 0 or z <= knee) else knee + knee * math.log1p((z - knee) / knee)


def _obj_distance(bbox, size_m, th, tv) -> float:
    """Solve the metric camera distance from real size: match size_m (largest real dimension) against the apparently longer image side --
    wide objects use width, tall ones height, so wide-flat things (bridges/walls/rows of houses) aren't over-distanced. Scene-agnostic."""
    bx, by, bw, bh = bbox
    ang = max(0.01, bw) * (2.0 * th) if bw >= bh else max(0.01, bh) * (2.0 * tv)
    z = size_m / (2.0 * math.tan(ang / 2.0)) if ang > 0 else 50.0
    return max(Z_MIN, min(Z_MAX, z))


def _world_height(v: float, dist: float, cam: dict, vfov_deg: float) -> float:
    """Camera back-projection: image v (0 top..1 bottom) + horizontal distance dist (m) -> world height (m) of that pixel ray at that distance.
    Uses the solved camera height + pitch, replacing every per-class hardcoded 'mounting height' range."""
    h_cam = _clampf(cam.get("height_m", 1.6), 0.1, 200.0, 1.6)
    pitch = _clampf(cam.get("pitch_deg", 0.0), -89.0, 89.0, 0.0)
    elev = math.radians(pitch - (v - 0.5) * vfov_deg)   # pixel ray's elevation from horizontal
    return h_cam + dist * math.tan(elev)


def _object_distances(results, fov_deg=65.0, img_aspect=1.333) -> dict:
    """Line-of-sight distance (m) from each real object to the camera. Size inversion gives the absolute scale; Depth Anything's relative depth
    corrects the ordering (the size method is noisy, DA gives reliable relative near/far). Returns {id: distance}."""
    hfov = math.radians(max(25.0, min(100.0, fov_deg)))
    th = math.tan(hfov / 2.0)
    tv = th / max(0.1, img_aspect)
    reals = [r for r in results if not r.get("imagined")]
    size_z = {}
    for r in reals:
        size_m = max(SIZE_MIN, min(SIZE_MAX, float(r.get("size_m", 1.0))))
        size_z[r["id"]] = _obj_distance(r.get("bbox") or [0.25, 0.25, 0.5, 0.5], size_m, th, tv)
    das = [r.get("depth") for r in reals]
    # When DA is usable (all values present, with spread): re-rank the size-derived metric distances by DA's near->far order, then blend
    if len(reals) >= 2 and all(isinstance(x, (int, float)) for x in das) and (max(das) - min(das) > 1e-3):
        slots = sorted(size_z.values())                                   # existing metric-distance 'slots'
        order = sorted(reals, key=lambda r: float(r.get("depth", 0.5)))    # DA: near -> far
        out = {}
        for rank, r in enumerate(order):
            out[r["id"]] = (1.0 - DEPTH_FUSE_W) * size_z[r["id"]] + DEPTH_FUSE_W * slots[rank]
    else:
        out = size_z
    # Far compression: pull distant objects in log-scale along the ray to tighten sprawling scenes (near untouched, order preserved)
    if ENABLE_DEPTH_COMPRESS and out:
        vals = sorted(out.values())
        knee = max(2.0, COMPRESS_KNEE_FRAC * vals[int(0.75 * (len(vals) - 1))])
        out = {k: _compress_far(v, knee) for k, v in out.items()}
    return out


def _scene_scale(results, fov_deg=65.0, img_aspect=1.333) -> float:
    """Scene scale S (m): the 75th percentile of (fused) real-object camera distances. Every relative quantity (imagined-object distance, spacing,
    ground/sky sizes) is in units of S -> a room (~3m) and a street (~50m) auto-scale; no absolute distance is hardcoded."""
    zs = sorted(_object_distances(results, fov_deg, img_aspect).values())
    if not zs:
        return 10.0
    return max(2.0, min(2000.0, zs[int(0.75 * (len(zs) - 1))]))


def _terrain_ground(terrain, x_m, y_m):
    """Terrain surface height (m) under world (x_m, y_m); 0 without terrain. Query points are clamped into the footprint (nearest-edge height),
    so objects lying outside the terrain patch stick to its edge instead of dropping back to the z=0 plane."""
    if not terrain or "cx_m" not in terrain:                              # legacy/incomplete terrain -> treat as flat, don't crash
        return 0.0
    cx = float(terrain.get("cx_m", 0.0)); cy = float(terrain.get("cy_m", 0.0))
    hf = float(terrain.get("half_fwd_m", 0.0)); hl = float(terrain.get("half_lat_m", 0.0))
    if hf <= 0.0 or hl <= 0.0:
        return 0.0
    xq = min(max(x_m, cx - hf), cx + hf)
    yq = min(max(y_m, cy - hl), cy + hl)
    s = _terrain_sample(terrain, xq, yq)
    return s[0] if s else 0.0


def _clamp_to_terrain(terrain, x_m, y_m, r_m=0.0):
    """Clamp world (x, y) into the terrain footprint (keeping object radius r_m + 0.5m margin) so objects neither leave the terrain nor hang half off it."""
    if not terrain or "cx_m" not in terrain:
        return x_m, y_m
    cx = float(terrain.get("cx_m", 0.0)); cy = float(terrain.get("cy_m", 0.0))
    hf = float(terrain.get("half_fwd_m", 0.0)); hl = float(terrain.get("half_lat_m", 0.0))
    if hf <= 0.0 or hl <= 0.0:
        return x_m, y_m
    mx = max(0.0, hf - r_m - 0.5); my = max(0.0, hl - r_m - 0.5)
    return (min(max(x_m, cx - mx), cx + mx), min(max(y_m, cy - my), cy + my))


def _terrain_ground_max(terrain, x_m, y_m, r_m):
    """Highest terrain point (m) within the object's footprint: the object sits on its highest contact point -> never sinks into the ground (man-made things like bicycles aren't half-buried)."""
    g = _terrain_ground(terrain, x_m, y_m)
    if not terrain or r_m <= 0:
        return g
    for dx, dy in ((r_m, 0), (-r_m, 0), (0, r_m), (0, -r_m),
                   (0.7 * r_m, 0.7 * r_m), (-0.7 * r_m, 0.7 * r_m), (0.7 * r_m, -0.7 * r_m), (-0.7 * r_m, -0.7 * r_m)):
        g = max(g, _terrain_ground(terrain, x_m + dx, y_m + dy))
    return g


def _layout_footprints(layout):
    """Extract placed-object footprints (x_m, y_m, r_m) from the layout, so dressing avoids the ground under them (r = half size, slightly padded)."""
    fp = []
    for it in (layout or []):
        loc = it.get("location") or [0.0, 0.0, 0.0]
        fp.append((loc[0] / SCALE_MULT, loc[1] / SCALE_MULT, max(0.3, float(it.get("size_m", 1.0)) * 0.6)))
    return fp


def _build_layout(results, models_3d, fov_deg=65.0, img_aspect=1.333, camera=None, scene_scale=None, terrain=None):
    """Uniform placement (every scene treated alike, no per-class rules): size -> metric distance -> pinhole projection (lateral);
    height via camera back-projection; imagined-object distances and anti-overlap spacing scale with scene scale S; the whole group is oriented
    along the dominant vanishing direction depth_axis; facing: real objects use facing_deg, imagined ones face the scene centre."""
    glb_by_id = {m["id"]: m.get("pbr_model", "") for m in (models_3d or [])}
    hfov = math.radians(max(25.0, min(100.0, fov_deg)))
    th = math.tan(hfov / 2.0)                                   # horizontal half-angle
    tv = th / max(0.1, img_aspect)                             # vertical half-angle = horizontal / aspect
    vfov_deg = math.degrees(2.0 * math.atan(tv))              # vertical FOV (deg)

    cam = camera or {}
    axis = math.radians(_clampf(cam.get("depth_axis_deg", 0.0), -80.0, 80.0, 0.0))   # dominant vanishing direction of the scene
    if terrain:                          # with terrain: objects use the raw camera frame (same frame as the terrain), no depth_axis rotation -> only then do terrain heights land correctly
        axis = 0.0
    ca, sa = math.cos(axis), math.sin(axis)
    axis_deg = math.degrees(axis)
    scale_corr = _clampf(cam.get("scale_correction", 1.0), 0.5, 2.0, 1.0)   # global scale calibration from the reference object
    S = scene_scale if scene_scale is not None else _scene_scale(results, fov_deg, img_aspect)
    floor_tol = max(0.5, 0.03 * S)                            # grounding tolerance: bases lower than this count as touching ground
                                                              # (absorbs camera-estimate noise; big street scenes still won't suck tall signs down)
    sep_cm = max(0.5, 0.12 * S) * SCALE_MULT                  # anti-overlap spacing (scales with the scene)

    pitch_deg = _clampf(cam.get("pitch_deg", 0.0), -89.0, 89.0, 0.0)
    dz = _object_distances(results, fov_deg, img_aspect)         # line-of-sight distances fused from size + DA
    items = []
    min_fwd = min_lat = 1e9
    max_fwd = max_lat = -1e9

    # -- Place the real objects first, recording the actual envelope (imagined objects reference it) --
    for r in results:
        if r.get("imagined"):
            continue
        size_m = max(SIZE_MIN, min(SIZE_MAX, float(r.get("size_m", 1.0)) * scale_corr))
        bbox = r.get("bbox") or [0.25, 0.25, 0.5, 0.5]
        bx, by, bw, bh = bbox
        cu, cv = bx + bw / 2.0, by + bh / 2.0
        z = dz.get(r["id"])
        if z is None:
            z = _obj_distance(bbox, size_m, th, tv)
        # Spherical correction: line-of-sight z -> horizontal distance (accurate at high pitch too); lateral/height both use the horizontal distance
        e_c = math.radians(pitch_deg - (cv - 0.5) * vfov_deg)
        horiz = max(0.3, z * math.cos(e_c))
        x_right = (cu - 0.5) * 2.0 * th * horiz                # lateral (m)
        # Height: only small objects (signs/wall lamps/hangings) may float via back-projection, capped; large structures always ground (stops towers floating from up-shots/occlusion)
        if size_m < ELEVATE_SIZE_MAX:
            base_h = _world_height(by + bh, horiz, cam, vfov_deg)
            base_h = 0.0 if base_h < floor_tol else min(ELEVATE_CAP, max(0.0, base_h))
        else:
            base_h = 0.0
        # Orient the whole group along the scene's dominant vanishing direction (axis=0 = pure perspective, no special case); facing rotates with it
        fwd, lat = horiz * ca - x_right * sa, horiz * sa + x_right * ca
        if terrain:                                            # clamp objects into the terrain footprint; never hanging off the terrain / half out of bounds
            fwd, lat = _clamp_to_terrain(terrain, fwd, lat, size_m * 0.5)
        if terrain:                                            # always ground on the footprint's highest contact point -> nothing is ever half-buried; natural objects then sink 0.3m to root, man-made sit firm
            gz = _terrain_ground_max(terrain, fwd, lat, size_m * 0.5)
            if not r.get("man_made"):
                gz -= 0.3
        else:
            gz = 0.0
        yaw = _face_yaw(r.get("facing_deg", 0.0)) + axis_deg
        min_fwd, max_fwd = min(min_fwd, fwd), max(max_fwd, fwd)
        min_lat, max_lat = min(min_lat, lat), max(max_lat, lat)
        items.append({
            "id": r["id"], "label": f"OBJ_{r['id']:02d}",
            "glb": glb_by_id.get(r["id"], ""), "image": r["url"], "prompt": r["prompt"],
            "bbox": bbox, "depth": r.get("depth", 0.5), "size_m": size_m,
            "location": [round(fwd * SCALE_MULT, 1), round(lat * SCALE_MULT, 1), round((gz + base_h) * SCALE_MULT, 1)],
            "rotation": [0.0, round(yaw, 1), 0.0],
            "scale": [round(size_m * SCALE_MULT, 1)] * 3,
            "ground": True, "hero": bool(r.get("hero")),   # hero passthrough (health-check M1: layout dropped hero -> hero unpinned / deletable by critique)
            "_radius": size_m * 50.0, "_man_made": bool(r.get("man_made")), "_base_h": base_h,
        })

    # -- Imagined objects: placed just OUTSIDE the real envelope (in asymmetric scenes they can't intersect real objects) --
    if max_fwd < min_fwd:                                        # no real objects -> fall back to S
        min_fwd, max_fwd, min_lat, max_lat = -0.5 * S, 1.5 * S, -0.8 * S, 0.8 * S
    mid_fwd, mid_lat = (min_fwd + max_fwd) / 2.0, (min_lat + max_lat) / 2.0
    margin, spread = 0.25 * S, 0.45 * S          # imagined objects stay a bit closer to the real envelope (don't fling them far)
    dir_idx = {}
    for r in results:
        if not r.get("imagined"):
            continue
        size_m = max(SIZE_MIN, min(SIZE_MAX, float(r.get("size_m", 1.0)) * scale_corr))
        d = (r.get("direction") or "left").lower()
        k = dir_idx.get(d, 0); dir_idx[d] = k + 1
        if d == "left":
            X, Y = mid_fwd, min_lat - margin - k * spread
        elif d == "right":
            X, Y = mid_fwd, max_lat + margin + k * spread
        elif d == "behind":                                  # behind the camera -> negative X
            X, Y = -(0.5 * S + margin + k * spread), mid_lat
        else:  # front: a bit deeper than the farthest real object
            X, Y = max_fwd + margin + k * spread, mid_lat
        if terrain:                                          # imagined objects are also clamped into the terrain (no longer flung outside it, floating)
            X, Y = _clamp_to_terrain(terrain, X, Y, size_m * 0.5)
        items.append({
            "id": r["id"], "label": f"OBJ_{r['id']:02d}",
            "glb": glb_by_id.get(r["id"], ""), "image": r["url"], "prompt": r["prompt"],
            "bbox": r.get("bbox"), "depth": r.get("depth", 0.7), "size_m": size_m,
            "imagined": True, "direction": d,
            "location": [round(X * SCALE_MULT, 1), round(Y * SCALE_MULT, 1),
                         round(_terrain_ground(terrain, X, Y) * SCALE_MULT, 1)],
            "rotation": [0.0, round(_face_origin_yaw(X, Y), 1), 0.0],
            "scale": [round(size_m * SCALE_MULT, 1)] * 3,
            "ground": True, "hero": bool(r.get("hero")), "_radius": size_m * 50.0,
        })

    # Size-aware anti-overlap on the ground plane (X fore-aft / Y lateral); spacing scales with the scene
    for _ in range(60):
        moved = False
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                need = items[i]["_radius"] + items[j]["_radius"] + sep_cm
                ax, ay = items[i]["location"][0], items[i]["location"][1]
                bx2, by2 = items[j]["location"][0], items[j]["location"][1]
                dx, dy = bx2 - ax, by2 - ay
                dist = (dx * dx + dy * dy) ** 0.5
                if dist < need:
                    if dist < 1e-6:                          # fully coincident: a zero vector never separates -> deterministic direction from j (golden-angle fan)
                        ang = j * 2.399963229
                        ux, uy = math.cos(ang), math.sin(ang)
                        dist = 1e-6
                    else:
                        ux, uy = dx / dist, dy / dist
                    push = (need - dist) / 2.0
                    items[i]["location"][0] = ax - ux * push
                    items[i]["location"][1] = ay - uy * push
                    items[j]["location"][0] = bx2 + ux * push
                    items[j]["location"][1] = by2 + uy * push
                    moved = True
        if not moved:
            break
    if terrain:                                              # after anti-overlap moved objects in X/Y, resample terrain height Z at the new position (else floating/buried on relief)
        for it in items:
            fwd_m = it["location"][0] / SCALE_MULT; lat_m = it["location"][1] / SCALE_MULT
            if it.get("imagined"):
                gz = _terrain_ground(terrain, fwd_m, lat_m)
            else:
                gz = _terrain_ground_max(terrain, fwd_m, lat_m, it.get("size_m", 1.0) * 0.5)
                if not it.get("_man_made"):
                    gz -= 0.3
                gz += it.get("_base_h", 0.0)
            it["location"][2] = gz * SCALE_MULT
    for it in items:
        it.pop("_radius", None); it.pop("_man_made", None); it.pop("_base_h", None)
        it["location"] = [round(c, 1) for c in it["location"]]
    return items


def _build_lights(lights, fov_deg=65.0, img_aspect=1.333, camera=None, scene_scale=12.0):
    """Artificial lights (normalized u,v) -> UE world coordinates + colour/lumens. Distance scales with scene scale S; height via camera back-projection
    (the same geometry as objects -- no more per-class hardcoded mounting heights)."""
    if not lights:
        return []
    hfov = math.radians(max(25.0, min(100.0, fov_deg)))
    th = math.tan(hfov / 2.0)
    tv = th / max(0.1, img_aspect)
    vfov_deg = math.degrees(2.0 * math.atan(tv))
    cam = camera or {}
    axis = math.radians(_clampf(cam.get("depth_axis_deg", 0.0), -80.0, 80.0, 0.0))   # dominant vanishing direction of the scene
    ca, sa = math.cos(axis), math.sin(axis)
    D = max(2.0, 0.5 * scene_scale)                          # assumed light horizontal distance = half the scene scale
    radius = max(400.0, 1.6 * scene_scale * SCALE_MULT)      # attenuation radius scales with the scene
    lm_factor = max(0.5, min(6.0, scene_scale / 8.0))        # intensity scales with the scene (far scenes brighter)
    out = []
    for i, l in enumerate(lights[:LIGHT_MAX], 1):
        u, v = float(l.get("u", 0.5)), float(l.get("v", 0.5))
        x_right = (u - 0.5) * 2.0 * th * D
        fwd, lat = D * ca - x_right * sa, D * sa + x_right * ca   # same orientation math as the objects
        up = max(0.2, _world_height(v, D, cam, vfov_deg))    # back-projected height (D = horizontal distance)
        col = l.get("color") or [1.0, 0.85, 0.6]
        out.append({
            "id": i,
            "color": [round(c, 3) for c in col[:3]],
            "intensity_lm": round((1500.0 + float(l.get("intensity", 0.6)) * 7000.0) * lm_factor, 0),
            "radius_cm": round(radius, 0),
            "location": [round(fwd * SCALE_MULT, 1), round(lat * SCALE_MULT, 1), round(up * SCALE_MULT, 1)],
        })
    return out


def _latest_task_id():
    """Return the most recently completed task id, by result.json mtime."""
    if not os.path.isdir(OUTPUT_FOLDER):
        return None
    best, bestt = None, -1.0
    for d in os.listdir(OUTPUT_FOLDER):
        p = os.path.join(OUTPUT_FOLDER, d, "result.json")
        if os.path.exists(p):
            t = os.path.getmtime(p)
            if t > bestt:
                bestt, best = t, d
    return best


@app.route("/layout/<task_id>")
def layout_route(task_id):
    """Serves UE the placement list for a task (recomputed live at scene scale, so parameter tweaks take effect immediately)."""
    data = _load_task_result(task_id)
    if not data:
        return jsonify({"error": "not found"}), 404
    S = _scene_scale(data.get("results", []), data.get("fov_deg", 65.0), data.get("img_aspect", 1.333))
    objs = _build_layout(data.get("results", []), data.get("models_3d", []),
                         data.get("fov_deg", 65.0), data.get("img_aspect", 1.333),
                         data.get("camera", {}), S, data.get("terrain"))
    lts = _build_lights(data.get("lights", []), data.get("fov_deg", 65.0),
                        data.get("img_aspect", 1.333), data.get("camera", {}), S)
    return jsonify({"task_id": task_id, "objects": objs, "lights": lts,
                    "environment": data.get("environment", {}),
                    "camera": data.get("camera", {}), "hdri": data.get("hdri", ""), "terrain": data.get("terrain"),
                    "effects": data.get("effects"), "img_aspect": data.get("img_aspect", 1.333),
                    "scene_scale": round(S, 1), "is_indoor": bool(data.get("camera", {}).get("is_indoor"))})


HDRI_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hdri_cache")


def fetch_hdri_online(client, image, env, out_dir, task_id):
    """Fetch a real HDRI live (Poly Haven, CC0): filter candidates by environment keywords -> Gemini picks visually against the source photo ->
    download the 2k .hdr (true high dynamic range, better skylight capture). Any failure returns '' (falls back to the generated panorama)."""
    import urllib.request as _rq

    def _open(u, timeout=20):
        return _rq.urlopen(_rq.Request(u, headers={"User-Agent": "photo2scene-rebuilder/1.0"}),
                           timeout=timeout)
    try:
        with _open("https://api.polyhaven.com/assets?type=hdris") as r:
            assets = json.loads(r.read())
        tod = str(env.get("time_of_day", "")).lower()
        weather = str(env.get("weather", "")).lower()
        want = set()
        if "night" in tod:
            want |= {"night", "moon", "stars"}
        elif tod in ("sunrise", "golden hour", "dusk"):
            want |= {"sunrise-sunset", "sunset", "sunrise", "golden hour"}
        else:
            want |= {"midday", "morning-afternoon", "day"}
        if "overcast" in weather or "fog" in weather:
            want |= {"overcast", "cloudy"}
        elif "partly" in weather:
            want |= {"partly cloudy"}
        else:
            want |= {"clear"}
        scored = []
        for aid, meta in assets.items():
            bag = {str(t).lower() for t in (meta.get("tags") or [])} | \
                  {str(c).lower() for c in (meta.get("categories") or [])}
            s = len(want & bag) + (1 if "skies" in bag else 0)
            if s > 0:
                scored.append((s, aid))
        scored.sort(reverse=True)
        cand = [aid for _, aid in scored[:8]]
        if not cand:
            return ""
        thumbs, labels = [], []
        for aid in cand:
            try:
                with _open("https://cdn.polyhaven.com/asset_img/thumbs/%s.png?width=256&height=128" % aid) as r:
                    thumbs.append(Image.open(io.BytesIO(r.read())).convert("RGB"))
                labels.append(aid)
            except Exception:
                continue
        if not labels:
            return ""
        contents = [image, "PHOTO above = the scene to match. Candidate real-sky HDRIs follow:"]
        for aid, th in zip(labels, thumbs):
            contents.append("CANDIDATE '%s':" % aid)
            contents.append(th)
        contents.append(
            "Pick the ONE candidate whose sky best matches the PHOTO's time of day, cloud character, "
            "sun/moon position and mood. Also report WHERE the brightest light source (sun/moon/glow) "
            "sits horizontally in the PICKED panorama thumbnail: light_frac 0-1 (0 = left edge, 0.5 = "
            'center, 1 = right edge). Return ONLY JSON: {"pick": "<candidate id>", "light_frac": <0-1>}')
        cfg = genai_types.GenerateContentConfig(temperature=0.0, seed=11, responseMimeType="application/json")
        try:
            resp = client.models.generate_content(model=TEXT_MODEL, contents=contents, config=cfg)
        except ServerError as e:
            if getattr(e, "code", None) != 503:
                raise
            resp = client.models.generate_content(model=FALLBACK_TEXT_MODEL, contents=contents, config=cfg)
        d_pick = json.loads((resp.text or "{}").strip())
        pick = str(d_pick.get("pick", "")).strip()
        light_frac = _clampf(d_pick.get("light_frac"), 0.0, 1.0, 0.5)
        if pick not in labels:
            pick = labels[0]
        os.makedirs(HDRI_CACHE_DIR, exist_ok=True)
        cache = os.path.join(HDRI_CACHE_DIR, "%s_2k.hdr" % pick)
        if not os.path.exists(cache):
            url = "https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/2k/%s_2k.hdr" % pick
            with _open(url, timeout=180) as r:
                with open(cache, "wb") as f:
                    f.write(r.read())
        dst = os.path.join(out_dir, "sky_%s.hdr" % pick)
        shutil.copy2(cache, dst)
        print(f"[HDRI] Poly Haven pick '{pick}' light_frac={light_frac:.2f} ({os.path.getsize(dst)//1024} KB)")
        return {"url": f"/output/{task_id}/sky_{pick}.hdr", "light_frac": light_frac}
    except Exception as e:
        print(f"[HDRI] online fetch failed, fallback to generated: {e}", file=sys.stderr)
        return ""


def critique_fx(client, photo, render, effects, rnd=1):
    """FX visual loop: review the PARTICLE look against the source photo (density/size/brightness; per-layer multiplier prescriptions).
    The render must come from the viewport channel (SceneCapture doesn't render Niagara -- engine-verified). Default verdict=ok."""
    out = {"verdict": "ok", "notes": "", "fx": []}
    rows = []
    for i, e in enumerate(effects or []):
        if e.get("primitive") != "niagara":
            continue
        n = e.get("niagara") or {}
        rows.append("i=%d name='%s' preset=%s emitters=%d per_emitter=%d size=%s-%scm" % (
            i, str(e.get("name", ""))[:30], n.get("preset"), len(n.get("spots") or [1]),
            int(n.get("count", 0)), n.get("size_cm_lo", "?"), n.get("size_cm_hi", "?")))
    if not rows:
        return out
    instruction = (
        "You are art-directing PARTICLE EFFECTS in a rebuilt 3D night/day scene. "
        "Image 1 = the ORIGINAL photo (mood reference). Image 2 = the CURRENT engine render "
        "from player eye level (particles visible). Particle layers:\n" + "\n".join(rows) +
        "\nRound %d. Judge the particle LOOK vs the photo's mood: a layer clearly too "
        "dense/sparse, particles clearly too big/small, or glowing too bright/too dim. "
        "LAYER HIERARCHY: distinct phenomena must read at clearly different visual scales — "
        "fine atmospheric motes (smallest, subtle sparkle) << chunky readable bits (leaves/petals/"
        "debris, mid-size) << soft volumetric banks (mist/smoke: largest, dimmest, slowest). If two "
        "layers look CONFUSABLY similar in apparent size or tone in the render, prescribe DIVERGING "
        "multipliers to pull them apart — gently (steps within 0.7-1.5x), keeping each layer's "
        "physical character; never exaggerate into theatrics or break the scene's mood. "
        "Subtle is fine — only prescribe for CLEAR problems. Multipliers, 1.0 = keep as is.\n"
        "Return ONLY raw JSON:\n"
        "{\n"
        '  "verdict": "ok|adjust",\n'
        '  "notes": "<one short sentence>",\n'
        '  "fx": [ { "i": <layer i>, "density_mul": <0.3..2.5>, "size_mul": <0.5..2.0>, '
        '"brightness_mul": <0.4..2.5> } ]\n'
        "}\n"
        "At most 3 entries, only clearly-wrong layers." % rnd
    )
    try:
        cfg = genai_types.GenerateContentConfig(temperature=0.2, seed=53 + rnd,
                                                responseMimeType="application/json")
        try:
            resp = client.models.generate_content(model=TEXT_MODEL, contents=[photo, render, instruction], config=cfg)
        except ServerError as e:
            if getattr(e, "code", None) != 503:
                raise
            resp = client.models.generate_content(model=FALLBACK_TEXT_MODEL,
                                                  contents=[photo, render, instruction], config=cfg)
        d = json.loads((resp.text or "").strip())
    except Exception as e:
        print(f"[CritiqueFX] r{rnd} failed: {e}", file=sys.stderr)
        return out
    valid = {i for i, e in enumerate(effects or []) if e.get("primitive") == "niagara"}
    fx = []
    for f in (d.get("fx") or [])[:3]:
        try:
            fi = int(f.get("i"))
            if fi in valid:
                fx.append({"i": fi,
                           "density_mul": _clampf(f.get("density_mul"), 0.3, 2.5, 1.0),
                           "size_mul": _clampf(f.get("size_mul"), 0.5, 2.0, 1.0),
                           "brightness_mul": _clampf(f.get("brightness_mul"), 0.4, 2.5, 1.0)})
        except Exception:
            continue
    out["verdict"] = "adjust" if (str(d.get("verdict", "ok")).lower() == "adjust" and fx) else "ok"
    out["notes"] = str(d.get("notes", ""))[:200]
    out["fx"] = fx
    print("[CritiqueFX] r%d: %s — %d rx — %s" % (rnd, out["verdict"], len(fx), out["notes"]))
    return out


def infer_night_practicals(client, results, environment):
    """Night practical-light inference (AI-driven, small text request): which reconstructed objects glow at night (building windows/bulbs/hearth fire),
    with colour/intensity/mount height judged by Gemini; rocks/dead trees/fences never glow. On failure -> [] (fill lights still work)."""
    if "night" not in str((environment or {}).get("time_of_day", "")).lower():
        return []
    objs = [{"id": r["id"], "desc": str(r.get("prompt", ""))[:90], "size_m": r.get("size_m", 2.0)}
            for r in (results or []) if not r.get("imagined")]
    if not objs:
        return []
    instruction = (
        "Reconstructed objects in a NIGHT game scene:\n" + json.dumps(objs, ensure_ascii=False) +
        "\nWhich objects would PLAUSIBLY emit light at night (building windows glow warm, lamps, stove/fire)? "
        "Natural/inert objects (rocks, bare trees, gates, sheds without windows) emit NOTHING — omit them. "
        "Output ONLY JSON: {\"practicals\": [{\"object_id\": <id>, \"kind\": \"window|bulb|fire\", "
        "\"color\": [r,g,b] (0-1, warm for windows/fire), \"intensity\": <0..1>, "
        "\"height_frac\": <0..1, fraction of object height where the light sits>}]} . Empty list is fine."
    )
    try:
        d = _gen_json(client, None, instruction, temperature=0.2, seed=41)
    except Exception as e:
        print(f"[Practicals] inference failed: {e}", file=sys.stderr)
        return []
    if not isinstance(d, dict):                          # _gen_json may return a list (its fallback parser allows [..]) -> d.get would crash (health-check M4)
        return []
    out = []
    for p in (d.get("practicals") or [])[:6]:
        try:
            col = p.get("color") or [1.0, 0.75, 0.45]
            out.append({
                "object_id": int(p.get("object_id")),
                "kind": str(p.get("kind", "bulb"))[:10],
                "color": [_clampf(col[i], 0.0, 1.0, 0.8) for i in range(3)],
                "intensity": _clampf(p.get("intensity", 0.5), 0.05, 1.0, 0.5),
                "height_frac": _clampf(p.get("height_frac", 0.4), 0.05, 0.95, 0.4),
            })
        except Exception:
            continue
    return out


MUSIC_MODEL = "models/lyria-3-clip-preview"   # generative score (same Gemini key; probe-verified available)


def generate_music(client, env, out_dir, task_id):
    """Generative score: photo mood (env.mood/time/weather, judged by Gemini) -> Lyria-3 text-to-music clip -> wav/mp3.
    Returns '' on failure (the scene simply has no score; never blocks)."""
    mood = str(env.get("mood", "calm ambient"))
    tod = str(env.get("time_of_day", ""))
    weather = str(env.get("weather", ""))
    prompt = (f"Instrumental ambient background music for a walkable game scene. Mood: {mood}. "
              f"Time: {tod}, weather: {weather}. Gentle, atmospheric, seamlessly loopable, "
              "no vocals, no sudden hits, evolving slowly.")
    try:
        existing = [f for f in os.listdir(out_dir) if f.startswith("music.")]
        if existing:
            return f"/output/{task_id}/{existing[0]}"
        resp = client.models.generate_content(model=MUSIC_MODEL, contents=[prompt])
        for p in resp.parts:
            if p.inline_data is not None:
                mime = (p.inline_data.mime_type or "audio/wav").lower()
                ext = ".mp3" if "mp3" in mime or "mpeg" in mime else ".wav"
                path = os.path.join(out_dir, "music" + ext)
                with open(path, "wb") as f:
                    f.write(p.inline_data.data)
                print(f"[Music] Lyria clip saved ({mime}, {os.path.getsize(path)//1024} KB) — '{mood}'")
                return f"/output/{task_id}/music{ext}"
        print("[Music] no audio part in response", file=sys.stderr)
        return ""
    except Exception as e:
        print(f"[Music] generation failed: {e}", file=sys.stderr)
        return ""


def generate_ambience(client, env, indoor, out_dir, task_id, window_owned=False):
    """Soundscape layer A: the ambience bed -- a separate track from the score. Field-recording style, no melody, driven by env time/weather/indoors.
    Semantic dedup (review flagged 'audio overlap'): with window point sources, outdoor bleed belongs solely to the spatialized window source
    and the bed degrades to pure room tone -- each sound semantic has exactly one owner."""
    tod = str(env.get("time_of_day", "day"))
    weather = str(env.get("weather", "clear"))
    # Synthesized ambience mix (Lyria retired from the bed after three content drifts: birdsong on a rainy night -- music models don't parse conditionals;
    # Lyria is kept only for actual music = the score). tod/weather are still AI-judged -- the decision chain is unbroken
    try:
        existing = [f for f in os.listdir(out_dir) if f.startswith("ambience.")]
        if existing:
            return f"/output/{task_id}/{existing[0]}"
        path = os.path.join(out_dir, "ambience.wav")
        _synth_ambience_mix(env, indoor, path, window_owned=window_owned)
        return f"/output/{task_id}/ambience.wav"
    except Exception as e:
        print(f"[Ambience] synth failed: {e}", file=sys.stderr)
        return ""


SFX_KINDS = ("birds", "crickets", "rain", "wind", "hum", "fire", "water", "clock", "traffic")


def _synth_wave(kind, seconds=14.0, seed=11):
    """Synthesize a single-source waveform (np array)."""
    import numpy as np
    sr = 22050
    n = int(seconds * sr)
    t = np.arange(n) / sr
    rng = np.random.default_rng(seed)
    sig = np.zeros(n)

    def lp(x, k):
        k = max(1, int(k * sr))
        return np.convolve(x, np.ones(k) / k, mode="same")

    if kind == "birds":
        for _ in range(int(seconds * 1.2)):                     # sparse chirps: FM-sweep phrases
            t0 = rng.uniform(0, seconds - 0.4)
            dur = rng.uniform(0.08, 0.25)
            i0, i1 = int(t0 * sr), int((t0 + dur) * sr)
            tt = np.arange(i1 - i0) / sr
            f = rng.uniform(2200, 4200) + rng.uniform(-900, 900) * tt / dur
            chirp = np.sin(2 * np.pi * (f * tt + rng.uniform(3, 9) * np.sin(2 * np.pi * 22 * tt)))
            env = np.sin(np.pi * tt / dur) ** 2
            sig[i0:i1] += chirp * env * rng.uniform(0.25, 0.6)
        sig += lp(rng.normal(0, 1, n), 0.01) * 0.04             # distant leaf-rustle bed
    elif kind == "crickets":
        for _ in range(3):                                      # a few out-of-phase pulse trains
            f0 = rng.uniform(3800, 4800); rate = rng.uniform(7, 11); ph = rng.uniform(0, 1)
            gate = (np.sin(2 * np.pi * rate * t + ph * 6.28) > 0.55).astype(float)
            sig += np.sin(2 * np.pi * f0 * t) * lp(gate, 0.004) * 0.18
    elif kind == "rain":
        sig = lp(rng.normal(0, 1, n), 0.0015) * 0.5
        for _ in range(int(seconds * 14)):                      # random fat drops
            i0 = rng.integers(0, n - 200)
            sig[i0:i0 + 160] += rng.normal(0, 1, 160) * np.exp(-np.arange(160) / 35.0) * 0.25
    elif kind == "wind":
        env_slow = 0.5 + 0.5 * np.interp(t, np.linspace(0, seconds, 24), rng.uniform(0.15, 1.0, 24))
        sig = lp(rng.normal(0, 1, n), 0.006) * env_slow * 0.8
    elif kind == "hum":                                         # fridge/AC: mains hum + harmonics + airflow bed
        sig = (np.sin(2 * np.pi * 100 * t) * 0.5 + np.sin(2 * np.pi * 200 * t) * 0.2
               + np.sin(2 * np.pi * 300 * t) * 0.08) * (0.9 + 0.1 * np.sin(2 * np.pi * 0.3 * t))
        sig += lp(rng.normal(0, 1, n), 0.002) * 0.12
    elif kind == "fire":
        sig = lp(rng.normal(0, 1, n), 0.004) * 0.35             # fire bed
        for _ in range(int(seconds * 6)):                       # crackles
            i0 = rng.integers(0, n - 400)
            pop = rng.normal(0, 1, 300) * np.exp(-np.arange(300) / rng.uniform(20, 70))
            sig[i0:i0 + 300] += pop * rng.uniform(0.3, 0.8)
    elif kind == "water":
        base = lp(rng.normal(0, 1, n), 0.001)
        warble = 0.6 + 0.4 * np.sin(2 * np.pi * (1.3 * t + 0.4 * np.sin(2 * np.pi * 0.37 * t)))
        sig = base * warble * 0.5
        for _ in range(int(seconds * 8)):                       # bubbling: short rising glides
            t0 = rng.uniform(0, seconds - 0.1); dur = rng.uniform(0.03, 0.08)
            i0, i1 = int(t0 * sr), int((t0 + dur) * sr)
            tt = np.arange(i1 - i0) / sr
            sig[i0:i1] += np.sin(2 * np.pi * (400 + 900 * tt / dur) * tt) * np.sin(np.pi * tt / dur) * 0.2
    elif kind == "clock":
        for k in range(int(seconds)):
            i0 = int(k * sr)
            click = rng.normal(0, 1, 90) * np.exp(-np.arange(90) / 12.0)
            sig[i0:i0 + 90] += click * (0.5 if k % 2 == 0 else 0.38)
    else:                                                       # traffic: distant low rumble + swell
        env_slow = 0.6 + 0.4 * np.interp(t, np.linspace(0, seconds, 16), rng.uniform(0.2, 1.0, 16))
        sig = lp(rng.normal(0, 1, n), 0.012) * env_slow
    k = int(0.8 * sr)                                           # seamless loop: mix the tail into the head, then cut the tail
    ramp = np.linspace(0, 1, k)
    sig[:k] = sig[:k] * ramp + sig[n - k:] * (1 - ramp)
    return sig[:n - k]


def _write_wav(sig, path, sr=22050):
    import numpy as np
    import wave as _wave
    sig = sig / (np.max(np.abs(sig)) + 1e-9) * 0.8
    pcm = (sig * 32767).astype(np.int16)
    with _wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


def _synth_sfx(kind, path, seconds=14.0, seed=11):
    """Procedurally synthesize a point-source sound (observed: Lyria is a music model, 'field recording' prompts still yield light music --
    SFX never go through the generative model). Mono 22050Hz wav, tail cross-faded into a seamless loop. AI picks the type, code makes the waveform."""
    return _write_wav(_synth_wave(kind, seconds, seed), path)


def _synth_ambience_mix(env, indoor, path, window_owned=False):
    """Ambience bed = synthesized mix (settled after Lyria drifted off-content three times: birdsong on a rainy night -- music models don't parse conditionals).
    Table lookup: keys = the AI-judged time/weather/ground (the decision chain stays AI's); waveforms = deterministic synthesis."""
    tod = str(env.get("time_of_day", "day")).lower()
    weather = str(env.get("weather", "clear")).lower()
    ground = str(env.get("ground_material", "")).lower()
    night = "night" in tod
    rainy = ("rain" in weather) or ("storm" in weather)
    urban = any(k in ground for k in ("asphalt", "concrete", "pav"))
    layers = []
    if indoor:
        layers = [("hum", 0.45), ("wind", 0.12)] if window_owned else \
                 [("hum", 0.4), ("wind", 0.15), ("birds" if not night else "crickets", 0.18)]
    else:
        if rainy:
            layers = [("rain", 1.0), ("wind", 0.45)] + ([("traffic", 0.3)] if urban else [])
        elif night:
            layers = ([("traffic", 0.5), ("wind", 0.35)] if urban else
                      [("crickets", 0.8), ("wind", 0.3)])
        else:
            layers = [("birds", 0.85), ("wind", 0.35)] + ([("traffic", 0.35)] if urban else [])
    import numpy as np
    mix = None
    for i, (kind, gain) in enumerate(layers):
        w = _synth_wave(kind, 16.0, seed=71 + i) * gain
        mix = w if mix is None else mix + w
    print(f"[Ambience] synth mix: {[k for k, _ in layers]} (tod={tod}, weather={weather})")
    return _write_wav(mix, path)


def _to_mono_wav(src_path, dst_path):
    """mp3/anything -> mono wav (miniaudio decode; no ffmpeg dependency).
    UE only spatializes mono -- a stereo point source ignores its attenuation and doesn't change with distance (observed)."""
    import miniaudio
    import wave as _wave
    dec = miniaudio.decode_file(src_path, output_format=miniaudio.SampleFormat.SIGNED16,
                                nchannels=1, sample_rate=22050)
    with _wave.open(dst_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(dec.sample_rate)
        w.writeframes(bytes(dec.samples))
    return dst_path


def infer_sound_sources(client, results, room, env, out_dir, task_id):
    """Soundscape layer B (AI semantic point sources): which objects/openings emit sound (fridge hum / fireplace crackle / street noise through the window);
    each source gets a Lyria-generated field-recording-style short loop, placed as a spatialized AmbientSound (louder as you approach)."""
    objs = [{"id": r["id"], "desc": str(r.get("prompt", ""))[:70]} for r in (results or []) if not r.get("imagined")]
    has_window = bool((room or {}).get("openings"))
    if not objs and not has_window:
        return []
    instruction = (
        "A navigable 3D scene. Which of these objects PLAUSIBLY EMIT a continuous ambient sound "
        "(fridge hum, fireplace crackle, fountain trickle, clock tick, AC unit, running water)? "
        "Most objects are SILENT — chairs, beds, shelves, plants, lamps emit nothing; answer none "
        "unless the object really hums/crackles/flows. "
        + ("There is also a WINDOW: if the outside would be audible (street, garden birds, rain), "
           'include {"id": "window", ...} with what bleeds in. ' if has_window else "")
        + "Scene: " + str(env.get("time_of_day", "")) + ", " + str(env.get("weather", "")) +
        ", outside: " + str((room or {}).get("outside_hint", "")) +
        "\nObjects: " + json.dumps(objs, ensure_ascii=False) +
        '\nReturn ONLY JSON: {"sources": [{"id": <object id or "window">, '
        '"kind": "birds|crickets|rain|wind|hum|fire|water|clock|traffic", '
        '"sound": "<5-10 words, e.g. low refrigerator hum>", "volume": <0.2-1.0>}]}  (0-3 sources)')
    try:
        d = _gen_json(client, None, instruction, temperature=0.1, seed=47)
        valid = {o["id"] for o in objs}
        out = []
        for s in (d.get("sources") or [])[:3]:
            sid = s.get("id")
            if sid != "window":
                try:
                    sid = int(sid)
                except Exception:
                    continue
                if sid not in valid:
                    continue
            desc = str(s.get("sound", ""))[:60].strip()
            kind = str(s.get("kind", "")).lower().strip()
            if not desc or kind not in SFX_KINDS:
                continue
            out.append({"id": sid, "kind": kind, "sound": desc,
                        "volume": _clampf(s.get("volume"), 0.2, 1.0, 0.5)})
        for i, s in enumerate(out):
            # waveform = procedural synthesis (Lyria is a music model: SFX prompts still yield light music -- review: 'the window plays light music');
            # AI judges type/volume, code produces a deterministic mono waveform (spatialization-ready)
            mono = os.path.join(out_dir, f"sfx_{i}.wav")
            if not os.path.exists(mono):
                try:
                    _synth_sfx(s["kind"], mono, seed=47 + i)
                except Exception as e:
                    print(f"[SFX] synth {s['kind']}: {e}", file=sys.stderr)
            if os.path.exists(mono):
                s["url"] = f"/output/{task_id}/sfx_{i}.wav"
        out = [s for s in out if s.get("url")]
        print("[SFX] " + (", ".join("%s:%s(%s)" % (s['id'], s['kind'], s['sound']) for s in out) or "no sources"))
        return out
    except Exception as e:
        print(f"[SFX] failed: {e}", file=sys.stderr)
        return []


MOMENTS = ("noon", "dusk", "night", "rain")


def make_moments(client, image, data, out_dir, task_id):
    """Four moments (demo: one photo -> day/dusk/night/rain moods; assets reused, only parameters change). One AI call judges everything:
    per-moment window transmission / indoor illuminance / exposure grade / which fixtures to light / window sound; one window view per moment (idempotent). Indoor v1."""
    room = data.get("room") or {}
    results = data.get("results") or []
    objs = [{"id": r["id"], "desc": str(r.get("prompt", ""))[:60]} for r in results if not r.get("imagined")]
    instruction = (
        "This indoor scene (photo attached, currently " + str((data.get("environment") or {}).get("time_of_day", "day")) +
        ") will be re-lit for FOUR moments: noon, dusk, night, rain(daytime rainstorm). For EACH moment give "
        "physically grounded values. Lamps in the object list may be TURNED ON for dark moments "
        "(practicals; pick only objects that ARE light fixtures, realistic lumens). Window sound: what "
        "bleeds in at that moment.\nObjects: " + json.dumps(objs, ensure_ascii=False) +
        "\nWindow now: " + json.dumps(room.get("openings") or []) +
        '\nReturn ONLY JSON: {"moments": {"<noon|dusk|night|rain>": {'
        '"window_lux": <0-60000>, "window_temp_k": <1700-12000>, "indoor_lux": <2-2000>, '
        '"exposure_ev": <-2..2>, "saturation": <0.7-1.2>, "contrast": <0.9-1.3>, "bloom": <0-1>, '
        '"practicals": [{"object_id": <id>, "lumens": <100-2000>, "color_temp_k": <1700-6500>}], '
        '"window_sound": "birds|crickets|rain|wind|traffic"}}}')
    try:
        d = _gen_json(client, image, instruction, temperature=0.15, seed=53)
        valid = {o["id"] for o in objs}
        out = {}
        for m in MOMENTS:
            v = (d.get("moments") or {}).get(m) or {}
            pr = []
            for p in (v.get("practicals") or [])[:3]:
                try:
                    oid = int(p.get("object_id"))
                except Exception:
                    continue
                if oid in valid:
                    pr.append({"object_id": oid,
                               "lumens": _clampf(p.get("lumens"), 100.0, 2000.0, 500.0),
                               "color_temp_k": _clampf(p.get("color_temp_k"), 1700.0, 6500.0, 2700.0)})
            snd = str(v.get("window_sound", "birds")).lower()
            out[m] = {"window_lux": _clampf(v.get("window_lux"), 0.0, 60000.0, 1000.0),
                      "window_temp_k": _clampf(v.get("window_temp_k"), 1700.0, 12000.0, 6500.0),
                      "indoor_lux": _clampf(v.get("indoor_lux"), 2.0, 2000.0, 100.0),
                      "exposure_ev": _clampf(v.get("exposure_ev"), -2.0, 2.0, 0.0),
                      "saturation": _clampf(v.get("saturation"), 0.7, 1.2, 1.0),
                      "contrast": _clampf(v.get("contrast"), 0.9, 1.3, 1.1),
                      "bloom": _clampf(v.get("bloom"), 0.0, 1.0, 0.3),
                      "practicals": pr,
                      "window_sound": snd if snd in SFX_KINDS else "birds"}
            # per-moment window view (idempotent): same viewpoint, different time of day
            wv = os.path.join(out_dir, f"window_view_{m}.png")
            if not os.path.exists(wv) and str(room.get("outside_hint", "")).strip():
                hint = {"noon": "bright midday sun, neutral daylight colors",
                        "dusk": "deep dusk just after sunset — the ENTIRE view bathed in warm "
                                "golden-orange light, strong ~2800K amber cast over everything, "
                                "greens turned olive-gold, long shadows",
                        "night": "dark night, faint cool moonlight",
                        "rain": "heavy daytime rain, everything in a muted grey-blue overcast cast"}[m]
                # Bake the colour cast into the image (seen at dusk: 'outside doesn't match the inside tones' -- multiplicative tint on the board can't tame the image's own green);
                # the daytime view serves as the reference image -> the same garden across moments (each moment used to 'grow a different garden')
                ref = None
                base_wv = os.path.join(out_dir, "window_view.png")
                if os.path.exists(base_wv):
                    try:
                        ref = Image.open(base_wv).convert("RGB")
                    except Exception:
                        ref = None
                prompt = (("Repaint THE SAME view as in the second image (keep its layout, plants and "
                           "structures recognisably identical), now at " if ref else
                           "The view seen THROUGH this room's window: " + str(room["outside_hint"])[:120] + ", at ")
                          + f"{hint}. Color-grade the WHOLE image consistently to that light — "
                          f"around {int(out[m]['window_temp_k'])}K. No window frame, no curtains, "
                          "no interior — only the outside view.")
                try:
                    cfg = genai_types.GenerateContentConfig(
                        responseModalities=["IMAGE"],
                        imageConfig=genai_types.ImageConfig(aspectRatio="16:9", imageSize="1K"))
                    contents = [prompt, image] + ([ref] if ref else [])
                    resp = _gen_image_content(client, contents, cfg)
                    if not getattr(resp, "parts", None):
                        resp = _gen_image_content(client, contents, cfg)
                    for p in (resp.parts or []):
                        if not getattr(p, "thought", False) and p.inline_data is not None:
                            p.as_image().save(wv)
                            break
                except Exception as e:
                    print(f"[Moments] window view {m}: {e}", file=sys.stderr)
            if os.path.exists(wv):
                out[m]["window_view"] = f"/output/{task_id}/window_view_{m}.png"
            sw = os.path.join(out_dir, f"sfx_{m}.wav")
            if not os.path.exists(sw):
                try:
                    _synth_sfx(out[m]["window_sound"], sw, seed=61 + MOMENTS.index(m))
                except Exception as e:
                    print(f"[Moments] sfx {m}: {e}", file=sys.stderr)
            if os.path.exists(sw):
                out[m]["sfx_url"] = f"/output/{task_id}/sfx_{m}.wav"
        print("[Moments] " + ", ".join("%s: win %.0flux %s%s" % (
            m, out[m]["window_lux"], out[m]["window_sound"],
            " +%d lamps" % len(out[m]["practicals"]) if out[m]["practicals"] else "") for m in MOMENTS))
        return out
    except Exception as e:
        print(f"[Moments] failed: {e}", file=sys.stderr)
        return {}


def _is_building_prompt(p) -> bool:
    """Building detection (subject-zone test): building/facade within the first 70 chars = the object itself is a building.
    The old [:40] cut missed buildings in practice: in 'A dark, weathered multi-story concrete building...' the keyword
    sits past char 40 -> 2 of 4 buildings missed -> street-corridor detection failed -> the whole AI block plan silently skipped (empty rainy alley)."""
    pl = str(p)[:70].lower()
    return "building" in pl or "facade" in pl


def _street_corridor_m(results, layout):
    """Infer the street corridor (m) from the buildings' lateral distribution: buildings in two rows -> take the gap; single row / no buildings -> None.
    Complements the executor-side AABB push-apart as the coarse of the two layers (this only outlines a clear band for the planner)."""
    ys = []
    for r in (results or []):
        if not _is_building_prompt(r.get("prompt", "")):
            continue
        it = next((o for o in (layout or []) if o.get("id") == r["id"]), None)
        if it and it.get("location"):
            ys.append((it["location"][1] / 100.0, float((it.get("scale") or [800])[0]) / 200.0))
    if len(ys) < 2:
        return None
    mid = sorted(y for y, _ in ys)[len(ys) // 2]
    left = [y + hw for y, hw in ys if y <= mid]
    right = [y - hw for y, hw in ys if y > mid]
    if not left or not right:
        return None
    lo, hi = max(left), min(right)
    if hi - lo < 4.0:                                   # gap too narrow: prop an 8m street around the midline
        c = (lo + hi) / 2.0
        lo, hi = c - 4.0, c + 4.0
    return (round(lo, 1), round(min(hi, lo + 14.0), 1))


def infer_city_block(client, image, results, layout, corridor_lat=(6.0, 16.0), fwd_range=(15.0, 175.0),
                     lat_range=(-55.0, 75.0), budget=(45, 80)):
    """AI city planner (S1 proper): photo + reconstructed buildings -> whole-block layout (iron rule: the AI produces the layout, code only clamps).
    Lesson learned: code hard-placing a row of clones = repeated heights / a single line / not a real block."""
    blds = []
    for r in (results or []):
        p = str(r.get("prompt", ""))
        if not _is_building_prompt(p):
            continue
        it = next((o for o in (layout or []) if o.get("id") == r["id"]), None)
        if not it or not it.get("location"):
            continue
        blds.append({"id": r["id"], "desc": p[:60],
                     "now_fwd_m": round(it["location"][0] / 100.0, 1),
                     "now_lat_m": round(it["location"][1] / 100.0, 1)})
    if not blds:
        return []
    props = []
    for r in (results or []):
        p = str(r.get("prompt", ""))
        pl = p.lower()
        if _is_building_prompt(p):
            continue
        if any(k in pl for k in ("planter", "plant rack", "pot", "scooter", "barrier",
                                 "bollard", "bin", "bench", "sign")):
            # note: wall-mounted items (meter boxes etc.) are excluded from street-prop sources (no wall to hang on = floating; seen live)
            props.append({"id": r["id"], "desc": p[:50]})
    instruction = (
        "You are a CITY PLANNER recreating this photo's urban fabric as a navigable block. We have "
        "these reconstructed buildings (already standing, DO NOT move them):\n" + json.dumps(blds, ensure_ascii=False) +
        ("\nAnd these small street props available for reuse:\n" + json.dumps(props, ensure_ascii=False)
         if props else "") +
        f"\nWorld frame: fwd = metres away from camera along the street, lat = metres to the right. "
        f"The walkable STREET corridor is lat {corridor_lat[0]}..{corridor_lat[1]} — keep it EMPTY. "
        f"This photo's alley sits inside a DENSE NEIGHBOURHOOD: plan the WHOLE DISTRICT, not one street. "
        f"Fill the ENTIRE buildable area fwd {fwd_range[0]}..{fwd_range[1]}, lat {lat_range[0]}..{lat_range[1]} "
        f"with {budget[0]}-{budget[1]} building instances (copies of the above, by id): continuous frontages "
        "on BOTH sides of the corridor, then MULTIPLE parallel rows of blocks extending outward to the area "
        "edges (back rows form the neighbourhood mass), with narrow service gaps (1-4 m), a couple of small "
        "courtyards, slight setback variation — leave NO large empty fields. AVOID CLONE FEEL: never place "
        "the same source id twice adjacent; alternate ids, vary yaw_deg (rows mostly parallel to the street "
        "±8, occasional perpendicular block), vary scale 0.85-1.25 (mass may rise toward the far edges, "
        "matching where the photo's skyline rises).\n"
        + ("ALSO place 8-15 street PROPS (by prop id) INSIDE the corridor along its EDGES "
           f"(lat within 1.5m of {corridor_lat[0]} or {corridor_lat[1]}), keeping the central walkway "
           "clear: planters cluster near doorways, scooters park against walls, realistic sparse rhythm "
           "— streets are lived-in, not empty. " if props else "") +
        'Return ONLY JSON: {"placements": [{"src_id": <id>, "fwd_m": <num>, "lat_m": <num>, '
        '"yaw_deg": <num>, "scale": <0.8-1.3>}], '
        '"props": [{"src_id": <prop id>, "fwd_m": <num>, "lat_m": <num>, "yaw_deg": <num>}]}')
    try:
        d = _gen_json(client, image, instruction, temperature=0.25, seed=59)
        valid = {b["id"] for b in blds}
        out = []
        for p in (d.get("placements") or [])[:budget[1] + 10]:
            try:
                sid = int(p.get("src_id"))
            except Exception:
                continue
            if sid not in valid:
                continue
            lat = _clampf(p.get("lat_m"), lat_range[0], lat_range[1], 0.0)
            if corridor_lat[0] - 1.0 < lat < corridor_lat[1] + 1.0:
                continue                                    # clamp: keep the corridor clear
            out.append({"src_id": sid,
                        "fwd_m": _clampf(p.get("fwd_m"), fwd_range[0], fwd_range[1], 80.0),
                        "lat_m": lat,
                        "yaw_deg": _clampf(p.get("yaw_deg"), -200.0, 200.0, 0.0),
                        "scale": _clampf(p.get("scale"), 0.8, 1.3, 1.0)})
        pvalid = {q["id"] for q in props}
        pout = []
        for q in (d.get("props") or [])[:18]:
            try:
                sid = int(q.get("src_id"))
            except Exception:
                continue
            if sid not in pvalid:
                continue
            # Clamp: props must stay in the corridor's INNER edge bands (seen: clamping outside the corridor = nailed into the building row) -- fold back into
            # [lower edge+0.6, midline-2] or [midline+2, upper edge-0.6], keeping a 4m central walkway clear
            lat = _clampf(q.get("lat_m"), corridor_lat[0] - 3.0, corridor_lat[1] + 3.0, corridor_lat[0] + 1.0)
            mid = (corridor_lat[0] + corridor_lat[1]) / 2.0
            if lat <= mid:
                lat = min(max(lat, corridor_lat[0] + 0.6), mid - 2.0)
            else:
                lat = max(min(lat, corridor_lat[1] - 0.6), mid + 2.0)
            pout.append({"src_id": sid,
                         "fwd_m": _clampf(q.get("fwd_m"), fwd_range[0], fwd_range[1], 60.0),
                         "lat_m": lat,
                         "yaw_deg": _clampf(q.get("yaw_deg"), -200.0, 200.0, 0.0)})
        print(f"[CityBlock] {len(out)} buildings + {len(pout)} props from {len(blds)}/{len(props)} sources")
        return {"placements": out, "props": pout}
    except Exception as e:
        print(f"[CityBlock] failed: {e}", file=sys.stderr)
        return []


def infer_night_windows(client, image, environment):
    """R2 night window lights: the AI reads the photo for the buildings' night-time liveliness -- lit-window ratio / warm-cool mix / colour temperature /
    window brightness / clustering. Geometry (per-building window grids / lit-window distribution / facade mapping) belongs to the executor --
    iron-rule split: parameters that need SEEING live here; whatever can be computed does not."""
    instruction = (
        "Night urban scene (photo attached). Judge how ALIVE the buildings' windows are at this hour, "
        "matching the photo's mood (weather: " + str((environment or {}).get("weather", "?")) +
        "). Consider: late rainy weeknights are sparse; lively commercial districts glow. Answer for the "
        "WHOLE district the photo implies, not just visible facades.\n"
        'Return ONLY JSON: {"lit_ratio": <0.02-0.65 fraction of windows lit>, '
        '"warm_ratio": <0-1 fraction of lit windows that are warm/residential vs cool/office>, '
        '"warm_k": <2200-3500 kelvin>, "cool_k": <3800-7000 kelvin>, '
        '"nits": <4-80 typical lit-window luminance, dim curtained glow=8, bright interior=45>, '
        '"cluster_bias": <0-1: 0=isolated scattered windows, 1=whole floors lit together>}')
    try:
        d = _gen_json(client, image, instruction, temperature=0.2, seed=61)
        nw = {"lit_ratio": _clampf(d.get("lit_ratio"), 0.02, 0.65, 0.15),
              "warm_ratio": _clampf(d.get("warm_ratio"), 0.0, 1.0, 0.7),
              "warm_k": _clampf(d.get("warm_k"), 2200.0, 3500.0, 2800.0),
              "cool_k": _clampf(d.get("cool_k"), 3800.0, 7000.0, 5200.0),
              "nits": _clampf(d.get("nits"), 4.0, 80.0, 22.0),
              "cluster_bias": _clampf(d.get("cluster_bias"), 0.0, 1.0, 0.3)}
        print(f"[NightWin] lit {nw['lit_ratio']:.2f} warm {nw['warm_ratio']:.2f} "
              f"{nw['warm_k']:.0f}K/{nw['cool_k']:.0f}K {nw['nits']:.0f}nits cluster {nw['cluster_bias']:.2f}")
        return nw
    except Exception as e:
        print(f"[NightWin] failed: {e}", file=sys.stderr)
        return None


def infer_room_arrange(client, image, room, results, layout, hero_id=None):
    """AI indoor arranger (design decision: inferred furniture is kept, not deleted -- room expanded + re-arranged; the indoor version of the city planner).
    Photo-visible objects are pinned in place (composition fidelity); other furniture gets AI wall positions/facing and may request room expansion (<= +60%)."""
    rows = []
    for r in (results or []):
        it = next((o for o in (layout or []) if o.get("id") == r["id"]), None)
        if not it or not it.get("location"):
            continue
        rows.append({"id": r["id"], "desc": str(r.get("prompt", ""))[:60],
                     "fwd_m": round(it["location"][0] / 100.0, 1),
                     "lat_m": round(it["location"][1] / 100.0, 1),
                     "size_m": round(float((it.get("scale") or [150])[0]) / 100.0, 1)})
    if len(rows) < 2:
        return {}
    sz = (room or {}).get("size_m", [4.0, 5.0, 2.8])
    instruction = (
        "You are an INTERIOR ARRANGER. This room was rebuilt from the photo; furniture list (world "
        "frame: fwd = metres from camera, lat = metres right):\n" + json.dumps(rows, ensure_ascii=False) +
        f"\nCurrent room (w x d): {sz[0]} x {sz[1]} m. Some pieces were inferred from dark/edge regions "
        "and currently overlap each other or the bed. RULES: 1) pieces CLEARLY VISIBLE in the photo keep "
        "their position EXACTLY (list them in keep_ids); 2) re-place the others sensibly: against walls, "
        "not blocking the window wall or the bed, leaving a walkable path; 3) you may ENLARGE the room "
        "up to +60% per axis to fit everything comfortably.\n"
        'Return ONLY JSON: {"room_w_m": <num>, "room_d_m": <num>, "keep_ids": [<ids>], '
        '"moves": [{"id": <id>, "fwd_m": <num>, "lat_m": <num>, "yaw_deg": <num>}]}')
    try:
        d = _gen_json(client, image, instruction, temperature=0.2, seed=67)
        valid = {r["id"] for r in rows}
        keep = set(int(x) for x in (d.get("keep_ids") or []) if int(x) in valid)
        moves = []
        for m in (d.get("moves") or [])[:12]:
            try:
                mid = int(m.get("id"))
            except Exception:
                continue
            if mid not in valid or mid in keep or mid == hero_id:
                continue
            moves.append({"id": mid, "fwd_m": _clampf(m.get("fwd_m"), 0.5, 30.0, 3.0),
                          "lat_m": _clampf(m.get("lat_m"), -15.0, 15.0, 0.0),
                          "yaw_deg": _clampf(m.get("yaw_deg"), -200.0, 200.0, 0.0)})
        out = {"room_w_m": _clampf(d.get("room_w_m"), sz[0], sz[0] * 1.6, sz[0]),
               "room_d_m": _clampf(d.get("room_d_m"), sz[1], sz[1] * 1.6, sz[1]),
               "keep_ids": sorted(keep), "moves": moves}
        print(f"[Arrange] room {out['room_w_m']:.1f}x{out['room_d_m']:.1f}m, "
              f"keep {out['keep_ids']}, move {[m['id'] for m in moves]}")
        return out
    except Exception as e:
        print(f"[Arrange] failed: {e}", file=sys.stderr)
        return {}


def infer_wall_snap(client, image, results):
    """Wall-snap semantics (AI; breaks the box feel): which furniture in the photo stands against which wall -- depth projection flattens all objects
    onto one distance arc, so nightstands/bookshelves/sofas often float mid-room (critique actually caught 'the chair shouldn't be mid-room')."""
    objs = [{"id": r["id"], "desc": str(r.get("prompt", ""))[:70]} for r in (results or []) if not r.get("imagined")]
    if not objs:
        return []
    instruction = (
        "INDOOR photo. For each reconstructed object decide whether it stands AGAINST a wall in the "
        "photo (touching or within ~20cm) and WHICH wall: 'front' = the far wall facing the camera, "
        "'back' = the near wall behind the camera, 'left'/'right' as seen in the photo, 'none' = "
        "free-standing. Beds (headboard), sofas, shelves, wardrobes, desks, dressers are usually "
        "against a wall; chairs, coffee tables, floor lamps are often free.\nObjects: "
        + json.dumps(objs, ensure_ascii=False) +
        '\nReturn ONLY JSON: {"objects": [{"id": <id>, "wall": "front|back|left|right|none"}]}')
    try:
        d = _gen_json(client, image, instruction, temperature=0.1, seed=41)
        valid = {o["id"] for o in objs}
        out = []
        for m in (d.get("objects") or [])[:16]:
            try:
                oid = int(m.get("id"))
            except Exception:
                continue
            w = str(m.get("wall", "none")).lower()
            if oid in valid and w in ("front", "back", "left", "right"):
                out.append({"id": oid, "wall": w})
        print("[WallSnap] " + (", ".join("%d->%s" % (m["id"], m["wall"]) for m in out) or "all free"))
        return out
    except Exception as e:
        print(f"[WallSnap] failed: {e}", file=sys.stderr)
        return []


def match_lights_to_fixtures(client, image, lights, results):
    """Light placement (AI; review flagged 'why is there a light on the chair / should that glowing orb exist'): match every detected light blob
    to a concrete fixture object, assign true lumens, judge whether the emitter body is visible (glow orb only if visible), drop false detections."""
    if not lights or not results:
        return []
    lrows = [{"id": l.get("id", i + 1), "u": l.get("u"), "v": l.get("v"),
              "intensity": l.get("intensity")} for i, l in enumerate(lights)]
    orows = [{"id": r["id"], "desc": str(r.get("prompt", ""))[:80]} for r in results if not r.get("imagined")]
    instruction = (
        "Match DETECTED LIGHT BLOBS to their physical fixtures in this photo.\n"
        "Lights (image u,v 0-1): " + json.dumps(lrows) + "\nReconstructed objects: " +
        json.dumps(orows, ensure_ascii=False) +
        "\nFor EACH light decide: which object is its fixture (a lamp/shade/screen — a chair or sofa can "
        "NEVER be a fixture); realistic real-world lumens (bedside lamp ~300, floor/arc lamp ~600-900, "
        "shelf accent glow ~150, ceiling light ~1200); color temperature in Kelvin from the light's hue "
        "(match flame 1700, candle 1850, dimmed incandescent 2400, incandescent bulb 2700, halogen 3000, "
        "office fluorescent 4000, daylight tube 5000, overcast daylight 6500); colored_rgb ONLY if the "
        "source is genuinely colored like neon or an RGB strip, else null; the physical size of the "
        "EMITTING surface in cm — this sets shadow softness (bare bulb 2-5, fabric/paper shade 10-25, "
        "lantern 20-40, diffuser panel 30-60); source_len_cm >0 only for tube/strip lights (30-120), 0 "
        "for bulbs; whether the BULB/SHADE itself is visibly glowing in the photo (only then a small glow "
        "orb is justified); keep=false for false detections (reflections, bright posters).\n"
        'Return ONLY JSON: {"lights": [{"id": <light id>, "fixture_object_id": <object id or -1 if it sits '
        'on a wall/ceiling with no reconstructed fixture>, "lumens": <100-2000>, "color_temp_k": '
        '<1700-8000>, "colored_rgb": [r,g,b] or null, "source_radius_cm": <1-60>, "source_len_cm": '
        '<0-120>, "visible_source": <true|false>, "keep": <true|false>, "height_frac": <0-1 of fixture '
        'height where the emitter sits>}]}')
    try:
        d = _gen_json(client, image, instruction, temperature=0.1, seed=31)
        out = []
        valid = {r["id"] for r in results}
        for m in (d.get("lights") or [])[:10]:
            fid = int(m.get("fixture_object_id", -1))
            crgb = m.get("colored_rgb")
            if isinstance(crgb, (list, tuple)) and len(crgb) >= 3:
                crgb = [_clampf(c, 0.0, 1.0, 1.0) for c in crgb[:3]]
                if max(crgb) - min(crgb) < 0.25:
                    crgb = None         # white/warm-white isn't 'coloured' (measured: AI passes [1,1,1] to dodge colour temperature) -- only neon qualifies
            else:
                crgb = None
            mid = int(m.get("id", 0))
            out.append({"id": mid,
                        # u baked into the map: /result light rows carry no u (the executor needs it to spread multi-lamp fixtures)
                        "u": next((r["u"] for r in lrows if r["id"] == mid and r.get("u") is not None), 0.5),
                        "fixture_object_id": fid if fid in valid else -1,
                        "lumens": _clampf(m.get("lumens"), 100.0, 2000.0, 500.0),
                        "color_temp_k": _clampf(m.get("color_temp_k"), 1700.0, 8000.0, 2700.0),
                        "colored_rgb": crgb,
                        "source_radius_cm": _clampf(m.get("source_radius_cm"), 1.0, 60.0, 8.0),
                        "source_len_cm": _clampf(m.get("source_len_cm"), 0.0, 120.0, 0.0),
                        "visible_source": bool(m.get("visible_source", False)),
                        "keep": bool(m.get("keep", True)),
                        "height_frac": _clampf(m.get("height_frac"), 0.1, 1.0, 0.6)})
        print(f"[LightMap] {len(out)} matched: " + ", ".join(
            "%d->obj%d %dlm %dK r%d%s%s" % (m['id'], m['fixture_object_id'], m['lumens'],
                                            m['color_temp_k'], m['source_radius_cm'],
                                            " glow" if m['visible_source'] else "",
                                            "" if m['keep'] else " DROP") for m in out))
        return out
    except Exception as e:
        print(f"[LightMap] failed: {e}", file=sys.stderr)
        return []


def analyze_room(client, image):
    """Indoor room shell P0 (AI): solve the room box and surface materials from perspective/ceiling/material cues (docs/INDOOR_SHELL.md)."""
    instruction = (
        "This photo is INDOORS. Solve the ROOM BOX from perspective cues (vanishing lines, ceiling "
        "height vs furniture scale, wall meetings). Return ONLY JSON:\n"
        "{\n"
        '  "room_size_m": [<width 2-15>, <depth 2-20>, <height 2-6>],   // w = left-right, d = away from camera\n'
        '  "camera_in_room": [<0-1 lateral frac, 0.5 = centered>, <0-1 depth frac, 0 = at the near wall>],\n'
        '  "wall_material": "plaster|wallpaper|brick|concrete|wood|tile",\n'
        '  "wall_color": [r,g,b],\n'
        '  "floor_material": "wood|tile|carpet|concrete|marble",\n'
        '  "floor_color": [r,g,b],\n'
        '  "ceiling_color": [r,g,b],\n'
        '  "openings": [ {"wall": "front|back|left|right", "kind": "window|door|arch", "u_frac": <0-1 '
        'position along that wall, measured from the wall\'s left end when FACING it from inside>, '
        '"w_m": <0.5-6>, "h_m": <0.5-4>, "sill_m": <0-2>, "lux": <light coming THROUGH this opening: '
        'direct sun 30000-50000, bright overcast 3000-8000, evening twilight 30-100, night street 5-20, '
        'dark/curtained 0-3>, "color_temp_k": <of that incoming light: daylight 6500, overcast 7000, '
        'evening sky 8000-10000, sodium streetlight 2200>} ],\n'
        '  "outside_hint": "<a few words: what is outside the windows>",\n'
        '  "indoor_lux": <30-2000 overall interior illuminance>\n'
        "}"
    )
    try:
        d = _gen_json(client, image, instruction, temperature=0.1, seed=23)
        sz = d.get("room_size_m") or [5, 6, 2.8]
        room = {
            "size_m": [_clampf(sz[0], 2.0, 15.0, 5.0), _clampf(sz[1], 2.0, 20.0, 6.0),
                       _clampf(sz[2] if len(sz) > 2 else 2.8, 2.0, 6.0, 2.8)],
            "cam": [_clampf((d.get("camera_in_room") or [0.5, 0.15])[0], 0.0, 1.0, 0.5),
                    _clampf((d.get("camera_in_room") or [0.5, 0.15])[-1], 0.0, 0.9, 0.15)],
            "wall_material": str(d.get("wall_material", "plaster"))[:16],
            "wall_color": [_clampf(c, 0.0, 1.0, 0.8) for c in (d.get("wall_color") or [0.85, 0.82, 0.78])[:3]],
            "floor_material": str(d.get("floor_material", "wood"))[:16],
            "floor_color": [_clampf(c, 0.0, 1.0, 0.5) for c in (d.get("floor_color") or [0.5, 0.4, 0.3])[:3]],
            "ceiling_color": [_clampf(c, 0.0, 1.0, 0.9) for c in (d.get("ceiling_color") or [0.92, 0.9, 0.88])[:3]],
            "outside_hint": str(d.get("outside_hint", ""))[:60],
            "indoor_lux": _clampf(d.get("indoor_lux"), 30.0, 2000.0, 200.0),
            "openings": [],
        }
        for o in (d.get("openings") or [])[:6]:
            w_ = str(o.get("wall", "left")).lower()
            k_ = str(o.get("kind", "window")).lower()
            if w_ in ("front", "back", "left", "right") and k_ in ("window", "door", "arch"):
                room["openings"].append({"wall": w_, "kind": k_,
                                         "u_frac": _clampf(o.get("u_frac"), 0.05, 0.95, 0.5),
                                         "w_m": _clampf(o.get("w_m"), 0.5, 6.0, 1.2),
                                         "h_m": _clampf(o.get("h_m"), 0.5, 4.0, 1.4),
                                         "sill_m": _clampf(o.get("sill_m"), 0.0, 2.0, 0.9),
                                         "lux": _clampf(o.get("lux"), 0.0, 60000.0, 50.0),
                                         "color_temp_k": _clampf(o.get("color_temp_k"), 1700.0, 12000.0, 6500.0)})
        print(f"[Room] {room['size_m']} cam={room['cam']} wall={room['wall_material']} "
              f"floor={room['floor_material']} lux={room['indoor_lux']:.0f} openings={len(room['openings'])}")
        return room
    except Exception as e:
        print(f"[Room] analyze failed: {e}", file=sys.stderr)
        return None


def generate_room_textures(client, image, room, output_dir, task_id):
    """Two photo-matched seamless tiling material patches (wall/floor), reusing the terrain-texture pipeline idea + _make_tileable."""
    urls = {}
    specs = [("wall", f"a seamless square swatch of the room's WALL surface ({room['wall_material']}), "
                      "matched to this photo's wall colour and texture, FLAT frontal view, even lighting, "
                      "no objects, no shadows, no edges"),
             ("floor", f"a seamless square swatch of the room's FLOOR surface ({room['floor_material']}), "
                       "matched to this photo's floor colour and pattern, FLAT top-down view, even "
                       "lighting, no objects, no shadows")]
    for key, prompt in specs:
        try:
            existing = os.path.join(output_dir, f"room_{key}.png")
            if os.path.exists(existing):
                urls[key] = f"/output/{task_id}/room_{key}.png"
                continue
            cfg = genai_types.GenerateContentConfig(
                responseModalities=["IMAGE"],
                imageConfig=genai_types.ImageConfig(aspectRatio="1:1", imageSize="1K"),
            )
            resp = _gen_image_content(client, [prompt, image], cfg)
            if not getattr(resp, "parts", None):       # model occasionally returns an empty response (no exception) -> retry once
                resp = _gen_image_content(client, [prompt, image], cfg)
            for p in (resp.parts or []):
                if getattr(p, "thought", False):
                    continue
                if p.inline_data is not None:
                    raw = os.path.join(output_dir, f"_room_raw_{key}.png")
                    p.as_image().save(raw)
                    _make_tileable(Image.open(raw)).save(existing)
                    try:
                        os.remove(raw)
                    except Exception:
                        pass
                    urls[key] = f"/output/{task_id}/room_{key}.png"
                    print(f"[Room] {key} swatch saved")
                    break
        except Exception as e:
            print(f"[Room] {key} swatch failed: {e}", file=sys.stderr)
    return urls


def generate_window_view(client, image, room, output_dir, task_id):
    """One image of the world outside the window (P1): generated from outside_hint and applied to the outside board; one per task, shared by all openings.
    Idempotent (file present -> reuse); skipped when there are no openings or no hint."""
    if not (room.get("openings") and str(room.get("outside_hint", "")).strip()):
        return None
    existing = os.path.join(output_dir, "window_view.png")
    url = f"/output/{task_id}/window_view.png"
    if os.path.exists(existing):
        return url
    temp_k = int((room["openings"][0] or {}).get("color_temp_k", 6500))
    prompt = ("The view seen THROUGH this room's window: " + str(room["outside_hint"])[:120] +
              ". Match the photo's time of day and light mood; color-grade the WHOLE image "
              f"consistently to that light, around {temp_k}K. Wide framing as seen from a few "
              "metres inside the room. No window frame, no curtains, no interior — only the outside view.")
    try:
        cfg = genai_types.GenerateContentConfig(
            responseModalities=["IMAGE"],
            imageConfig=genai_types.ImageConfig(aspectRatio="16:9", imageSize="1K"),
        )
        resp = _gen_image_content(client, [prompt, image], cfg)
        if not getattr(resp, "parts", None):
            resp = _gen_image_content(client, [prompt, image], cfg)
        for p in (resp.parts or []):
            if getattr(p, "thought", False):
                continue
            if p.inline_data is not None:
                p.as_image().save(existing)
                print("[Room] window view saved")
                return url
    except Exception as e:
        print(f"[Room] window view failed: {e}", file=sys.stderr)
    return None


def infer_midground(client, results, environment):
    """Midground silhouette selection (AI): pick which reconstructed objects suit the midground band (150-600m) -- NATURAL/LARGE types only --
    with counts and scale ranges. Small man-made things (cars/lamps/chairs) don't belong in a wilderness midground -- the AI judges. Failure -> {}."""
    objs = [{"id": r["id"], "desc": str(r.get("prompt", ""))[:90]}
            for r in (results or []) if not r.get("imagined")]
    if not objs:
        return {}
    instruction = (
        "NIGHT/DAY game scene mid-ground dressing. Reconstructed objects:\n" + json.dumps(objs, ensure_ascii=False) +
        "\nWe scatter SCALED SILHOUETTE VARIANTS of some of these objects in the 150-600m mid-ground ring "
        "to give the world depth. Pick ONLY objects that plausibly repeat across this landscape (rocks, "
        "trees, vegetation, large natural forms; courtyard/landmark buildings usually NOT, small man-made "
        "items NEVER). Environment: " + str(environment.get("mood", "")) + ", ground=" +
        str(environment.get("ground_material", "")) +
        '\nReturn ONLY JSON: {"use_object_ids": [<ids, can be empty>], "count": <0-40 total instances>, '
        '"scale_range": [<0.6..1.0>, <1.0..4.0>]}')
    try:
        d = _gen_json(client, None, instruction, temperature=0.2, seed=67)
        ids = [int(i) for i in (d.get("use_object_ids") or []) if any(o["id"] == int(i) for o in objs)]
        return {"use_object_ids": ids[:4],
                "count": int(_clampf(d.get("count"), 0, 40, 12)),
                "scale_range": [_clampf((d.get("scale_range") or [0.8])[0], 0.5, 1.5, 0.8),
                                _clampf((d.get("scale_range") or [0, 2.0])[-1], 1.0, 5.0, 2.0)]}
    except Exception as e:
        print(f"[Midground] inference failed: {e}", file=sys.stderr)
        return {}


def _recenter_layout(layout, lights, terrain):
    """RIGIDLY translate the object group to the platform centre (design decision: preserve the photo composition, no random scatter; only the datum moves).
    AI inference yields relative composition; footprint prediction and the placement pipeline use slightly different distance math, and the group once
    drifted to the platform edge leaving a dead-empty foreground. Translating the whole cluster (lights included) puts its bounding-box centre at (cx, cy);
    the composition doesn't move an inch."""
    if not terrain or not layout:
        return
    try:
        xs = [float(it["location"][0]) for it in layout if it.get("location")]
        ys = [float(it["location"][1]) for it in layout if it.get("location")]
        if not xs:
            return
        dx = float(terrain["cx_m"]) * 100.0 - (min(xs) + max(xs)) / 2.0
        dy = float(terrain["cy_m"]) * 100.0 - (min(ys) + max(ys)) / 2.0
        for it in layout:
            if it.get("location"):
                it["location"][0] = round(it["location"][0] + dx, 1)
                it["location"][1] = round(it["location"][1] + dy, 1)
        for lt in (lights or []):
            if lt.get("location"):
                lt["location"][0] = round(lt["location"][0] + dx, 1)
                lt["location"][1] = round(lt["location"][1] + dy, 1)
    except Exception as e:
        print(f"[Layout] recenter skipped: {e}", file=sys.stderr)


def _task_payload(tid):
    """Placement list + environment lighting for a given task (computed live)."""
    data = _load_task_result(tid) or {}
    S = _scene_scale(data.get("results", []), data.get("fov_deg", 65.0), data.get("img_aspect", 1.333))
    objs = _build_layout(data.get("results", []), data.get("models_3d", []),
                         data.get("fov_deg", 65.0), data.get("img_aspect", 1.333),
                         data.get("camera", {}), S, data.get("terrain"))
    lts = _build_lights(data.get("lights", []), data.get("fov_deg", 65.0),
                        data.get("img_aspect", 1.333), data.get("camera", {}), S)
    _recenter_layout(objs, lts, data.get("terrain"))
    return {"task_id": tid, "objects": objs, "lights": lts,
            # results rows are passed through verbatim (id+prompt+bbox+imagined): executor semantics (storey heights / wall-mounted detection / night-window
            # building identification / basin 'object in front of water' photo evidence) rely on them -- missing prompt = incident #3, missing bbox/imagined = incident #4 (payload dropped fields; methodology sec.5)
            "results": [{"id": r.get("id"), "prompt": r.get("prompt", ""),
                         "bbox": r.get("bbox"), "imagined": bool(r.get("imagined")),
                         "size3_m": r.get("size3_m")}
                        for r in (data.get("results") or [])],
            "environment": data.get("environment", {}),
            "camera": data.get("camera", {}), "hdri": data.get("hdri", ""), "terrain": data.get("terrain"),
            "effects": data.get("effects"), "practicals": data.get("practicals") or [],
            "midground": data.get("midground") or {},
            "room": data.get("room"), "music": data.get("music") or "",
            "ambience": data.get("ambience") or "",
            "sound_sources": data.get("sound_sources") or [],
            "moments": data.get("moments") or {}, "city_block": data.get("city_block") or [],
            "night_windows": data.get("night_windows"), "room_arrange": data.get("room_arrange") or {},
            "light_map": data.get("light_map") or [], "wall_snap": data.get("wall_snap") or [],
            "img_aspect": data.get("img_aspect", 1.333),
            "scene_scale": round(S, 1), "is_indoor": bool(data.get("camera", {}).get("is_indoor"))}


@app.route("/latest")
def latest_route():
    """Polled by UE: returns the placement list of the most recently completed task."""
    tid = _latest_task_id()
    if not tid:
        return jsonify({"task_id": None, "objects": [], "lights": [], "environment": {},
                        "camera": {}, "scene_scale": 10.0, "is_indoor": False})
    return jsonify(_task_payload(tid))


@app.route("/result/<tid>")
def result_route(tid):
    """Pin a specific task (used by UE_PLACE_TASK on the UE side, so /latest can't be hijacked by a newly completed task)."""
    if not _load_task_result(tid):
        return jsonify({"error": "task not found", "task_id": tid}), 404
    return jsonify(_task_payload(tid))


@app.route("/preview")
def preview():
    """Preview: straight to the topology canvas (local sample, zero API)."""
    return redirect(url_for("canvas"))


@app.route("/canvas")
def canvas():
    """The fractured topology canvas. With ?task=<id> uses real task data; otherwise the local sample (preview / zero API)."""
    task_id = request.args.get("task")

    if task_id:
        data = _load_task_result(task_id)
        results = (data or {}).get("results") or []
        glb_by_id = {m["id"]: m.get("pbr_model", "") for m in (data or {}).get("models_3d", [])}
        nodes = [{
            "id": r["id"],
            "label": f"OBJ_{r['id']:02d}",
            "img": r["url"],
            "glb": glb_by_id.get(r["id"], ""),
            "prompt": r["prompt"],
        } for r in results]
        return render_template("canvas.html", nodes=nodes,
                               source_url=(data or {}).get("original_url") or "")

    # No task param -> local sample fallback (preview)
    tid, base, source_url = _find_local_sample()
    nodes = [{
        "id": i + 1,
        "label": f"OBJ_{i + 1:02d}",
        "img": f"/output/{tid}/{base[i % len(base)]}",
        "glb": SAMPLE_GLB,
        "prompt": SAMPLE_PROMPTS[i % len(SAMPLE_PROMPTS)],
    } for i in range(14)] if base else []
    return render_template("canvas.html", nodes=nodes, source_url=source_url or "")


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "未找到文件"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "未选择文件"}), 400

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": f"不支持的文件格式: {ext}"}), 400

    # Read and clamp the object count (homepage slider)
    try:
        max_objects = int(request.form.get("count", DEFAULT_OBJECTS))
    except (TypeError, ValueError):
        max_objects = DEFAULT_OBJECTS
    max_objects = max(MIN_OBJECTS, min(MAX_OBJECTS, max_objects))

    task_id = uuid.uuid4().hex[:12]
    filename = secure_filename(f"{task_id}_{file.filename}")
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(filepath)

    tasks[task_id] = {
        "status": "queued",
        "progress": "Queued…",
        "prompts": [],
        "results": [],
        "models_3d": [],
        "model_errors": None,
        "object_count": max_objects,
        "error": None,
    }

    thread = threading.Thread(target=process_task, args=(task_id, filepath, max_objects), daemon=True)
    thread.start()

    return jsonify({"task_id": task_id, "redirect": url_for("canvas", task=task_id)})


@app.route("/rerun/<tid>", methods=["POST", "GET"])
def rerun(tid):
    """Reuse-rerun (objects + 3D models kept): re-run ALL AI analysis in place under the same tid, skipping extraction + Tripo (zero credits); overwrites result.json."""
    old = _load_task_result(tid)
    if not old:
        return jsonify({"error": "task not found", "task_id": tid}), 404
    fp = os.path.join(app.config["UPLOAD_FOLDER"], os.path.basename(old.get("original_url", "")))
    if not os.path.exists(fp):
        return jsonify({"error": "original photo missing", "path": fp}), 404
    threading.Thread(target=process_task, args=(tid, fp, DEFAULT_OBJECTS),
                     kwargs={"reuse_tid": tid}, daemon=True).start()
    return jsonify({"task_id": tid, "rerun": True})


@app.route("/status/<task_id>")
def get_status(task_id):
    task = tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify({
        "status": task["status"],
        "progress": task.get("progress"),
        "prompts": task.get("prompts", []),
        "results": task.get("results", []),
        "models_3d": task.get("models_3d", []),
        "model_errors": task.get("model_errors"),
        "original_url": task.get("original_url"),
        "error": task.get("error"),
        "errors": task.get("errors"),
    })


@app.route("/uploads/<filename>")
def serve_upload(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


@app.route("/output/<task_id>/<filename>")
def serve_output(task_id, filename):
    return send_from_directory(os.path.join(app.config["OUTPUT_FOLDER"], task_id), filename)


@app.route("/fxsprite/<name>")
def serve_fxsprite(name):
    """FX sprite textures (leaves/petals/ash/scraps; bundled in-repo): UE fetches on demand and imports into any new project (portable)."""
    return send_from_directory(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "ue_library", "FxSprites"), name)


@app.route("/amblife/<name>")
def serve_amblife(name):
    """Ambient-life assets (pedestrian silhouette mask textures etc.; bundled in-repo): UE fetches on demand and imports (portable)."""
    return send_from_directory(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "ue_library", "AmbLife"), name)


@app.route("/moment/<task_id>/<name>", methods=["POST"])
def switch_moment(task_id, name):
    """Four-moment switch button (results page): the server drives ue_remote_bridge to switch the moment in place inside the editor."""
    name = name.lower()
    if name not in MOMENTS + ("base",):
        return jsonify({"error": "unknown moment"}), 400
    data = _load_task_result(task_id)
    if not data or name not in (data.get("moments") or {}):
        return jsonify({"error": "moments not generated for this task"}), 404
    import subprocess
    base_dir = os.path.dirname(os.path.abspath(__file__))
    script = ("import os\n"
              f"os.environ['MOMENT']={name!r}; os.environ['FXR_TID']={task_id!r}\n"
              "exec(compile(open(r'" + os.path.join(base_dir, "dev", "_moment.py") +
              "', encoding='utf-8').read(), '_moment.py', 'exec'), {'__name__': '__main__'})")
    try:
        r = subprocess.run([sys.executable, os.path.join(base_dir, "ue_remote_bridge.py"), script],
                           capture_output=True, text=True, timeout=240, cwd=base_dir)
        ok = "SUCCESS = True" in (r.stdout or "")
        tail = (r.stdout or r.stderr or "")[-400:]
        print(f"[Moment] {task_id} -> {name}: {'ok' if ok else 'FAIL'}")
        return jsonify({"ok": ok, "log": tail})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/capture/<task_id>", methods=["POST"])
def capture_view(task_id):
    """UE posts its SceneCapture2D 'reconstruction view' render here; saved as output/<task>/ue_view.png
    for the web page and the visual-critique loop (bypasses the unreliable editor-viewport screenshot)."""
    out_dir = os.path.join(app.config["OUTPUT_FOLDER"], task_id)
    os.makedirs(out_dir, exist_ok=True)
    data = request.get_data()
    if not data:
        return jsonify({"error": "empty body"}), 400
    with open(os.path.join(out_dir, "ue_view.png"), "wb") as f:
        f.write(data)
    print(f"[Capture] ue_view.png saved for {task_id} ({len(data)} bytes)")
    return jsonify({"ok": True, "url": f"/output/{task_id}/ue_view.png", "bytes": len(data)})


@app.route("/adjustments/<task_id>")
def adjustments_route(task_id):
    """Visual-critique loop: reviews the latest ue_view.png on demand (lazy), cached by (task, round, render-file mtime) --
    the same render is never billed twice, and a fresh screenshot invalidates the cache automatically. Failure always degrades to verdict=ok (loop-safe)."""
    rnd = max(1, min(4, int(request.args.get("round", 1) or 1)))
    render_p = os.path.join(OUTPUT_FOLDER, task_id, "ue_view.png")
    mt = int(os.path.getmtime(render_p)) if os.path.exists(render_p) else 0
    key = (task_id, rnd, mt)
    if key not in _critique_cache:
        try:
            _critique_cache[key] = critique_scene(genai.Client(api_key=API_KEY), task_id, rnd)
        except Exception as e:
            print(f"[Critique] route failed: {e}", file=sys.stderr)
            _critique_cache[key] = {"verdict": "ok", "notes": "critique unavailable",
                                    "objects": [], "exposure_ev_delta": 0.0}
    return jsonify(_critique_cache[key])


# ── Startup ─────────────────────────────────────────────────
if __name__ == "__main__":
    print("🚀 启动 Flask 服务: http://localhost:5001")
    # use_reloader=False: hot reload kills a mid-run pipeline task the moment code changes (paid that tuition)
    app.run(host="0.0.0.0", port=5001, debug=True, use_reloader=False)
