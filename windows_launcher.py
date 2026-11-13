"""Windows entry point: start the shared-network app when launched by double-click."""
import os

os.environ.setdefault("EFILE_NETWORK", "1")

import server

if __name__ == "__main__":
    server.main()
