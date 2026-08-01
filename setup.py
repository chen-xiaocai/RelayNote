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
    "packages": ["relaynote", "mcp"],
    "extra_scripts": ["src/relaynote/ask_mcp.py"],
}

setup(app=APP, name="RelayNote", options={"py2app": OPTIONS}, setup_requires=["py2app"])
