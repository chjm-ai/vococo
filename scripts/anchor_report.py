"""三线回执的固定中文模板；不访问网络，不推断询盘意向。"""
from datetime import datetime

LINES = {"陶纤": None, "过滤管": "ray@anchor-filter.com", "微孔板": "ray@anchor-microporous.com"}
UNKNOWN = "未知"


def cell(value):
    if value is None:
        return UNKNOWN
    return str(value).replace("|", "／").replace("\n", " ").replace("\r", " ")


def count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def event(row, kind, field):
    value = row.get(kind)
    return count(value.get(field)) if isinstance(value, dict) else None


def scan_row(payload, ceramic):
    if not isinstance(payload, dict):
        return "缺失", ["扫描回执缺失"], True
    coverage = payload.get("coverage", "缺失")
    issues = []
    required = ["manual_review_ids", "manual_review", "errors"]
    required += ["submitted_forward_ids", "pending"] if ceramic else ["counts"]
    for key in required:
        expected = dict if key in ("counts", "manual_review") else list
        if not isinstance(payload.get(key), expected):
            issues.append(f"{key} 缺失或格式错误")
    if coverage != "ok":
        issues.append(f"扫描不完整（{cell(coverage)}）")
    if payload.get("error") or payload.get("errors"):
        issues.append("扫描报错，详见原始回执")
    if ceramic:
        forwards = payload.get("submitted_forward_ids")
        summary = f"回执 {cell(coverage)}；本次提交/核实转发记录 {len(forwards) if isinstance(forwards, list) else UNKNOWN}"
        if payload.get("pending"):
            issues.append(f"pending/结果待确认 {len(payload['pending'])}")
    else:
        counts = payload.get("counts")
        sent = count(counts.get("sent")) if isinstance(counts, dict) else None
        manual = count(counts.get("manual")) if isinstance(counts, dict) else None
        summary = f"{cell(coverage)}；本次提交/核实转发记录 {cell(sent)}，本次转人工 {cell(manual)}"
        if isinstance(counts, dict):
            if any(count(value) is None for value in counts.values()):
                issues.append("扫描计数格式错误，不能确认转发状态")
            if any(counts.get(key) for key in ("retry", "uncertain", "sending")):
                issues.append("存在重试/转发结果不明，不能称已转发")
    manual_summary, manual_issues = manual_review(payload)
    return summary + manual_summary, issues + manual_issues, bool(issues or manual_issues)


def manual_review(payload):
    issues = []
    manual_ids = payload.get("manual_review_ids")
    valid_ids = isinstance(manual_ids, list) and all(isinstance(mid, str) for mid in manual_ids)
    stock = len(set(manual_ids)) if valid_ids else None
    if not valid_ids:
        issues.append("人工队列 ID 缺失或格式错误")
    summary = f"；存量待人工 {cell(stock)}"
    if stock:
        issues.append(f"存量待人工 {stock} 封（不是本次新增，不等于客户询盘）")
    details = payload.get("manual_review")
    if isinstance(details, dict) and valid_ids:
        valid_details = [details[mid] for mid in set(manual_ids) if isinstance(details.get(mid), dict)]
        if len(valid_details) != stock:
            issues.append("部分人工队列详情缺失或格式错误；不能确认全部标题")
        warm = sum("lemwarmup" in str(item.get("subject", "")).lower() for item in valid_details)
        if warm:
            issues.append(f"其中 {warm} 封主题带 lemwarmup 养号标记，不可算客户询盘；引用关联不证明采购意向")
    return summary, issues


def campaign_row(row):
    if not isinstance(row, dict):
        row = {}
    issues = []
    fields = [cell(row.get("status")), cell(count(row.get("leads")))]
    for field in ("events_day", "events_all"):
        values = [event(row, kind, field) for kind in ("emailsSent", "emailsBounced", "emailsReplied")]
        fields.append(" / ".join(cell(value) for value in values))
        if None in values:
            issues.append("活动字段缺失，不能填零")
    bounced = event(row, "emailsBounced", "leads_all")
    sent = event(row, "emailsSent", "leads_all")
    rate = f"{bounced}/{sent} = {bounced / sent:.2%}" if bounced is not None and sent else UNKNOWN
    fields.append(rate)
    if row.get("error") or row.get("coverage") == "failed":
        issues.append("campaign API 读取失败；保留本次可用字段")
    if row.get("status") is None or count(row.get("leads")) is None or bounced is None or sent is None:
        issues.append("campaign 基础字段缺失")
    warnings = row.get("warnings", [])
    if isinstance(warnings, list):
        issues.extend(cell(warning) for warning in warnings)
    else:
        issues.append("campaign warnings 格式错误")
    return fields, issues


def report(results, checked_at, raw_path):
    monitor = results.get("monitor", {}).get("data")
    monitor = monitor if isinstance(monitor, dict) else {}
    campaigns = monitor.get("campaigns", {})
    campaigns = campaigns if isinstance(campaigns, dict) else {}
    gmail = results.get("gmail", {}).get("data")
    gmail = gmail if isinstance(gmail, dict) else {}
    rows, issues = [], []
    for name, address in LINES.items():
        scan = gmail.get(address) if address else results.get("ceramic", {}).get("data")
        summary, scan_issues, _ = scan_row(scan, not address)
        fields, stats_issues = campaign_row(campaigns.get(name))
        rows.append("| " + " | ".join([name, *fields, summary]) + " |")
        issues.extend(f"{name}：{issue}" for issue in dict.fromkeys(scan_issues + stats_issues))
    for stage in ("ceramic", "gmail", "monitor"):
        result = results.get(stage, {})
        if result.get("returncode") != 0 or result.get("error"):
            issues.append(f"{stage} 脚本未成功（退出码 {cell(result.get('returncode'))}）；保留其余可用回执")
    for key in ("day_cn", "checked_at"):
        try:
            datetime.fromisoformat(monitor[key].replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError, AttributeError):
            issues.append(f"统计 {key} 缺失或无效，日期范围未确认")
    return render_report(rows, issues, monitor, checked_at, raw_path), bool(issues)


def render_report(rows, issues, monitor, checked_at, raw_path):
    header = ["## 结论", "巡检存在异常/覆盖不完整，不能据此报告零询盘。" if issues else
              "三线回执无异常；未判定有效询盘数量。", f"检查时间（UTC）：{cell(checked_at)}",
              f"统计日（北京时间自然日）：{cell(monitor.get('day_cn'))}；指标抓取时间：{cell(monitor.get('checked_at'))}",
              "## 三线数据", "| 产品线 | campaign状态 | lead总数 | 当日发/退/回事件 | 累计发/退/回事件 | 累计退信lead/发送lead | 扫描回执 |",
              "|---|---|---:|---|---|---|---|", *rows,
              "发送按邮件事件计数（含跟进），不是新增人数，也不证明送达；累计回复不等于有效询盘。",
              "转发记录可能含上轮发送、本轮才核实的项目，不等于本次新发送，更不证明对方收到。",
              "缺失计数显示未知；陶纤仅描述扫描回执，无提交转发记录不代表全邮箱无询盘或全量覆盖。",
              "## 异常事项", *[f"- {issue}" for issue in issues]]
    if not issues:
        header.append("- 无已报告异常。")
    header.append(f"原始回执（排错，不直接展示）：{cell(raw_path)}")
    return "\n".join(header)
