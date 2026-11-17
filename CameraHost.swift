import AppKit
import AVFoundation
import Darwin

// This app supplies a stable, named macOS camera permission identity.
final class CameraHost: NSObject, NSApplicationDelegate {
    let root = Bundle.main.bundleURL.deletingLastPathComponent()
    var worker: Process?
    var requested = false
    var timer: Timer?

    func status(_ state: String) {
        let path = root.appendingPathComponent("state/status.json")
        let payload: [String: Any] = ["state": state, "pid": getpid(),
                                     "host_pid": getpid(), "updated_at": Date().timeIntervalSince1970]
        if let data = try? JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted]) {
            try? data.write(to: path, options: [.atomic])
            try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
        }
    }

    func checkPermission() {
        guard worker == nil else { return }
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            startWorker()
        case .notDetermined:
            status("camera_permission_required")
            if !requested {
                requested = true
                print("等待授权：请允许 Screen Privacy Guard 使用摄像头。")
                NSApp.activate(ignoringOtherApps: true)
                AVCaptureDevice.requestAccess(for: .video) { _ in
                    DispatchQueue.main.async { self.checkPermission() }
                }
            }
        case .denied, .restricted:
            status("camera_permission_denied")
        @unknown default:
            status("camera_permission_unknown")
        }
    }

    func startWorker() {
        if CommandLine.arguments.count == 1 {
            status("camera_permission_granted")
            print("摄像头授权已获得。后台服务会自动使用这项授权。")
            exit(0)
        }
        let process = Process()
        process.executableURL = root.appendingPathComponent(".venv/bin/python")
        process.arguments = Array(CommandLine.arguments.dropFirst())
        process.currentDirectoryURL = root
        process.standardOutput = FileHandle.standardOutput
        process.standardError = FileHandle.standardError
        process.terminationHandler = { p in exit(p.terminationStatus) }
        do {
            try process.run()
            worker = process
            timer?.invalidate()
            print("摄像头授权已获得；工作进程 pid=\(process.processIdentifier)")
        } catch {
            print("启动摄像头工作进程失败：\(error)")
            exit(1)
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { _ in self.checkPermission() }
        checkPermission()
    }
}

setbuf(stdout, nil)
setbuf(stderr, nil)
let application = NSApplication.shared
application.setActivationPolicy(.accessory)
let cameraHost = CameraHost()
application.delegate = cameraHost
application.run()
