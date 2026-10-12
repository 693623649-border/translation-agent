"""Small, dependency-free human-readable reports for every ingestion attempt."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Mapping


REPORT_NAME = "quality-report.md"


def _json(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _status(result: Mapping[str, Any], audits: list[Mapping[str, Any]]) -> str:
    if result.get("status") in {"passed", "failed", "blocked", "partial", "unverified"}:
        return str(result["status"])
    if result.get("release_blocked") is True:
        return "blocked"
    if any(a.get("status") in {"failed", "blocked", "partial"} or a.get("release_blocked") is True for a in audits):
        return "blocked"
    return "passed" if any(a.get('status') == 'passed' for a in audits) else "unverified"


def write_ingestion_quality_report(
    workspace: Path | str,
    result: Mapping[str, Any] | None = None,
    *,
    evidence: tuple[str, ...] = (),
    status: str | None = None,
    report_filename: str | None = None,
) -> Path:
    """Write a concise report by summarising existing audit JSON files.

    This function performs no OCR, translation, or verifier rerun.  It is safe
    to call from success and early-blocked ingestion paths.
    """
    root = Path(workspace).expanduser().resolve()
    audit_dir = root / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    result = result or {}
    paths = []
    for raw in evidence:
        path = Path(raw)
        if not path.is_absolute():
            path = root / path
        if path.is_file():
            paths.append(path.resolve())
    for path in sorted(audit_dir.glob("*.json")):
        if path.name != REPORT_NAME and path not in paths:
            paths.append(path)
    audit_pairs = [(path, payload) for path in paths if (payload := _json(path)) is not None]
    audits = [payload for _, payload in audit_pairs]
    final_status = status or _status(result, audits)
    release_ready = bool(result.get("release_ready") is True)
    if final_status != "passed":
        release_ready = False
    source = result.get("source") or result.get("source_pdf")
    source_sha = result.get("source_sha256")
    if not source:
        source = next((a.get('source') for a in audits if a.get('source')), None)
    if isinstance(source, Mapping):
        source_sha = source_sha or source.get('sha256')
        source = source.get('path')
    release_label = '允许' if release_ready else ('禁止' if final_status != 'passed' or
                    'release_ready' in result else '未执行完整发布验收')
    lines = [
        "# 入库质检报告",
        "",
        f"- 生成时间（UTC）：{dt.datetime.now(dt.timezone.utc).isoformat()}",
        f"- 原始检查时间：{result.get('generated_at') or '入口未记录；生成报告不代表重新执行检查'}",
        f"- 状态：**{final_status}**",
        f"- 正式发布：**{release_label}**",
        f"- 书名/任务：{result.get('book_title') or result.get('title') or '见来源记录'}",
        f"- 覆盖阶段：{result.get('mode') or result.get('stage') or result.get('adapter') or '入库阶段；未证明全书验收完成'}",
        f"- 来源：`{source or '未记录'}`",
        f"- 来源 SHA256：`{source_sha or '未记录'}`",
        "",
        "## 已有质检证据",
        "",
    ]
    if not paths:
        lines.append("- 未发现可复用的 JSON 质检证据；本报告仅记录入库状态，不能作为正式发布凭证。")
    else:
        for path, payload in audit_pairs:
            summary = payload.get("summary") if isinstance(payload.get("summary"), Mapping) else {}
            errors = payload.get('errors') or payload.get('issues') or []
            issue_count = summary.get("issue_count", len(errors) if isinstance(errors, list) and
                ('errors' in payload or 'issues' in payload) else '未记录')
            lines.append(f"- `{path.relative_to(root)}`：状态 `{payload.get('status', 'unknown')}`，问题数 `{issue_count}`")
    checks = result.get("checks")
    if isinstance(checks, list):
        lines.extend(["", "## 发布检查", "", "| 检查 | 状态 | 摘要 | 度量（实际检查范围） |", "|---|---|---|---|"])
        for check in checks:
            if isinstance(check, Mapping):
                metrics = check.get('metrics') or {}
                brief = '; '.join(f'{k}={v}' for k,v in metrics.items() if isinstance(v,(int,float,bool)))
                lines.append(f"| `{check.get('id', 'unknown')}` | {check.get('status', 'unknown')} | {check.get('summary', '')} | {brief or '未记录'} |")
    gate = result.get('language_gate')
    if isinstance(gate, Mapping):
        lines.extend(['','## 中文入库检查','',
            f"- 外文待处理块：{gate.get('pending_count','未记录')}；语言：{gate.get('languages','未记录')}。",
            f"- 原文/书目豁免范围：{gate.get('exempt','未记录')}；显式保留外文：{gate.get('allowed_foreign',False)}。"])
    fidelity = result.get('docx_fidelity')
    if isinstance(fidelity, Mapping):
        lines.extend(['','## 源文本与 Word 保真','',
            f"- 结果：{fidelity.get('ok','未记录')}；缺字：{fidelity.get('missing_characters','未记录')}；多字：{fidelity.get('extra_characters','未记录')}。",
            '- 比较的是当前源文本与 Word，不证明扫描原图 OCR 逐字准确。'])
    issues = result.get("errors") or result.get("issues") or ([result['error']] if result.get('error') else None)
    if isinstance(issues, list) and issues:
        lines.extend(["", "## 失败问题", ""])
        for item in issues[:100]:
            if isinstance(item, Mapping):
                code = item.get("code") or item.get("kind") or item.get("check_id") or "unknown"
                details = item.get('evidence') or {}
                pages = item.get("pages") or item.get("page") or item.get("pdf_page") or details.get('pages')
                suffix = f"；页：{pages}" if pages is not None else ""
                lines.append(f"- `{code}`{suffix}：{item.get('message') or item.get('summary') or item}")
            else:
                lines.append(f"- {item}")
    summary = result.get("summary") or result
    if isinstance(summary, Mapping):
        counts = ", ".join(f"{key}={summary[key]}" for key in ("checkpoint_pages", "translated_pages", "chapter_count", "issue_count") if key in summary)
        if counts:
            lines.extend(["", f"- 实际统计：{counts}"])
    if isinstance(checks, list):
        human = next((c.get('metrics',{}) for c in checks if c.get('id')=='ocr.human_review'),{})
        if 'confirmed_review_count' in human:
            lines.extend(['', '## 人工核图范围', '',
                f"- 当前有效人类确认：{human['confirmed_review_count']} 页。",
                f"- 当前要求核图：{len(human.get('required_review_pages',[]))} 页；抽样 {human.get('sample_page_count','未记录')} 页。",
                '- 代理建议校订、机器检查及测试用例不计为人工逐字核图。'])
    lines.extend([
        "",
        "## 验收边界",
        "",
        "- 本报告汇总已有审计结果，不替代逐页人工核图。",
        "- OCR 结构匹配只能说明结构检查结果，不能证明原图逐字准确，也不能证明语义完整。",
        "- 页眉页脚、书眉和纸页码不进入读者正文；原始 OCR 与结构证据仍保存在审计层。",
        "- 状态为 blocked、partial 或 failed 时，不得视为正式发布通过。",
    ])
    # Chapter/incremental verification must not replace the whole-book report.
    filename = report_filename or (
        "quality-report-chapters.md" if result.get("mode") == "chapters" else REPORT_NAME
    )
    target = audit_dir / filename
    temp = target.with_name(f'.{target.name}.{uuid.uuid4().hex}.tmp')
    try:
        temp.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)
    return target


__all__ = ["REPORT_NAME", "write_ingestion_quality_report"]
