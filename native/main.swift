// Claude 工作台：原生 macOS 窗口（WKWebView），显示本机工作台服务的界面。
// 只负责窗口、菜单、通知、下载和看护后台服务；CLI 仍由 server.py 在伪终端里以交互方式运行，请求头不变。
import Cocoa
import WebKit
import UserNotifications

let WB = ProcessInfo.processInfo.environment["CWB_DIR"] ?? (Bundle.main.object(forInfoDictionaryKey: "CWBDir") as? String) ?? (NSHomeDirectory() + "/claude-workbench")
let PORT = ProcessInfo.processInfo.environment["CWB_PORT"] ?? "47821"
let TOPBAR: CGFloat = 38  // 第十四轮 J：标题栏隐藏后，红绿灯垂直居中在页面顶部 38px 高的区域里

/* 第十四轮 J：文档开始时给 <html> 加 native-inset（页面据此给红绿灯留位置），并提前套上外观、字号偏好，避免先闪一下默认样式 */
let BOOT_JS = """
(function(){var r=document.documentElement;r.classList.add('native-inset');
try{var g=function(k){return JSON.parse(localStorage.getItem('cwb.pref.'+k)||'null')};
var t=g('theme');if(t==='light'||t==='dark')r.setAttribute('data-theme',t);
var f=g('font');if(f==='s'||f==='l')r.setAttribute('data-font',f);}catch(e){}})();
"""

