# 隔热三线巡检与两新箱转发

## 范围与入口

- `scripts/anchor_monitor.py`：固定三个 ANCHOR campaign，只读 Lemlist。复用外部 `面料外贸/scripts/yxx_auto.py` 的连接与 reviewed CSV 导出，不调用 daily、scan、验证、暂停或导入逻辑。
- `scripts/anchor_gmail_scan.py`：只覆盖过滤管、微孔板两个新箱；默认只读核查，`--apply` 才允许转发到 `ray.chen@anchorgd.com`。
- `scripts/anchor_checks.py`：原地供任务 `13f146ce` 调用，依次执行原陶纤扫描、新两箱扫描、隔热统计。单段失败继续检查其他段，最后统一失败信号。没有新增 cron，没有自动重启。
- 陶纤 campaign、原 Gmail 脚本与状态不变。旧 Gmail 脚本无完整分页、元数据失败可能静默，故其静默不能作为完整扫描覆盖证据。

## 统计口径

- `events_all`：该类型累计活动条数；`leads_all`：按 leadId 去重数，不能称为公司数。
- `events_day`：北京时间指定自然日内活动条数；默认昨天。发送活动不是送达凭证，回复事件不保证是真人询盘。
- `reviewed_state`：API 当前 state 为 reviewed 的条数；不能算已发，也不保证未暂停。
- `sendable_export`：服务端 reviewed 导出条数；失败填 null 并告警，不用 state 数冒充可发送池。
- `running` 只是 campaign 运行状态。paused/异常 campaign 也查询，失败填 error，不能填零。
- 滚动退信采用外部配置的现有窗口、最小样本、隔热告警阈值；只记录告警，无暂停操作。
- 分页为 leads 500、activities 100，无 20000 条静默截断；重复页判失败。
- 隔热快照独立追加至 `隔热材料外贸/邮件获客/数据/自动化/隔热指标.jsonl`。同一天多次是快照，禁止累加为发送总量。
- 共享脚本的 `--fabric-only` 原本只隐藏报告，仍混写 ANCHOR 指标；本次补丁仅排除其面料指标落盘，保留共享安全检查与面料熔断。补丁在 `scripts/patches/yxx_anchor_isolation.patch`，不迁移或删除历史台账。

## OAuth 与安全门

明确配置、不得搜索或借用其他项目凭据：

| 邮箱 | 独立 OAuth 文件 |
|---|---|
| ray@anchor-filter.com | ~/.gmail-anchor-filter.json |
| ray@anchor-microporous.com | ~/.gmail-anchor-microporous.json |

- 必须由本人完成对应 Google 账号授权，读取需 `gmail.readonly`，转发需 `gmail.send`（现有更广 scope 也校验兼容，但不申请扩大权限）。文件格式与既有 Gmail 脚本相同，需 client_id、client_secret、refresh_token、token_uri。
- token_uri 仅接受官方 `https://oauth2.googleapis.com/token`。先刷新、核对 `/profile` 邮箱身份和返回 scope；不足则明确报错。不依靠 Lemlist 已连接发件箱推断 Gmail 授权可用。
- 每箱独立状态与锁：`~/.local/state/anchor-mail/<邮箱替换@为_>.json`；原子保存、无 200/3000 条去重裁剪。状态损坏拒绝运行，不重置为首次扫描。
- 首次 `--apply` 起点为 max(首次启用时间, 2026-10-26 06:00 UTC)；精确检查 internalDate，不批转启用之前旧信。只读核查不建状态、不转发。
- 后续一小时重叠查询、全分页、持久化候选；元数据或正文读取失败保留重试，列表失败不推进水位。
- 自动信、自有域、转发目标、转发标记、群发列表、非回复头邮件均排除；新箱扫描包含收件箱、归档和垃圾邮件，不含回收站，避免已归档或误判垃圾邮件的回复漏扫。标准头过滤不能保证识别所有不规范养号/自动信，仍需人工抽查。
- 发信前记 sending，成功以 Gmail send 返回 id 确认“已提交转发”，不声称对方送达。使用固定 Message-ID 查询已发送记录；网络超时/5xx/崩溃后结果不明，不盲目再发。未查到时保留待人工核对，避免重复转发。
- 明确 4xx 拒绝的转发下轮重试。每箱任何错误不阻断另一箱；coverage=blocked/partial 均不可验收为完整覆盖。

## 验证与运维

```sh
python3 -m pytest tests/test_anchor_automation.py -q
python3 scripts/anchor_monitor.py --read-only --day 2026-10-26
python3 scripts/anchor_gmail_scan.py
```

测试中外发均用内存替身；真实接口核查只有 Lemlist GET/CSV 导出及 Gmail OAuth/读取。不要运行整条生产 wrapper 来“测试”，因为其包含已授权的真实回复转发。

正式调度命令（合并 main 后，用正规任务更新工具原地修改 13f146ce，频率不变）：

```sh
python3 /Users/wesley/Repos/vococo/scripts/anchor_checks.py
```

面料 e7ea5660 仍运行 `python3 scripts/yxx_auto.py daily --fabric-only`，不引入隔热日报。不得用 cron success 或缺信时的静默标记冒充三箱实际覆盖。新两箱缺 OAuth 时，统计可独立生效，但新两箱转发仍是阻塞状态。
