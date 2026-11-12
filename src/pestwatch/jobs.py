"""Run long PestWatch work outside the web request.

uWSGI kills requests after 60 seconds and no Celery worker runs on this
server, so jobs run as detached ``manage.py pestwatch_job`` processes.
"""
import logging
import os
import subprocess
import sys
from importlib import import_module

from django.conf import settings

from . import conf

logger = logging.getLogger(__name__)


def _python():
    configured = conf.get("PYTHON")
    if configured:
        return configured
    # Under uWSGI sys.executable is the uwsgi binary; the virtualenv's python is in sys.prefix.
    candidate = os.path.join(sys.prefix, "bin", "python")
    return candidate if os.path.exists(candidate) else sys.executable


def _manage_py():
    configured = conf.get("MANAGE_PY")
    if configured:
        return configured
    project = import_module(settings.SETTINGS_MODULE.split(".")[0])
    return os.path.join(os.path.dirname(os.path.dirname(project.__file__)), "manage.py")


def log_path():
    path = os.path.join(conf.get("STAGING_DIR"), "jobs.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def launch(*args):
    """Start ``manage.py pestwatch_job <args>`` in the background."""
    cmd = [_python(), _manage_py(), "pestwatch_job", *[str(a) for a in args]]
    with open(log_path(), "ab") as log:
        # The shell backgrounds the job and exits at once, so the job is
        # re-parented to init and never lingers as a zombie of the uWSGI worker.
        shell = subprocess.Popen(
            ["/bin/sh", "-c", '"$@" &', "sh", *cmd], cwd=os.path.dirname(_manage_py()), env=os.environ.copy(),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True)
        shell.wait()
    logger.info("Started PestWatch job: %s", " ".join(cmd))


def process_layer(layer_id):
    launch("process_layer", layer_id)


def recompute_counties(county_ids):
    if county_ids:
        launch("recompute_counties", *county_ids)


def recompute_layer(layer_id):
    launch("recompute_layer", layer_id)
