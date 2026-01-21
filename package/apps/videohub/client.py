"""Compatibility wrapper for the VideoHub client.

The preferred import location in this repo is the top-level module:

  from videohub import VideohubClient

This file exists so older imports continue working:

  from package.apps.videohub.client import VideohubClient
"""

from videohub import (  # noqa: F401
    DEFAULT_PORT,
    VideohubClient,
    VideohubConfig,
    get_videohub_client_from_config,
)
