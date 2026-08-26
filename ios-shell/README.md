# ios-shell — vococo iOS 原生套壳 App

用 WKWebView 把 vococo 的 PWA(https://vococo.chjm.cc/) 包成原生 App,解决开车场景下 PWA 息屏断聊的问题。

## 为什么做原生壳

之前排查定案:iOS 的 PWA 里 Wake Lock(屏幕常亮锁)会掉,掉了必须用户手势才能重新申请,开车时手机一锁屏,语音对话就断。Web 端已经做过「掉锁立刻补申请 + 看门狗」的修复,但原生壳可以直接用系统的 `isIdleTimerDisabled` 让屏幕物理常亮,这是导航类 App 的做法,比 Wake Lock 稳得多,不依赖网页逻辑。

## 工程结构

```
ios-shell/
├── Vococo.xcodeproj/           # Xcode 工程(直接打开)
└── Vococo/
    ├── AppDelegate.swift       # 常亮核心:isIdleTimerDisabled = true
    ├── SceneDelegate.swift     # Scene 生命周期 + 回前台再次确认常亮
    ├── ViewController.swift    # WKWebView 加载 PWA,加载失败弹窗重试
    ├── Info.plist              # 麦克风权限 / 横竖屏 / Scene manifest
    └── Assets.xcassets/        # App 图标(用的 PWA 的 icon-512 放大)
```

## 加载的 URL

`https://vococo.chjm.cc/` — vococo Web 服务经 cloudflared 隧道对外(`vococo.chjm.cc → localhost:8848`),URL 常量在 `Vococo/ViewController.swift` 顶部 `kVococoURL`,要换地址改这一处即可。

## 屏幕常亮怎么实现的

**三处**:

1. `AppDelegate.swift:9` — `application.isIdleTimerDisabled = true`,App 启动即常亮(核心)。
2. `SceneDelegate.swift:19` — `sceneDidBecomeActive` 里再设一次,防系统在后台阶段重置开关(双保险)。
3. `ViewController.swift:24` — `viewDidAppear` 兜底。

只要 App 在前台,系统自动锁屏计时就不会触发。进后台(按 Home 键/来电等)时系统照常锁屏——这是有意为之:苹果平台不允许后台录音,行为对齐 PWA 的「显式挂起」设计,回前台时 WKWebView 自动恢复,前端 `voice.js` 的 `visibilitychange` 逻辑会自动重连,无需壳做任何事。

## 怎么装到 iPhone

### 1. 打开工程

双击 `Vococo.xcodeproj` 用 Xcode 打开(需 macOS 上装了 Xcode 15+,命令行工具不行)。

### 2. 选签名

- 左侧点工程文件 → TARGETS 选 **Vococo** → 顶部 **Signing & Capabilities**。
- **Team** 下拉选你自己的 Apple ID 开发者账号(免费账号即可)。
- 若报 "No profiles found",等 Xcode 自动生成,或到 **Signing** 里勾选 Automatically manage signing。
- Bundle ID 默认 `com.chjm.vococo`,想换在 **General → Bundle Identifier** 改,注意免费账号的 Bundle ID 不能和别人重复。

### 3. 真机安装(开车主力机)

- iPhone 用数据线连 Mac,手机弹「信任此电脑」点信任。
- Xcode 顶部设备栏选你的 iPhone,点 ▶ Run。
- 第一次会提示手机没信任开发者:**iPhone 设置 → 通用 → VPN 与设备管理 → 开发者 App → 信任**。
- 信任后断开数据线也能用,但**免费账号签名的 App 每 7 天过期**,过期后要再连一次 Mac 点 Run 续期。

### 4. TestFlight(可选)

需要付费开发者账号(¥688/年):
- Xcode → Product → Archive,然后 Window → Organizer 里 Distribute App → App Store Connect。
- 手机上装 TestFlight App,接受测试邀请即可。TestFlight 版没有 7 天过期问题,适合长期自用。

## 常见问题

| 现象 | 处理 |
|---|---|
| 打开白屏/加载失败弹窗 | 确认 vococo 服务在线(`vococo doctor` / 日志里「✅ Web 已上线」)、手机网络正常,点重试 |
| 语音没声音/麦克风没反应 | 第一次说话时允许麦克风权限(Info.plist 已配 `NSMicrophoneUsageDescription`);确认 App 里网页弹了授权框 |
| 通话中屏幕熄了 | 检查是否真的运行的是这个 App 而不是 Safari 里的 PWA(套壳后不应该再发生) |
| User-Agent 会不会被网页识别成 Safari | 不会走 PWA 分支:前端代码里只区分 iPhone/Android(`isIOSNonStandalone`),WKWebView 默认 UA 带 iPhone 标识,页面按手机浏览器形态渲染,与 Safari 打开一致。壳里没有改 UA,保持默认 |
| 免提通话音频异常(少见) | 若 Omni 通话声音路由不对,可在 `ViewController.swift` 加 `AVAudioSession` 配置(category `.playAndRecord` + mode `.voiceChat`),当前版本刻意没加,先验证最简形态 |

## 开发备注

- 工程是手写的标准 pbxproj(objectVersion 56, Xcode 14+ 通用),没走 xcodegen;增删文件建议直接在 Xcode 里操作。
- 部署目标 iOS 15.0,只支持 iPhone(`TARGETED_DEVICE_FAMILY = 1`)。
- 支持横竖屏,竖屏为主;状态栏跟随网页自适应,无额外设置。
- 前后台不做任何后台保持——遵循「苹果不允许后台录音,显式挂起 + 回前台自动接回」的定案。
