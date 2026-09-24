"""Runner-owned pytest evidence; model tests report metrics via record_property."""
import json
import os
from pathlib import Path

_results = {}


def pytest_runtest_logreport(report):
    result = _results.setdefault(report.nodeid, {"calls": 0, "passed": True, "metrics": None})
    if report.failed or report.skipped or hasattr(report, "wasxfail"):
        result["passed"] = False
    if report.when == "call":
        result["calls"] += 1
        properties = [value for key, value in report.user_properties if key == "golden_metrics"]
        if len(properties) == 1:
            try:
                result["metrics"] = json.loads(properties[0])
            except (TypeError, ValueError):
                result["passed"] = False


def pytest_sessionfinish(session, exitstatus):
    Path(os.environ["TT_GOLDEN_REPORT"]).write_text(json.dumps({
        "exit_code": int(exitstatus), "collected": session.testscollected,
        "tests": _results,
    }, indent=2) + "\n")
