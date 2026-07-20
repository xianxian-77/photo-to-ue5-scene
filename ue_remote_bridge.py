"""Drive the RUNNING UE5 editor's Python over UE's NATIVE Remote Execution (no Nwiro).

One-time prereq in UE:  Project Settings -> Plugins -> Python ->
    [x] Enable Remote Execution      (leave the multicast settings at DEFAULT)
  then restart the editor.

Usage:
    python ue_remote_bridge.py <script.py>      # run a file's contents in the editor
    python ue_remote_bridge.py "import unreal; unreal.log('hi')"   # or inline code

Prints SUCCESS + whatever the editor printed (Remote Execution returns stdout,
unlike the old channel which only said {success:true}).
"""
import sys, os, time

import glob

# UE remote-execution client path. The install has moved before (H:\UE_5.7 -> H:\5.7\UE_5.7) and
# will move again on the 5.8 upgrade, so AUTO-DETECT it (known spots first, then a fast bounded glob).
_ENGINE_PY_CANDIDATES = [
    r"H:\5.7\UE_5.7\Engine\Plugins\Experimental\PythonScriptPlugin\Content\Python",
    r"H:\5.8\UE_5.8\Engine\Plugins\Experimental\PythonScriptPlugin\Content\Python",
    r"H:\UE_5.8\Engine\Plugins\Experimental\PythonScriptPlugin\Content\Python",
    r"H:\UE_5.7\Engine\Plugins\Experimental\PythonScriptPlugin\Content\Python",
]


def _find_engine_py():
    for p in _ENGINE_PY_CANDIDATES:
        if os.path.isdir(p):
            return p
    # fast bounded search (single-level wildcards, NOT recursive) across UE drives + plugin-folder moves
    for drive in ("H:", "G:", "F:", "E:", "D:", "C:"):
        for pat in (r"\*\Engine\Plugins\*\PythonScriptPlugin\Content\Python",
                    r"\*\*\Engine\Plugins\*\PythonScriptPlugin\Content\Python",
                    r"\*\Engine\Plugins\*\*\PythonScriptPlugin\Content\Python"):
            hits = glob.glob(drive + pat)
            if hits:
                return hits[0]
    return _ENGINE_PY_CANDIDATES[0]


ENGINE_PY = _find_engine_py()
if ENGINE_PY not in sys.path:
    sys.path.insert(0, ENGINE_PY)
import remote_execution as rexec_mod


def run(code, timeout=15.0):
    rx = rexec_mod.RemoteExecution(rexec_mod.RemoteExecutionConfig())
    rx.start()
    try:
        node = None
        t0 = time.time()
        while time.time() - t0 < timeout:
            nodes = rx.remote_nodes
            if nodes:
                node = nodes[0]
                break
            time.sleep(0.3)
        if not node:
            print("NO_UE_NODE — editor not discovered. Check: Remote Execution enabled + editor running + restarted.")
            return 1
        nid = node.get("node_id") if isinstance(node, dict) else node
        rx.open_command_connection(nid)
        res = rx.run_command(code, unattended=True, exec_mode=rexec_mod.MODE_EXEC_FILE, raise_on_failure=False)
        print("SUCCESS =", res.get("success"))
        out = res.get("output") or []
        if isinstance(out, list):
            for o in out:
                txt = o.get("output", "")
                sys.stdout.write(txt if txt.endswith("\n") else txt + "\n")
        elif out:
            print(out)
        if not res.get("success"):
            print("RESULT =", res.get("result"))
        return 0
    finally:
        try:
            rx.close_command_connection()
        except Exception:
            pass
        rx.stop()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python ue_remote_bridge.py <script.py | inline-code>")
        sys.exit(2)
    arg = sys.argv[1]
    code = open(arg, "r", encoding="utf-8").read() if os.path.isfile(arg) else arg
    sys.exit(run(code))
