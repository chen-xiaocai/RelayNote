from setuptools import setup

APP = ["src/relaynote_app.py"]
OPTIONS = {
    "argv_emulation": False,
    # cryptography 50's Intel macOS wheel links against Homebrew OpenSSL.
    # Py2app cannot coexist with Python's framework libcrypto under the same
    # bundle name, and this app does not use the cryptography extension.
    "excludes": ["cryptography", "cryptography.hazmat.bindings._rust"],
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

setup(app=APP, name="RelayNote", options={"py2app": OPTIONS})