final class App: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate,
                 WKScriptMessageHandler, WKDownloadDelegate, UNUserNotificationCenterDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var titleObs: NSKeyValueObservation?
    var retrying = false
    var lastDown: NSEvent?        // 第十四轮 J：最近一次在窗口里按下鼠标左键，页面要拖动窗口时用它 performDrag
    var notifyMode = UserDefaults.standard.string(forKey: "cwbNotify") ?? "bg"  // 页面“系统通知”偏好：bg / always / off

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(self, name: "cwb")
        // 第十一轮 B：告诉页面外壳支持读取剪贴板里的文件路径（粘贴访达里复制的文件）
        cfg.userContentController.addUserScript(WKUserScript(source: "window.CWB_NATIVE_PASTE = 1;", injectionTime: .atDocumentStart, forMainFrameOnly: true))
        cfg.userContentController.addUserScript(WKUserScript(source: "window.CWB_NATIVE_SHOT = 1;", injectionTime: .atDocumentStart, forMainFrameOnly: true))  // 第十三轮 G：外壳能截图（cwb.py shot）
        cfg.userContentController.addUserScript(WKUserScript(source: BOOT_JS, injectionTime: .atDocumentStart, forMainFrameOnly: true))  // 第十四轮 J
        applyAppearance(UserDefaults.standard.string(forKey: "cwbTheme") ?? "system")
        cfg.websiteDataStore = .default()
        web = WKWebView(frame: NSRect(x: 0, y: 0, width: 1440, height: 920), configuration: cfg)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsBackForwardNavigationGestures = false
        web.setValue(false, forKey: "drawsBackground")
        if #available(macOS 13.3, *) { web.isInspectable = true }
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1440, height: 920),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView], backing: .buffered, defer: false)
        window.title = "Claude 工作台"
        // 第十四轮 J：隐藏标题栏，内容铺到顶；红绿灯嵌在页面左上角（layoutTrafficLights），拖动和双击由页面的 [data-drag] 区域发消息
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.titlebarSeparatorStyle = .none
        window.isMovableByWindowBackground = false
        window.isReleasedWhenClosed = false
        window.contentView = web
        window.delegate = self
        window.minSize = NSSize(width: 760, height: 480)
        window.tabbingMode = .disallowed
        if !window.setFrameUsingName("CWBMain") { window.center() }
        window.setFrameAutosaveName("CWBMain")
        window.makeKeyAndOrderFront(nil)
        layoutTrafficLights()
        NSApp.activate(ignoringOtherApps: true)
        titleObs = web.observe(\.title, options: [.new]) { [weak self] w, _ in
            if let t = w.title, !t.isEmpty { self?.window.title = t; self?.layoutTrafficLights() }
        }
        NSEvent.addLocalMonitorForEvents(matching: .leftMouseDown) { [weak self] e in
            if let s = self, e.window === s.window { s.lastDown = e }
            return e
        }
        let c = UNUserNotificationCenter.current()
        c.delegate = self
        c.requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
        web.loadHTMLString(loadingPage("正在启动…"), baseURL: nil)
        startAndLoad()
        // 看护：后台服务意外掉线就拉起来（stop.sh 和长时间无窗口的主动退出会留标记，不拉起）
        Timer.scheduledTimer(withTimeInterval: 20, repeats: true) { _ in
            DispatchQueue.global().async { self.run(["--ensure"]) }
        }
    }

    func startAndLoad() {
        DispatchQueue.global().async {
            self.run(["--server"])
            DispatchQueue.main.async { self.load() }
        }
    }

    func load() {
        let tok = (try? String(contentsOfFile: "\(WB)/.run/token-\(PORT)", encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if let u = URL(string: "http://127.0.0.1:\(PORT)/?t=\(tok)") { web.load(URLRequest(url: u)) }
    }

    @discardableResult func run(_ args: [String]) -> Int32 {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = ["\(WB)/start.sh"] + args
        p.environment = ["HOME": NSHomeDirectory(), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "CWB_PORT": PORT]
        p.standardInput = FileHandle.nullDevice
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle(forWritingAtPath: "/tmp/cwb-launch.log") ?? FileHandle.nullDevice
        do { try p.run() } catch { return -1 }
        p.waitUntilExit()
        return p.terminationStatus
    }

    func loadingPage(_ msg: String) -> String {
        return """
        <html><head><meta charset="utf-8"><style>
        *{box-sizing:border-box}
        body{margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;
        font:14px/1.5 -apple-system,"SF Pro Text","PingFang SC",sans-serif;color:#9c9890;background:#1a1a19;-webkit-font-smoothing:antialiased}
        .mark{width:54px;height:54px;border-radius:14px;display:grid;place-items:center;
        background:linear-gradient(180deg,#272522,#141311);box-shadow:0 8px 24px rgba(0,0,0,.38),inset 0 0 0 1px rgba(255,255,255,.09);
        animation:pulse 1.8s ease-in-out infinite}
        .mark svg{width:34px;height:34px;display:block}
        @keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.78;transform:scale(.97)}}
        @media (prefers-color-scheme: light){
          body{background:#ffffff;color:#75726b}
          .mark{box-shadow:0 8px 24px rgba(28,26,22,.14),inset 0 0 0 1px rgba(255,255,255,.14)}
        }
        @media (prefers-reduced-motion: reduce){.mark{animation:none}}
        </style></head><body>
        <div class="mark"><svg viewBox="0 0 64 64" aria-hidden="true">
          <path d="M16 17l15 15-15 15" fill="none" stroke="#f2efe9" stroke-width="6.5" stroke-linecap="round" stroke-linejoin="round"/>
          <g transform="translate(44 43)" stroke="#e2835c" stroke-width="3.4" stroke-linecap="round">
            <line x1="0" y1="-10" x2="0" y2="10"/><line x1="-10" y1="0" x2="10" y2="0"/>
            <line x1="-7.1" y1="-7.1" x2="7.1" y2="7.1"/><line x1="7.1" y1="-7.1" x2="-7.1" y2="7.1"/>
          </g>
        </svg></div>
        <div>\(msg)</div></body></html>
        """
    }

    // ---- 第十四轮 J：红绿灯垂直居中在 TOPBAR 高的顶区（与 Electron trafficLightPosition 的做法相同：
    //      把标题栏容器设成 TOPBAR 高、按钮在容器里居中；AppKit 在缩放、改标题、切换主窗口时会重排，所以这些时候再排一次）
    func layoutTrafficLights() {
        guard let w = window, !w.styleMask.contains(.fullScreen),
              let close = w.standardWindowButton(.closeButton),
              let mini = w.standardWindowButton(.miniaturizeButton),
              let zoom = w.standardWindowButton(.zoomButton),
              let container = close.superview?.superview else { return }
        var f = container.frame
        f.size.height = TOPBAR
        f.origin.y = w.frame.height - TOPBAR
        if container.frame != f { container.frame = f }
        for b in [close, mini, zoom] {
            let y = ((b.superview?.frame.height ?? TOPBAR) - b.frame.height) / 2
            if abs(b.frame.origin.y - y) > 0.5 { b.setFrameOrigin(NSPoint(x: b.frame.origin.x, y: y)) }
        }
    }
    func windowDidResize(_ n: Notification) { layoutTrafficLights() }
    func windowDidBecomeKey(_ n: Notification) { layoutTrafficLights() }
    func windowDidResignKey(_ n: Notification) { layoutTrafficLights() }
    func windowDidEnterFullScreen(_ n: Notification) { syncFullScreen() }
    func windowDidExitFullScreen(_ n: Notification) { syncFullScreen(); layoutTrafficLights() }
    /* 全屏时没有红绿灯，页面去掉左上角留白 */
    func syncFullScreen() {
        let fs = window.styleMask.contains(.fullScreen)
        web.evaluateJavaScript("document.documentElement.classList.toggle('native-fs', \(fs))", completionHandler: nil)
    }
    func webView(_ w: WKWebView, didFinish n: WKNavigation!) { syncFullScreen() }
    /* 双击页面顶区：按系统设置（桌面与程序坞 › 连按窗口标题栏）缩放或最小化 */
    func titlebarDoubleClick() {
        let g = UserDefaults.standard.persistentDomain(forName: UserDefaults.globalDomain) ?? [:]
        var act = (g["AppleActionOnDoubleClick"] as? String) ?? ""
        if act.isEmpty, (g["AppleMiniaturizeOnDoubleClick"] as? Bool) == true { act = "Minimize" }
        switch act {
        case "Minimize": window.performMiniaturize(nil)
        case "None", "Do Nothing": break
        default: window.performZoom(nil)   // Maximize / Fill / 未设置：缩放到铺满可用区域，再双击还原
        }
    }
    func applyAppearance(_ t: String) {
        NSApp.appearance = t == "dark" ? NSAppearance(named: .darkAqua) : t == "light" ? NSAppearance(named: .aqua) : nil
    }

    // ---- 窗口：关掉只是隐藏；点 Dock 图标回到这个窗口
    func windowShouldClose(_ sender: NSWindow) -> Bool { window.orderOut(nil); return false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showWindow(); return true }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    @objc func showWindow() {
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // ---- 页面导航：工作台自己的页面在窗口里打开，其他链接交给默认浏览器
    func webView(_ w: WKWebView, decidePolicyFor a: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if a.shouldPerformDownload { decisionHandler(.download); return }
        guard let u = a.request.url else { decisionHandler(.cancel); return }
        // 第十一轮 B：右栏“网页预览”的 iframe（本机开发服务）在框里打开，不交给浏览器
        if let f = a.targetFrame, !f.isMainFrame { decisionHandler(.allow); return }
        let s = u.scheme ?? ""
        if ["about", "blob", "data"].contains(s) || (u.host == "127.0.0.1" && String(u.port ?? 0) == PORT) {
            decisionHandler(.allow); return
        }
        NSWorkspace.shared.open(u)
        decisionHandler(.cancel)
    }
    func webView(_ w: WKWebView, decidePolicyFor r: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        decisionHandler(r.canShowMIMEType ? .allow : .download)
    }
    func webView(_ w: WKWebView, createWebViewWith c: WKWebViewConfiguration, for a: WKNavigationAction, windowFeatures f: WKWindowFeatures) -> WKWebView? {
        if let u = a.request.url { NSWorkspace.shared.open(u) }
        return nil
    }
    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) { retry() }
    func webViewWebContentProcessDidTerminate(_ w: WKWebView) { load() }
    func retry() {
        if retrying { return }
        retrying = true
        web.loadHTMLString(loadingPage("正在连接后台服务…"), baseURL: nil)
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            self.retrying = false
            self.startAndLoad()
        }
    }

    // ---- 下载（导出 Markdown/网页）：弹“存储为”
    func webView(_ w: WKWebView, navigationAction: WKNavigationAction, didBecome d: WKDownload) { d.delegate = self }
    func webView(_ w: WKWebView, navigationResponse: WKNavigationResponse, didBecome d: WKDownload) { d.delegate = self }
    func download(_ d: WKDownload, decideDestinationUsing r: URLResponse, suggestedFilename name: String, completionHandler: @escaping (URL?) -> Void) {
        let p = NSSavePanel()
        p.nameFieldStringValue = name
        p.directoryURL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
        p.beginSheetModal(for: window) { resp in
            guard resp == .OK, let u = p.url else { completionHandler(nil); return }
            try? FileManager.default.removeItem(at: u)
            completionHandler(u)
        }
    }

    // ---- 网页里的 alert / confirm / prompt / 选择文件
    func webView(_ w: WKWebView, runJavaScriptAlertPanelWithMessage m: String, initiatedByFrame f: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert(); a.messageText = m; a.addButton(withTitle: "好")
        a.beginSheetModal(for: window) { _ in completionHandler() }
    }
    func webView(_ w: WKWebView, runJavaScriptConfirmPanelWithMessage m: String, initiatedByFrame f: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert(); a.messageText = m; a.addButton(withTitle: "确定"); a.addButton(withTitle: "取消")
        a.beginSheetModal(for: window) { r in completionHandler(r == .alertFirstButtonReturn) }
    }
    func webView(_ w: WKWebView, runJavaScriptTextInputPanelWithPrompt p: String, defaultText d: String?, initiatedByFrame f: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let a = NSAlert(); a.messageText = p
        let tf = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24)); tf.stringValue = d ?? ""
        a.accessoryView = tf; a.addButton(withTitle: "确定"); a.addButton(withTitle: "取消")
        a.beginSheetModal(for: window) { r in completionHandler(r == .alertFirstButtonReturn ? tf.stringValue : nil) }
    }
    func webView(_ w: WKWebView, runOpenPanelWith p: WKOpenPanelParameters, initiatedByFrame f: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let o = NSOpenPanel(); o.allowsMultipleSelection = p.allowsMultipleSelection; o.canChooseDirectories = p.allowsDirectories; o.canChooseFiles = true
        o.beginSheetModal(for: window) { r in completionHandler(r == .OK ? o.urls : nil) }
    }

    // ---- 页面发来的消息：系统通知、Dock 角标
    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        // 只接受工作台主页面的消息：网页预览 iframe 里的第三方页面不能借这个通道弹通知、读剪贴板文件路径、截图
        guard m.frameInfo.isMainFrame, let mu = m.frameInfo.request.url, mu.host == "127.0.0.1", String(mu.port ?? 0) == PORT else { return }
        guard let d = m.body as? [String: Any], let t = d["type"] as? String else { return }
        if t == "notify" {
            if notifyMode == "off" { return }  // 第十四轮 J：设置里关掉了系统通知
            let n = UNMutableNotificationContent()
            n.title = (d["title"] as? String) ?? "Claude 工作台"
            n.body = (d["text"] as? String) ?? ""
            n.userInfo = ["sid": (d["sid"] as? String) ?? ""]
            n.sound = .default
            UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: n, trigger: nil))
            if !NSApp.isActive { NSApp.requestUserAttention(.informationalRequest) }
        } else if t == "winDrag" {
            // 第十四轮 J：页面 [data-drag] 区域按下。只用刚发生、左键仍按着的那次按下，避免拿旧事件拖
            if let e = lastDown, NSEvent.pressedMouseButtons & 1 == 1, ProcessInfo.processInfo.systemUptime - e.timestamp < 1.5 {
                window.performDrag(with: e)
            }
        } else if t == "winDbl" {
            titlebarDoubleClick()
        } else if t == "prefs" {
            // 第十四轮 J：外观（跟随系统 / 浅色 / 深色）与系统通知偏好；存起来，下次启动在页面载入前就生效
            if let th = d["theme"] as? String, ["system", "light", "dark"].contains(th) {
                UserDefaults.standard.set(th, forKey: "cwbTheme"); applyAppearance(th)
            }
            if let nm = d["notify"] as? String, ["bg", "always", "off"].contains(nm) {
                UserDefaults.standard.set(nm, forKey: "cwbNotify"); notifyMode = nm
            }
        } else if t == "badge" {
            let k = (d["n"] as? Int) ?? 0
            NSApp.dockTile.badgeLabel = k > 0 ? String(k) : nil
        } else if t == "pasteFiles" {
            // 第十一轮 B：读剪贴板里的文件 URL（只要 fileURL），把路径数组回传给页面的回调
            let id = (d["id"] as? Int) ?? 0
            let urls = NSPasteboard.general.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL] ?? []
            let paths = urls.filter { $0.isFileURL }.map { $0.path }
            let json = (try? JSONSerialization.data(withJSONObject: paths)).flatMap { String(data: $0, encoding: .utf8) } ?? "[]"
            web.evaluateJavaScript("window.CWB_pasteFiles && window.CWB_pasteFiles(\(id), \(json))", completionHandler: nil)
        } else if t == "shot" {
            // 第十三轮 G：cwb.py shot，在不显示的网页视图里截图（shot.swift），结果回传页面，页面交给后台保存
            Shooter.start(d) { [weak self] id, png, err in
                let r: [String: String] = ["id": id, "png": png ?? "", "error": err ?? ""]
                let json = (try? JSONSerialization.data(withJSONObject: r)).flatMap { String(data: $0, encoding: .utf8) } ?? "{}"
                self?.web.evaluateJavaScript("window.CWB_shotDone && window.CWB_shotDone(\(json))", completionHandler: nil)
            }
        }
    }
    func userNotificationCenter(_ c: UNUserNotificationCenter, didReceive r: UNNotificationResponse, withCompletionHandler h: @escaping () -> Void) {
        let sid = ((r.notification.request.content.userInfo["sid"] as? String) ?? "").filter { $0.isHexDigit || $0 == "-" }
        DispatchQueue.main.async {
            self.showWindow()
            if !sid.isEmpty { self.web.evaluateJavaScript("window.CWB && CWB.openSession('\(sid)')", completionHandler: nil) }
        }
        h()
    }
    func userNotificationCenter(_ c: UNUserNotificationCenter, willPresent n: UNNotification, withCompletionHandler h: @escaping (UNNotificationPresentationOptions) -> Void) {
        h([.banner, .sound])
    }

    // ---- 菜单（复制粘贴等快捷键要靠“编辑”菜单才能在网页里生效）
    @objc func reloadPage() { load() }
    @objc func openSettings() {  // 第十四轮 J：设置面板在页面里（ux-j.js 的 CWB.openSettings）
        showWindow()
        web.evaluateJavaScript("window.CWB && CWB.openSettings && CWB.openSettings()", completionHandler: nil)
    }
    @objc func zoomIn() { web.pageZoom = min(2.0, web.pageZoom + 0.1) }
    @objc func zoomOut() { web.pageZoom = max(0.6, web.pageZoom - 0.1) }
    @objc func zoomReset() { web.pageZoom = 1.0 }
    func buildMenu() {
        let main = NSMenu()
        func sub(_ title: String) -> NSMenu { let i = NSMenuItem(); let m = NSMenu(title: title); i.submenu = m; main.addItem(i); return m }
        let a = sub("Claude 工作台")
        a.addItem(withTitle: "关于 Claude 工作台", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        a.addItem(.separator())
        a.addItem(withTitle: "设置…", action: #selector(openSettings), keyEquivalent: ",").target = self
        a.addItem(.separator())
        a.addItem(withTitle: "隐藏 Claude 工作台", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        a.addItem(withTitle: "隐藏其他", action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h").keyEquivalentModifierMask = [.command, .option]
        a.addItem(withTitle: "全部显示", action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: "")
        a.addItem(.separator())
        a.addItem(withTitle: "退出 Claude 工作台", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let e = sub("编辑")
        e.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        e.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "z").keyEquivalentModifierMask = [.command, .shift]
        e.addItem(.separator())
        e.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        e.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        e.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        e.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        let v = sub("显示")
        v.addItem(withTitle: "重新载入", action: #selector(reloadPage), keyEquivalent: "r").target = self
        v.addItem(.separator())
        v.addItem(withTitle: "实际大小", action: #selector(zoomReset), keyEquivalent: "0").target = self
        v.addItem(withTitle: "放大", action: #selector(zoomIn), keyEquivalent: "=").target = self
        v.addItem(withTitle: "缩小", action: #selector(zoomOut), keyEquivalent: "-").target = self
        v.addItem(.separator())
        v.addItem(withTitle: "进入全屏幕", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f").keyEquivalentModifierMask = [.command, .control]
        let w = sub("窗口")
        w.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        w.addItem(withTitle: "关闭窗口", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        w.addItem(.separator())
        w.addItem(withTitle: "工作台", action: #selector(showWindow), keyEquivalent: "1").target = self
        NSApp.windowsMenu = w
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
