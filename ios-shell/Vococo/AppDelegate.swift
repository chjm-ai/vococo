import UIKit

@main
class AppDelegate: UIResponder, UIApplicationDelegate {

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        // 核心常亮:禁用系统自动锁屏,让 App 运行期间屏幕一直亮着。
        // 这是导航类 App 的做法,彻底绕开 PWA 里 iOS Wake Lock 会掉、
        // 掉了必须用户手势才能重新申请的坑(开车场景息屏断聊的根因)。
        application.isIdleTimerDisabled = true
        return true
    }

    func application(_ application: UIApplication,
                     configurationForConnecting connectingSceneSession: UISceneSession,
                     options: UIScene.ConnectionOptions) -> UISceneConfiguration {
        // 标准 Scene 生命周期配置(对应 Info.plist 里的 UIApplicationSceneManifest)
        UISceneConfiguration(name: "Default Configuration", sessionRole: connectingSceneSession.role)
    }
}
