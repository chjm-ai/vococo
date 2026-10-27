# 隔热三线巡检与两新箱转发

## 范围与入口

- `scripts/anchor_monitor.py`：固定三个 ANCHOR campaign，只读 Lemlist。复用外部 `面料外贸/scripts/yxx_auto.py` 的连接与 reviewed CSV 导出，不调用 daily、scan、验证、暂停或导入逻辑。
- `scripts/anchor_gmail_scan.py`：只覆盖过滤管、微孔板两个新箱；默认只读核查，`--apply` 才允许转发到 `ray.chen@anchorgd.com`。
- `scripts/anchor_checks.py`：原地供任务 `13f146ce` 调用，依次执行仓库内陶纤扫描、新两箱扫描、隔热统计。单段失败继续检查其他段，最后统一非零退出码；信号恒为 0（包括异常），只控制零 LLM 直出，不代表巡检成功。没有新增 cron，没有自动重启。
- `scripts/anchor_ceramic_scan.py --scan`：替代旧 `/Users/wesley/scripts/raychen_gmail_daily.py --scan` 的执行位置，复用原 `~/.gmail-raychen.json` 和 `~/scripts/.raychen_scan_state.json`。不清空 forwarded_ids/pending/last_ts；保留 60 秒重叠、INBOX 范围与首次不追历史。原状态增补 delivery/details 字段，持久化所有候选、未知来源及结果不明状态；不再裁掉 200 条去重记录。损坏状态拒绝重置，列表失败不推进水位，单封读取/明确发送拒绝下轮重试。旧版 pending 没有发送凭证，可能实际已发，迁移为 legacy_uncertain 保留 pending 与原主题/发件人，持续告警而不自动重发；本轮线上 pending 核实为零。
- 旧外部脚本未覆盖、未删除，**不含本次修复，禁止再手动执行它发信**。本轮检索现有 vococo cron 配置没有任务直接引用它；13f146ce 命令仍是 main 的 anchor_checks.py，但内部调用已改为仓库受控实现，不能靠更新外部脚本来部署。合并 main 即供下次调度加载，不需重启 vococo。

## 固定模板直出（2026-10-27）

- `anchor_report.py` 仅用 Python 标准库生成「结论＋三线数据表＋异常事项」；不导入供应商或模型模块。原扫描与分类/转发逻辑不变。
- 包装入口捕获三段 stdout/stderr/退出码，先保存至 `~/.local/state/anchor-mail/reports/<UTC时间>-<PID>.json`（目录初建 0700、文件 0600），中文通知只展示可读表格与排错文件路径，不拼接 JSON。此目录含邮件主题等信息，不公开分享；暂未自动清理。
- 时间分开列检查时间、指标抓取时间与北京时间统计日；发送事件包含跟进，不是新增人数。累计回复不等于有效询盘。扫描缺失/partial/blocked、非零退出、统计缺字段、存档失败均返回非零，其他成功段继续展示，不把失败填零。
- 新两箱 `counts.manual` 是本次新转人工，缺键显示未知而不补零；`manual_review_ids` 是存量待核查，重复巡检不累加。转发记录可能包含上轮发送、本轮才通过已发送邮件核实的项目，不称本次新发，也不称对方已收到；主题带 lemwarmup 只在展示中警示「不可算客户询盘」，不据此改变分类/转发。陶纤仍仅描述回执，不提升为全量覆盖结论。
- stdout 最后一行恒为 `##CRON_SIGNAL:0##`；退出码独立控制 cron success/error。调度器的 sidecar 总结仅在「成功且信号1（或无信号）且配置总结」时执行。测试用真实调度执行器＋入口 fixture，正常/partial、旧总结配置保留/无总结配置四种组合均禁止 sidecar 与 Agent 派发。
- 13f146ce 的 summarize_prompt 最初被审批拒绝；主人补充批准后，已原地清空，回读为 null。mode=script、command、23 8-22/2 * * * 频率、model=null、enabled=true 均保持原值。配置无 AI 总结＋入口恒信号 0 双重保障，异常也直出；未新建任务、未改发送设置。

无邮件/网络副作用验收（fixture 文件结构为 ceramic/gmail/monitor 各含 stdout、stderr、returncode；stdout 为各段 JSON 原回执及可选信号）：

```sh
python3 scripts/anchor_checks.py --fixture /path/to/fixture.json --raw-dir /tmp/anchor-fixture-raw
uv run pytest tests/test_anchor_report.py tests/test_anchor_automation.py tests/test_anchor_mail_safety.py tests/test_anchor_reply_association.py tests/test_cron_scheduler.py -q
```

