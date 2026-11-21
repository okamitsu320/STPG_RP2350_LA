"""Cycle-addressed logic-analyzer capture for STPG runner bundles (the separate GPL logic-analyzer tool).

Importing this package performs no device I/O. Planning, mock captures and
trace import are offline; only ``AnalyzerSession`` opens a port, and only the
explicitly bound one: inside a run through ``accessory`` (the process the STPG
core starts), or for ``identify_analyzer``, a separate explicit identity
exchange with a new local evidence reservation. This package never drives the
STPG; the core never imports it.
"""

from .address import Address, AddressError, resolve
from .align import align
from .config import (AnalyzerConfig, CaptureConfigError, CaptureRequestSpec, MARKER_SIGNAL,
                     load_analyzer_config, load_capture_request)
from .coordinator import offline_capture
from .identify import identify_analyzer
from .planner import PlanError, plan_capture
from .timeline import BundleTimeline, load_bundle_timeline
from .trace import LaTrace, import_csv, import_lac

__all__ = [
    "Address", "AddressError", "AnalyzerConfig", "BundleTimeline", "CaptureConfigError", "CaptureRequestSpec",
    "LaTrace", "MARKER_SIGNAL", "PlanError", "align", "identify_analyzer", "import_csv", "import_lac", "load_analyzer_config",
    "load_bundle_timeline", "load_capture_request", "offline_capture", "plan_capture", "resolve",
]
