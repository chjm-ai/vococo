"""冻结业务副本的命令行闸门；不跳转、不读取业务状态。"""


def refuse_legacy_run() -> int:
    print("旧隔热业务入口已停用，仅保留离线测试和历史参考。"
          "正式操作请使用 anchor-mail-ops Skill；本次未扫描、转发或写入业务数据。")
    print("##CRON_SIGNAL:1##")
    return 1
