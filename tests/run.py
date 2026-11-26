import sys
from pathlib import Path

from runner import report, run

if __name__ == "__main__":
    suite = run(sys.argv[1:])
    sys.exit(report(suite))
