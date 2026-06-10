# -*- coding: utf-8 -*-
"""
PDF OCR + 翻译任务监控脚本
持续监测 pdf_text_agent.py 的运行进度，显示处理速度与预计完成时间。
"""
from __future__ import annotations

import os
import sys
import time
import json
from datetime import datetime, timedelta
from pathlib import Path

# ── 基本路径配置 ──
BASE_DIR = Path(__file__).parent
OUTPUTS_DIR = BASE_DIR / "outputs"
TOTAL_PAGES = 190  # 源 PDF 总页数


def get_process_info() -> dict | None:
    """获取正在运行的 pdf_text_agent 进程信息（兼容 Windows 多种查询方式）"""
    import subprocess

    # 方法1: 使用 PowerShell 查询（更可靠）
    ps_script = (
        "Get-WmiObject Win32_Process -Filter \"Name='python.exe'\" "
        "| Where-Object { $_.CommandLine -like '*pdf_text_agent*' } "
        "| Select-Object ProcessId,CommandLine,CreationDate "
        "| ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
        )
        if result.stdout.strip():
            import json as _json
            data = _json.loads(result.stdout.strip())
            if isinstance(data, dict):
                data = [data]
            for item in data:
                cmd = item.get("CommandLine", "")
                if "pdf_text_agent" in cmd:
                    pid = str(item.get("ProcessId", ""))
                    creation = item.get("CreationDate", "")
                    start = None
                    if creation:
                        ts = creation.split(".")[0]
                        try:
                            start = datetime.strptime(ts, "%Y%m%d%H%M%S")
                        except ValueError:
                            start = None
                    return {"pid": pid, "command": cmd, "start": start}
    except Exception:
        pass

    # 方法2: 备用 wmic
    try:
        result = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get",
             "ProcessId,CommandLine,CreationDate", "/format:list"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        blocks = result.stdout.strip().split("\n\n")
        for block in blocks:
            info = {}
            for line in block.strip().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    info[k.strip()] = v.strip()
            cmd = info.get("CommandLine", "")
            if "pdf_text_agent" in cmd:
                pid = info.get("ProcessId", "")
                creation = info.get("CreationDate", "")
                start = None
                if creation:
                    ts = creation.split(".")[0]
                    try:
                        start = datetime.strptime(ts, "%Y%m%d%H%M%S")
                    except ValueError:
                        start = None
                return {"pid": pid, "command": cmd, "start": start}
    except Exception:
        pass

    # 方法3: 最终回退 - 通过 psutil 检查
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "cmdline", "create_time", "name"]):
            if proc.info.get("name", "").lower() == "python.exe":
                cmdline = " ".join(proc.info.get("cmdline", []) or [])
                if "pdf_text_agent" in cmdline:
                    return {
                        "pid": str(proc.info["pid"]),
                        "command": cmdline,
                        "start": datetime.fromtimestamp(proc.info["create_time"]),
                    }
    except ImportError:
        pass
    except Exception:
        pass

    return None


def _find_best_stamp_dir(output_dir: Path) -> Path | None:
    """在 output_dir 下找到最适合监控的 stamp 子目录。

    优先级：
    1. 有 _page_images 且图片最多且未完成（中断任务）
    2. 有 _checkpoints 且最多且未完成
    3. 最新的有页面图片的目录
    """
    if not output_dir.exists():
        return None

    candidates: list[Path] = []
    # 遍历所有目录（含子目录中的 stamp 子目录）
    for d in output_root_iter(output_dir):
        if (d / "_page_images").exists() or (d / "_checkpoints").exists():
            candidates.append(d)

    if not candidates:
        return None

    def _score(p: Path) -> tuple:
        has_trans = (p / "translation.md").exists()
        has_summary = (p / "summary.md").exists()
        is_done = has_trans and has_summary
        img_count = len(list((p / "_page_images").glob("page_*.jpg"))) if (p / "_page_images").exists() else 0
        cp_count = len(list((p / "_checkpoints").glob("page_*.json"))) if (p / "_checkpoints").exists() else 0
        # 未完成 + 图片/检查点多 = 优先
        return (0 if is_done else 1, img_count + cp_count, p.stat().st_mtime)

    candidates.sort(key=_score, reverse=True)
    return candidates[0]


