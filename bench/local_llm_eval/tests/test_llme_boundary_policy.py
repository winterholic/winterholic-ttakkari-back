"""Reads/imports that must stay outside the candidate boundary after the 4.2 relaxations.

Reachability only: each source names a forbidden member (or calls the guarded
constructor with a descriptor that does not exist). No payload, no side effect.
"""

import pytest
from local_llm_eval.runner import run_check

STILL_DENIED = {
    "dunder store": "class A:\n    pass\nA.__name__ = 'x'\n",
    "function globals": "def f():\n    pass\nx = f.__globals__\n",
    "instance dict": "class A:\n    pass\nx = A().__dict__\n",
    "raw getattribute": "x = object.__getattribute__\n",
    "raw setattr": "x = object.__setattr__\n",
    "subclasses": "x = object.__subclasses__\n",
    "bases": "x = int.__bases__\n",
    "bound self": "x = len.__self__\n",
    "string getattr of a denied dunder": "x = getattr(len, '__self__')\n",
    "sys.modules": "import sys\nx = sys.modules\n",
    "sys._getframe": "import sys\nx = sys._getframe\n",
    "sys.stdout": "import sys\nx = sys.stdout\n",
    "sys.exit": "import sys\nx = sys.exit\n",
    "threading.Thread": "import threading\nx = threading.Thread\n",
    "threading.Timer": "import threading\nx = threading.Timer\n",
    "shutil.make_archive": "import shutil\nx = shutil.make_archive\n",
    "shutil.chown": "import shutil\nx = shutil.chown\n",
    "os.environ internals": "import os\nx = os._exit\n",
    "vars of a module": "import os\nx = vars(os)\n",
    "vars of a class": "class A:\n    pass\nx = vars(A)\n",
    "vars without argument": "x = vars()\n",
    "io.FileIO": "import io\nx = io.FileIO\n",
    "io.open with a descriptor number": "import io\nx = io.open(1000000)\n",
    "builtin open with a descriptor number": "x = open(1000000)\n",
    "traceback": "import traceback\nx = traceback.walk_stack\n",
}


@pytest.mark.parametrize("name", sorted(STILL_DENIED))
def test_boundary_members_stay_denied(tmp_path, name):
    (tmp_path / "cand.py").write_text(STILL_DENIED[name])
    run = run_check(tmp_path, "denied", "import cand\nrec('reached', 1)")
    denied = run.status == "integrity" or bool(run.obs.get("__tampered__"))
    assert denied, (name, run.status, run.detail, run.obs)
