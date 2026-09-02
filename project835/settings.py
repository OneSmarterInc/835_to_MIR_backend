"""Django settings loader.

The environment-based configuration is kept in the repository-root
``localsettings.py`` for backwards compatibility with existing deployments.
Execute it from this module so Django always receives the tracked settings
while ``__file__`` continues to point here and BASE_DIR resolves correctly.
"""

from pathlib import Path


_settings_source = Path(__file__).resolve().parent.parent / "localsettings.py"
exec(compile(_settings_source.read_bytes(), str(_settings_source), "exec"))

del _settings_source
