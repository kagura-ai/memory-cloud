"""Configuration directory paths.

Issue #13: OAuth2 authentication
Issue #31: Frontend integration
"""

import os
from pathlib import Path


def get_config_dir() -> Path:
    """Get configuration directory path.

    Returns Kagura config directory, creating it if necessary.
    For Docker/production: /app/config
    For development: ~/.config/kagura or XDG_CONFIG_HOME/kagura

    Returns:
        Path to configuration directory
    """
    # Docker/production mode
    if os.path.exists("/app"):
        config_dir = Path("/app/config")
    # Development mode
    else:
        xdg_config = os.getenv("XDG_CONFIG_HOME")
        if xdg_config:
            config_dir = Path(xdg_config) / "kagura"
        else:
            config_dir = Path.home() / ".config" / "kagura"

    # Create directory if it doesn't exist
    config_dir.mkdir(parents=True, exist_ok=True)

    return config_dir


def get_data_dir() -> Path:
    """Get data directory path.

    Returns:
        Path to data directory
    """
    # Docker/production mode
    if os.path.exists("/app"):
        data_dir = Path("/app/data")
    # Development mode
    else:
        xdg_data = os.getenv("XDG_DATA_HOME")
        if xdg_data:
            data_dir = Path(xdg_data) / "kagura"
        else:
            data_dir = Path.home() / ".local" / "share" / "kagura"

    # Create directory if it doesn't exist
    data_dir.mkdir(parents=True, exist_ok=True)

    return data_dir
