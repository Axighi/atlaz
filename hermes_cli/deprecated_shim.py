"""Deprecated hermes CLI entry points.

These shims print a deprecation warning and delegate to the real atlaz CLI.
Remove after the migration grace period (suggested: v1.0).
"""

import sys
import warnings


def hermes():
    """Deprecated 'hermes' entry point — delegates to 'atlaz'."""
    warnings.warn(
        "'hermes' is deprecated, use 'atlaz' instead",
        DeprecationWarning,
        stacklevel=2,
    )
    from hermes_cli.main import main  # type: ignore[import-untyped]

    sys.exit(main())


def hermes_agent():
    """Deprecated 'hermes-agent' entry point — delegates to 'atlaz-agent'."""
    warnings.warn(
        "'hermes-agent' is deprecated, use 'atlaz-agent' instead",
        DeprecationWarning,
        stacklevel=2,
    )
    from run_agent import main  # type: ignore[import-untyped]

    sys.exit(main())


def hermes_acp():
    """Deprecated 'hermes-acp' entry point — delegates to 'atlaz-acp'."""
    warnings.warn(
        "'hermes-acp' is deprecated, use 'atlaz-acp' instead",
        DeprecationWarning,
        stacklevel=2,
    )
    from acp_adapter.entry import main  # type: ignore[import-untyped]

    sys.exit(main())
