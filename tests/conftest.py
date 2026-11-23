import sys
from pathlib import Path

# Make the application modules importable when pytest is invoked from another
# working directory.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
