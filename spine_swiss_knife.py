#!/usr/bin/python3
"""
Spine Swiss Knife — launcher.
Runs the spine_swiss_knife package from the same directory.
Usage: python spine_swiss_knife.py
   or: python -m spine_swiss_knife
"""

import subprocess
import sys
import os

if __name__ == "__main__":
    # Run as package: python -m spine_swiss_knife
    script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.exit(subprocess.call([sys.executable, "-m", "spine_swiss_knife"], cwd=script_dir))
