# Backward-compat shim: hermes_logging → atlaz_logging
# Deprecated — will be removed in v1.0
import warnings
warnings.warn(
    "Import from 'hermes_logging' is deprecated. Use 'atlaz_logging' instead.",
    DeprecationWarning, stacklevel=2
)
from atlaz_logging import *  # noqa: F401, F403
