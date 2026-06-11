"""Guard: main.py's `settings` must stay the config singleton, not a route module.

A `from routes import settings` once shadowed `from config import settings`,
crashing startup (settings.google_cloud_project -> AttributeError). The full
suite missed it because tests don't run the app lifespan. This guards it.
"""
from __future__ import annotations

import main
from config import settings as config_settings


def test_main_settings_is_config_singleton():
    assert main.settings is config_settings
    # the attributes the lifespan reads must exist on it
    assert hasattr(main.settings, "google_cloud_project")
    assert hasattr(main.settings, "telegram_enabled")


def test_settings_router_registered_under_alias():
    # the settings ROUTER is wired without colliding with the config singleton
    assert main.settings_routes.router.prefix == "/settings"
