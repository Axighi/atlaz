"""Tests for atlaz_constants module."""

import os
from pathlib import Path

import pytest

import atlaz_constants
from atlaz_constants import (
    VALID_REASONING_EFFORTS,
    get_default_atlaz_root,
    is_container,
    parse_reasoning_effort,
    secure_parent_dir,
)


class TestGetDefaultAtlazRoot:
    """Tests for get_default_atlaz_root() — Docker/custom deployment awareness."""

    def test_no_atlaz_home_returns_native(self, tmp_path, monkeypatch):
        """When ATLAZ_HOME is not set, returns ~/.atlaz."""
        monkeypatch.delenv("ATLAZ_HOME", raising=False)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        assert get_default_atlaz_root() == tmp_path / ".atlaz"

    def test_atlaz_home_is_native(self, tmp_path, monkeypatch):
        """When ATLAZ_HOME = ~/.atlaz, returns ~/.atlaz."""
        native = tmp_path / ".atlaz"
        native.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("ATLAZ_HOME", str(native))
        assert get_default_atlaz_root() == native

    def test_atlaz_home_is_profile(self, tmp_path, monkeypatch):
        """When ATLAZ_HOME is a profile under ~/.atlaz, returns ~/.atlaz."""
        native = tmp_path / ".atlaz"
        profile = native / "profiles" / "coder"
        profile.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("ATLAZ_HOME", str(profile))
        assert get_default_atlaz_root() == native

    def test_atlaz_home_is_docker(self, tmp_path, monkeypatch):
        """When ATLAZ_HOME points outside ~/.atlaz (Docker), returns ATLAZ_HOME."""
        docker_home = tmp_path / "opt" / "data"
        docker_home.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("ATLAZ_HOME", str(docker_home))
        assert get_default_atlaz_root() == docker_home

    def test_atlaz_home_is_custom_path(self, tmp_path, monkeypatch):
        """Any ATLAZ_HOME outside ~/.atlaz is treated as the root."""
        custom = tmp_path / "my-atlaz-data"
        custom.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("ATLAZ_HOME", str(custom))
        assert get_default_atlaz_root() == custom

    def test_docker_profile_active(self, tmp_path, monkeypatch):
        """When a Docker profile is active (ATLAZ_HOME=<root>/profiles/<name>),
        returns the Docker root, not the profile dir."""
        docker_root = tmp_path / "opt" / "data"
        profile = docker_root / "profiles" / "coder"
        profile.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("ATLAZ_HOME", str(profile))
        assert get_default_atlaz_root() == docker_root


class TestIsContainer:
    """Tests for is_container() — Docker/Podman detection."""

    def _reset_cache(self, monkeypatch):
        """Reset the cached detection result before each test."""
        monkeypatch.setattr(atlaz_constants, "_container_detected", None)

    def test_detects_dockerenv(self, monkeypatch, tmp_path):
        """/.dockerenv triggers container detection."""
        self._reset_cache(monkeypatch)
        monkeypatch.setattr(os.path, "exists", lambda p: p == "/.dockerenv")
        assert is_container() is True

    def test_detects_containerenv(self, monkeypatch, tmp_path):
        """/run/.containerenv triggers container detection (Podman)."""
        self._reset_cache(monkeypatch)
        monkeypatch.setattr(os.path, "exists", lambda p: p == "/run/.containerenv")
        assert is_container() is True

    def test_detects_cgroup_docker(self, monkeypatch, tmp_path):
        """/proc/1/cgroup containing 'docker' triggers detection."""
        import builtins
        self._reset_cache(monkeypatch)
        monkeypatch.setattr(os.path, "exists", lambda p: False)
        cgroup_file = tmp_path / "cgroup"
        cgroup_file.write_text("12:memory:/docker/abc123\n")
        _real_open = builtins.open
        monkeypatch.setattr("builtins.open", lambda p, *a, **kw: _real_open(str(cgroup_file), *a, **kw) if p == "/proc/1/cgroup" else _real_open(p, *a, **kw))
        assert is_container() is True

    def test_negative_case(self, monkeypatch, tmp_path):
        """Returns False on a regular Linux host."""
        import builtins
        self._reset_cache(monkeypatch)
        monkeypatch.setattr(os.path, "exists", lambda p: False)
        cgroup_file = tmp_path / "cgroup"
        cgroup_file.write_text("12:memory:/\n")
        _real_open = builtins.open
        monkeypatch.setattr("builtins.open", lambda p, *a, **kw: _real_open(str(cgroup_file), *a, **kw) if p == "/proc/1/cgroup" else _real_open(p, *a, **kw))
        assert is_container() is False

    def test_caches_result(self, monkeypatch):
        """Second call uses cached value without re-probing."""
        monkeypatch.setattr(atlaz_constants, "_container_detected", True)
        assert is_container() is True
        # Even if we make os.path.exists return False, cached value wins
        monkeypatch.setattr(os.path, "exists", lambda p: False)
        assert is_container() is True


