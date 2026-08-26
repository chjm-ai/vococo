import UIKit
import WebKit

/// vococo PWA 线上地址(cloudflared 隧道 vococo.chjm.cc → 本地 8848)
private let kVococoURL = URL(string: "https://vococo.chjm.cc/")!

final class ViewController: UIViewController {

    private var webView: WKWebView!

    override func viewDidLoad() {
        super.viewDidLoad()
        setupWebView()
        loadURL()
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        // 常亮兜底(主设置在 AppDelegate,这里保证页面出现时一定生效)
        UIApplication.shared.isIdleTimerDisabled = true
    }

    // MARK: - WebView

    private func setupWebView() {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true          // 语音通话内联播放(不用全屏拉起)
        config.mediaTypesRequiringUserActionForPlayback = []  // 允许自动播放(TTS/来电提示音)
        config.websiteDataStore = .default()             // 保留登录态,同 PWA 一样持久

        webView = WKWebView(frame: .zero, configuration: config)
        webView.allowsBackForwardNavigationGestures = true
        webView.navigationDelegate = self
        view.addSubview(webView)

        webView.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            webView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            webView.topAnchor.constraint(equalTo: view.topAnchor),
            webView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
    }

    private func loadURL() {
        webView.load(URLRequest(url: kVococoURL))
    }
}

// MARK: - 加载失败提示(第一次装机排查用)

extension ViewController: WKNavigationDelegate {
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        showLoadError(error)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        showLoadError(error)
    }

    private func showLoadError(_ error: Error) {
        let msg = "加载失败:\(error.localizedDescription)\n\n请确认:\n1. 手机网络正常\n2. vococo 服务在线(vococo.chjm.cc 走 cloudflared 隧道)"
        let alert = UIAlertController(title: "连接不上 vococo", message: msg, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "重试", style: .default) { [weak self] _ in
            self?.loadURL()
        })
        present(alert, animated: true)
    }
}
