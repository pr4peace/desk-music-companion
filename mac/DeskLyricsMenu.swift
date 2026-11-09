// The Mac's menu bar helper. It shows what's playing and looks after the Desk Music Companion
// server, which keeps running on its own as a login item whether this helper is open or not.
import AppKit
import SystemConfiguration

private let port = 8787
private let serverLabel = "com.desklyrics.server"
private let menuLabel = "com.desklyrics.menu"
private let agents = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/LaunchAgents")
private let serverPlist = agents.appendingPathComponent("\(serverLabel).plist")
private let menuPlist = agents.appendingPathComponent("\(menuLabel).plist")
private let logFile = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/DeskLyrics.log")

private struct Status {
    var running = false
    var nowPlaying: String?
}

@discardableResult
private func launchctl(_ arguments: String...) -> Bool {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
    process.arguments = arguments
    process.standardOutput = FileHandle.nullDevice
    process.standardError = FileHandle.nullDevice
    do { try process.run() } catch { return false }
    process.waitUntilExit()
    return process.terminationStatus == 0
}

private var session: String { "gui/\(getuid())" }

private func phoneAddress() -> String {
    let name = SCDynamicStoreCopyLocalHostName(nil) as String? ?? "your-mac"
    return "http://\(name).local:\(port)"
}

// A ring around a lyric line, in the menu bar's own colour.
private func menuBarImage(dimmed: Bool) -> NSImage {
    let image = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { _ in
        NSColor.black.withAlphaComponent(dimmed ? 0.4 : 1).set()
        let ring = NSBezierPath(ovalIn: NSRect(x: 1.75, y: 1.75, width: 14.5, height: 14.5))
        ring.lineWidth = 1.6
        ring.stroke()
        for (y, width) in [(11.0, 5.0), (8.1, 9.0), (5.2, 6.0)] {
            NSBezierPath(roundedRect: NSRect(x: 9 - width / 2, y: y, width: width, height: 1.8), xRadius: 0.9, yRadius: 0.9).fill()
        }
        return true
    }
    image.isTemplate = true
    return image
}

@main
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var item: NSStatusItem!
    private var status = Status()
    private var timer: Timer?

    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        app.run()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let others = NSRunningApplication.runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier ?? "")
        if others.count > 1 { NSApp.terminate(nil); return }

        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.button?.image = menuBarImage(dimmed: true)
        item.button?.toolTip = "Desk Music Companion"
        let menu = NSMenu()
        menu.delegate = self
        item.menu = menu
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in self?.refresh() }
    }

    private func refresh() {
        var request = URLRequest(url: URL(string: "http://127.0.0.1:\(port)/api/state")!)
        request.timeoutInterval = 2
        URLSession.shared.dataTask(with: request) { data, _, error in
            var next = Status()
            if error == nil, let data, let state = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                next.running = true
                if let track = state["track"] as? [String: Any], let name = track["name"] as? String {
                    let artist = track["artist"] as? String ?? ""
                    next.nowPlaying = artist.isEmpty ? name : "\(name) — \(artist)"
                }
            }
            DispatchQueue.main.async {
                self.status = next
                self.item.button?.image = menuBarImage(dimmed: !next.running)
            }
        }.resume()
    }

    private func info(_ title: String) -> NSMenuItem {
        let line = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        line.isEnabled = false
        return line
    }

    private func action(_ title: String, _ selector: Selector) -> NSMenuItem {
        let line = NSMenuItem(title: title, action: selector, keyEquivalent: "")
        line.target = self
        return line
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let playing = status.nowPlaying.map { $0.count > 46 ? String($0.prefix(45)) + "…" : $0 }
        menu.addItem(info(playing ?? "Nothing playing"))
        menu.addItem(.separator())
        menu.addItem(info("iPhone address: \(phoneAddress())"))
        menu.addItem(action("Copy iPhone Address", #selector(copyAddress)))
        menu.addItem(action("Open in Browser", #selector(openPage)))
        menu.addItem(.separator())
        menu.addItem(info(status.running ? "Server is running" : "Server is stopped"))
        if status.running {
            menu.addItem(action("Restart Server", #selector(restartServer)))
            menu.addItem(action("Stop Server", #selector(stopServer)))
        } else {
            menu.addItem(action("Start Server", #selector(startServer)))
        }
        menu.addItem(.separator())
        let login = action("Open Menu Helper at Login", #selector(toggleLogin))
        login.state = FileManager.default.fileExists(atPath: menuPlist.path) ? .on : .off
        menu.addItem(login)
        menu.addItem(action("Show Log", #selector(showLog)))
        menu.addItem(.separator())
        menu.addItem(action("Quit Menu Helper", #selector(quit)))
    }

    @objc private func copyAddress() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(phoneAddress(), forType: .string)
    }

    @objc private func openPage() { NSWorkspace.shared.open(URL(string: "http://127.0.0.1:\(port)/")!) }

    @objc private func startServer() {
        launchctl("bootstrap", session, serverPlist.path)
        refreshSoon()
    }

    @objc private func restartServer() {
        launchctl("kickstart", "-k", "\(session)/\(serverLabel)")
        refreshSoon()
    }

    @objc private func stopServer() {
        launchctl("bootout", "\(session)/\(serverLabel)")
        refreshSoon()
    }

    private func refreshSoon() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { self.refresh() }
    }

    // Takes effect at the next login, so turning it off never closes the helper in use.
    @objc private func toggleLogin() {
        let manager = FileManager.default
        if manager.fileExists(atPath: menuPlist.path) {
            try? manager.removeItem(at: menuPlist)
            return
        }
        guard let program = Bundle.main.executablePath else { return }
        let settings: [String: Any] = ["Label": menuLabel, "ProgramArguments": [program], "RunAtLoad": true, "LimitLoadToSessionType": "Aqua"]
        if let data = try? PropertyListSerialization.data(fromPropertyList: settings, format: .xml, options: 0) {
            try? manager.createDirectory(at: agents, withIntermediateDirectories: true)
            try? data.write(to: menuPlist)
        }
    }

    @objc private func showLog() { NSWorkspace.shared.open(logFile) }

    @objc private func quit() { NSApp.terminate(nil) }
}
