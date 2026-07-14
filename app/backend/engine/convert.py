"""
DWG -> DXF conversion via the ODA File Converter CLI.

ODA File Converter is free and headless. Bundle it in the backend image and
point to it with the ODA_CONVERTER env var (or rely on it being on PATH).
If it is not installed, conversion fails with a clear, actionable message
rather than crashing the service.
"""

import os
import shutil
import subprocess
import tempfile

# Common install locations / binary names across platforms.
_CANDIDATES = [
    os.environ.get("ODA_CONVERTER"),
    "ODAFileConverter",
    "ODAFileConverter.exe",
    "/usr/bin/ODAFileConverter",
    "/opt/ODAFileConverter/ODAFileConverter",
]


class ConversionError(Exception):
    pass


def find_converter():
    for cand in _CANDIDATES:
        if not cand:
            continue
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
        found = shutil.which(cand)
        if found:
            return found
    return None


def converter_available():
    return find_converter() is not None


def dwg_to_dxf(dwg_path, out_dir=None, dxf_version="ACAD2018"):
    """
    Convert a single DWG to DXF. Returns the path to the produced DXF, written
    beside the source so the caller can parse it. The temporary working
    directories are always cleaned up, success or failure.

    ODA's CLI works on directories:
      ODAFileConverter <inDir> <outDir> <ver> DXF 0 1 <filter>
    """
    converter = find_converter()
    if not converter:
        raise ConversionError(
            "DWG support needs the ODA File Converter, which isn't installed "
            "on this server. Either install it (free, headless) and set the "
            "ODA_CONVERTER env var, or convert the DWG to DXF yourself "
            "(Revit DXFOUT or https://sharecad.org/) and upload the DXF."
        )

    dwg_path = os.path.abspath(dwg_path)
    in_dir = tempfile.mkdtemp(prefix="dwgin_")
    own_out = out_dir is None
    out_dir = out_dir or tempfile.mkdtemp(prefix="dxfout_")
    base = os.path.splitext(os.path.basename(dwg_path))[0]
    try:
        shutil.copy(dwg_path, os.path.join(in_dir, base + ".dwg"))

        # Filter case must match the copied file's extension: ODA's Qt name
        # filter is case-sensitive on some Linux builds, so "*.DWG" would miss the
        # lowercase "base.dwg" we just wrote and report "no DXF output".
        cmd = [converter, in_dir, out_dir, dxf_version, "DXF", "0", "1", "*.dwg"]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        except subprocess.CalledProcessError as exc:
            raise ConversionError(
                f"ODA File Converter failed: {exc.stderr.decode(errors='ignore')[:300]}")
        except subprocess.TimeoutExpired:
            raise ConversionError("DWG conversion timed out.")

        produced = os.path.join(out_dir, base + ".dxf")
        if not os.path.isfile(produced):
            # ODA sometimes cases the extension differently
            produced = next((os.path.join(out_dir, f) for f in os.listdir(out_dir)
                             if f.lower().endswith(".dxf")), None)
            if produced is None:
                raise ConversionError("Conversion produced no DXF output.")
        # Move the result beside the source (a managed dir the uploads sweep
        # owns) so the temp working dirs can be dropped without losing it.
        final = os.path.splitext(dwg_path)[0] + ".converted.dxf"
        if os.path.exists(final):
            os.remove(final)
        shutil.move(produced, final)
        return final
    finally:
        shutil.rmtree(in_dir, ignore_errors=True)
        if own_out:
            shutil.rmtree(out_dir, ignore_errors=True)
