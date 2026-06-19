"""
convert.py — DWG -> DXF via the optional ODA File Converter CLI.

The valuable, deterministic output here is the *graceful degradation*: when the
converter isn't installed, detection reports false and conversion fails with an
actionable message rather than crashing. We force the "absent" state by
emptying the candidate list, so the result is independent of whatever is on the
test machine.
"""
import unittest

from engine import dwg_to_dxf, converter_available, ConversionError
from engine import convert


class ConverterAbsentTest(unittest.TestCase):
    def setUp(self):
        self._saved = convert._CANDIDATES
        convert._CANDIDATES = [None]      # nothing to find

    def tearDown(self):
        convert._CANDIDATES = self._saved

    def test_reports_unavailable(self):
        self.assertFalse(converter_available())

    def test_conversion_raises_actionable_error(self):
        with self.assertRaises(ConversionError) as ctx:
            dwg_to_dxf("/whatever/plan.dwg")
        msg = str(ctx.exception)
        self.assertIn("ODA File Converter", msg)
        self.assertIn("DXF", msg)         # tells the user the workaround


if __name__ == "__main__":
    unittest.main()
