"""ICB build orchestrator: runs the split QCOM AOSP build stages for the ICB board."""

import logging

__version__ = "0.1.0"

# Library code logs to "icb_build"; the CLI attaches a file handler per run.
# The NullHandler keeps records from leaking to stderr when no handler is set.
logging.getLogger("icb_build").addHandler(logging.NullHandler())
