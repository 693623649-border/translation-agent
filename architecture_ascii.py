"""One character-cell layout projected to plain text and interactive HTML."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
import json
import unicodedata

COLUMNS = 118
COLORS = {"cyan", "blue", "green", "purple", "muted", "text", "red"}


def _clean_text(value) -> str:
    text = unicodedata.normalize("NFC", str(value if value is not None else ""))
    return "".join(" " if (unicodedata.category(char).startswith("C")
                            or unicodedata.category(char) in {"Zl", "Zp"}) else char
                   for char in text)


def _display_width(text: str) -> int:
    return sum(0 if unicodedata.combining(char) else
               2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
               for char in text)


def _fit_text(value, width: int, *, center: bool = False) -> str:
    text = _clean_text(value)
    if _display_width(text) > width:
        shortened = ""
        for char in text:
            if _display_width(shortened + char) > max(0, width - 1):
                break
            shortened += char
        text = shortened + ("…" if width else "")
    padding = max(0, width - _display_width(text))
    left = padding // 2 if center else 0
    return " " * left + text + " " * (padding - left)


def _local_time(value, *, clock_only: bool = False) -> str:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return _clean_text(value)
        stamp = stamp.astimezone(timezone(timedelta(hours=8)))
        return stamp.strftime("%H:%M:%S" if clock_only else "%m-%d %H:%M +08")
    except (ValueError, TypeError):
        return _clean_text(value or "未记录")


def _marker(status) -> str:
    return {"执行失败": "[!]", "开始记录（当前状态未确认）": "[~]",
            "复用缓存记录": "[C]", "索引存在（快照）": "[DB]",
            "执行完成记录": "[OK]"}.get(str(status), "[?]")


class _Canvas:
    def __init__(self, rows: int, nodes: dict):
        self.cells = [[(" ", "muted", None) for _ in range(COLUMNS)] for _ in range(rows)]
        self.nodes = nodes
        self.used = set()

    def put(self, x: int, y: int, value, color="text", node=None):
        for char in _clean_text(value):
            width = _display_width(char)
            if not width:
                if x > 0:
                    previous = x - 1
                    while previous and not self.cells[y][previous][0]:
                        previous -= 1
                    glyph, ink, owner = self.cells[y][previous]
                    self.cells[y][previous] = (glyph + char, ink, owner)
                continue
            if x + width > COLUMNS:
                break
            self.cells[y][x] = (char, color, node)
            for continuation in range(1, width):
                self.cells[y][x + continuation] = ("", color, node)
            x += width

    def box(self, x, y, width, height, color, title):
        self.put(x, y, "┌" + "─" * (width - 2) + "┐", color)
        for row in range(y + 1, y + height - 1):
            self.put(x, row, "│", color)
            self.put(x + width - 1, row, "│", color)
        self.put(x, y + height - 1, "└" + "─" * (width - 2) + "┘", color)
        self.put(x + 1, y + 1, _fit_text(title, width - 2, center=True), color)

    def link(self, node_id, x, y, width, color):
        node = self.nodes.get(node_id)
        if node is None:
            self.put(x, y, _fit_text("未定义", width), "muted")
            return
        status = _marker(node.get("status", "未记录"))
        label = _fit_text(node.get("label", node_id), width - len(status) - 1)
        self.put(x, y, label + " " + status, color, node_id)
        self.used.add(node_id)

    def text(self):
        return "\n".join("".join(cell[0] for cell in row) for row in self.cells)

    def html(self):
        lines = []
        for row in self.cells:
            pieces = []
            index = 0
            while index < COLUMNS:
                color, owner = row[index][1:]
                end = index + 1
                while end < COLUMNS and row[end][1:] == (color, owner):
                    end += 1
                glyphs = []
                for glyph, _ink, _owner in row[index:end]:
                    if glyph:
                        wide = " wide" if _display_width(glyph) == 2 else ""
                        glyphs.append('<span class="glyph' + wide + '" aria-hidden="true">'
                                      + escape(glyph) + "</span>")
                content = '<span class="ink-' + color + '">' + "".join(glyphs) + "</span>"
                if owner is not None:
                    node = self.nodes[owner]
                    label = _clean_text(node.get("label", owner)) + " · " + _clean_text(node.get("status", "未记录"))
                    content = ('<button type="button" class="node-link" data-node="'
                               + escape(owner, quote=True) + '" aria-label="'
                               + escape(label, quote=True) + '" aria-pressed="false">'
                               + content + "</button>")
                pieces.append(content)
                index = end
            lines.append('<span class="terminal-line">' + "".join(pieces) + "</span>")
        return "".join(lines)


def _layout(snapshot: dict) -> _Canvas:
    nodes = {str(node["id"]): node for node in snapshot.get("nodes", [])
             if isinstance(node, dict) and "id" in node}
    known = {"application", "core.source.inspect", "core.pages.import",
             "core.pages.text_extract", "core.pages.load", "core.pages.ocr",
             "core.pages.proofread", "core.pages.translate", "core.reconstruct.semantic",
             "core.toc.load", "core.toc.resolve", "core.toc.from_outline",
             "core.chapters.load", "core.chapters.compile", "core.publication.sanitize",
             "core.publish.docx", "core.publish.epub", "core.publish.reference_pdf",
             "core.publish.knowledge_base", "global_index", "core.publication.verify",
             "core.publication.verify.word", "core.pipeline.status"}
    extra = sorted(set(nodes) - known)
    canvas = _Canvas(65 + (len(extra) + 2 if extra else 0), nodes)
    canvas.put(0, 0, _fit_text("TRANSLATION AGENT TREE · 项目架构与审查", COLUMNS, center=True), "text")
    canvas.put(0, 1, "═" * COLUMNS, "muted")
    for x, value, color in ((8, "■ 编排", "cyan"), (32, "■ 来源策略", "green"),
                            (59, "■ 文档模块", "blue"), (87, "■ 独立审查", "purple")):
        canvas.put(x, 2, value, color)
    offset = 4
    canvas.box(0, offset, 26, 53, "purple", "REVIEW / 审查")
    review = [(4, "◇ 编排前核对"), (7, "source / recipe / output"),
              (10, "检查职责、来源与边界"), (12, "执行记录 ≠ 发布质量"),
              (18, "任务：" + str(snapshot.get("job", {}).get("status", "未记录"))),
              (19, f"节点：{len(nodes)} / evidence"),
              (22, "KNOWLEDGE SNAPSHOT"), (28, "◇ 出错时定位"),
              (31, "时间 / 日志 / 失败节点"), (34, "◇ 核对近期评测"),
              (42, "历史报告：需复核" if snapshot.get("review") else "发布验证：未记录"),
              (46, "◇ 完成前复核"), (49, "缺少证据 = 未记录"),
              (51, "只采集，不执行模型")]
    knowledge = snapshot.get("knowledge", {})
    benchmark = snapshot.get("benchmark", {})
    recorded = sum(_marker(node.get("status")) != "[?]" for node in nodes.values())
    review = [(row, f"有证据节点：{recorded}/{len(nodes)}" if row == 19 else value)
              for row, value in review]
    review += [(23, str(knowledge.get("workspaces", "—")) + " workspaces"),
               (24, str(knowledge.get("chunks", "—")) + " chunks"),
               (35, "KB评测快照：" + ("通过" if benchmark.get("passed") is True else
                                    "未通过" if benchmark.get("passed") is False else "未记录")),
               (36, "结果等价：" + ("已核验" if benchmark.get("equivalent") is True else "未记录")),
               (38, _local_time(benchmark.get("snapshot_at") or snapshot.get("captured_at")))]
    for row, value in review:
        canvas.put(1, row + offset, _fit_text(value, 24), "purple" if value.startswith("◇") else "muted")
    canvas.box(52, offset, 42, 7, "cyan", "MAIN / 主编排")
    canvas.link("application", 54, offset + 3, 38, "cyan")
    canvas.put(54, offset + 5, _fit_text("plan → run → checkpoints", 38, center=True), "muted")
    canvas.put(26, offset + 3, "─" * 25 + "→", "muted")
    for row in range(7, 10):
        canvas.put(73, offset + row, "↓" if row == 9 else "│", "muted")
    canvas.box(41, offset + 10, 64, 7, "green", "INPUT ROUTER / 来源策略")
    mode = snapshot.get("job", {}).get("source_mode", "由任务配置决定")
    canvas.put(43, offset + 12, _fit_text("入口：" + str(mode), 60, center=True), "muted")
    canvas.link("core.source.inspect", 43, offset + 13, 60, "green")
    for x, node_id in ((43, "core.pages.import"), (63, "core.pages.text_extract"), (83, "core.pages.load")):
        canvas.link(node_id, x, offset + 14, 19, "green")
    canvas.put(44, offset + 15, _fit_text("MODULE LANES · 模块关系概览，具体依赖见 recipe", 58, center=True), "muted")
    for row in (17, 18, 19):
        canvas.put(73, offset + row, "│", "muted")
    canvas.put(43, offset + 20, "┌" + "─" * 59 + "┐", "muted")
    canvas.put(73, offset + 20, "┼", "muted")
    for center in (43, 73, 103):
        canvas.put(center, offset + 21, "│", "muted")
        canvas.put(center, offset + 22, "↓", "blue")
    for x, title in ((30, "READ / 识别校对"), (60, "TRANSLATE / 译文"), (90, "ASSEMBLE / 编排")):
        canvas.box(x, offset + 23, 26, 9, "blue", title)
    for x, row, node_id in (
            (31, 26, "core.pages.ocr"), (31, 27, "core.pages.proofread"),
            (61, 26, "core.pages.translate"), (61, 27, "core.reconstruct.semantic"),
            (91, 25, "core.toc.load"), (91, 26, "core.toc.resolve"),
            (91, 27, "core.toc.from_outline"), (91, 28, "core.chapters.load"),
            (91, 29, "core.chapters.compile"), (91, 30, "core.publication.sanitize")):
        canvas.link(node_id, x, offset + row, 24, "blue")
    canvas.put(31, offset + 29, _fit_text("pages → text", 24, center=True), "muted")
    canvas.put(61, offset + 29, _fit_text("semantic IR + units", 24, center=True), "muted")
    canvas.put(26, offset + 28, "───→", "muted")
    for center in (43, 73, 103):
        for row in (32, 33):
            canvas.put(center, offset + row, "│", "muted")
    canvas.put(43, offset + 34, "└" + "─" * 59 + "┘", "muted")
    canvas.put(73, offset + 34, "┼", "muted")
    canvas.put(73, offset + 35, "↓", "green")
    canvas.box(41, offset + 36, 64, 7, "green", "PUBLISH / 产物与知识管理")
    for x, node_id in ((43, "core.publish.docx"), (63, "core.publish.epub"), (83, "core.publish.reference_pdf")):
        canvas.link(node_id, x, offset + 38, 19, "green")
    canvas.link("core.publish.knowledge_base", 43, offset + 39, 28, "green")
    canvas.link("global_index", 74, offset + 39, 29, "green")
    canvas.put(43, offset + 40, _fit_text(f"SQLite snapshot: {knowledge.get('workspaces', '—')} books / {knowledge.get('chunks', '—')} chunks", 60, center=True), "muted")
    canvas.put(43, offset + 41, _fit_text("canonical JSONL → local SQLite index", 60, center=True), "muted")
    for row in (43, 44, 45):
        canvas.put(73, offset + row, "↓" if row == 45 else "│", "muted")
    canvas.box(52, offset + 46, 42, 7, "cyan", "BACK TO REVIEW / 独立验证")
    canvas.put(26, offset + 48, "─" * 25 + "→", "muted")
    canvas.link("core.publication.verify", 54, offset + 48, 18, "purple")
    canvas.link("core.publication.verify.word", 73, offset + 48, 19, "purple")
    canvas.put(54, offset + 49, _fit_text("执行记录 ≠ 发布质量", 38, center=True), "muted")
    canvas.link("core.pipeline.status", 54, offset + 51, 38, "cyan")
    canvas.box(0, 58, COLUMNS, 6, "muted", "SESSION LOG / 最近执行事件")
    events = snapshot.get("events", [])[-3:]
    for row, event in enumerate(events, 60):
        text = " · ".join(_clean_text(event.get(key)) for key in ("node", "event", "error_type") if event.get(key))
        canvas.put(2, row, _fit_text(_local_time(event.get("timestamp"), clock_only=True) + "  " + text, COLUMNS - 4), "muted")
    if not events:
        canvas.put(2, 60, _fit_text("未记录事件 · 选择已有任务或发布工作区查看执行记录", COLUMNS - 4), "muted")
    canvas.put(0, 64, _fit_text("~/translation-agent $ python -m streamlit run streamlit_app.py", COLUMNS), "green")
    if extra:
        canvas.put(0, 65, _fit_text("MORE NODES / 其他已定义节点", COLUMNS), "text")
        for row, node_id in enumerate(extra, 66):
            canvas.link(node_id, 0, row, COLUMNS, "blue")
    return canvas


def render_ascii(snapshot: dict) -> str:
    """Return copyable Unicode terminal art, with no markup or ANSI escapes."""
    return _layout(snapshot).text()


def render_dashboard(snapshot: dict) -> str:
    """Project the same character cells into keyboard-operable HTML."""
    payload = json.dumps(snapshot, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return _HEAD + _layout(snapshot).html() + _MIDDLE + payload + _SCRIPT


_HEAD = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><style>
:root{--bg:#11191f;--fg:#e6edf3;--muted:#a4b0bb;--cyan:#7ddde8;--blue:#93b8ed;--green:#79d49f;--purple:#bea0ec;--red:#f9a8a8;--font:clamp(12px,calc((100vw - 40px)/71),15px)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:var(--font)/1.55 "Cascadia Mono","SFMono-Regular",Consolas,"Liberation Mono","Microsoft YaHei UI","PingFang SC",monospace;padding:18px}
.chrome{display:flex;gap:8px;align-items:center;background:#20272f;padding:10px 14px;margin:-18px -18px 16px;color:var(--muted)}.dot{font-size:16px}.title{flex:1;text-align:center}
.canvas-scroll{max-width:100%;overflow-x:auto;padding:6px 2px 14px;scrollbar-color:#40505e var(--bg)}.terminal{margin:0 auto;font:inherit;width:max-content}.terminal-line{display:block;height:1.55em;white-space:pre}
.glyph{display:inline-block;width:1ch;text-align:center}.glyph.wide{width:2ch}.ink-text{color:var(--fg)}.ink-muted{color:var(--muted)}.ink-cyan{color:var(--cyan)}.ink-blue{color:var(--blue)}.ink-green{color:var(--green)}.ink-purple{color:var(--purple)}.ink-red{color:var(--red)}
.node-link{display:inline;background:transparent;color:inherit;font:inherit;line-height:inherit;border:0;border-radius:0;margin:0;padding:0;cursor:pointer;white-space:pre}.node-link:hover{background:#24303b}.node-link[aria-pressed=true]{background:#263b45}.node-link:focus-visible{outline:2px dashed var(--cyan);outline-offset:2px}
.inspector{margin-top:16px}h2{font:inherit;color:var(--cyan);margin:0 0 8px}#detail{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;color:var(--fg);line-height:1.8;margin:0}.hint{color:var(--muted);margin-top:12px}.warn{color:#ffd492}.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}
@media(max-width:700px){:root{--font:12px}body{padding:12px}.chrome{margin:-12px -12px 12px}.title{font-size:11px}}
</style><div class="chrome"><span class="dot ink-red" aria-hidden="true">●</span><span class="dot" style="color:#fbbf24" aria-hidden="true">●</span><span class="dot ink-green" aria-hidden="true">●</span><span class="title">~/translation-agent · architecture · snapshot</span></div>
<div class="canvas-scroll" role="region" aria-label="字符架构树，窄屏可横向滚动" tabindex="0"><pre class="terminal" role="group" aria-label="项目模块字符图">"""
_MIDDLE = """</pre></div><p class="hint" id="stamp"></p><p class="hint">[?] 未记录 / 未知 · [OK] 执行完成记录 · [C] 缓存记录 · [DB] 索引快照 · [!] 失败 · [~] 开始记录，当前状态未确认</p>
<p class="hint">点击节点或用 Tab / Enter 查看详情。图中连接表示模块关系概览；具体执行依赖以任务 recipe 为准。</p><p class="warn" id="warnings"></p>
<section class="inspector" aria-label="节点详情"><h2>┌─ NODE INSPECTOR / 节点详情 ─</h2><pre id="detail" role="status" aria-live="polite">选择字符框内的节点，查看来源、职责与证据。</pre></section>
<div class="sr-only" id="event-accessible" role="log" aria-label="最近执行事件"></div><script type="application/json" id="snapshot">"""
_SCRIPT = """</script><script>
const data=JSON.parse(document.getElementById('snapshot').textContent);
const clean=value=>String(value??'').replace(/[\\u0000-\\u001f\\u007f-\\u009f\\u2028-\\u202e\\u2066-\\u2069]/g,' ');
const local=value=>{const date=new Date(value);return Number.isNaN(date.getTime())?clean(value||'未记录'):new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',dateStyle:'short',timeStyle:'medium'}).format(date)+' +08';};
document.getElementById('stamp').textContent=clean(data.scope)+' · 采集时间 '+local(data.captured_at)+' · 只读快照';
document.getElementById('warnings').textContent=(data.warnings||[]).map(clean).join('\\n');
document.getElementById('event-accessible').textContent=(data.events||[]).slice(-3).map(e=>[e.timestamp,e.node,e.event].filter(Boolean).map(clean).join(' · ')).join('\\n')||'未记录事件';
const nodes=new Map((data.nodes||[]).map(node=>[String(node.id),node]));
document.querySelectorAll('button[data-node]').forEach(button=>button.addEventListener('click',()=>{
 const node=nodes.get(button.dataset.node);if(!node)return;
 document.querySelectorAll('button[data-node]').forEach(other=>other.setAttribute('aria-pressed','false'));
 button.setAttribute('aria-pressed','true');
 document.getElementById('detail').textContent=[clean(node.label),'ID：'+clean(node.id),'职责：'+clean(node.responsibility),'来源：'+clean(node.module),'依赖：此图展示模块关系，具体依赖由任务 recipe 决定','记录：'+clean(node.status),'证据：'+clean(node.evidence),'时间：'+local(node.timestamp)].join('\\n');
}));
</script></html>"""
