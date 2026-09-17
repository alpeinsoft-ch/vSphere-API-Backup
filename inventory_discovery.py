#!/usr/bin/env python3
"""Read-only vSphere inventory discovery wrapper.

Default behavior prints only the configured target VM. Use --show-all for a
full inventory printout.
"""

import sys

from safe_vsphere_backup import main


if __name__ == "__main__":
    raise SystemExit(main(["inventory", *sys.argv[1:]]))
