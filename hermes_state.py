# Backward-compat shim: hermes_state → atlaz_state
# Deprecated — will be removed in v1.0
import warnings
warnings.warn(
    "Import from 'hermes_state' is deprecated. Use 'atlaz_state' instead.",
    DeprecationWarning, stacklevel=2
)
from atlaz_state import *  # noqa: F401, F403
