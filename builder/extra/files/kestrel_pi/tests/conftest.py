"""Make the package importable when running pytest from a checkout (no install
needed): put the project root - the directory holding ``kestrel_pi/`` - on
sys.path."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
