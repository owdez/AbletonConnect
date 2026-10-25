import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import fake_live  # noqa: E402

fake_live.install()

import pytest  # noqa: E402


@pytest.fixture
def live():
    """Fresh fake song + application for each test."""
    song, app = fake_live.reset()
    return song, app