def output_root_iter(output_dir: Path):
    """遍历 output_dir 下所有 stamp 子目录（含子目录中的子目录）"""
    for d in output_dir.iterdir():
        if not d.is_dir() or d.name.startswith("_"):
            continue
        # 检查直接子目录
        if (d / "_page_images").exists() or (d / "_checkpoints").exists():
            yield d
        # 检查子目录中的子目录
        for sub in d.iterdir():
            if sub.is_dir() and ((sub / "_page_images").exists() or (sub / "_checkpoints").exists()):
                yield sub


def count_page_images(output_dir: Path) -> tuple[int, str, float]:
    """统计已渲染的页面图片数量，返回 (数量, 最新文件名, 最新文件距今秒数)"""
    stamp_dir = _find_best_stamp_dir(output_dir)
    if stamp_dir is None:
        return 0, "", 0

    images = sorted(stamp_dir.rglob("page_*.jpg"))
    count = len(images)
    if images:
        last = images[-1]
        age = (datetime.now() - datetime.fromtimestamp(last.stat().st_mtime)).total_seconds()
        return count, last.name, age
    return 0, "", 0


def count_checkpoints(output_dir: Path) -> tuple[int, str, float]:
    """统计已保存的 OCR 检查点数量，返回 (数量, 最新文件名, 最新文件距今秒数)"""
    stamp_dir = _find_best_stamp_dir(output_dir)
    if stamp_dir is None:
        return 0, "", 0

    cp_dir = stamp_dir / "_checkpoints"
    if not cp_dir.exists():
        return 0, "", 0

    cps = sorted(cp_dir.glob("page_*.json"))
    count = len(cps)
    if cps:
        last = cps[-1]
        age = (datetime.now() - datetime.fromtimestamp(last.stat().st_mtime)).total_seconds()
        return count, last.name, age
    return 0, "", 0


def check_phase(output_dir: Path) -> str:
    """判断当前处于哪个阶段：ocr / translate / summary / done"""
    stamp_dir = _find_best_stamp_dir(output_dir)
    if stamp_dir is None:
        return "unknown"
    has_trans = (stamp_dir / "translation.md").exists()
    has_summary = (stamp_dir / "summary.md").exists()
    has_docx = (stamp_dir / "summary.docx").exists()

    if has_docx or has_summary:
        return "done"
    if has_trans:
        return "summary"
    return "ocr"


