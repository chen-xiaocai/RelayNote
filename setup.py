"""py2app macOS 应用打包配置。"""

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
    # anyio 会在运行时动态加载 asyncio 后端，必须显式告诉 py2app 打包，
    # 不能依赖静态 import 扫描。
    "includes": ["anyio._backends._asyncio"],
    "extra_scripts": ["src/relaynote/ask_mcp.py"],
}

setup(app=APP, name="RelayNote", options={"py2app": OPTIONS})
