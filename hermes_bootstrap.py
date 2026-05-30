# Backward-compat shim: hermes_bootstrap → atlaz_bootstrap
# Deprecated — will be removed in v1.0
import warnings
warnings.warn(
    "Import from 'hermes_bootstrap' is deprecated. Use 'atlaz_bootstrap' instead.",
    DeprecationWarning, stacklevel=2
)
from atlaz_bootstrap import *  # noqa: F401, F403
