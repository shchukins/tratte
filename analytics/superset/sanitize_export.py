"""Sanitize this SYNTHETIC Superset export, using PyYAML bundled with Superset.

Run only on the isolated demo export. This is not an anonymizer for private SQL,
chart titles, filters or database exports. Review the resulting JSON before Git.
"""

import json
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import yaml


def sanitize(source: Path, destination: Path):
    with ZipFile(source) as src, ZipFile(destination, "w", ZIP_DEFLATED) as dst:
        for name in src.namelist():
            if name.endswith("/"):
                continue
            path = name.split("/", 1)[1]
            if path.split("/")[0] not in {
                "metadata.yaml", "databases", "datasets", "charts", "dashboards",
            }:
                raise ValueError("Unexpected object in synthetic export")
            obj = yaml.safe_load(src.read(name))
            if path.startswith("databases/"):
                # Discard ALL actual connection fields, encrypted extras and SSH config.
                obj = {"database_name": "Tratte BI snapshot", "uuid": obj["uuid"],
                       "version": "1.0.0", "extra": {},
                       "sqlalchemy_uri": "postgresql://tratte_bi_reader@CHANGE_ME/tratte_bi",
                       "allow_dml": False, "allow_ctas": False, "allow_cvas": False,
                       "expose_in_sqllab": False, "allow_file_upload": False}
            dst.writestr("tratte/"+path, json.dumps(obj, ensure_ascii=False, indent=2)+"\n")


if __name__ == "__main__":
    sanitize(Path(sys.argv[1]), Path(sys.argv[2]))
