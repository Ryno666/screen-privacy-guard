import AppKit
import Foundation
import Darwin

let runtime = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/ScreenPrivacyGuard")
let captures = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Pictures/ScreenPrivacyGuard")
let serviceTarget = "gui/\(getuid())/com.local.screen-privacy-guard"

final class FixedWhiteStatusText: NSView {
    var text = "" { didSet { needsDisplay = true } }
    static let font = NSFont.systemFont(ofSize: 12, weight: .medium)
    override var allowsVibrancy: Bool { false }
    override var isOpaque: Bool { false }
    // The standard status-bar button underneath continues to receive menu clicks.
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
    var preferredWidth: CGFloat {
        ceil((text as NSString).size(withAttributes: [.font: Self.font]).width) + 12
    }
    override func draw(_ dirtyRect: NSRect) {
        let title = NSAttributedString(string: text, attributes: [
            .foregroundColor: NSColor.white, .font: Self.font,
        ])
        let size = title.size()
        title.draw(at: NSPoint(x: round((bounds.width-size.width)/2),
                               y: round((bounds.height-size.height)/2)))
    }
}

struct CommandResult { let code: Int32; let output: String }
func command(_ executable: URL, _ args: [String], timeout: Double = 35) -> CommandResult {
    let process = Process()
    let pipe = Pipe()
    process.executableURL = executable
    process.arguments = args
    process.standardOutput = pipe
    process.standardError = pipe
    do { try process.run() } catch { return CommandResult(code: -1, output: error.localizedDescription) }
    let deadline = DispatchWorkItem { if process.isRunning { process.terminate() } }
    DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: deadline)
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    deadline.cancel()
    return CommandResult(code: process.terminationStatus, output: String(decoding: data, as: UTF8.self))
}

struct ProtectionState {
    let title: String
    let detail: String
    let menuTitle: String
    let symbol: String
    let color: NSColor
    let canStart: Bool
    let canStop: Bool
    let active: Bool

    static let loading = ProtectionState(title: "正在读取状态", detail: "不会自动开启摄像头", menuTitle: "查询中",
                                         symbol: "shield", color: .secondaryLabelColor, canStart: false, canStop: false, active: false)
    static let off = ProtectionState(title: "保护已关闭", detail: "监测已停止，摄像头不会被本程序使用", menuTitle: "已关闭",
                                     symbol: "eye.slash", color: .secondaryLabelColor, canStart: true, canStop: false, active: false)

    static func evaluate(loaded: Bool, raw: [String: Any], now: Double, workerAlive: Bool) -> ProtectionState {
        let state = raw["state"] as? String ?? ""
        let age = now - (raw["updated_at"] as? Double ?? 0)
        if !loaded {
            if state != "stopped" && workerAlive {
                return ProtectionState(title: "监测尚未完全退出", detail: "请点击停止，结束残留的监测进程", menuTitle: "待停止",
                                       symbol: "exclamationmark.shield", color: .systemOrange, canStart: false, canStop: true, active: true)
            }
            return .off
        }
        if age > 30 && state != "stopped" {
            return ProtectionState(title: "正在恢复监测", detail: "最近未收到运行状态，请稍候或停止后重试", menuTitle: "恢复中",
                                   symbol: "arrow.clockwise", color: .systemOrange, canStart: false, canStop: true, active: true)
        }
        switch state {
        case "monitoring":
            return ProtectionState(title: "保护已开启", detail: "正在监测旁观者，只有你时不会保存图片", menuTitle: "保护中",
                                   symbol: "eye", color: .systemGreen, canStart: false, canStop: true, active: true)
        case "locked":
            return ProtectionState(title: "保护已开启 · 屏幕已锁定", detail: "摄像头暂时释放，解锁后自动恢复监测", menuTitle: "已锁屏",
                                   symbol: "lock.shield", color: .systemGreen, canStart: false, canStop: true, active: true)
        case "camera_permission_required", "camera_permission_denied", "camera_permission_unknown":
            return ProtectionState(title: "等待摄像头授权", detail: "请允许“屏幕旁观保护”使用摄像头，尚未开始监测", menuTitle: "待授权",
                                   symbol: "camera.badge.ellipsis", color: .systemOrange, canStart: false, canStop: true, active: true)
        case "camera_retry":
            return ProtectionState(title: "正在重连摄像头", detail: "暂时无法取景，程序会自动重试", menuTitle: "重连中",
                                   symbol: "arrow.clockwise", color: .systemOrange, canStart: false, canStop: true, active: true)
        default:
            return ProtectionState(title: "正在启动保护", detail: "正在准备摄像头和识别模型", menuTitle: "启动中",
                                   symbol: "shield", color: .systemOrange, canStart: false, canStop: true, active: true)
        }
    }
}

