"""
Backward-compatibility shim for the renamed ``atlaz_cli`` package.

``hermes_cli`` was renamed to ``atlaz_cli`` in the atlaz rebrand. This shim
issues a deprecation warning and then redirects all imports to ``atlaz_cli``,
so that existing code using ``import hermes_cli`` or ``from hermes_cli import
...`` continues to work.

New code should use ``atlaz_cli`` directly. This shim will be removed in a
future release.
"""

import warnings
import os

warnings.warn(
    "Import from 'hermes_cli' is deprecated. Use 'atlaz_cli'.",
    DeprecationWarning,
    stacklevel=2,
)

# Redirect submodule lookups to atlaz_cli/ directory so that
# ``from hermes_cli.commands import ...`` etc. still resolve.
__path__ = [
    os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "atlaz_cli"))
]

# Re-export everything from atlaz_cli for ``import hermes_cli`` → attribute access
try:
    import atlaz_cli as _atlaz_cli
    import sys as _sys
    _globals = globals()
    for _attr in dir(_atlaz_cli):
        if not _attr.startswith("_"):
            _globals[_attr] = getattr(_atlaz_cli, _attr)
    del _attr, _globals, _sys, _atlaz_cli
except ImportError:
    pass  # atlaz_cli not installed yet
