"""Opt-in, isolated PostgreSQL tests. Never load application DB credentials."""
import os
import getpass
from .settings import *  # noqa: F403

socket = os.environ.get("EXAM_TEST_PG_SOCKET", "")
if not socket.startswith("/tmp/amootech-sprint7-pg."):
    raise RuntimeError("EXAM_TEST_PG_SOCKET must name the isolated temporary QA cluster")
DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql", "NAME": "postgres",
    "USER": os.environ.get("EXAM_TEST_PG_USER", getpass.getuser()), "PASSWORD": "",
    "HOST": socket, "PORT": "55437",
    "TEST": {"NAME": "test_amootech_sprint7"},
}}
