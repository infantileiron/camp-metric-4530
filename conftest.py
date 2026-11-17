import os
import sys

# Make the backend and the standalone receiver importable from the repo root.
ROOT = os.path.dirname(os.path.abspath(__file__))
for path in (os.path.join(ROOT, 'backend'), os.path.join(ROOT, 'receiver')):
    if path not in sys.path:
        sys.path.insert(0, path)