class TestParseReasoningEffort:
    """Tests for parse_reasoning_effort() — string → reasoning config dict."""

    @pytest.mark.parametrize("value", ["", "   ", "\t", "\n"])
    def test_empty_or_whitespace_returns_none(self, value):
        """Empty / whitespace-only input falls back to caller default (None)."""
        assert parse_reasoning_effort(value) is None

    def test_none_disables_reasoning(self):
        """The literal "none" disables reasoning explicitly."""
        assert parse_reasoning_effort("none") == {"enabled": False}

    @pytest.mark.parametrize("level", list(VALID_REASONING_EFFORTS))
    def test_each_valid_level(self, level):
        """Every level listed in VALID_REASONING_EFFORTS is accepted as-is."""
        assert parse_reasoning_effort(level) == {"enabled": True, "effort": level}

    @pytest.mark.parametrize(
        "raw, expected_effort",
        [
            ("MEDIUM", "medium"),
            ("High", "high"),
            ("  low  ", "low"),
            ("\tXHIGH\n", "xhigh"),
            ("None", False),
        ],
    )
    def test_case_and_whitespace_normalized(self, raw, expected_effort):
        """Mixed case and surrounding whitespace are normalized before lookup."""
        result = parse_reasoning_effort(raw)
        if expected_effort is False:
            assert result == {"enabled": False}
        else:
            assert result == {"enabled": True, "effort": expected_effort}

    @pytest.mark.parametrize(
        "value",
        ["bogus", "very-high", "max", "0", "off", "true", "default"],
    )
    def test_unknown_levels_return_none(self, value):
        """Unrecognized strings fall back to the caller default (None)."""
        assert parse_reasoning_effort(value) is None

    def test_known_supported_levels_are_documented(self):
        """Guard against silently dropping a documented level.

        The docstring promises "minimal", "low", "medium", "high", "xhigh".
        If someone removes one from VALID_REASONING_EFFORTS without updating
        the docstring, this test will fail and force the call out.
        """
        documented = {"minimal", "low", "medium", "high", "xhigh"}
        assert documented.issubset(set(VALID_REASONING_EFFORTS))


class TestSecureParentDir:
    """Tests for secure_parent_dir() — prevents chmod on / or top-level dirs."""

    def test_safe_path_calls_chmod(self, tmp_path, monkeypatch):
        """Normal nested path (depth >= 3) should call os.chmod."""
        safe_dir = tmp_path / "home" / "user" / ".atlaz"
        safe_dir.mkdir(parents=True)
        target = safe_dir / "auth.json"
        target.touch()

        called_with = []
        monkeypatch.setattr(os, "chmod", lambda p, m: called_with.append((str(p), m)))

        secure_parent_dir(target)
        assert len(called_with) == 1
        assert called_with[0] == (str(safe_dir), 0o700)

    def test_root_dir_skipped(self, monkeypatch):
        """Parent resolving to / must NOT be chmod'd."""
        called_with = []
        monkeypatch.setattr(os, "chmod", lambda p, m: called_with.append((str(p), m)))

        secure_parent_dir(Path("/foo"))
        assert called_with == []

    def test_top_level_dir_skipped(self, monkeypatch):
        """Parent resolving to a top-level dir (depth 2) must NOT be chmod'd."""
        called_with = []
        monkeypatch.setattr(os, "chmod", lambda p, m: called_with.append((str(p), m)))

        secure_parent_dir(Path("/usr/foo"))
        assert called_with == []

    def test_two_component_path_skipped(self, monkeypatch):
        """Parent with < 3 resolved parts must NOT be chmod'd."""
        called_with = []
        monkeypatch.setattr(os, "chmod", lambda p, m: called_with.append((str(p), m)))

        original_resolve = Path.resolve
        def mock_resolve(self):
            if str(self) == "/x/y":
                return Path("/x")
            return original_resolve(self)
        monkeypatch.setattr(Path, "resolve", mock_resolve)

        secure_parent_dir(Path("/x/y"))
        assert called_with == []

    def test_oserror_suppressed(self, tmp_path, monkeypatch):
        """OSError from chmod should be silently caught."""
        safe_dir = tmp_path / "a" / "b" / "c"
        safe_dir.mkdir(parents=True)
        target = safe_dir / "file.json"
        target.touch()

        def raise_oserror(p, m):
            raise OSError("permission denied")

        monkeypatch.setattr(os, "chmod", raise_oserror)
        secure_parent_dir(target)

    def test_symlink_resolved(self, tmp_path, monkeypatch):
        """Symlinks should be resolved before checking depth."""
        real_dir = tmp_path / "a" / "b"
        real_dir.mkdir(parents=True)
        target = real_dir / "file.json"
        target.touch()

        link = tmp_path / "link"
        link.symlink_to(real_dir)
        link_target = link / "file.json"

        called_with = []
        monkeypatch.setattr(os, "chmod", lambda p, m: called_with.append((str(p), m)))

        secure_parent_dir(link_target)
        assert len(called_with) == 1
        assert called_with[0] == (str(real_dir), 0o700)