fixture 分支不加载 Gmail、Lemlist 或业务脚本，不启动子扫描。生产入口仍沿原 cron 命令运行，**不带 fixture 会实际执行既有扫描/转发**，不可用它做发送测试。

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
- 三箱共用判定：完整解码主题/邮件头，排除实际 `lemwarmup <至少6位字母数字标记>`（大小写不敏感、支持 Re/Fwd/编码主题）及 X-Lemwarm*/X-Lem-Warm* 专用头；不以 warm、warmup、storyboard 等泛词判养号。自动信、自有域、转发目标、转发标记、群发列表排除。
- 余下邮件按 In-Reply-To/References 查询**当前已验证邮箱**已发记录；逐封核对 SENT、完整 Message-ID、From=本箱、收件人非自有域。引用养号已发信则排除；已发内容带 YXX Fabric/yxxfabric 域名串线线索则待人工。允许客户同事/转发人变更 From，不能仅靠 From 白名单。没有可核对的已发引用（含无回复头）一律 manual，不自动转发；不拿普通 Lemlist 回复事件冒充客户证据，也不调用其他线 campaign 做补证。
- manual 在独立状态中保留，持续输出 ID/主题/发件人并标 partial；不会自动重判或外发，后续需人工核对。已发引用只能证明关联来往，不能证明采购意向，也不能保证识别所有无标记养号/不规范自动信，仍需人工抽查。
- 新箱扫描包含收件箱、归档和垃圾邮件，不含回收站；陶纤仍保留旧 INBOX 范围。Gmail 列表全分页，重复分页标失败而不无限等待或静默推进。
- 发信前记 sending，成功以 Gmail send 返回 id 确认“已提交转发”，不声称对方送达。使用固定 Message-ID 查询已发送记录；网络超时/5xx/崩溃后结果不明，不盲目再发。未查到时保留待人工核对，避免重复转发。
- 明确 4xx 拒绝的转发下轮重试。每箱任何错误不阻断另一箱；coverage=blocked/partial 均不可验收为完整覆盖。

## 验证与运维

```sh
python3 -m pytest tests/test_anchor_automation.py tests/test_anchor_mail_safety.py tests/test_anchor_reply_association.py -q
python3 scripts/anchor_ceramic_scan.py  # 只读，不带 --scan
python3 scripts/anchor_monitor.py --read-only --day 2026-10-26
python3 scripts/anchor_gmail_scan.py
```

测试中外发均用内存替身；真实接口核查只有 Lemlist GET/CSV 导出及 Gmail OAuth/读取。不要运行整条生产 wrapper 来“测试”，因为其包含已授权的真实回复转发。

正式调度命令（13f146ce 本轮已核实配置为下列命令；本次保持频率、模型和命令不变，合并 main 生效）：

```sh
python3 /Users/wesley/Repos/vococo/scripts/anchor_checks.py
```

面料 e7ea5660 仍运行 `python3 scripts/yxx_auto.py daily --fabric-only`，不引入隔热日报。不得用 cron success 或缺信时的静默标记冒充三箱实际覆盖。新两箱缺 OAuth 时，统计可独立生效，但新两箱转发仍是阻塞状态。

### 2026-10-27 本轮验收

- 隔离测试：上述三个测试文件共 **65 passed**；覆盖真实事故标记、编码/大小写/回复前缀、三箱扫描入口、联系人变更、未知引用、自动信、串线与合法引用混合、分页/状态损坏/读取失败/明确重试/不确定发送/旧 pending 迁移。无测试信外发。
- 独立 code-reviewer 首轮确认两点：旧 pending 结果不明不能直接重发、混合引用不能覆盖身份冲突。均修复并补回归；第二轮复核无阻塞问题。
- 陶纤独立 OAuth 刷新、profile 邮箱身份与已有读/发 scope 本轮核验通过（未申请新 scope）。事故原信 `1a17bb2f0154d630`、回复 `1a17c05079c9e4da`、已转发 `1a17c073bccf4738` 均从 Gmail full 只读获取，完整主题有 `lemwarmup 0A3GN9JM`，新规则全部 excluded。
- 两新箱 `~/.gmail-anchor-filter.json`、`~/.gmail-anchor-microporous.json` 均缺失，结果 blocked；仅实现/fixture 覆盖，不能称线上邮箱可用。
- 核验前后陶纤状态 SHA256 同为 `0dd20cd3b70a8b63f51a7be1e09e05cda5094aca2f119dc3e773cd2c82a58fa2`；last_ts=1793082197，pending=0。只读核验不写状态、不追转旧信、不运行生产 wrapper。
- 未更改 Lemlist 共用姓名、养号、发送序列、配额，因此身份串用源头尚未消除；已误转发邮件不撤回。引用缺失、已发记录缺失或串线线索只进人工待核对，可能需要人工补证，不能报为自动覆盖成功。
