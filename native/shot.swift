// 第十三轮 G：cwb.py shot 的截图。用一个不显示的 WKWebView（放在屏幕外的无边框窗口里）加载网址，
// 加载完、页面稳定后 takeSnapshot，画成正好 宽×高 像素的 PNG，base64 交给 reply（main.swift 回传页面，页面再交给后台保存）。
import Cocoa
import WebKit

final class Shooter: NSObject, WKNavigationDelegate, WKUIDelegate {
    static var live: [String: Shooter] = [:]
    let id: String, w: Int, h: Int
    let reply: (String, String?, String?) -> Void   // id, PNG 的 base64, 错误
    let win: NSWindow
    let web: WKWebView
    var finished = false, loadedAt: Date?
    var lastSig = "", same = 0, pollGen = 0

    /// d: {id, url, w, h}。外壳超时 28 秒（后台总超时 30 秒）。
    static func start(_ d: [String: Any], reply: @escaping (String, String?, String?) -> Void) {
        let id = String(((d["id"] as? String) ?? "").filter { $0.isHexDigit }.prefix(32))
        if id.isEmpty { return }
        let w = max(320, min(3840, (d["w"] as? Int) ?? 1280)), h = max(240, min(4000, (d["h"] as? Int) ?? 800))
        guard let s = d["url"] as? String, let u = URL(string: s), ["http", "https"].contains(u.scheme?.lowercased() ?? "") else {
            reply(id, nil, "网址无效"); return
        }
        let sh = Shooter(id: id, w: w, h: h, reply: reply)
        live[id] = sh
        sh.web.load(URLRequest(url: u, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 25))
        DispatchQueue.main.asyncAfter(deadline: .now() + 28) { [weak sh] in sh?.end(nil, "页面加载超时") }
    }

    init(id: String, w: Int, h: Int, reply: @escaping (String, String?, String?) -> Void) {
        self.id = id; self.w = w; self.h = h; self.reply = reply
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .default()
        let r = NSRect(x: 0, y: 0, width: w, height: h)
        web = WKWebView(frame: r, configuration: cfg)
        win = NSWindow(contentRect: NSRect(x: -30000, y: -30000, width: w, height: h), styleMask: [.borderless], backing: .buffered, defer: false)
        super.init()
        web.navigationDelegate = self
        web.uiDelegate = self
        win.isReleasedWhenClosed = false
        win.isExcludedFromWindowsMenu = true
        win.ignoresMouseEvents = true
        win.hasShadow = false
        win.collectionBehavior = [.transient, .ignoresCycle, .stationary]
        win.contentView = web
        win.setFrameOrigin(NSPoint(x: -30000, y: -30000))
        win.orderBack(nil)   // 在屏幕外：用户看不到，也不进窗口循环和 Mission Control
    }

    // ---- 导航：只在这个视图里加载，不交给浏览器；弹窗、下载一律不要
    func webView(_ v: WKWebView, decidePolicyFor a: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        let s = a.request.url?.scheme?.lowercased() ?? ""
        decisionHandler(a.shouldPerformDownload ? .cancel : (["http", "https", "about", "blob", "data"].contains(s) ? .allow : .cancel))
    }
    func webView(_ v: WKWebView, decidePolicyFor r: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if r.isForMainFrame && !r.canShowMIMEType { decisionHandler(.cancel); end(nil, "这个网址不是网页"); return }
        decisionHandler(.allow)
    }
    func webView(_ v: WKWebView, createWebViewWith c: WKWebViewConfiguration, for a: WKNavigationAction, windowFeatures f: WKWindowFeatures) -> WKWebView? { nil }
    func webView(_ v: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) { failed(e) }
    func webView(_ v: WKWebView, didFail n: WKNavigation!, withError e: Error) { failed(e) }
    func webViewWebContentProcessDidTerminate(_ v: WKWebView) { end(nil, "页面进程意外退出") }
    func failed(_ e: Error) {
        let ne = e as NSError
        if ne.domain == NSURLErrorDomain && ne.code == NSURLErrorCancelled { return }   // 重定向、页面自己跳转
        if ne.domain == "WebKitErrorDomain" && ne.code == 102 { return }                // 框架加载被打断（多半是跳转）
        end(nil, "页面加载失败：" + ne.localizedDescription)
    }
    func webView(_ v: WKWebView, didFinish n: WKNavigation!) {
        loadedAt = Date(); lastSig = ""; same = 0; pollGen += 1
        poll(pollGen)
    }

    // ---- 稳定：每 250ms 看一次文档状态、正文长度、页面高度、字体；连续 3 次不变（且加载完至少 0.6 秒）就截，最多等 6 秒
    func poll(_ g: Int) {
        if finished || g != pollGen { return }
        let js = "[document.readyState, document.body ? document.body.innerHTML.length : 0, document.documentElement ? document.documentElement.scrollHeight : 0, document.fonts ? document.fonts.status : ''].join('|')"
        web.evaluateJavaScript(js) { [weak self] r, _ in
            guard let self = self, !self.finished, g == self.pollGen, let t0 = self.loadedAt else { return }
            let sig = (r as? String) ?? ""
            self.same = sig == self.lastSig ? self.same + 1 : 0
            self.lastSig = sig
            let age = Date().timeIntervalSince(t0)
            let ready = sig.hasPrefix("complete|") && !sig.hasSuffix("|loading")
            if (ready && self.same >= 2 && age >= 0.6) || age >= 6 { self.snap(); return }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [weak self] in self?.poll(g) }
        }
    }

    func snap() {
        if finished { return }
        let c = WKSnapshotConfiguration()
        c.rect = NSRect(x: 0, y: 0, width: w, height: h)
        c.snapshotWidth = NSNumber(value: w)
        c.afterScreenUpdates = true
        web.takeSnapshot(with: c) { [weak self] img, err in
            guard let self = self else { return }
            guard let img = img, let png = self.png(img) else { self.end(nil, "截图失败" + (err.map { "：" + $0.localizedDescription } ?? "")); return }
            self.end(png.base64EncodedString(), nil)
        }
    }

    /// 不管屏幕是不是 Retina，都画成正好 w×h 像素（白底，透明页面也看得清）
    func png(_ img: NSImage) -> Data? {
        guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: w, pixelsHigh: h, bitsPerSample: 8, samplesPerPixel: 4,
                                         hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0),
              let ctx = NSGraphicsContext(bitmapImageRep: rep) else { return nil }
        rep.size = NSSize(width: w, height: h)
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = ctx
        let r = NSRect(x: 0, y: 0, width: w, height: h)
        NSColor.white.setFill(); r.fill()
        img.draw(in: r, from: .zero, operation: .sourceOver, fraction: 1)
        NSGraphicsContext.restoreGraphicsState()
        return rep.representation(using: .png, properties: [:])
    }

    func end(_ png: String?, _ err: String?) {
        if finished { return }
        finished = true
        reply(id, png, err)
        web.stopLoading()
        web.navigationDelegate = nil
        web.uiDelegate = nil
        win.contentView = nil
        win.orderOut(nil)
        win.close()
        Shooter.live[id] = nil
    }
}
