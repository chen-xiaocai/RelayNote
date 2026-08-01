from __future__ import annotations

from typing import Any

from .config import Settings
from .db import Store


def run_app(settings: Settings) -> None:
    import AppKit
    import objc

    class Delegate(AppKit.NSObject):
        settings: Settings
        store: Store

        def applicationDidFinishLaunching_(self, notification: Any) -> None:
            self.store = Store(self.settings.data_dir / "relaynote.sqlite3")
            self.item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
            self.item.button().setTitle_("RelayNote")
            menu = AppKit.NSMenu.alloc().init()
            self.refresh_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("打开待办清单", "showTodos:", "")
            self.refresh_item.setTarget_(self)
            menu.addItem_(self.refresh_item)
            menu.addItem_(AppKit.NSMenuItem.separatorItem())
            quit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("退出", "terminate:", "q")
            menu.addItem_(quit_item)
            self.item.setMenu_(menu)

        @objc.python_method
        def _text(self) -> str:
            todos = self.store.list_todos()
            return "\n".join(f"[{t.state}] {t.body}" for t in todos) or "暂无待办"

        def showTodos_(self, sender: Any) -> None:
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("RelayNote")
            alert.setInformativeText_(self._text())
            alert.runModal()

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    delegate = Delegate.alloc().init()
    delegate.settings = settings
    app.setDelegate_(delegate)
    app.run()
