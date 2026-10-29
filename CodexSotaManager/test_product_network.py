"""Include the core's offline network policy tests in the Windows regression gate."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "CodexHistorySync"))
from test_sota_network import NetworkPolicyTests, RedirectOriginTests  # noqa: F401,E402
