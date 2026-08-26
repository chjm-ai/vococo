import UIKit

class SceneDelegate: UIResponder, UIWindowSceneDelegate {

    var window: UIWindow?

    func scene(_ scene: UIScene, willConnectTo session: UISceneSession,
               options connectionOptions: UIScene.ConnectionOptions) {
        guard let windowScene = scene as? UIWindowScene else { return }
        let window = UIWindow(windowScene: windowScene)
        window.rootViewController = ViewController()
        window.makeKeyAndVisible()
        self.window = window
    }

    func sceneDidBecomeActive(_ scene: UIScene) {
        // 回前台时再确认一次常亮,防止系统在后台阶段重置过该开关。
        // (原生壳下不会像 PWA 的 Wake Lock 那样掉,这里只是双保险)
        UIApplication.shared.isIdleTimerDisabled = true
    }

    // 后台行为对齐现有 PWA 的「显式挂起」设计:
    // 苹果平台不允许后台录音,进后台时 WKWebView 会被系统自动挂起,
    // 回前台页面自动恢复,前端 voice.js 的 visibilitychange 逻辑会重连。
    // 所以这里不需要也不应该做任何后台保持。
}