# ─── Backward-compat tests for the shim ─────────────────────────────────


def test_hermes_constants_still_works():
    """Verify that hermes_constants re-exports the old names."""
    import atlaz_constants
    assert atlaz_constants.get_atlaz_home is atlaz_constants.get_atlaz_home
    assert atlaz_constants.display_atlaz_home is atlaz_constants.display_atlaz_home
    assert atlaz_constants.get_default_atlaz_root is atlaz_constants.get_default_atlaz_root
    assert atlaz_constants.get_atlaz_dir is atlaz_constants.get_atlaz_dir
    assert atlaz_constants.get_atlaz_home is atlaz_constants.get_atlaz_home
    assert atlaz_constants.display_atlaz_home is atlaz_constants.display_atlaz_home


def test_get_atlaz_home_backward_compat(tmp_path, monkeypatch):
    """When ~/.atlaz doesn't exist but ~/.hermes does, get_atlaz_home uses ~/.hermes."""
    monkeypatch.delenv("ATLAZ_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    hermes_legacy = tmp_path / ".hermes"
    hermes_legacy.mkdir()
    with pytest.warns(DeprecationWarning, match="~/.hermes"):
        result = atlaz_constants.get_atlaz_home()
    assert result == hermes_legacy


def test_get_atlaz_home_prefers_new(tmp_path, monkeypatch):
    """When both ~/.atlaz and ~/.hermes exist, get_atlaz_home prefers ~/.atlaz."""
    monkeypatch.delenv("ATLAZ_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    atlaz_dir = tmp_path / ".atlaz"
    atlaz_dir.mkdir()
    hermes_legacy = tmp_path / ".hermes"
    hermes_legacy.mkdir()
    result = atlaz_constants.get_atlaz_home()
    assert result == atlaz_dir


def test_get_atlaz_home_hermes_env_var(tmp_path, monkeypatch):
    """HERMES_HOME env var is respected with DeprecationWarning."""
    monkeypatch.delenv("ATLAZ_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    custom_path = tmp_path / "custom"
    custom_path.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(custom_path))
    with pytest.warns(DeprecationWarning, match="HERMES_HOME"):
        result = atlaz_constants.get_atlaz_home()
    assert result == custom_path


def test_get_atlaz_home_atlaz_env_var_wins(tmp_path, monkeypatch):
    """ATLAZ_HOME env var takes priority over HERMES_HOME."""
    atlaz_custom = tmp_path / "atlaz-custom"
    atlaz_custom.mkdir()
    hermes_custom = tmp_path / "hermes-custom"
    hermes_custom.mkdir()
    monkeypatch.setenv("ATLAZ_HOME", str(atlaz_custom))
    monkeypatch.setenv("HERMES_HOME", str(hermes_custom))
    result = atlaz_constants.get_atlaz_home()
    assert result == atlaz_custom
