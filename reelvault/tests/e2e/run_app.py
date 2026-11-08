"""Start ReelVault with Instagram faked out, for extension_e2e.js. Usage: python run_app.py <home>"""
import os
import sys
from pathlib import Path

os.environ["REELVAULT_HOME"] = sys.argv[1]
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import reelvault.app as app  # noqa: E402
from reelvault import igweb  # noqa: E402


def fake_whoami(s, cookies):
    if cookies.get("sessionid") != "S":
        raise igweb.IGError("Instagram logged you out.")
    return "scaleitads"


igweb.whoami = fake_whoami
app.open_instagram_tab = lambda: "Chrome"
app.main(["--no-browser"])
