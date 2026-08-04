"""macOS 菜单栏应用：状态栏图标、待办弹窗、问题面板与异步运行时。"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from typing import Any

from .ask import Answer, Presenter, Question
from .config import Settings
from .model import AUTO_SLOT_STATES, TodoState


def run_app(settings: Settings) -> None:
    """启动 AppKit 应用：创建后台循环、菜单栏代理并进入主事件循环。"""
    import AppKit
    import Foundation
    import objc

    from .runtime import Runtime

    class AsyncLoop:
        """在独立线程中运行 asyncio 事件循环，供 AppKit 主线程提交协程。"""

        def __init__(self) -> None:
            self.loop = asyncio.new_event_loop()
            self.thread = threading.Thread(target=self._run, name="RelayNoteRuntime", daemon=True)
            self.thread.start()

        def _run(self) -> None:
            """线程入口，持续运行事件循环直到 stop 被调用。"""
            asyncio.set_event_loop(self.loop)
            self.loop.run_forever()

        def submit(self, coroutine: Any) -> Future[Any]:
            """把协程调度到后台事件循环并返回 Future。"""
            return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

        def stop(self) -> None:
            """停止事件循环并等待后台线程退出。"""
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)

    class PanelPresenter(Presenter):
        """把 Question 交给主线程展示，并把面板答案带回异步 Future。"""

        def __init__(self, delegate: Any) -> None:
            self.delegate = delegate

        async def show(self, question: Question) -> Answer:
            """在主线程弹窗展示问题，等待用户作答后返回。"""
            loop = asyncio.get_running_loop()
            future: asyncio.Future[Answer] = loop.create_future()
            payload = {"question": question, "future": future, "loop": loop}
            self.delegate.performSelectorOnMainThread_withObject_waitUntilDone_("presentQuestion:", payload, False)
            try:
                return await future
            finally:
                self.delegate.performSelectorOnMainThread_withObject_waitUntilDone_("dismissQuestion:", question.id, False)

    class Delegate(AppKit.NSObject):
        """AppKit 应用代理：负责菜单栏 UI、运行时生命周期和问题面板。"""

        settings: Settings

        def applicationDidFinishLaunching_(self, notification: Any) -> None:
            """应用启动后创建后台运行时、状态栏项和待办弹窗。"""
            self.loop_thread = AsyncLoop()
            self.presenter = PanelPresenter(self)
            self.runtime = Runtime(self.settings, self.presenter, self._schedule_refresh)
            self.loop_thread.submit(self.runtime.start())
            self.todos = []
            self.selected_id = None
            self.question_payload = None
            self.question_panel = None
            self._build_status_item()
            self._build_popover()
            self.refreshUI_(None)

        def applicationWillTerminate_(self, notification: Any) -> None:
            """退出前关闭运行时，并限制等待时间避免卡住应用终止。"""
            try:
                self.loop_thread.submit(self.runtime.close()).result(timeout=40)
            finally:
                self.loop_thread.stop()

        @objc.python_method
        def _schedule_refresh(self) -> None:
            """把刷新动作安全地调度回 AppKit 主线程。"""
            self.performSelectorOnMainThread_withObject_waitUntilDone_("refreshUI:", None, False)

        @objc.python_method
        def _build_status_item(self) -> None:
            """创建菜单栏状态项和点击切换弹窗的按钮。"""
            self.status_item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
            button = self.status_item.button()
            button.setTitle_("✓")
            button.setToolTip_("RelayNote")
            button.setTarget_(self)
            button.setAction_("togglePopover:")

        @objc.python_method
        def _build_popover(self) -> None:
            """创建承载待办列表的 NSPopover 与根视图。"""
            self.popover = AppKit.NSPopover.alloc().init()
            self.popover.setBehavior_(AppKit.NSPopoverBehaviorTransient)
            controller = AppKit.NSViewController.alloc().init()
            root = AppKit.NSView.alloc().initWithFrame_(Foundation.NSMakeRect(0, 0, 430, 560))
            controller.setView_(root)
            self.popover.setContentViewController_(controller)
            self.root_view = root
            self._show_list()

        @objc.python_method
        def _clear_root(self) -> None:
            """移除根视图上的全部子视图，准备重新渲染。"""
            for view in list(self.root_view.subviews()):
                view.removeFromSuperview()

        @objc.python_method
        def _label(self, text: str, frame: Any, size: float = 13, bold: bool = False) -> Any:
            """创建可换行的 NSTextField 标签。"""
            label = AppKit.NSTextField.labelWithString_(text)
            label.setFrame_(frame)
            label.setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
            label.setMaximumNumberOfLines_(0)
            label.setFont_(AppKit.NSFont.boldSystemFontOfSize_(size) if bold else AppKit.NSFont.systemFontOfSize_(size))
            return label

        @objc.python_method
        def _button(self, title: str, action: str, frame: Any) -> Any:
            """创建绑定到 Delegate 方法的圆角按钮。"""
            button = AppKit.NSButton.alloc().initWithFrame_(frame)
            button.setTitle_(title)
            button.setBezelStyle_(AppKit.NSBezelStyleRounded)
            button.setTarget_(self)
            button.setAction_(action)
            return button

        @objc.python_method
        def _show_list(self) -> None:
            """渲染待办列表：诊断提示、滚动表格和新建/退出按钮。"""
            self._clear_root()
            self.detail_mode = False
            self.root_view.addSubview_(self._label("RelayNote", Foundation.NSMakeRect(18, 520, 280, 28), 20, True))
            diagnostics = []
            if not self.settings.deepseek_api_key:
                diagnostics.append("未设置 DEEPSEEK_API_KEY，自动调度已停用")
            if diagnostics:
                warning = self._label("\n".join(diagnostics), Foundation.NSMakeRect(18, 488, 394, 28), 11)
                warning.setTextColor_(AppKit.NSColor.systemOrangeColor())
                self.root_view.addSubview_(warning)
            scroll = AppKit.NSScrollView.alloc().initWithFrame_(Foundation.NSMakeRect(12, 58, 406, 428))
            scroll.setHasVerticalScroller_(True)
            table = AppKit.NSTableView.alloc().initWithFrame_(scroll.bounds())
            column = AppKit.NSTableColumn.alloc().initWithIdentifier_("todo")
            column.setWidth_(398)
            table.addTableColumn_(column)
            table.setHeaderView_(None)
            table.setRowHeight_(64)
            table.setDelegate_(self)
            table.setDataSource_(self)
            table.setTarget_(self)
            table.setAction_("openSelected:")
            table.registerForDraggedTypes_(["dev.relaynote.todo"])
            scroll.setDocumentView_(table)
            self.table = table
            self.root_view.addSubview_(scroll)
            self.root_view.addSubview_(self._button("＋ 新建待办", "addTodo:", Foundation.NSMakeRect(145, 14, 140, 34)))
            self.root_view.addSubview_(self._button("退出", "quitApp:", Foundation.NSMakeRect(356, 14, 62, 34)))
            table.reloadData()

        def togglePopover_(self, sender: Any) -> None:
            """点击状态栏图标时切换弹窗显示状态。"""
            if self.popover.isShown():
                self.popover.performClose_(sender)
            else:
                self.refreshUI_(None)
                self.popover.showRelativeToRect_ofView_preferredEdge_(sender.bounds(), sender, AppKit.NSRectEdgeMinY)

        def refreshUI_(self, sender: Any) -> None:
            """重新读取待办并刷新当前列表或详情界面。"""
            if not hasattr(self, "runtime"):
                return
            self.todos = self.runtime.store.list_todos()
            if getattr(self, "detail_mode", False) and self.selected_id:
                try:
                    self._show_detail(self.selected_id)
                except KeyError:
                    self.selected_id = None
                    self._show_list()
            elif hasattr(self, "table"):
                self.table.reloadData()

        def numberOfRowsInTableView_(self, table: Any) -> int:
            """NSTableView 数据源：返回待办行数。"""
            return len(self.todos)

        def tableView_viewForTableColumn_row_(self, table: Any, column: Any, row: int) -> Any:
            """渲染每行待办的标题、状态和最近进度。"""
            todo = self.todos[row]
            cell = AppKit.NSTableCellView.alloc().initWithFrame_(Foundation.NSMakeRect(0, 0, 398, 64))
            title = todo.title
            detail = todo.latest_detail or "尚无 Codex 进度"
            text = self._label(f"{title}\n{todo.state.label} · {detail}", Foundation.NSMakeRect(10, 5, 378, 54), 12)
            text.setMaximumNumberOfLines_(3)
            cell.addSubview_(text)
            return cell

        def tableView_writeRowsWithIndexes_toPasteboard_(self, table: Any, indexes: Any, pasteboard: Any) -> bool:
            """允许拖动可移动待办；自动任务保持固定置顶。"""
            row = indexes.firstIndex()
            if row == Foundation.NSNotFound or self.todos[row].state in AUTO_SLOT_STATES:
                return False
            pasteboard.declareTypes_owner_(["dev.relaynote.todo"], self)
            pasteboard.setString_forType_(self.todos[row].id, "dev.relaynote.todo")
            return True

        def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table: Any, info: Any, row: int, operation: int) -> int:
            """始终接受拖动操作，便于统一处理目标位置。"""
            return AppKit.NSDragOperationMove

        def tableView_acceptDrop_row_dropOperation_(self, table: Any, info: Any, row: int, operation: int) -> bool:
            """把拖动的待办插入新位置并持久化排序。"""
            todo_id = info.draggingPasteboard().stringForType_("dev.relaynote.todo")
            movable = [todo for todo in self.todos if todo.state not in AUTO_SLOT_STATES]
            source = next((index for index, todo in enumerate(movable) if todo.id == todo_id), None)
            if source is None:
                return False
            item = movable.pop(source)
            pinned_before = sum(1 for todo in self.todos[:row] if todo.state in AUTO_SLOT_STATES)
            destination = max(0, min(len(movable), row - pinned_before))
            movable.insert(destination, item)
            try:
                self.runtime.store.reorder([todo.id for todo in movable])
            except ValueError:
                return False
            self.refreshUI_(None)
            return True

        def openSelected_(self, sender: Any) -> None:
            """打开用户点击或选中的待办详情。"""
            row = self.table.clickedRow()
            if row < 0:
                row = self.table.selectedRow()
            if 0 <= row < len(self.todos):
                self.selected_id = self.todos[row].id
                self._show_detail(self.selected_id)

        def addTodo_(self, sender: Any) -> None:
            """弹出新建待办对话框，输入有效时创建待办。"""
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("新建待办")
            alert.setInformativeText_("写一句简洁描述即可，细节可以稍后追加。")
            field = AppKit.NSTextField.alloc().initWithFrame_(Foundation.NSMakeRect(0, 0, 320, 56))
            alert.setAccessoryView_(field)
            alert.addButtonWithTitle_("添加")
            alert.addButtonWithTitle_("取消")
            if alert.runModal() == AppKit.NSAlertFirstButtonReturn and field.stringValue().strip():
                self.runtime.create_todo(field.stringValue())
                self.refreshUI_(None)

        def quitApp_(self, sender: Any) -> None:
            """终止 NSApplication 并触发退出清理。"""
            AppKit.NSApplication.sharedApplication().terminate_(sender)

        @objc.python_method
        def _show_detail(self, todo_id: str) -> None:
            """渲染待办详情：时间线、Codex 事件和可用操作按钮。"""
            todo = self.runtime.store.get_todo(todo_id)
            self._clear_root()
            self.detail_mode = True
            self.selected_id = todo_id
            self.root_view.addSubview_(self._button("‹ 返回", "backToList:", Foundation.NSMakeRect(12, 520, 74, 28)))
            self.root_view.addSubview_(self._label(todo.title, Foundation.NSMakeRect(94, 518, 316, 34), 18, True))
            state = self._label(todo.state.label, Foundation.NSMakeRect(18, 486, 180, 24), 12, True)
            self.root_view.addSubview_(state)
            timeline = self.runtime.store.timeline(todo_id)
            events = self.runtime.store.codex_events(todo_id)
            lines = [todo.body]
            for item in timeline:
                if item["kind"] == "original":
                    continue
                lines.append(f"\n[{item['kind']}] {item['body']}")
                if item.get("answer"):
                    lines.append(f"回答：{item['answer']}")
            for event in events:
                if event["method"] == "item/completed":
                    content = event["raw"].get("params", {}).get("item", {})
                    if content.get("type") == "agentMessage" and content.get("text"):
                        lines.append(f"\n[Codex] {content['text']}")
            scroll = AppKit.NSScrollView.alloc().initWithFrame_(Foundation.NSMakeRect(14, 166, 402, 310))
            scroll.setHasVerticalScroller_(True)
            text = AppKit.NSTextView.alloc().initWithFrame_(scroll.bounds())
            text.setEditable_(False)
            text.setSelectable_(True)
            text.setString_("\n".join(lines))
            scroll.setDocumentView_(text)
            self.root_view.addSubview_(scroll)
            self.append_field = AppKit.NSTextField.alloc().initWithFrame_(Foundation.NSMakeRect(14, 116, 318, 36))
            self.append_field.setPlaceholderString_("追加描述…")
            self.root_view.addSubview_(self.append_field)
            self.root_view.addSubview_(self._button("追加", "appendTodo:", Foundation.NSMakeRect(340, 116, 76, 36)))
            x = 14
            if todo.state in {TodoState.PENDING, TodoState.RUNNING, TodoState.WAITING}:
                self.root_view.addSubview_(self._button("挂起", "suspendTodo:", Foundation.NSMakeRect(x, 62, 82, 34)))
                x += 92
            if todo.state in {TodoState.PENDING, TodoState.SUSPENDED, TodoState.RUNNING, TodoState.WAITING}:
                self.root_view.addSubview_(self._button("接管", "takeoverTodo:", Foundation.NSMakeRect(x, 62, 82, 34)))
                x += 92
            if todo.state is TodoState.COMPLETED:
                self.root_view.addSubview_(self._button("退回待完成", "resetTodo:", Foundation.NSMakeRect(x, 62, 112, 34)))
                x += 122
                self.root_view.addSubview_(self._button("✓ 验收", "archiveTodo:", Foundation.NSMakeRect(x, 62, 82, 34)))
            elif todo.state is TodoState.TAKEN_OVER:
                self.root_view.addSubview_(self._button("✓ 完成并移除", "completeClaimed:", Foundation.NSMakeRect(x, 62, 128, 34)))
            elif todo.state is TodoState.ERROR:
                self.root_view.addSubview_(self._button("重试", "retryTodo:", Foundation.NSMakeRect(x, 62, 82, 34)))

        def backToList_(self, sender: Any) -> None:
            """返回待办列表并刷新。"""
            self.selected_id = None
            self._show_list()
            self.refreshUI_(None)

        def appendTodo_(self, sender: Any) -> None:
            """把追加框中的文本提交给运行时。"""
            body = self.append_field.stringValue()
            if body.strip():
                self._submit(self.runtime.append(self.selected_id, body))
                self.append_field.setStringValue_("")

        def suspendTodo_(self, sender: Any) -> None:
            """挂起当前详情中的待办。"""
            self._submit(self.runtime.suspend(self.selected_id))

        def takeoverTodo_(self, sender: Any) -> None:
            """接管当前详情中的待办。"""
            self._submit(self.runtime.takeover(self.selected_id))

        def resetTodo_(self, sender: Any) -> None:
            """把已完成待办退回待完成状态。"""
            self.runtime.reset_completed(self.selected_id)

        def archiveTodo_(self, sender: Any) -> None:
            """归档已完成待办并返回列表。"""
            self.runtime.archive(self.selected_id)
            self.backToList_(sender)

        def completeClaimed_(self, sender: Any) -> None:
            """完成已接管待办并立即归档。"""
            todo = self.runtime.store.get_todo(self.selected_id)
            completed = self.runtime.store.transition(todo.id, TodoState.COMPLETED, todo.version)
            self.runtime.archive(completed.id)
            self.backToList_(sender)

        def retryTodo_(self, sender: Any) -> None:
            """重试异常状态的待办。"""
            self._submit(self.runtime.retry_error(self.selected_id))

        @objc.python_method
        def _submit(self, coroutine: Any) -> None:
            """把协程提交到后台循环，完成后回到主线程处理。"""
            future = self.loop_thread.submit(coroutine)
            future.add_done_callback(lambda result: self.performSelectorOnMainThread_withObject_waitUntilDone_("asyncFinished:", result, False))

        def asyncFinished_(self, future: Future[Any]) -> None:
            """后台任务结束回调：失败时弹窗，成功后刷新 UI。"""
            try:
                future.result()
            except Exception as error:  # noqa: BLE001 - 在 UI 中展示后台操作失败
                alert = AppKit.NSAlert.alloc().init()
                alert.setMessageText_("RelayNote 操作失败")
                alert.setInformativeText_(str(error))
                alert.runModal()
            self.refreshUI_(None)

        def presentQuestion_(self, payload: dict[str, Any]) -> None:
            """在主线程创建并显示问题面板。"""
            question = payload["question"]
            self.question_payload = payload
            panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                Foundation.NSMakeRect(0, 0, 430, 330),
                AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskNonactivatingPanel,
                AppKit.NSBackingStoreBuffered,
                False,
            )
            panel.setLevel_(AppKit.NSFloatingWindowLevel)
            panel.setHidesOnDeactivate_(False)
            panel.setReleasedWhenClosed_(False)
            panel.setTitle_("RelayNote")
            content = panel.contentView()
            todo_title = "调度器"
            if question.todo_id:
                try:
                    todo_title = self.runtime.store.get_todo(question.todo_id).title
                except KeyError:
                    pass
            content.addSubview_(self._label(todo_title, Foundation.NSMakeRect(20, 286, 390, 24), 13, True))
            content.addSubview_(self._label(question.prompt, Foundation.NSMakeRect(20, 214, 390, 66), 14))
            y = 176
            for index, option in enumerate(question.options):
                suffix = "（推荐）" if index == question.recommended else ""
                button = self._button(f"{option.label}{suffix} — {option.description}", "answerOption:", Foundation.NSMakeRect(20, y, 390, 32))
                button.setTag_(index)
                content.addSubview_(button)
                y -= 38
            self.other_field = AppKit.NSTextField.alloc().initWithFrame_(Foundation.NSMakeRect(20, 46, 302, 34))
            self.other_field.setPlaceholderString_("其他回答…")
            content.addSubview_(self.other_field)
            content.addSubview_(self._button("发送", "answerOther:", Foundation.NSMakeRect(330, 46, 80, 34)))
            panel.center()
            panel.orderFrontRegardless()
            self.question_panel = panel

        def dismissQuestion_(self, question_id: str) -> None:
            """关闭与当前问题匹配的面板。"""
            payload = self.question_payload
            if payload and payload["question"].id == question_id:
                if self.question_panel:
                    self.question_panel.orderOut_(None)
                self.question_panel = None
                self.question_payload = None

        def answerOption_(self, sender: Any) -> None:
            """用户点击推荐/选项按钮时提交对应 Answer。"""
            self._answer_panel(Answer(sender.tag(), None, False))

        def answerOther_(self, sender: Any) -> None:
            """用户填写其他回答时提交自由文本。"""
            value = self.other_field.stringValue()
            if value.strip():
                self._answer_panel(Answer(None, value, False))

        @objc.python_method
        def _answer_panel(self, answer: Answer) -> None:
            """把面板答案写回等待中的异步 Future 并关闭面板。"""
            payload = self.question_payload
            if payload is None:
                return
            future, loop = payload["future"], payload["loop"]
            loop.call_soon_threadsafe(lambda: None if future.done() else future.set_result(answer))
            self.dismissQuestion_(payload["question"].id)

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    delegate = Delegate.alloc().init()
    delegate.settings = settings
    app.setDelegate_(delegate)
    app.run()
