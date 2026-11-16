#!/usr/bin/python3
"""
Spine Swiss Knife — launcher.
Runs the spine_swiss_knife package from the same directory, from any working
directory.

Usage: python spine_swiss_knife.py            (GUI)
       python spine_swiss_knife.py --mcp      (MCP server on stdio)
       python spine_swiss_knife.py --cli ...  (command line tools)
   or: python -m spine_swiss_knife            (from this directory)
"""

import os
import runpy
import sys

if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    runpy.run_module("spine_swiss_knife", run_name="__main__", alter_sys=True)
