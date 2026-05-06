#!/usr/bin/env python3
"""Frontend lag reports must be independent of browser/server clock skew."""

import re
import subprocess
import textwrap
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FrontendLagClockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "frontend" / "index.html").read_text()

    def _lag_clock_js(self):
        match = re.search(
            r"// LAG_CLOCK_BEGIN\n(?P<body>.*?)\n// LAG_CLOCK_END",
            self.source,
            re.S,
        )
        self.assertIsNotNone(match, "lag clock calibration block not found")
        return match.group("body")

    def test_lag_report_does_not_use_raw_wall_clock_delta(self):
        compact = re.sub(r"\s+", "", self.source)
        self.assertNotIn("constage=Date.now()-capMs", compact)
        self.assertIn("constage=_linkAgeFromServerTimestamp(Date.now(),capMs)", compact)

    def test_clock_skew_is_calibrated_to_queue_growth(self):
        script = textwrap.dedent(
            f"""
            let _lagClockOffsetMs = null;
            let metricRtt = 0;
            {self._lag_clock_js()}
            function assertEq(actual, expected, label) {{
              if (actual !== expected) {{
                throw new Error(`${{label}}: expected ${{expected}}, got ${{actual}}`);
              }}
            }}

            // Browser clock is 10s behind the server. First packet establishes
            // the baseline and must not look like a giant negative/positive lag.
            assertEq(_linkAgeFromServerTimestamp(90020, 100000), 1, 'initial negative skew');

            // Same skew, downstream queue grew by 25ms over the best observation.
            assertEq(_linkAgeFromServerTimestamp(90145, 100100), 25, 'queue growth under skew');

            // A better packet lowers the baseline instead of preserving stale lag.
            assertEq(_linkAgeFromServerTimestamp(90210, 100205), 1, 'baseline refresh');

            // Browser clock is far ahead after reconnect; new session starts fresh.
            _lagClockOffsetMs = null;
            assertEq(_linkAgeFromServerTimestamp(250050, 100000), 1, 'initial positive skew');
            assertEq(_linkAgeFromServerTimestamp(250300, 100100), 150, 'positive skew queue growth');

            // Invalid timestamps are treated as clear path, not congestion.
            _lagClockOffsetMs = null;
            assertEq(_linkAgeFromServerTimestamp(NaN, 100000), 1, 'invalid now');
            """
        )
        subprocess.run(["node", "-e", script], check=True)


if __name__ == "__main__":
    unittest.main()
