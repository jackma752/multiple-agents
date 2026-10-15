"""Historical real-call helpers refuse before looking at credentials or launching."""
import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("relative,args", [
    ("tools/codex_one_call.py", []),
    ("tools/demo/codex_verify.py", ["--mode", "real", "--i-understand-this-spends-quota"]),
    ("tools/probes/capability_probe.py", ["--probe", "readonly-write", "--mode", "real", "--i-understand-this-spends-quota"]),
])
def test_real_helpers_refuse_without_side_effects(relative, args, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("suspended_tool", ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Imports above may read Python source; the actual command must touch neither
    # files nor processes. This includes preflight credential/write probes.
    launch = Mock(side_effect=AssertionError("real process attempted"))
    opened = Mock(side_effect=AssertionError("credential or output file touched"))
    monkeypatch.setattr("subprocess.Popen", launch)
    monkeypatch.setattr("builtins.open", opened)
    monkeypatch.setattr("sys.argv", [relative, *args])
    assert module.main() == 2
    assert "native_subagents_uncontrolled" in capsys.readouterr().out
    launch.assert_not_called()
    opened.assert_not_called()
