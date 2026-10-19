import os
import sys

if __package__ in (None, ""):
    # Run as a plain script (e.g. `python pyrx/__main__.py` or an IDE "run file"):
    # make the project folder importable and use absolute imports.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from pyrx.app import main
else:
    from .app import main

if __name__ == "__main__":
    sys.exit(main())
