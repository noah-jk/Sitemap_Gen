"""Golden-file tests: the CLI's full output must match tests/golden byte for byte.

After an intended output change, regenerate with `pytest --update-golden` and
review the diff in git before committing.
"""
import difflib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden"

CASES = {
    "example-default": ("sitemap-example.md", []),
    "edge-default": ("tests/fixtures/edge-cases.md", []),
    "edge-sidebar-dropdown": ("tests/fixtures/edge-cases.md", ["--nav-style", "sidebar", "--dropdown-depth", "2"]),
    "edge-mega-sticky": ("tests/fixtures/edge-cases.md", ["--mega-menu", "--sticky-header"]),
}


def files_in(d: Path) -> dict:
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("case", CASES)
def test_cli_output_matches_golden(case, tmp_path, request):
    src, flags = CASES[case]
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(ROOT / "sitemap2proto.py"), str(ROOT / src), str(out), *flags],
                   check=True, capture_output=True)
    golden = GOLDEN / case
    if request.config.getoption("--update-golden"):
        shutil.rmtree(golden, ignore_errors=True)
        shutil.copytree(out, golden)
        return
    got, want = files_in(out), files_in(golden)
    assert sorted(got) == sorted(want), "different set of output files"
    for name in want:
        if got[name] != want[name]:
            diff = difflib.unified_diff(want[name].decode().splitlines(), got[name].decode().splitlines(),
                                        f"golden/{name}", f"output/{name}", lineterm="", n=2)
            pytest.fail(f"{name} differs:\n" + "\n".join(list(diff)[:60]))
