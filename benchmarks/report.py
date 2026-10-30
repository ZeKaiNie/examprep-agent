# -*- coding: utf-8 -*-
"""Render ``BENCH_REPORT.md`` and the README / P3 report summary blocks.

Every number comes from the **same in-memory results object** written to
``results.json`` in the same run.  ``sync_docs`` replaces marked blocks in
``README.md`` and ``P3_REPORT.md``; there are no hand-copied benchmark numbers.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .common import REPO_ROOT, fmt

README_EN_START = "<!-- BENCH_SUMMARY_EN_START -->"
README_EN_END = "<!-- BENCH_SUMMARY_EN_END -->"
README_ZH_START = "<!-- BENCH_SUMMARY_ZH_START -->"
README_ZH_END = "<!-- BENCH_SUMMARY_ZH_END -->"
P3_START = "<!-- BENCH_SNAPSHOT_START -->"
P3_END = "<!-- BENCH_SNAPSHOT_END -->"


def _summary_rows(results: Mapping[str, Any], lang: str) -> list[str]:
    grounding = results["layers"]["grounding"]["arms"]
    behavior = results["layers"]["behavior"]["summary"]
    longhorizon = results["layers"]["longhorizon"]
    arms = longhorizon["arms"]
    tutor = results["layers"]["tutor"]
    performance = results["layers"]["performance"]
    speedup = (performance.get("ingest") or {}).get("speedup_serial_over_parallel")
    equal = longhorizon.get("equal_time") or {}
    agent_arm = arms["agent_guided"]
    baseline_arm = arms["baseline_fixed_order"]

    def pct(value: Any) -> str:
        return "N/A" if value is None else "%s%%" % fmt(value)

    def num(value: Any) -> str:
        return "N/A" if value is None else fmt(value)

    if lang == "en":
        rows = [
            "| Layer | Primary metric | Result |",
            "| --- | --- | --- |",
            "| GroundingBench | material answer accuracy, top-1 (closed-book / raw-file / agent) | %s / %s / %s |" % (
                pct(grounding["closed_book"]["material_answer_accuracy_pct"]),
                pct(grounding["raw_file"]["material_answer_accuracy_pct"]),
                pct(grounding["agent"]["material_answer_accuracy_pct"])),
            "| GroundingBench | evidence recall@3 (agent) | %s (%d/%d) |" % (
                pct(grounding["agent"]["evidence_recall_at_3_pct"]),
                grounding["agent"]["answer_terms_in_evidence_top3"],
                grounding["agent"]["in_material_total"]),
            "| GroundingBench | page-citation accuracy (agent) | %s (%d/%d) |" % (
                pct(grounding["agent"]["citation_accuracy_pct"]),
                grounding["agent"]["page_citation_verified"],
                grounding["agent"]["page_citation_attempts"]),
            "| GroundingBench | out-of-material honest refusal (agent) | %s (%d/%d) |" % (
                pct(grounding["agent"]["out_of_material_refusal_pct"]),
                grounding["agent"]["probe_refusals"], grounding["agent"]["probes_total"]),
            "| BehaviorBench | induced attack block / positive normal pass / negative-control pass | %s / %s / %s |" % (
                pct(behavior["induced_attack_block_pct"]), pct(behavior["normal_pass_pct"]),
                pct(behavior["negative_control_pass_pct"])),
            "| BehaviorBench | cross-command invariants | %s (%d/%d) |" % (
                pct(behavior["invariant_pass_pct"]), behavior["invariant_passed"],
                behavior["invariant_total"]),
            "| LongHorizon Sim | 30-day delayed retention (agent / baseline) | %s / %s |" % (
                pct(agent_arm["delayed_retention_pct"]),
                pct(baseline_arm["delayed_retention_pct"])),
            "| LongHorizon Sim | learning gain = post - pre (agent / baseline) | %s / %s pp |" % (
                num(agent_arm["learning_gain_pct_points"]),
                num(baseline_arm["learning_gain_pct_points"])),
            "| LongHorizon Sim | gain per available budget hour (agent / baseline) | %s / %s |" % (
                num(agent_arm["gain_per_budget_hour"]),
                num(baseline_arm["gain_per_budget_hour"])),
            "| LongHorizon Sim | equal-time intervention, cap %s min (agent / baseline retention) | %s / %s |" % (
                equal.get("cap_minutes"),
                pct(((equal.get("agent_guided") or {}).get("delayed_retention_pct"))),
                pct(((equal.get("baseline_fixed_order") or {}).get("delayed_retention_pct")))),
            "| TutorRubric | mean score (overall / good / medium / flawed / bypass) | %s / %s / %s / %s / %s |" % (
                num(tutor["overall_mean_score"]), num(tutor["good_mean_score"]),
                num(tutor["medium_mean_score"]), num(tutor["flawed_mean_score"]),
                num(tutor["bypass_mean_score"])),
            "| TutorRubric | discrimination (good - flawed) / bypass full-score count | %s / %d |" % (
                num(tutor["discrimination"]), tutor["bypass_full_score_count"]),
            "| Performance | ingest serial/parallel speedup (current container) | %s |" % (
                "%sx" % fmt(speedup) if speedup is not None else "skipped"),
        ]
    else:
        rows = [
            "| 层 | 主要指标 | 结果 |",
            "| --- | --- | --- |",
            "| GroundingBench | 材料答案准确率 top-1（闭卷 / 裸文件 / agent） | %s / %s / %s |" % (
                pct(grounding["closed_book"]["material_answer_accuracy_pct"]),
                pct(grounding["raw_file"]["material_answer_accuracy_pct"]),
                pct(grounding["agent"]["material_answer_accuracy_pct"])),
            "| GroundingBench | 证据召回@3（agent） | %s (%d/%d) |" % (
                pct(grounding["agent"]["evidence_recall_at_3_pct"]),
                grounding["agent"]["answer_terms_in_evidence_top3"],
                grounding["agent"]["in_material_total"]),
            "| GroundingBench | 页码引用核验率（agent） | %s (%d/%d) |" % (
                pct(grounding["agent"]["citation_accuracy_pct"]),
                grounding["agent"]["page_citation_verified"],
                grounding["agent"]["page_citation_attempts"]),
            "| GroundingBench | 材料外诚实拒答率（agent） | %s (%d/%d) |" % (
                pct(grounding["agent"]["out_of_material_refusal_pct"]),
                grounding["agent"]["probe_refusals"], grounding["agent"]["probes_total"]),
            "| BehaviorBench | 材料诱导攻击拦截率 / 正向正常用例通过率 / 负向控制通过率 | %s / %s / %s |" % (
                pct(behavior["induced_attack_block_pct"]), pct(behavior["normal_pass_pct"]),
                pct(behavior["negative_control_pass_pct"])),
            "| BehaviorBench | 跨命令不变量 | %s (%d/%d) |" % (
                pct(behavior["invariant_pass_pct"]), behavior["invariant_passed"],
                behavior["invariant_total"]),
            "| LongHorizon Sim | 30 天延迟留存率（agent / 基线） | %s / %s |" % (
                pct(agent_arm["delayed_retention_pct"]),
                pct(baseline_arm["delayed_retention_pct"])),
            "| LongHorizon Sim | 学习增益 = 后测 − 前测（agent / 基线） | %s / %s 百分点 |" % (
                num(agent_arm["learning_gain_pct_points"]),
                num(baseline_arm["learning_gain_pct_points"])),
            "| LongHorizon Sim | 每可用预算小时增益（agent / 基线） | %s / %s |" % (
                num(agent_arm["gain_per_budget_hour"]),
                num(baseline_arm["gain_per_budget_hour"])),
            "| LongHorizon Sim | 等实际时间干预，上限 %s 分钟（agent / 基线留存） | %s / %s |" % (
                equal.get("cap_minutes"),
                pct(((equal.get("agent_guided") or {}).get("delayed_retention_pct"))),
                pct(((equal.get("baseline_fixed_order") or {}).get("delayed_retention_pct")))),
            "| TutorRubric | 平均分（总体 / 良好 / 中等 / 缺陷 / 绕过） | %s / %s / %s / %s / %s |" % (
                num(tutor["overall_mean_score"]), num(tutor["good_mean_score"]),
                num(tutor["medium_mean_score"]), num(tutor["flawed_mean_score"]),
                num(tutor["bypass_mean_score"])),
            "| TutorRubric | 区分度（良好 − 缺陷）/ 绕过满分数 | %s / %d |" % (
                num(tutor["discrimination"]), tutor["bypass_full_score_count"]),
            "| Performance | 摄取串行/并行加速比（当前容器） | %s |" % (
                "%sx" % fmt(speedup) if speedup is not None else "skipped"),
        ]
    return rows


def summary_table(results: Mapping[str, Any], lang: str = "zh") -> str:
    """Compact markdown table used in BENCH_REPORT, README and P3_REPORT."""
    return "\n".join(_summary_rows(results, lang)) + "\n"


# ---------------------------------------------------------------- sections
def _grounding_section(results: Mapping[str, Any]) -> list[str]:
    section = results["layers"]["grounding"]
    lines = ["## 1. GroundingBench（溯源层）", ""]
    lines.append("金标集：%d 道材料内题 + %d 道材料外探针（共 %d 道）；检索 k=%d。" % (
        section["gold_set"]["in_material"], section["gold_set"]["out_of_material_probes"],
        section["gold_set"]["total"], section["gold_set"]["retrieval_k"]))
    lines.append("")
    lines.append("三臂使用**同一读取器与同一评分器**，仅信息条件不同：")
    lines.append("")
    lines.append("| 臂 | 信息条件 |")
    lines.append("| --- | --- |")
    for arm in ("closed_book", "raw_file", "agent"):
        lines.append("| %s | %s |" % (arm, section["arms"][arm]["information_condition"]))
    lines.append("")
    lines.append("读取器：确定性抽取式。支持检查为全局、无金标规则：%s。" % (
        section["definition"]["support_check"]))
    lines.append("阈值标定：材料内 agent 覆盖率最小值 `%s`，探针最大值 `%s`；逐题支持分数见 "
                 "`results.json -> layers.grounding.records[]`。" % (
                     fmt(section["support_calibration"]["in_material_agent_coverage_min"]),
                     fmt(section["support_calibration"]["probe_agent_coverage_max"])))
    lines.append("")
    lines.append("| 臂 | 材料内答对 top-1 / 总题 | top-1 准确率 | 证据召回@3 | 页码引用核验/尝试 | 引用核验率 | 探针拒答/总数 | 诚实拒答率 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for arm in ("closed_book", "raw_file", "agent"):
        item = section["arms"][arm]
        citation_cell = ("%s%%" % fmt(item["citation_accuracy_pct"])
                         if item["citation_accuracy_pct"] is not None else "N/A")
        lines.append("| %s | %d/%d | %s%% | %s%% (%d/%d) | %d/%d | %s | %d/%d | %s%% |" % (
            arm, item["correct_in_material_top1"], item["in_material_total"],
            fmt(item["material_answer_accuracy_pct"]),
            fmt(item["evidence_recall_at_3_pct"]),
            item["answer_terms_in_evidence_top3"], item["in_material_total"],
            item["page_citation_verified"], item["page_citation_attempts"],
            citation_cell,
            item["probe_refusals"], item["probes_total"],
            fmt(item["out_of_material_refusal_pct"])))
    lines.append("")
    agent = section["arms"]["agent"]
    lines.append("- agent 在 %d/%d 道探针上仍有检索命中（共 %d 条），拒答由支持检查产生，"
                 "不是“检索为空”。" % (
                     agent["probes_with_retrieval_hits"], agent["probes_total"],
                     agent["retrieval_hits_on_probes"]))
    lines.append("- 引用正确率的分母仅为**页码引用尝试**；闭卷/裸文件不产生页码引用，"
                 "因此显示 N/A，而不是被预设为 0。")
    lines.append("- 材料答案是命中片段上的抽取式 top-1；top-3 召回单列为证据召回@3。"
                 "金标 `answer_terms` 只被评分器读取。")
    misses = [record for record in section["records"]
              if record.get("arm_name") == "agent" and not record["out_of_material"]
              and not record["answer_correct"]]
    if misses:
        lines.append("- agent top-1 未答对的材料内题：%s（逐题证据见 records）。" %
                     "、".join(item["id"] for item in misses))
    lines.append("")
    return lines


def _behavior_section(results: Mapping[str, Any]) -> list[str]:
    section = results["layers"]["behavior"]
    summary = section["summary"]
    lines = [
        "## 2. BehaviorBench（行为层）", "",
        "**材料诱导攻击拦截率 %s%%（%d/%d）；正向正常用例通过率 %s%%（%d/%d）；"
        "负向控制通过率 %s%%（%d/%d）；不变量 %s%%（%d/%d）。**" % (
            fmt(summary["induced_attack_block_pct"]), summary["induced_attack_blocked"],
            summary["induced_attack_total"], fmt(summary["normal_pass_pct"]),
            summary["normal_passed"], summary["normal_total"],
            fmt(summary["negative_control_pass_pct"]), summary["negative_control_passed"],
            summary["negative_control_total"], fmt(summary["invariant_pass_pct"]),
            summary["invariant_passed"], summary["invariant_total"]),
        "",
        "主指标只统计 **induced** 用例：毒材料进入 agent 上下文，FakeProvider 从材料中解析 "
        "`[AGENT ACTION]` 后经真实 `run_agent` 循环发起工具调用；材料在上下文中、动作来自材料"
        "两项在 results 中逐条记录。静态/setup/直调用例单列为补充。",
        "",
        "### 2.1 材料诱导攻击", "",
        "| 用例 | 类别 | 是否拦截 | 工具/拒绝码 | 材料在上下文 | 动作来自材料 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for case in section["attacks"]:
        if case["channel"] != "induced":
            continue
        metrics = case.get("metrics") or {}
        lines.append("| %s | %s | %s | %s / %s | %s | %s |" % (
            case["id"], case["category"], "✅" if case["passed"] else "❌",
            metrics.get("tool"), metrics.get("code"),
            metrics.get("material_in_context"), metrics.get("action_from_material")))
    lines.append("")
    lines.append("### 2.2 补充攻击检查（静态 / setup / 直接调用）")
    lines.append("")
    lines.append("| 用例 | 类别 | 通道 | 是否拦截 | 证据 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for case in section["attacks"]:
        if case["channel"] == "induced":
            continue
        lines.append("| %s | %s | %s | %s | %s |" % (
            case["id"], case["category"], case["channel"],
            "✅" if case["passed"] else "❌",
            str(case["detail"]).replace("|", "\\|")))
    lines.append("")
    lines.append("### 2.3 正向正常用例")
    lines.append("")
    lines.append("| 用例 | 通过 | 检查项 |")
    lines.append("| --- | --- | --- |")
    for case in section["normal_cases"]:
        lines.append("| %s | %s | %s |" % (
            case["id"], "✅" if case["passed"] else "❌",
            str(case["detail"]).replace("|", "\\|")))
    lines.append("")
    lines.append("### 2.4 负向控制用例（预期拒绝/阻断，单独计数）")
    lines.append("")
    lines.append("| 用例 | 通过 | 检查项 |")
    lines.append("| --- | --- | --- |")
    for case in section["negative_controls"]:
        lines.append("| %s | %s | %s |" % (
            case["id"], "✅" if case["passed"] else "❌",
            str(case["detail"]).replace("|", "\\|")))
    lines.append("")
    lines.append("### 2.5 跨命令不变量")
    lines.append("")
    lines.append("| 不变量 | 通过 | 证据 |")
    lines.append("| --- | --- | --- |")
    for case in section["invariants"]:
        lines.append("| %s | %s | %s |" % (
            case["id"], "✅" if case["passed"] else "❌",
            str(case["detail"]).replace("|", "\\|")))
    lines.append("")
    return lines


def _longhorizon_section(results: Mapping[str, Any]) -> list[str]:
    section = results["layers"]["longhorizon"]
    lines = ["## 3. LongHorizon Sim（长程层）", ""]
    protocol = section["protocol"]
    lines.append("协议：模拟 %d 天（其中学习 %d 天，最后 %d 天不学习），第 %d 天做延迟测试；"
                 "两臂可用预算相同：每天 %d 分钟，共 %d 分钟。种子 **%d**。" % (
                     protocol["total_days"], protocol["study_days"],
                     protocol["retention_gap_days"], protocol["delayed_test_day"],
                     protocol["day_minutes"], protocol["available_budget_minutes"],
                     section["seed"]))
    lines.append("")
    lines.append("指标定义（公式）：")
    lines.append("")
    lines.append("- 学习增益 = `delayed_retention_pct - pretest_retention_pct`（百分点）")
    lines.append("- 每实际学习小时增益 = `learning_gain_pct_points / (study_minutes / 60)`")
    lines.append("- 每可用预算小时增益 = `learning_gain_pct_points / (available_budget_minutes / 60)`"
                 "（两臂同一分母）")
    lines.append("- 增量复习效率 = `(有调度复习的延迟留存 - 同策略无调度复习的延迟留存) / (review_minutes / 60)`")
    lines.append("- 增量重读效率 = `(有 massed restudy 的延迟留存 - 无 massed restudy 的延迟留存) / (restudy_minutes / 60)`")
    lines.append("")
    lines.append("参数（全部写入 results.json）：")
    lines.append("")
    lines.append("| 参数 | 值 |")
    lines.append("| --- | --- |")
    for key, value in sorted(section["params"].items()):
        lines.append("| %s | %s |" % (key, value))
    lines.append("")
    lines.append("| 臂 | 前测留存 | 延迟留存 | 延迟实测 | 95% CI | 学习增益(pp) | 可用预算(min) | 实际学习(min) | 复习(min) | 重读(min) | 增益/实际小时 | 增益/预算小时 | 增量复习效率 | 增量重读效率 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for key in ("agent_guided", "baseline_fixed_order"):
        arm = section["arms"][key]
        lines.append("| %s | %s%% | %s%% | %s%% | %s | %s | %d | %d | %d | %d | %s | %s | %s | %s |" % (
            arm["arm"], fmt(arm["pretest_retention_pct"]), fmt(arm["delayed_retention_pct"]),
            fmt(arm["delayed_test_accuracy_pct"]), arm.get("delayed_test_ci95_pct"),
            fmt(arm["learning_gain_pct_points"]), arm["available_budget_minutes"],
            arm["study_minutes"], arm["review_minutes"], arm["restudy_minutes"],
            fmt(arm.get("gain_per_study_hour")), fmt(arm.get("gain_per_budget_hour")),
            fmt(arm.get("incremental_review_efficiency_pp_per_review_hour")),
            fmt(arm.get("incremental_restudy_efficiency_pp_per_restudy_hour"))))
    lines.append("")
    equal = section.get("equal_time") or {}
    lines.append("等实际时间干预：两臂都在累计动作达到 **%s 分钟**后停止；延迟留存 agent %s%%，"
                 "基线 %s%%。" % (
                     equal.get("cap_minutes"),
                     fmt((equal.get("agent_guided") or {}).get("delayed_retention_pct")),
                     fmt((equal.get("baseline_fixed_order") or {}).get("delayed_retention_pct"))))
    lines.append("")
    counterfactuals = section.get("counterfactual_arms") or {}
    if counterfactuals:
        lines.append("反事实臂：agent 无调度复习延迟留存 %s%%；基线无 massed restudy 延迟留存 %s%%。" % (
            fmt((counterfactuals.get("agent_no_scheduled_review") or {}).get("delayed_retention_pct")),
            fmt((counterfactuals.get("baseline_no_massed_restudy") or {}).get("delayed_retention_pct"))))
        lines.append("")
    isolation = section["integrity"].get("isolation") or {}
    lines.append("隐藏状态隔离：`hidden_state_isolated = %s`，由以下实际读写/篡改实验计算：" % (
        section["integrity"]["hidden_state_isolated"]))
    lines.append("")
    for name, passed in sorted((isolation.get("checks") or {}).items()):
        lines.append("- `%s`: %s" % (name, passed))
    lines.append("")
    lines.append("> **诚信边界**：%s" % section["caveat"])
    lines.append("")
    return lines


def _tutor_section(results: Mapping[str, Any]) -> list[str]:
    section = results["layers"]["tutor"]
    lines = ["## 4. TutorRubric（教学层）", ""]
    lines.append("场景 %d 个（good %d / medium %d / flawed %d / bypass %d）；"
                 "总分 %s；良好 %s；中等 %s；缺陷 %s；绕过 %s。" % (
                     section["scenario_count"],
                     section["quality_counts"]["good"], section["quality_counts"]["medium"],
                     section["quality_counts"]["flawed"], section["quality_counts"]["bypass"],
                     fmt(section["overall_mean_score"]), fmt(section["good_mean_score"]),
                     fmt(section["medium_mean_score"]), fmt(section["flawed_mean_score"]),
                     fmt(section["bypass_mean_score"])))
    lines.append("")
    lines.append("区分度（良好 − 缺陷）：%s；良好 − 中等：%s；中等 − 缺陷：%s。"
                 "绕过样本满分个数：**%d**（必须为 0）。" % (
                     fmt(section["discrimination"]),
                     fmt(section["discrimination_good_vs_medium"]),
                     fmt(section["discrimination_medium_vs_flawed"]),
                     section["bypass_full_score_count"]))
    lines.append("")
    lines.append("| 类别 | 场景数 | 平均分 | 引用材料 | 先测后讲 | 给出下一步 | 不越界编造 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for category, item in section["categories"].items():
        signals = item["signal_pass_pct"]
        lines.append("| %s | %d | %s | %s%% | %s%% | %s%% | %s%% |" % (
            category, item["count"], fmt(item["mean_score"]),
            fmt(signals["cites_material"]), fmt(signals["tests_first"]),
            fmt(signals["gives_next_step"]), fmt(signals["no_fabrication"])))
    lines.append("")
    lines.append("全局引用页面可核验率：%s%%；引用支持率（被引页含场景 evidence_keys）：%s%%。" % (
        fmt(section["citation_validity_pct"]), fmt(section["citation_support_pct"])))
    lines.append("")
    judge = section["judge"]
    if judge.get("enabled"):
        lines.append("LLM judge：已启用（%s）。确定性 rubric 仍是离线参考。" % judge.get("provider"))
    else:
        lines.append("LLM judge：**未启用**（%s：%s）。本期报告全部为确定性 rubric 分数。"
                     % (judge.get("reason"), judge.get("note")))
    lines.append("")
    lines.append("| 场景 | 类别 | 质量 | 分数 | cites | tests-first | next-step | no-fabrication |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for item in section["scenarios"]:
        signals = item["signals"]
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
            item["id"], item["category"], item["quality"], fmt(item["score"]),
            "✅" if signals["cites_material"] else "❌",
            "✅" if signals["tests_first"] else "❌",
            "✅" if signals["gives_next_step"] else "❌",
            "✅" if signals["no_fabrication"] else "❌"))
    lines.append("")
    return lines


def _performance_section(results: Mapping[str, Any]) -> list[str]:
    section = results["layers"]["performance"]
    if section.get("skipped"):
        return ["## 5. 性能小项（当前容器）", "", "本次运行使用 `--no-performance` 跳过了计时探针。", ""]
    ingest = section["ingest"]
    machine = section["machine"]
    lines = ["## 5. 性能小项（当前容器）", ""]
    lines.append("机器：`%s`，Python %s，CPU %s，%s。%s。" % (
        machine["platform"], machine["python"], machine["cpu_count"],
        machine["machine"], machine["note"]))
    lines.append("")
    lines.append("| 摄取 | workers | 文件 | 页 | 中位耗时(s) | 文件/分钟 | 页/分钟 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for key in ("serial", "parallel"):
        item = ingest[key]
        lines.append("| %s | %d | %d | %d | %s | %s | %s |" % (
            key, item["workers"], item["files"], item["pages"], fmt(item["median_seconds"]),
            fmt(item["files_per_minute"]), fmt(item["pages_per_minute"])))
    lines.append("")
    lines.append("并行加速比（串行/并行，%d workers）：**%sx**。" % (
        ingest["parallel_workers"], fmt(ingest["speedup_serial_over_parallel"])))
    lines.append("")
    lines.append("说明：文本摄取主要是 CPU/GIL-bound。本容器上的实测加速比见上表；该数字如实"
                 "报告，不作为“并行更快”的结论。并行路径的**业务产物等价性**在 P2 已由独立"
                 "测试验证。")
    lines.append("")
    lines.append("| 核心命令（进程内） | 中位延迟(ms) | p95(ms) |")
    lines.append("| --- | --- | --- |")
    for key in ("status_ms", "ask_ms", "quiz_ms", "cli_status_ms"):
        item = section["commands"][key]
        lines.append("| %s | %s | %s |" % (key, fmt(item["median"]), fmt(item["p95"])))
    lines.append("")
    lines.append("说明：%s。绝对延迟与机器强相关，不作为 CI 门槛。" % section["commands"]["note"])
    lines.append("")
    return lines


def render_report(results: Mapping[str, Any]) -> str:
    """Render the full BENCH_REPORT.md from one results object."""
    meta = results["meta"]
    lines = [
        "# examprep-agent Benchmark Report",
        "",
        "由运行器自动生成，禁止手填。每次运行写入 `results.json` 与本报告；本报告所有数字均来自同一次运行的 JSON。",
        "",
        "- 生成时间（UTC）：`%s`" % meta["generated_at"],
        "- 运行命令：`%s`" % meta["command"],
        "- 种子：`%s`" % meta["seed"],
        "- Python：`%s`；平台：`%s`；CPU：`%s`" % (
            meta["machine"]["python"], meta["machine"]["platform"], meta["machine"]["cpu_count"]),
        "- 确定性说明：Grounding / Behavior / LongHorizon / Tutor 四层在相同种子下可复现；"
        "Performance 是计时数据，随机器负载波动。",
        "",
        "## 0. 结果摘要",
        "",
        *(_summary_rows(results, "zh")),
        "",
        "## 目录",
        "",
        "1. [GroundingBench（溯源层）](#1-groundingbench溯源层)",
        "2. [BehaviorBench（行为层）](#2-behaviorbench行为层)",
        "3. [LongHorizon Sim（长程层）](#3-longhorizon-sim长程层)",
        "4. [TutorRubric（教学层）](#4-tutorrubric教学层)",
        "5. [性能小项（当前容器）](#5-性能小项当前容器)",
        "6. [诚实边界与复现](#6-诚实边界与复现)",
        "",
        *_grounding_section(results),
        *_behavior_section(results),
        *_longhorizon_section(results),
        *_tutor_section(results),
        *_performance_section(results),
        "## 6. 诚实边界与复现",
        "",
        "- 四层默认全部离线：Grounding 的抽取器、Behavior 的注入脚本、LongHorizon 的模拟学习者、"
        "Tutor 的 rubric 都是确定性的；默认路径不调用真实 LLM。",
        "- Grounding 的三臂是**信息可得性**对照：闭卷只有 5 条记忆事实、裸文件有全文但无页码锚点、"
        "agent 走生产 `StudyService.ask`。读取器确定性抽取，金标只进入评分器；结论只覆盖检索、"
        "支持检查、抽取与引用核验管线，不是某个 LLM 的世界知识评测。",
        "- Behavior 的主指标是**材料诱导**链路（毒材料在上下文，脚本模型据此发起工具调用），"
        "补充用例单列；100% 拦截只说明这些用例上的确定性边界成立，不是外部红队结论。",
        "- LongHorizon 是参数化模拟；**模拟结论 ≠ 真人效果**。隐藏学习者运行在独立子进程，"
        "隔离布尔值由实际读写/篡改实验计算。",
        "- TutorRubric 是固定转录上的确定性 rubric；引用/claim/下一步均做可核验检查，"
        "但仍是关键词与命令语法级校验，不是逐句 NLI。`--judge` 未启用时报告已注明。",
        "- 性能数字来自当前容器，仅用于回归比较，不构成跨机器承诺。",
        "",
        "复现：",
        "",
        "```bash",
        meta["command"],
        "python -m pytest -q",
        "```",
        "",
        "机器可读结果：`results.json`（与本报告同目录）。",
        "",
    ]
    return "\n".join(lines)


def render_preview(results: Mapping[str, Any]) -> str:
    """Short human-readable stdout summary printed by ``run_all``."""
    grounding = results["layers"]["grounding"]["arms"]
    behavior = results["layers"]["behavior"]["summary"]
    longhorizon = results["layers"]["longhorizon"]["arms"]
    tutor = results["layers"]["tutor"]
    performance = results["layers"]["performance"]
    perf_line = "skipped"
    if not performance.get("skipped"):
        ingest = performance["ingest"]
        perf_line = "%sx (workers=%d)" % (
            fmt(ingest["speedup_serial_over_parallel"]), ingest["parallel_workers"])
    return "\n".join([
        "GroundingBench  top1 closed=%s%% raw=%s%% agent=%s%%; recall@3=%s%%; "
        "citation=%s%%; refusal=%s%%" % (
            fmt(grounding["closed_book"]["material_answer_accuracy_pct"]),
            fmt(grounding["raw_file"]["material_answer_accuracy_pct"]),
            fmt(grounding["agent"]["material_answer_accuracy_pct"]),
            fmt(grounding["agent"]["evidence_recall_at_3_pct"]),
            fmt(grounding["agent"]["citation_accuracy_pct"]),
            fmt(grounding["agent"]["out_of_material_refusal_pct"])),
        "BehaviorBench    induced attack block=%s%% (%d/%d); normal=%s%% (%d/%d); "
        "negative=%s%% (%d/%d); invariants=%s%%" % (
            fmt(behavior["induced_attack_block_pct"]), behavior["induced_attack_blocked"],
            behavior["induced_attack_total"], fmt(behavior["normal_pass_pct"]),
            behavior["normal_passed"], behavior["normal_total"],
            fmt(behavior["negative_control_pass_pct"]), behavior["negative_control_passed"],
            behavior["negative_control_total"], fmt(behavior["invariant_pass_pct"])),
        "LongHorizon      delayed retention agent=%s%% baseline=%s%%; gain/budget h agent=%s baseline=%s" % (
            fmt(longhorizon["agent_guided"]["delayed_retention_pct"]),
            fmt(longhorizon["baseline_fixed_order"]["delayed_retention_pct"]),
            fmt(longhorizon["agent_guided"]["gain_per_budget_hour"]),
            fmt(longhorizon["baseline_fixed_order"]["gain_per_budget_hour"])),
        "TutorRubric      overall=%s good=%s medium=%s flawed=%s bypass=%s judge=%s" % (
            fmt(tutor["overall_mean_score"]), fmt(tutor["good_mean_score"]),
            fmt(tutor["medium_mean_score"]), fmt(tutor["flawed_mean_score"]),
            fmt(tutor["bypass_mean_score"]), tutor["judge"].get("enabled")),
        "Performance      ingest speedup=%s" % perf_line,
    ]) + "\n"


# ---------------------------------------------------------------- docs sync
def _replace_block(text: str, start: str, end: str, content: str) -> str:
    left = text.find(start)
    right = text.find(end)
    if left < 0 or right < 0 or right <= left:
        raise ValueError("missing documentation marker: %s ... %s" % (start, end))
    return text[:left + len(start)] + "\n" + content.rstrip() + "\n" + text[right:]


def p3_snapshot(results: Mapping[str, Any]) -> str:
    """Generated snapshot block for P3_REPORT.md."""
    meta = results["meta"]
    return "\n".join([
        "由 `run_all` 从同一次 `results.json` 自动生成，禁止手填。",
        "",
        "- 生成时间（UTC）：`%s`" % meta["generated_at"],
        "- 运行命令：`%s`" % meta["command"],
        "- 种子：`%s`" % meta["seed"],
        "",
        summary_table(results, "zh").rstrip(),
    ]) + "\n"


def sync_docs(results: Mapping[str, Any], *, readme_path: str | Path | None = None,
              p3_path: str | Path | None = None) -> dict[str, bool]:
    """Write the generated tables into README.md and P3_REPORT.md.

    Both files must carry the marker pairs; a missing marker is an error so a
    stale table can never be silently left behind.
    """
    readme = Path(readme_path or (REPO_ROOT / "README.md"))
    p3 = Path(p3_path or (REPO_ROOT / "P3_REPORT.md"))
    readme_text = readme.read_text(encoding="utf-8")
    readme_text = _replace_block(readme_text, README_EN_START, README_EN_END,
                                 summary_table(results, "en"))
    readme_text = _replace_block(readme_text, README_ZH_START, README_ZH_END,
                                 summary_table(results, "zh"))
    readme.write_text(readme_text, encoding="utf-8")
    p3_text = p3.read_text(encoding="utf-8")
    p3_text = _replace_block(p3_text, P3_START, P3_END, p3_snapshot(results))
    p3.write_text(p3_text, encoding="utf-8")
    return {"readme": True, "p3_report": True}
