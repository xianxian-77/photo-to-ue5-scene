"""Deploy the repo-bundled UE material content into ANY UE project (pure file ops, no editor needed).

The downloaded master materials (MA_YX_Blend layered blend + WATER functions + bundled textures) are
committed IN this repo at ue_library/Content/ — their .uasset package paths are baked /Game/Tool/..., so
they must land under a project's Content/. This script copies that content tree into a target project's
Content/ so the assets load at /Game/Tool/... and the pipeline can instance MA_YX_Blend. A GitHub clone is
fully self-contained: others run this and get the materials — no separate download, no plugin, no editor.
Idempotent — safe to re-run per project.

Usage:
    python ue_deploy_library.py [<project_dir>]      # default: the current TEST5_71 project

One-time packaging (how ue_library/Content gets built, needs UE once): docs/MATERIAL_LIBRARY.md.
"""
import os, sys, shutil

# The library ships inside the repo (ue_library/Content) -- self-contained: a GitHub clone has it, no extra download/plugin.
LIBRARY_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ue_library")
DEFAULT_PROJECT = r"H:\5.7TEST\TEST5_71"


def deploy(project_dir, library_root=LIBRARY_ROOT):
    """Copy ue_library/Content/* into <project>/Content/* so assets mount at /Game/...  Pure on-disk file
    ops — does NOT need the editor (it picks them up on next start / asset-registry scan). Returns the list
    of top-level folders deployed. Idempotent: skips folders already present in the project."""
    src = os.path.join(library_root, "Content")
    if not os.path.isdir(src):
        raise FileNotFoundError(
            "material content not found at %s — do the one-time UE packaging first "
            "(see docs/MATERIAL_LIBRARY.md)." % src)
    if not os.path.isdir(project_dir):
        raise FileNotFoundError("project dir not found: %s" % project_dir)
    if not any(f.lower().endswith(".uproject") for f in os.listdir(project_dir)):
        raise FileNotFoundError("no .uproject in %s (not a UE project)" % project_dir)

    dst_content = os.path.join(project_dir, "Content")
    os.makedirs(dst_content, exist_ok=True)
    deployed = []
    for item in os.listdir(src):                       # e.g. "Tool"
        s = os.path.join(src, item)
        if not os.path.isdir(s):
            continue
        d = os.path.join(dst_content, item)
        if os.path.isdir(d):
            print("[lib] %s already in project Content (skip)" % item)
        else:
            shutil.copytree(s, d)
            print("[lib] copied Content/%s -> %s" % (item, d))
            deployed.append(item)
    print("[lib] done — restart the editor (or rescan /Game) so assets at /Game/Tool/... appear.")
    return deployed


if __name__ == "__main__":
    deploy(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROJECT)
