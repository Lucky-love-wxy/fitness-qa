// 健身问答：原生 macOS 外壳，写法照 reading/app/mac/main.swift（Winner）。
// 界面是 fitness-qa/index.html（由本机 server.py 提供），这里负责：
//   - 自己拉起 / 退出时关掉 8775 上的 server.py，用户看不到终端和浏览器
//   - 原生窗口、编辑菜单（输入框要能 ⌘C/⌘V）、⌘N 新话题、⌘R 重新载入
//   - 视频能内嵌播放、能全屏；点出处后由页面脚本直接 play，不需要再点一次
// 页面文件直接读源码目录，改 index.html 不用重新打包；只有改本文件才需要 build.sh。
import Cocoa
import WebKit

// 源码目录由 build.sh 写进 Info.plist（FitnessQAProjectDir），App 装到别处也能找到 server.py
let projectDir = Bundle.main.object(forInfoDictionaryKey: "FitnessQAProjectDir") as? String ?? ""
let serverScript = projectDir + "/server.py"
let serverLog = projectDir + "/data/server.log"
let baseURL = URL(string: "http://127.0.0.1:8775/")!   // 端口已登记 ports-registry.json
// Finder 启动的 App 只有最小 PATH，按顺序找 python3
let pythonCandidates = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var ownedServer: Process?      // 只有自己拉起的才在退出时关；别人起的（如终端）不碰
    var loadRetries = 0

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()

        let config = WKWebViewConfiguration()
        config.mediaTypesRequiringUserActionForPlayback = []   // 点出处后视频在 loadedmetadata 回调里开播，默认策略会拦
        config.preferences.isElementFullscreenEnabled = true    // 视频控件的全屏按钮
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.setValue(false, forKey: "drawsBackground")  // 加载前不闪白

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1240, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "健身问答"
        window.minSize = NSSize(width: 720, height: 560)
        window.appearance = NSAppearance(named: .darkAqua)   // 小工具界面始终暗色（审美库）
        window.backgroundColor = NSColor(red: 0x1B / 255, green: 0x1A / 255, blue: 0x17 / 255, alpha: 1)
        window.contentView = webView
        window.center()
        window.setFrameAutosaveName("main")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        // 启动先清网页缓存再加载：页面直接读源码目录、随时在改，旧缓存会让窗口停在旧版
        let cacheTypes: Set<String> = [WKWebsiteDataTypeDiskCache, WKWebsiteDataTypeMemoryCache]
        WKWebsiteDataStore.default().removeData(ofTypes: cacheTypes, modifiedSince: .distantPast) { [weak self] in
            self?.ensureServer { ok in self?.loadOrFail(ok) }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        if let p = ownedServer, p.isRunning { p.terminate() }
    }

    // MARK: 后台服务

    func serverAlive(_ done: @escaping (Bool) -> Void) {
        var req = URLRequest(url: baseURL.appendingPathComponent("api/state"))
        req.timeoutInterval = 1
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            let ok = (resp as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async { done(ok) }
        }.resume()
    }

    /// 服务已在跑就直接用；否则拉起 server.py，最多等 10 秒（启动要载入 2.5MB 索引、建 BM25）。
    func ensureServer(_ done: @escaping (Bool) -> Void) {
        serverAlive { [self] alive in
            if alive { return done(true) }
            guard let py = pythonCandidates.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else {
                return done(false)
            }
            let p = Process()
            p.executableURL = URL(fileURLWithPath: py)
            p.arguments = [serverScript]
            p.currentDirectoryURL = URL(fileURLWithPath: projectDir)
            FileManager.default.createFile(atPath: serverLog, contents: nil)
            if let log = FileHandle(forWritingAtPath: serverLog) { p.standardOutput = log; p.standardError = log }
            do { try p.run() } catch { return done(false) }
            ownedServer = p
            waitForServer(deadline: Date().addingTimeInterval(10), done)
        }
    }

    func waitForServer(deadline: Date, _ done: @escaping (Bool) -> Void) {
        serverAlive { [self] alive in
            if alive { return done(true) }
            if Date() > deadline || ownedServer?.isRunning == false { return done(false) }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { self.waitForServer(deadline: deadline, done) }
        }
    }

    func showServerError() {
        let alert = NSAlert()
        alert.messageText = "健身问答的后台服务没起来"
        alert.informativeText = "端口 8775 可能被别的程序占用，或找不到 python3。\n日志：\(serverLog)\n查端口：lsof -nP -iTCP:8775 -sTCP:LISTEN"
        alert.addButton(withTitle: "重试")
        alert.addButton(withTitle: "退出")
        if alert.runModal() == .alertFirstButtonReturn {
            ensureServer { [weak self] ok in self?.loadOrFail(ok) }
        } else {
            NSApp.terminate(nil)
        }
    }

    func loadOrFail(_ ok: Bool) {
        if ok { webView.load(URLRequest(url: baseURL)) } else { showServerError() }
    }

    // 服务中途没了：重新拉起再载入，连续失败 3 次才报错
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        loadRetries += 1
        guard loadRetries <= 3 else { loadRetries = 0; return showServerError() }
        ensureServer { [weak self] ok in self?.loadOrFail(ok) }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { loadRetries = 0 }

    // target=_blank：不在 App 里开新窗口，交给默认浏览器
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    // MARK: 菜单（WKWebView 需要编辑菜单才响应 ⌘C/⌘V/⌘Z）

    func buildMenu() {
        let main = NSMenu()
        func add(_ menu: NSMenu, _ title: String, _ action: Selector?, _ key: String,
                 _ mods: NSEvent.ModifierFlags = .command, target: AnyObject? = nil) {
            let item = menu.addItem(withTitle: title, action: action, keyEquivalent: key)
            item.keyEquivalentModifierMask = mods
            item.target = target
        }
        func submenu(_ title: String) -> NSMenu {
            let item = main.addItem(withTitle: title, action: nil, keyEquivalent: "")
            let m = NSMenu(title: title)
            main.setSubmenu(m, for: item)
            return m
        }

        let app = submenu("健身问答")
        add(app, "隐藏", #selector(NSApplication.hide(_:)), "h")
        app.addItem(.separator())
        add(app, "退出", #selector(NSApplication.terminate(_:)), "q")

        let edit = submenu("编辑")
        add(edit, "撤销", Selector(("undo:")), "z")
        add(edit, "重做", Selector(("redo:")), "z", [.command, .shift])
        edit.addItem(.separator())
        add(edit, "剪切", #selector(NSText.cut(_:)), "x")
        add(edit, "拷贝", #selector(NSText.copy(_:)), "c")
        add(edit, "粘贴", #selector(NSText.paste(_:)), "v")
        add(edit, "全选", #selector(NSText.selectAll(_:)), "a")

        let view = submenu("视图")
        add(view, "新话题", #selector(newTopic), "n", target: self)
        add(view, "重新载入", #selector(reloadPage), "r", target: self)
        add(view, "进入 / 退出全屏", #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control])

        let win = submenu("窗口")
        add(win, "最小化", #selector(NSWindow.performMiniaturize(_:)), "m")
        add(win, "关闭", #selector(NSWindow.performClose(_:)), "w")

        NSApp.mainMenu = main
    }

    @objc func newTopic() { webView.evaluateJavaScript("document.getElementById('newBtn').click()") }

    @objc func reloadPage() { webView.load(URLRequest(url: baseURL)) }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
