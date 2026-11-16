"""Console entry for the Windows MCP executable (SpineSwissKnifeMCP.exe).

The GUI build is windowed (no stdio), so MCP clients need this console build.
"""

import sys

from spine_swiss_knife import mcp_server

if __name__ == "__main__":
    mcp_server.main()
    sys.exit(0)
