# -*- coding: utf-8 -*-
"""Shared setup.

The tools are single-file modules at the repository root, so the root goes on
sys.path rather than restructuring the project to suit the test runner.

Nothing here shells out to ffmpeg. Every function under test is chosen because
it is pure: the arithmetic that decides what gets cut, what gets rotated and
what gets called too loud. The subprocess layer is exercised by actually running
the tools on real footage, which is not something CI can or should do.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def event(start, end):
    """One detection window as bird_sweep records it."""
    return {"start": float(start), "end": float(end), "dur": float(end) - float(start)}


@pytest.fixture
def make_event():
    return event
