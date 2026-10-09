"""Build a reproducible, source-only ZIP for app.lambda_handler."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
output = ROOT / "dist" / "iot-digital-twin-ingest.zip"
output.parent.mkdir(exist_ok=True)
with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
    for source in sorted((ROOT / "iot-digital-twin-ingest").glob("*.py")):
        info = ZipInfo(source.name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        archive.writestr(info, source.read_bytes(), compresslevel=9)
print(output)
