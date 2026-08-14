# vococo 自动重启加固设计

## 目标

修复正式 `vococo serve` 与守护循环被测试清理命令一起误杀后永久离线的问题，并让自我重启、崩溃自愈、自动回滚的结果可验证。

## 已确认根因

- 正式进程身份依赖 `pgrep/pkill -f "vococo serve"`，会同时匹配测试实例和包含该文本的守护 shell。
- `restart_self` 与 watchdog 只负责退出，复活完全依赖 `run.sh`；当前 LaunchAgent 只执行一次 `run.sh`，不监督守护循环。
- 自动回滚把当前已提交的新 HEAD 当作回滚锚点，连崩时等于重置到坏版本本身。
- 安全闸把直接 `kill/pkill/killall/xargs kill` 判为 `allow`，没有引导使用标准运维入口。

## 设计

### 1. 唯一监督者与精确进程身份

`deploy/run.sh` 改为前台监督模式，由 LaunchAgent `KeepAlive` 监督。监督者写入自己的 PID 文件，子进程 PID 由 shell 的 `$!` 精确持有。启动时不再全局 `pkill`，而是用原子目录锁拒绝第二个监督者。

`deploy/restart.sh` 与 `deploy/stop.sh` 只读取 PID 文件并验证 PID 对应的仓库根和进程角色，不再按名字搜索。测试进程必须由创建者保存 `$!` 并精确清理。

### 2. 可验证健康状态

增加本机 `/healthz`：返回 `ok`、`boot_id`、当前 PID 和 Git revision。启动与重启必须同时满足：HTTP 200、boot_id 与旧值不同、PID 属于正式仓库。`vococo doctor` 同样使用该健康信号，不再用 `pgrep`。

### 3. 重启事务与正确回滚

进程启动时记录 `data/running_revision.json`，稳定运行满 20 秒后才把当前 revision 标记为 stable。`restart_self` 保存 stable revision 和 candidate revision，并在退出前确认监督者 PID 存活。

回滚元数据与还魂遗书分离。还魂消息可以在启动后消费，但监督者始终保留独立的 restart transaction；候选版本连续快速失败三次时，仅在 HEAD 仍等于 candidate 且工作区干净时回退到 stable，避免覆盖其他会话的新提交。

同一时刻只允许一个全局重启事务，使用原子文件创建实现单飞。

### 4. Hard Guard

针对 vococo 正式进程的直接 `kill/pkill/killall/xargs kill` 进入常开 Hard Guard，直接拒绝并提示标准入口。通用进程控制进入 Approval Gate。安全分类内部异常改为 fail-closed，与 ADR 0003 一致。

### 5. LaunchAgent 迁移

`deploy/launchd.sh install` 会先卸载旧 `com.vococo.boot` 和旧 `com.vococo`，再安装唯一 `com.vococo`。LaunchAgent 通过登录 shell `exec deploy/run.sh --foreground`，保留完整登录环境且让 launchd 真正拥有监督者生命周期。

## 失败处理

- PID/锁陈旧：验证进程不存在后才清理。
- 监督者不存在：`restart_self` 拒绝退出，保留当前可用进程。
- 多实例：运维命令返回错误，不猜测哪个实例正确。
- 健康检查失败：不报告成功；LaunchAgent 继续重拉监督者。
- 回滚条件不安全：拒绝 `reset --hard` 并在日志中明确说明，不丢用户改动。
- watchdog 无法写日志：仍执行受控退出，不能让看门狗线程静默死亡。

## 测试

- 危险命令回归：事故原命令、`pkill -f vococo serve` 被拒。
- 监督者集成测试：子进程退出会拉起；杀监督者后 LaunchAgent 模板具备 KeepAlive；并发启动被锁拒绝。
- 重启事务单测：正确 stable/candidate、全局单飞、遗书消费不影响回滚。
- 脚本集成测试：重复实例拒绝、旧 PID 不误杀、健康 boot_id 必须变化。
- 全量 `pytest` 和线上 `/healthz`、数据库完整性、自动重启实测。

## 非目标

- 不重构业务会话、任务调度或前端 UI。
- 不引入第三方依赖。
- 不修改数据库结构。
