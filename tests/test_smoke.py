import subprocess
import sys

import civ6_ai


def test_package_imports():
    assert civ6_ai.__version__


def test_game_bridge_loads_without_its_server_dependencies():
    code = (
        "import sys; import civ_mcp.game_state; import civ_mcp.end_turn; "
        "assert not {'mcp', 'fastapi', 'anthropic', 'uvicorn'} & set(sys.modules)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
