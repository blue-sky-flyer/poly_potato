"""
Single-pass collector for GitHub Actions.

Runs exactly one poll cycle and exits. The workflow commits the updated
DB back to the repo after each run, so data persists across stateless runners.
"""

import time
from collector import init_db, run_once

con = init_db()
run_once(con)
con.close()
