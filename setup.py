from setuptools import setup

APP = ["src/relaynote_app.py"]
OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "LSUIElement": True,
        "LSMinimumSystemVersion": "13.0",
        "CFBundleName": "RelayNote",
        "CFBundleIdentifier": "dev.relaynote.app",
        "CFBundleShortVersionString": "0.1.0",
    },
    # py2app turns namespace packages into empty packages in python312.zip;
    # jaraco.text must be a real top-level package or pkg_resources fails to boot.
    "packages": ["relaynote", "mcp", "jaraco.text"],
    "extra_scripts": ["src/relaynote/ask_mcp.py"],
}

setup(app=APP, name="RelayNote", options={"py2app": OPTIONS})
