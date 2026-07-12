# Test package for lms_cover_display. Run from the repo root:
#   python3 -m unittest discover -s tests -v
# Pure-stdlib unittest (no pytest dependency); pytest collects these too.
import os
import sys

# Headless SDL before any pygame import (the module imports pygame lazily,
# but tests that touch Display/dhash need the dummy drivers set first).
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
