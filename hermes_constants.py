# Backward-compat shim: hermes_constants → atlaz_constants
# Deprecated — will be removed in v1.0
import warnings
warnings.warn(
    "Import from 'hermes_constants' is deprecated. Use 'atlaz_constants' instead.",
    DeprecationWarning, stacklevel=2
)
from atlaz_constants import *  # noqa: F401, F403
