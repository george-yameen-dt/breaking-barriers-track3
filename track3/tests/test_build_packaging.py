"""The AgentCore build copies a fixed list of files: a module the runtime imports but the list misses crashes the runtime on start."""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (ROOT / "agentcore" / "build_swarm.sh").read_text()


def _top_level_imports(path: Path) -> set:
    found = set()
    for node in ast.parse(path.read_text()).body:  # module level only: lazy imports inside functions are not loaded at start
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_every_module_the_runtime_imports_at_start_is_packaged():
    copied = set(re.findall(r"\.\./(\w+)\.py", SCRIPT)) | set(re.findall(r"cp -R ((?:\.\./\w+ ?)+) build_swarm", SCRIPT) and
                                                              re.findall(r"\.\./(\w+)(?= |\n)", re.search(r"cp -R (.*) build_swarm/\n", SCRIPT).group(1) + "\n"))
    copied |= {"main"}
    local = {p.stem for p in ROOT.glob("*.py")} | {p.name for p in ROOT.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    files = [ROOT / "agentcore" / "main.py"] + [ROOT / f"{m}.py" for m in copied if (ROOT / f"{m}.py").exists()] + list((ROOT / "agents").glob("*.py"))
    missing = {(f.name, m) for f in files for m in _top_level_imports(f) if m in local and m not in copied}
    assert not missing, f"imported at start but not copied by build_swarm.sh: {sorted(missing)}"