func readProtectionState() -> ProtectionState {
    let result = command(URL(fileURLWithPath: "/bin/launchctl"), ["print", serviceTarget], timeout: 5)
    let path = runtime.appendingPathComponent("state/status.json")
    let raw = (try? Data(contentsOf: path)).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
    let pid = (raw["pid"] as? NSNumber)?.int32Value ?? 0
    let alive = pid > 1 && kill(pid, 0) == 0
    if result.code != 0 && !result.output.contains("Could not find service") && !result.output.contains("service not found") {
        return ProtectionState(title: "暂时无法确认状态", detail: "可以点击停止，确保监测关闭", menuTitle: "待检查",
                               symbol: "exclamationmark.shield", color: .systemOrange, canStart: false, canStop: true, active: true)
    }
    return ProtectionState.evaluate(loaded: result.code == 0, raw: raw, now: Date().timeIntervalSince1970, workerAlive: alive)
}

final class ControlApp: NSObject, NSApplicationDelegate, NSMenuDelegate {
    var window: NSWindow!
    var statusItem: NSStatusItem!
    var fixedStatusText: FixedWhiteStatusText?
    var headline = NSTextField(labelWithString: "正在读取状态")
    var detail = NSTextField(wrappingLabelWithString: "不会自动开启摄像头")
    var symbolView = NSImageView()
    var primary = NSButton()
    var menuStatus = NSMenuItem(title: "正在读取状态", action: nil, keyEquivalent: "")
    var menuStart = NSMenuItem(title: "启动监测", action: #selector(startProtection), keyEquivalent: "")
    var menuStop = NSMenuItem(title: "停止监测", action: #selector(stopProtection), keyEquivalent: "")
    var menuQuit = NSMenuItem(title: "退出控制界面", action: #selector(quitControl), keyEquivalent: "q")
    var state = ProtectionState.loading
    var busy = false
    var refreshing = false
    var ready = false
    var timer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildWindow()
        buildMenu()
        ready = true
        showWindow()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { _ in self.refresh() }
    }

    func label(_ text: String, size: CGFloat = 13, color: NSColor = .secondaryLabelColor) -> NSTextField {
        let view = NSTextField(wrappingLabelWithString: text)
        view.font = .systemFont(ofSize: size)
        view.textColor = color
        return view
    }

    func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 480, height: 332),
                          styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "屏幕旁观保护"
        window.isReleasedWhenClosed = false
        window.center()
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 18
        root.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 26),
            root.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -26),
            root.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 28),
        ])
        let hero = NSStackView()
        hero.orientation = .horizontal
        hero.alignment = .centerY
        hero.spacing = 17
        symbolView.imageScaling = .scaleProportionallyUpOrDown
        symbolView.setAccessibilityLabel("保护状态图标")
        symbolView.widthAnchor.constraint(equalToConstant: 46).isActive = true
        symbolView.heightAnchor.constraint(equalToConstant: 46).isActive = true
        hero.addArrangedSubview(symbolView)
        let text = NSStackView()
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = 7
        headline.font = .systemFont(ofSize: 24, weight: .semibold)
        detail.font = .systemFont(ofSize: 13)
        detail.textColor = .secondaryLabelColor
        text.addArrangedSubview(headline)
        text.addArrangedSubview(detail)
        hero.addArrangedSubview(text)
        root.addArrangedSubview(hero)
        hero.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        primary = NSButton(title: "启动监测", target: self, action: #selector(toggleProtection))
        primary.bezelStyle = .rounded
        primary.controlSize = .large
        primary.font = .systemFont(ofSize: 15, weight: .semibold)
        primary.keyEquivalent = "\r"
        primary.heightAnchor.constraint(equalToConstant: 42).isActive = true
        primary.setAccessibilityIdentifier("protection-toggle")
        root.addArrangedSubview(primary)
        primary.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        let links = NSStackView()
        links.orientation = .horizontal
        links.spacing = 10
        links.distribution = .fillEqually
        links.addArrangedSubview(NSButton(title: "查看截图", target: self, action: #selector(openCaptures)))
        links.addArrangedSubview(NSButton(title: "查看运行记录", target: self, action: #selector(openLogs)))
        root.addArrangedSubview(links)
        links.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        let rules = label("其他人疑似持续注视超过 5 秒保存一张截图，达到 15 秒锁屏。只有你时不保存；识别仍可能误判。", size: 12)
        root.addArrangedSubview(rules)
        rules.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        let footer = label("启动后登录自动恢复，停止时一并关闭自启。\n关闭窗口只隐藏界面，不改变监测状态。", size: 11)
        root.addArrangedSubview(footer)
        footer.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
    }

    func buildMenu() {
        let appMenu = NSMenu()
        let applicationItem = NSMenuItem()
        let applicationMenu = NSMenu()
        let quit = NSMenuItem(title: "退出控制界面", action: #selector(quitControl), keyEquivalent: "q")
        quit.target = self
        applicationMenu.addItem(quit)
        applicationItem.submenu = applicationMenu
        appMenu.addItem(applicationItem)
        NSApp.mainMenu = appMenu
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        let menu = NSMenu()
        menu.autoenablesItems = false
        menu.delegate = self
        menu.addItem(menuStatus)
        menu.addItem(.separator())
        let panel = NSMenuItem(title: "打开控制面板", action: #selector(showWindow), keyEquivalent: "")
        panel.target = self
        menu.addItem(panel)
        for item in [menuStart, menuStop] { item.target = self; menu.addItem(item) }
        menu.addItem(.separator())
        let pictures = NSMenuItem(title: "查看截图", action: #selector(openCaptures), keyEquivalent: "")
        pictures.target = self
        menu.addItem(pictures)
        menu.addItem(.separator())
        menuQuit.target = self
        menu.addItem(menuQuit)
        statusItem.menu = menu
        apply(.loading)
    }

    func apply(_ new: ProtectionState) {
        guard let statusItem = statusItem else { return }
        state = new
        headline.stringValue = new.title
        detail.stringValue = new.detail
        symbolView.image = NSImage(systemSymbolName: new.symbol, accessibilityDescription: new.title)
        symbolView.contentTintColor = new.color
        primary.title = new.canStop ? "停止监测" : "启动监测"
        primary.isEnabled = !busy && (new.canStart || new.canStop)
        primary.bezelColor = new.canStop ? .systemRed : .controlAccentColor
        menuStart.isEnabled = !busy && new.canStart
        menuStop.isEnabled = !busy && new.canStop
        menuStatus.title = new.title
        setStatusText(new.menuTitle)
        statusItem.button?.toolTip = "屏幕旁观保护：\(new.title)"
        menuQuit.title = new.active ? "退出控制界面（监测继续）" : "退出控制界面"
    }

    func setStatusText(_ text: String) {
        guard let button = statusItem?.button else { return }
        button.image = nil
        button.imagePosition = .noImage
        button.attributedTitle = NSAttributedString(string: "")
        button.title = ""
        button.contentTintColor = nil
        if fixedStatusText == nil {
            let view = FixedWhiteStatusText(frame: button.bounds)
            view.autoresizingMask = [.width, .height]
            view.appearance = NSAppearance(named: .aqua)
            view.wantsLayer = true
            view.layer?.backgroundColor = NSColor.clear.cgColor
            view.setAccessibilityElement(false)
            button.addSubview(view)
            fixedStatusText = view
        }
        fixedStatusText?.text = text
        statusItem.length = fixedStatusText?.preferredWidth ?? 50
        fixedStatusText?.frame = button.bounds
        button.setAccessibilityLabel("屏幕旁观保护：\(text)")
    }

    func refresh() {
        guard ready && !refreshing && !busy else { return }
        refreshing = true
        DispatchQueue.global(qos: .utility).async {
            let next = readProtectionState()
            DispatchQueue.main.async {
                self.refreshing = false
                if !self.busy { self.apply(next) }
            }
        }
    }

    func perform(_ action: String) {
        guard !busy else { return }
        busy = true
        primary.isEnabled = false
        menuStart.isEnabled = false
        menuStop.isEnabled = false
        headline.stringValue = action == "start" ? "正在启动保护" : "正在停止保护"
        detail.stringValue = action == "start" ? "请稍候，启动后会检查摄像头状态" : "正在结束监测进程并释放摄像头"
        setStatusText(action == "start" ? "启动中" : "停止中")
        DispatchQueue.global(qos: .userInitiated).async {
            let result = command(runtime.appendingPathComponent(".venv/bin/python"), [runtime.appendingPathComponent("service.py").path, action], timeout: 45)
            let log = runtime.appendingPathComponent("logs/control-actions.log")
            let line = "\n\(Date()) action=\(action) code=\(result.code)\n\(result.output)"
            if let data = line.data(using: .utf8) {
                if !FileManager.default.fileExists(atPath: log.path) { FileManager.default.createFile(atPath: log.path, contents: nil, attributes: [.posixPermissions: 0o600]) }
                if let handle = try? FileHandle(forWritingTo: log) { handle.seekToEndOfFile(); handle.write(data); try? handle.close() }
            }
            let next = readProtectionState()
            DispatchQueue.main.async {
                self.busy = false
                self.apply(next)
                if result.code != 0 {
                    let alert = NSAlert()
                    alert.messageText = action == "start" ? "启动未完成" : "停止未完成"
                    alert.informativeText = "当前状态已重新检查。可以点击“查看运行记录”查看原因。"
                    alert.addButton(withTitle: "知道了")
                    alert.runModal()
                }
            }
        }
    }

    @objc func showWindow() {
        guard ready, let window = window else { return }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        refresh()
    }
    @objc func toggleProtection() { state.canStop ? stopProtection() : startProtection() }
    @objc func startProtection() { if state.canStart && !busy { perform("start") } }
    @objc func stopProtection() { if state.canStop && !busy { perform("stop") } }
    @objc func openCaptures() { NSWorkspace.shared.open(captures) }
    @objc func openLogs() { NSWorkspace.shared.open(runtime.appendingPathComponent("logs")) }
    @objc func quitControl() { NSApp.terminate(nil) }
    func menuWillOpen(_ menu: NSMenu) { refresh() }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showWindow(); return true }
}

func selfTest() {
    let now = 1000.0
    func status(_ loaded: Bool, _ value: String, age: Double = 0, alive: Bool = true) -> ProtectionState {
        ProtectionState.evaluate(loaded: loaded, raw: ["state": value, "updated_at": now-age], now: now, workerAlive: alive)
    }
    assert(status(false, "stopped", alive: false).canStart)
    assert(!status(false, "stopped", alive: false).canStop)
    assert(status(true, "monitoring").title == "保护已开启")
    assert(status(true, "camera_permission_required").title == "等待摄像头授权")
    assert(status(true, "monitoring", age: 40).title == "正在恢复监测")
    assert(status(false, "monitoring").canStop)
    assert(!status(false, "monitoring").canStart)
    assert(status(false, "monitoring", age: 80, alive: false).canStart)
    assert(status(true, "camera_retry").title == "正在重连摄像头")
    assert(status(true, "stopped").title == "正在启动保护")
    assert(status(true, "locked").canStop)
    let _ = NSApplication.shared
    let notReady = ControlApp()
    notReady.refresh()
    notReady.showWindow()
    assert(!notReady.refreshing && !notReady.ready)
    notReady.buildWindow()
    notReady.window.contentView!.layoutSubtreeIfNeeded()
    assert(notReady.window.contentView!.subviews.count == 1)
    assert(!notReady.window.isVisible)
    for name in [NSAppearance.Name.aqua, .darkAqua] {
        let label = FixedWhiteStatusText(frame: NSRect(x: 0, y: 0, width: 70, height: 24))
        label.text = "已关闭"
        label.appearance = NSAppearance(named: name)
        notReady.window.contentView!.addSubview(label)
        let pixels = label.bitmapImageRepForCachingDisplay(in: label.bounds)!
        label.cacheDisplay(in: label.bounds, to: pixels)
        var ink = 0, white = 0
        for y in 0..<pixels.pixelsHigh {
            for x in 0..<pixels.pixelsWide {
                guard let color = pixels.colorAt(x: x, y: y)?.usingColorSpace(.deviceRGB), color.alphaComponent > 0.2 else { continue }
                ink += 1
                if min(color.redComponent, color.greenComponent, color.blueComponent) > 0.9 { white += 1 }
            }
        }
        assert(ink > 0 && white == ink)
        assert(!label.allowsVibrancy && label.hitTest(.zero) == nil)
        print("Fixed status text render: \(name.rawValue), white pixels \(white)/\(ink)")
        label.removeFromSuperview()
    }
    print("16 UI state/startup/layout/render checks passed; no camera or service started.")
}

if CommandLine.arguments.contains("--self-test") { selfTest(); exit(0) }
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let controller = ControlApp()
app.delegate = controller
app.run()
