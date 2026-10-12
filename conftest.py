"""Test isolation for the w2a suites.

These tests import the real core (``PYTHONPATH=~/Documents/odysseus``) and the
real extension manifest, but they must never write to the operator's live
state. Without this file they did, and it cost real data:

- ``SettingsNamespace`` persists every ``set()`` to
  ``data/oxs/permissions/<id>.json`` on the caller's disk. The suite's
  ``settings.set("api_token", "fake")`` /
  ``settings.set("default_connection_id", "DEFAULT_CID")`` lines overwrote
  the operator's real whats2api token in that file.
- ``./data`` is a bind mount into the container (``docker-compose.yml``:
  ``${APP_DATA_DIR:-./data}:/app/data``), so the host-side clobber landed in
  the running container too. The bridge started returning HTTP 401 on every
  outbound call, and no test failed to explain why.
- ``DATA_EXTENSIONS_DIR`` is both the discovery fallback and each
  extension's ``data_path``. Enable-time writes (the pinned inbound webhook
  token) and install-time copies went to the live directory.

So: point the database, the settings file and the extension data dir at a
throwaway tree for the whole pytest session. ``EXTENSIONS_DIR`` (the bundled
checkout) stays real — it is the code under test, not state.

Set ``W2A_TEST_LIVE_STATE=1`` to opt out and run against live state. That is
only ever right when you are deliberately reproducing an on-box incident.
"""

from __future__ import annotations

import os
import tempfile

_LIVE = os.environ.get("W2A_TEST_LIVE_STATE") == "1"

if not _LIVE:
    _tmp = tempfile.mkdtemp(prefix="w2a-test-state-")
    os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_tmp, "test.db")

    # ``src.constants`` is imported by the core lazily, so patch the module
    # attributes the OXS layer reads rather than the environment.
    import src.constants as _constants

    _oxs_perms = os.path.join(_tmp, "oxs", "permissions")
    _data_ext = os.path.join(_tmp, "data", "extensions")
    _oxs_perms_data = os.path.join(_tmp, "data", "oxs", "permissions")
    for _d in (_oxs_perms, _data_ext, _oxs_perms_data):
        os.makedirs(_d, exist_ok=True)

    _constants.OXS_PERMISSIONS_DIR = _oxs_perms
    _constants.DATA_EXTENSIONS_DIR = _data_ext
    # Keep the two permission roots in agreement: the host builds the path
    # from ``OXS_PERMISSIONS_DIR`` only, but a container-mode checkout may
    # read the data-root spelling.
    _constants.OXS_PERMISSIONS_DIR_DATA = _oxs_perms_data