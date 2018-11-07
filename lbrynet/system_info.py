import platform
import os

from lbrynet.schema import __version__ as schema_version
from lbrynet import build_type, __version__ as lbrynet_version
import logging.handlers

log = logging.getLogger(__name__)


def get_lbrynet_version() -> str:
    return lbrynet_version


def get_platform() -> dict:
    p = {
        "processor": platform.processor(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "os_release": platform.release(),
        "os_system": platform.system(),
        "lbrynet_version": get_lbrynet_version(),
        "lbryschema_version": schema_version,
        "build": build_type.BUILD,  # CI server sets this during build step
    }
    if p["os_system"] == "Linux":
        try:
            import distro
            p["distro"] = distro.info()
            p["desktop"] = os.environ.get('XDG_CURRENT_DESKTOP', 'Unknown')
        except ModuleNotFoundError:
            pass

    return p
