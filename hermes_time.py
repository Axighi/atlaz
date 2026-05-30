# Backward-compat shim: hermes_time → atlaz_time
# Deprecated — will be removed in v1.0
import warnings
warnings.warn(
    "Import from 'hermes_time' is deprecated. Use 'atlaz_time' instead.",
    DeprecationWarning, stacklevel=2
)
from atlaz_time import *  # noqa: F401, F403
