"""Package runtime with simulator files extracted from an exact reviewed Git revision."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

parser = argparse.ArgumentParser()
parser.add_argument("--model-repo", type=Path, required=True)
parser.add_argument("--model-commit", required=True)
args = parser.parse_args()
commit = subprocess.check_output(
    ["git", "-C", str(args.model_repo), "rev-parse", args.model_commit + "^{commit}"],
    text=True,
).strip()
if len(args.model_commit) != 40 or commit != args.model_commit:
    parser.error("supply an exact full reviewed commit SHA")
root = Path(__file__).resolve().parents[1]
files = {p.name: p.read_bytes() for p in (root / "iot-interview-demo").glob("*.py")}
for name in (
    "__init__.py",
    "adapters.py",
    "telemetry.py",
    "grid_simulator.py",
    "transition_trace.py",
):
    files["business_app/" + name] = subprocess.check_output(
        ["git", "-C", str(args.model_repo), "show", f"{commit}:src/business_app/{name}"]
    )
files["model-provenance.json"] = json.dumps(
    {
        "model_commit": commit,
        "files": {
            k: hashlib.sha256(v).hexdigest()
            for k, v in files.items()
            if k.startswith("business_app/")
        },
    },
    sort_keys=True,
).encode()
output = root / "dist/iot-interview-demo.zip"
output.parent.mkdir(exist_ok=True)
with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
    for name, data in sorted(files.items()):
        info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        archive.writestr(info, data, compresslevel=9)
print("Artifact:", output)
print("SHA-256:", hashlib.sha256(output.read_bytes()).hexdigest())
