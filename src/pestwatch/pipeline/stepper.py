"""Run one pipeline step in the model environment.

    /opt/pestmodels/bin/python stepper.py <spec.json>

Started by the orchestrator (``pestwatch.orchestrator``) as a subprocess, so the
model libraries (numpy 2, scikit-learn, xgboost, PyTorch, Earth Engine, cdsapi)
never load into GeoNode's own Python. Writes ``spec["result_path"]`` and exits
with 0 on success, 1 on failure. Everything printed goes to the run log.
"""
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))


def main(spec_path):
    with open(spec_path) as f:
        spec = json.load(f)
    os.environ.setdefault("MPLBACKEND", "Agg")
    os.environ.setdefault("TQDM_DISABLE", "1")
    sys.path.insert(0, HERE)
    os.chdir(spec["run_dir"])
    t0 = time.time()
    print(f"==== step {spec['step']} started {time.strftime('%Y-%m-%d %H:%M:%S')} ====", flush=True)
    import steps
    try:
        result = {"ok": True, **(steps.run(spec) or {})}
    except steps.StepSkipped as e:
        result = {"ok": True, "skipped": str(e)}
        print(f"Skipped: {e}", flush=True)
    except BaseException as e:                       # noqa: B902 - report everything, including SystemExit
        traceback.print_exc()
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"[:4000]}
    result["seconds"] = round(time.time() - t0)
    tmp = spec["result_path"] + ".part"
    with open(tmp, "w") as f:
        json.dump(result, f, indent=2, default=str)
    os.replace(tmp, spec["result_path"])
    print(f"==== step {spec['step']} {'finished' if result['ok'] else 'FAILED'} in {result['seconds']} s ====", flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
