import json
from pathlib import Path

import pytest

PADS = json.loads((Path(__file__).with_name("pads.json")).read_text())
PLAY = {"Out": 0.005, "Doubtful": 0.05, "Questionable": 0.5, "Probable": 0.89,
        "Available": 0.87, "none|rep|0": 0.93, "none|norep|0": 0.91}  # fmt: skip


@pytest.fixture
def pads():
    return json.loads(json.dumps(PADS))
