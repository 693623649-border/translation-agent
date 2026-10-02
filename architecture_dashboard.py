"""Bounded, read-only architecture evidence and an isolated HTML inspector."""
from __future__ import annotations

import ast
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

MAX_BYTES = 512 * 1024
GROUPS = {"orchestration": "编排", "processing": "文档处理", "knowledge": "知识管理", "review": "独立审查"}
LABELS = {"inspect": "检查来源", "import": "导入页面", "load": "加载", "ocr": "文字识别", "text_extract": "提取文本", "proofread": "校对", "translate": "翻译", "resolve": "解析目录", "from_outline": "读取书签", "compile": "编排章节", "semantic": "语义重建", "sanitize": "出版清理", "knowledge_base": "发布知识库", "epub": "发布 EPUB", "docx": "发布 Word", "reference_pdf": "参考 PDF", "verify": "出版验证", "word": "Word 验证", "status": "汇总状态"}


def _contained(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("工作区路径超出允许范围")
    return resolved


def _json(path: Path, root: Path, warnings: list[str]) -> dict:
    try:
        path = _contained(path, root)
        if not path.exists():
            return {}
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("文件超过读取上限")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("需要 JSON 对象")
        return data
    except (OSError, ValueError) as exc:
        warnings.append(f"{path.name}：无法读取（{type(exc).__name__}）")
        return {}


def discover_workspaces(repo: Path, limit: int = 100) -> list[Path]:
    root = repo / "outputs"
    if not root.is_dir():
        return []
    found = []
    for path in root.iterdir():
        if len(found) >= min(max(limit, 0), 100):
            break
        recognizable = any((path / relative).is_file() for relative in (
            "chapters.json", "knowledge_base.jsonl", ".pipeline_graph/state.json",
            "audit/release-report.json"))
        if (not path.name.startswith(".") and path.is_dir() and recognizable
                and not path.is_symlink() and path.resolve().is_relative_to(root.resolve())):
            found.append(path.resolve())
    return sorted(found, key=lambda path: path.name)


def _nodes(repo: Path) -> list[dict]:
    source = repo / "pipeline_graph/book.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    nodes = [{"id": "application", "label": "任务编排", "group": "orchestration", "module": "application_service.py", "responsibility": "登记任务、构建运行计划与管理工作区"}]
    for item in tree.body:
        if isinstance(item, ast.Assign) and isinstance(item.value, ast.Constant) and isinstance(item.value.value, str):
            if any(isinstance(target, ast.Name) and target.id.startswith("NODE_") for target in item.targets):
                node_id = item.value.value
                group = "review" if ".verify" in node_id else "knowledge" if "knowledge_base" in node_id else "orchestration" if "status" in node_id else "processing"
                nodes.append({"id": node_id, "label": ({"core.pages.load": "加载页面", "core.toc.load": "加载目录", "core.chapters.load": "加载章节"}.get(node_id) or LABELS.get(node_id.split(".")[-1], node_id)), "group": group, "module": f"pipeline_graph/book.py:{item.lineno}", "responsibility": f"{node_id} 对应的流水线节点；具体启用及依赖由任务 recipe 决定"})
    nodes.append({"id": "global_index", "label": "全局 SQLite 索引", "group": "knowledge", "module": "global_knowledge_base.py", "responsibility": "跨书索引、短词检索与增量同步"})
    return nodes


def collect_snapshot(repo: Path, *, workspace: Path | None = None, allowed_root: Path | None = None, job: dict | None = None) -> dict:
    repo = repo.resolve()
    warnings: list[str] = []
    result = {"captured_at": datetime.now(timezone.utc).isoformat(), "scope": workspace.name if workspace else "项目总览", "nodes": _nodes(repo), "events": [], "warnings": warnings, "job": {}, "review": {}, "knowledge": {}}
    if job:
        result["job"] = {key: str(job[key])[:160] for key in ("id", "status", "updated_at", "source_mode") if key in job}
    if workspace:
        workspace = _contained(workspace, allowed_root or repo / "outputs")
        state = _json(workspace / ".pipeline_graph/state.json", workspace, warnings)
        stored = state.get("nodes", {})
        stored = stored if isinstance(stored, dict) else {}
        for node in result["nodes"]:
            item = stored.get(node["id"], {})
            if isinstance(item, dict) and isinstance(item.get("completed_at"), str):
                node.update(status="执行完成记录", timestamp=item["completed_at"][:80], evidence=".pipeline_graph/state.json")
        path = workspace / ".pipeline_graph/events.jsonl"
        try:
            path = _contained(path, workspace)
            if path.is_file():
                with path.open("rb") as stream:
                    stream.seek(0, 2)
                    offset = max(0, stream.tell() - MAX_BYTES)
                    stream.seek(offset)
                    if offset:
                        stream.readline()
                    lines = stream.read(MAX_BYTES).splitlines()[-100:]
                for line in lines:
                    try:
                        event = json.loads(line)
                        if isinstance(event, dict):
                            result["events"].append({key: str(event[key])[:160] for key in ("event", "node", "timestamp", "run_id", "error_type") if key in event})
                    except ValueError:
                        warnings.append("events.jsonl：存在损坏事件")
                status_map = {"node_started": "开始记录（当前状态未确认）", "node_succeeded": "执行完成记录", "node_failed": "执行失败", "node_skipped": "复用缓存记录"}
                for event in result["events"]:
                    for node in result["nodes"]:
                        if node["id"] == event.get("node") and event.get("event") in status_map:
                            node.update(status=status_map[event["event"]], timestamp=event.get("timestamp", "未记录"), evidence=".pipeline_graph/events.jsonl")
        except (OSError, ValueError):
            warnings.append("events.jsonl：无法读取")
        report = _json(workspace / "audit/release-report.json", workspace, warnings)
        result["review"] = {key: report[key] for key in ("release_ready", "mode", "publication_profile", "generated_at") if isinstance(report.get(key), (str, bool, int, float))}
        if report:
            result["review"]["note"] = "历史报告；尚未复核来源新鲜度，不能据此认定当前可发布"
    db_path = repo / "global_knowledge_base.sqlite3"
    if db_path.is_file() and not db_path.is_symlink():
        try:
            with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                db.set_progress_handler(lambda: 1, 500000)
                meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('schema_version','updated_at','built_at','created_at') LIMIT 4"))
                result["knowledge"] = {**meta, "workspaces": db.execute("SELECT count(*) FROM workspaces").fetchone()[0], "chunks": db.execute("SELECT count(*) FROM chunks").fetchone()[0], "snapshot_at": datetime.fromtimestamp(db_path.stat().st_mtime, timezone.utc).isoformat()}
        except sqlite3.Error:
            warnings.append("知识库索引：读取失败或超过查询预算")
    benchmark_path = repo / "work/kb-core-optimization/benchmark.json"
    benchmark = _json(benchmark_path, repo, warnings)
    evaluation = benchmark.get("evaluation", {})
    if isinstance(evaluation, dict) and benchmark:
        result["benchmark"] = {"snapshot_at": datetime.fromtimestamp(benchmark_path.stat().st_mtime, timezone.utc).isoformat()}
        for key in ("passed", "source_current", "integrity"):
            if isinstance(evaluation.get(key), (str, bool, int, float)):
                result["benchmark"][key] = evaluation[key]
        if isinstance(benchmark.get("equivalent"), bool):
            result["benchmark"]["equivalent"] = benchmark["equivalent"]
    for node in result["nodes"]:
        if node["id"] == "global_index" and result["knowledge"]:
            knowledge = result["knowledge"]
            node.update(status="索引存在（快照）", timestamp=knowledge.get("built_at", knowledge["snapshot_at"]),
                        evidence=f"global_knowledge_base.sqlite3：{knowledge['workspaces']} 个工作区，{knowledge['chunks']} 块；未在本次刷新重新验证来源")
        node.setdefault("status", "未记录")
        node.setdefault("evidence", "仅架构定义")
    return result



from architecture_ascii import render_ascii, render_dashboard