def render_dashboard(proc_info: dict | None, done: int, phase: str,
                     last_img: str, last_age: float,
                     checkpoints: int = 0, last_cp: str = "", last_cp_age: float = 0) -> str:
    """渲染监控仪表盘文本"""
    now = datetime.now()
    lines = []
    lines.append("")
    lines.append("=" * 60)
    lines.append("  PDF OCR + 翻译任务监控仪表盘")
    lines.append(f"  刷新时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 60)

    if proc_info is None:
        lines.append("")
        lines.append("  ⚠ 未检测到 pdf_text_agent 运行进程")
        if phase == "done":
            lines.append("  ✅ 任务已完成!")
        else:
            lines.append("  ❌ 进程可能已退出或尚未启动")
        lines.append("")
        lines.append("=" * 60)
        return "\n".join(lines)

    pid = proc_info["pid"]
    start = proc_info["start"]
    cmd = proc_info["command"]

    # 解析命令行参数
    llm_core = "unknown"
    ocr_core = "unknown"
    for part in cmd.split():
        if "--llm-core" in part:
            pass  # 下一个参数
    parts = cmd.split()
    for i, p in enumerate(parts):
        if p == "--llm-core" and i + 1 < len(parts):
            llm_core = parts[i + 1]
        if p == "--ocr-llm-core" and i + 1 < len(parts):
            ocr_core = parts[i + 1]

    elapsed = now - start if start else timedelta(0)
    elapsed_sec = elapsed.total_seconds()
    elapsed_min = elapsed_sec / 60

    remaining = TOTAL_PAGES - done
    pct = round(done / TOTAL_PAGES * 100, 1) if TOTAL_PAGES > 0 else 0

    speed_ppm = round(done / elapsed_min, 3) if elapsed_min > 0 and done > 0 else 0
    speed_mpp = round(elapsed_min / done, 2) if done > 0 else 0
    speed_spp = round(elapsed_sec / done, 1) if done > 0 else 0

    # 时间预估
    phase_names = {"ocr": "OCR 识别", "translate": "翻译", "summary": "总结"}
    current_phase_name = phase_names.get(phase, phase)

    if phase == "ocr":
        ocr_remain_min = round(remaining / speed_ppm, 1) if speed_ppm > 0 else 0
        trans_est_min = round(TOTAL_PAGES * 0.35, 0)
        summary_est_min = 3
        total_remain_min = ocr_remain_min + trans_est_min + summary_est_min
    elif phase == "translate":
        ocr_remain_min = 0
        # 检查已翻译页数
        trans_est_min = round(remaining * 0.35, 0)  # 剩余翻译时间
        summary_est_min = 3
        total_remain_min = trans_est_min + summary_est_min
    elif phase == "summary":
        ocr_remain_min = 0
        trans_est_min = 0
        summary_est_min = 3
        total_remain_min = summary_est_min
    else:
        ocr_remain_min = 0
        trans_est_min = 0
        summary_est_min = 0
        total_remain_min = 0

    eta = now + timedelta(minutes=total_remain_min)

    # 进度条
    bar_width = 40
    filled = int(pct / 100 * bar_width)
    bar = "█" * filled + "░" * (bar_width - filled)

    # 输出
    lines.append("")
    lines.append(f"  进程 PID:        {pid}")
    if start:
        lines.append(f"  启动时间:        {start.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"  已运行:          {elapsed}")
    lines.append(f"  LLM 核心:        {llm_core} (翻译) / {ocr_core} (OCR)")
    lines.append("")
    lines.append(f"  源文件:          定本 柄谷行人文学论集 ({TOTAL_PAGES} 页)")
    lines.append(f"  当前阶段:        {current_phase_name}")
    lines.append("")
    lines.append(f"  [{bar}] {pct}%")
    lines.append(f"  已完成: {done}/{TOTAL_PAGES} 页  |  剩余: {remaining} 页")
    if checkpoints > 0:
        lines.append(f"  OCR 检查点:     {checkpoints}/{TOTAL_PAGES} 页")
    lines.append("")
    lines.append("  --- 处理速度 ---")
    lines.append(f"    平均速度:      {speed_ppm} 页/分钟 ({speed_spp} 秒/页)")
    lines.append(f"    最新渲染:      {last_img}  ({last_age:.0f}秒前)")
    if last_cp:
        lines.append(f"    最新检查点:    {last_cp}  ({last_cp_age:.0f}秒前)")
    lines.append("")
    lines.append("  --- 时间预估 ---")

    if phase == "ocr":
        lines.append(f"    OCR 剩余:      {ocr_remain_min:.0f} 分钟")
    lines.append(f"    翻译预估:      {trans_est_min:.0f} 分钟")
    lines.append(f"    总结预估:      {summary_est_min} 分钟")
    lines.append(f"    ──────────────────────────────")
    lines.append(f"    总计剩余:      {total_remain_min:.0f} 分钟 (~{total_remain_min/60:.1f} 小时)")
    lines.append(f"    预计完成:      {eta.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append("=" * 60)

    return "\n".join(lines)


def monitor_loop(interval_sec: int = 120) -> None:
    """持续监控循环，每 interval_sec 秒刷新一次"""
    print("🔍 启动持续监控模式 (每 {} 秒刷新)...".format(interval_sec))
    print("   按 Ctrl+C 停止监控\n")

    prev_done = 0
    round_num = 0
    while True:
        round_num += 1
        proc_info = get_process_info()
        done, last_img, last_age = count_page_images(OUTPUTS_DIR)
        cp_count, last_cp, last_cp_age = count_checkpoints(OUTPUTS_DIR)
        phase = check_phase(OUTPUTS_DIR)

        # 清屏（可选）
        if round_num > 1:
            print("\n" + "─" * 60)

        dashboard = render_dashboard(proc_info, done, phase, last_img, last_age,
                                     checkpoints=cp_count, last_cp=last_cp, last_cp_age=last_cp_age)
        print(dashboard)

        # 检测完成
        if phase == "done":
            print("\n✅ 任务已完成！监控结束。")
            break

        # 进程消失但未完成
        if proc_info is None and phase != "done":
            print("\n⚠ 进程已消失但任务未完成，可能出错。")
            print("   等待下一次检查确认...")
            time.sleep(interval_sec)
            proc_info2 = get_process_info()
            if proc_info2 is None:
                print("   确认：进程未恢复，停止监控。")
                break
            continue

        # 速度变化提示
        if prev_done > 0 and done > prev_done:
            delta = done - prev_done
            delta_min = interval_sec / 60
            recent_speed = round(delta / delta_min, 2)
            print(f"\n  📈 最近 {interval_sec}s 内完成 {delta} 页 (瞬时速度: {recent_speed} 页/分)")

        prev_done = cp_count if cp_count > 0 else done
        time.sleep(interval_sec)


if __name__ == "__main__":
    interval = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    monitor_loop(interval)
