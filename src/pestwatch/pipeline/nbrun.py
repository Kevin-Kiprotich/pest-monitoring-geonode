"""Run selected cells of a Jupyter notebook as a script, with settings injected.

The modelling team keeps working in notebooks. Instead of rewriting them, the
pipeline executes the cells that define and run things, and skips the cells
that only plot or check. Each notebook keeps its settings in plain top-level
assignments (``OUT_DIR = r"F:\\..."``, ``cfg = SimpleNamespace(...)``). Right
after any such assignment, the runner inserts the value the system wants:

    run("chirps.ipynb", cells=[3], params={"OUTPUT_DIR": "/data/...", "COUNTIES": ["Kakamega"]})

Dotted names (``"cfg.out_root"``) set an attribute right after ``cfg`` is
assigned. Lines starting with ``!`` or ``%`` (shell and IPython magics, e.g.
``%pip install``) are replaced by ``pass``.

This module only uses the standard library, so it can be imported both by the
Django side (for tests) and by the model environment.
"""
import ast
import json
import os
import re
import sys
import time

_MAGIC = re.compile(r"^(\s*)[!%].*$")


class Expr(str):
    """A parameter given as Python source, e.g. ``Expr("range(2020, 2027)")``."""


class NotebookError(RuntimeError):
    pass


def load_cells(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)["cells"]


def cell_source(cells, index):
    if index >= len(cells):
        raise NotebookError(f"The notebook has {len(cells)} cells; cell {index} does not exist.")
    cell = cells[index]
    if cell["cell_type"] != "code":
        raise NotebookError(f"Cell {index} is a {cell['cell_type']} cell, not code. "
                            "The notebook has changed: update the cell list in steps.py.")
    src = cell["source"]
    src = "".join(src) if isinstance(src, list) else src
    return "\n".join(_MAGIC.sub(r"\1pass", line) for line in src.splitlines())


def _value_node(value):
    text = str(value) if isinstance(value, Expr) else repr(value)
    try:
        return ast.parse(text, mode="eval").body
    except SyntaxError:
        raise NotebookError(f"Parameter value {text[:80]!r} cannot be written as Python source.")


def _assigned_names(stmt):
    targets = []
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets = [stmt.target]
    names = []
    for t in targets:
        for node in ast.walk(t):
            if isinstance(node, ast.Name):
                names.append(node.id)
    return names


def inject(tree, params):
    """Insert ``name = value`` after each top-level statement that assigns ``name``."""
    if not params:
        return tree, set()
    plain = {k: v for k, v in params.items() if "." not in k}
    dotted = {}
    for k, v in params.items():
        if "." in k:
            base, attr = k.split(".", 1)
            dotted.setdefault(base, {})[attr] = v
    used, body = set(), []
    for stmt in tree.body:
        body.append(stmt)
        for name in _assigned_names(stmt):
            if name in plain:
                body.append(ast.Assign(targets=[ast.Name(id=name, ctx=ast.Store())], value=_value_node(plain[name])))
                used.add(name)
            for attr, value in dotted.get(name, {}).items():
                target = ast.Attribute(value=ast.Name(id=name, ctx=ast.Load()), attr=attr, ctx=ast.Store())
                body.append(ast.Assign(targets=[target], value=_value_node(value)))
                used.add(f"{name}.{attr}")
    tree.body = body
    return ast.fix_missing_locations(tree), used


def only_definitions(tree, names):
    """Keep the imports and the functions/classes called ``names`` (e.g. one helper redefined in a run cell)."""
    keep = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            keep.append(stmt)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and stmt.name in names:
            keep.append(stmt)
    missing = set(names) - {s.name for s in keep if hasattr(s, "name")}
    if missing:
        raise NotebookError(f"Definitions not found in the cell: {', '.join(sorted(missing))}")
    tree.body = keep
    return tree


def new_namespace(name):
    ns = {"__name__": "__notebook__", "__file__": name, "__builtins__": __builtins__}
    ns["display"] = lambda *a, **k: print(*a)
    return ns


def run(path, cells, params=None, ns=None, only=None, log=print):
    """Execute ``cells`` (indices) of the notebook at ``path`` in ``ns`` and return the namespace.

    ``only`` maps a cell index to a list of function/class names: only those
    definitions (and the cell's imports) are executed from that cell.
    """
    name = os.path.basename(path)
    nb = load_cells(path)
    ns = ns if ns is not None else new_namespace(path)
    params = dict(params or {})
    pending = set(params)
    for index in cells:
        src = cell_source(nb, index)
        try:
            tree = ast.parse(src, filename=f"{name}[{index}]")
        except SyntaxError as e:
            raise NotebookError(f"{name} cell {index} is not valid Python: {e}")
        if only and index in only:
            tree = only_definitions(tree, only[index])
        tree, used = inject(tree, params)
        pending -= used
        log(f"--- {name} cell {index} ---")
        sys.stdout.flush()
        t0 = time.time()
        exec(compile(tree, f"<{name} cell {index}>", "exec"), ns)
        log(f"--- {name} cell {index} done in {time.time() - t0:.0f} s ---")
    if pending:
        # A setting the notebook no longer assigns is probably a renamed one: say so loudly.
        log(f"WARNING: {name}: these settings were never assigned in the cells run, so they were not applied: "
            f"{', '.join(sorted(pending))}")
    return ns
