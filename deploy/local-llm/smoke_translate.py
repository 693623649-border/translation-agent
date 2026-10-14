"""Save local translation evidence, including rejected raw responses, for human review."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ingestion.book_pipeline import (  # noqa: E402
    ChatTranslator, LocalClient, TRANSLATION_PROMPT_VERSION,
    build_parser, build_translation_client,
)
from translation.paragraph_translation import QUOTES, validate_translation  # noqa: E402
from ingestion.ocr_quality_gate import punctuation_signature  # noqa: E402
from orchestration.pipeline_profiles import load_pipeline_profiles  # noqa: E402

SAMPLES = [
    {"id": "ja-title", "source_language": "ja", "source": (
        "『世界の終りとハードボイルド・ワンダーランド』は村上春樹の長編小説である。"
        "「壁」という概念が作品の中心にあり、読者は二つの物語を行き来しながら読み進める。"
        "彼は計算機の部屋で、古いデータの符号を整理している。"
    )},
    {"id": "ja-punctuation", "source_language": "ja", "source": (
        "「本当ですか」と彼は言った。『秘密や社会――その光と影』（2015年刊）は、"
        "彼の代表作であり…多くの読者（特に若い世代）に愛されている。これは事実である。"
    )},
    {"id": "en-prose", "source_language": "en", "source": (
        "The author distinguishes a description of social institutions from a moral judgment. "
        "A change in terminology alone does not establish that the argument has changed. "
        "The reader must examine the reasons given in the following paragraph."
    )},
]


def check_reply(source: str, reply: str, *, preserve_punctuation: bool) -> dict:
    try:
        validate_translation(source, reply, preserve_punctuation=preserve_punctuation)
    except ValueError as exc:
        return {"passed": False, "error": str(exc)}
    return {"passed": True, "error": None}


def collect_samples(client: LocalClient, samples: list[dict]) -> list[dict]:
    """Capture HTTP bodies before stripping/normalization; never record headers."""
    original_post = client._post
    calls: list[dict] = []

    def capture(endpoint, payload):
        response = original_post(endpoint, payload)
        calls.append({"endpoint": endpoint, "request": deepcopy(payload),
                      "response": deepcopy(response)})
        return response

    client._post = capture
    records = []
    try:
        for sample in samples:
            calls = []
            translator = ChatTranslator(client)
            record = {**sample, "calls": calls, "human_acceptance": "pending"}
            try:
                record["pipeline_output"] = translator.translate(
                    sample["source"], source_language=sample["source_language"],
                    target_language="简体中文",
                )
                record["pipeline_error"] = None
            except Exception as exc:  # Preserve rejected attempts for inspection.
                record["pipeline_output"] = None
                record["pipeline_error"] = str(exc)
            record["telemetry"] = translator.last_telemetry
            for call in calls:
                prompt = call["request"]["messages"][-1]["content"]
                match = re.search(r"<source>\n(.*?)\n</source>", prompt, re.DOTALL)
                source = match.group(1) if match else sample["source"]
                raw = call["response"]["choices"][0]["message"]["content"]
                call["source"] = source
                call["raw_translation"] = raw
                call["sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                call["quote_sequence"] = "".join(c for c in raw if c in QUOTES)
                call["source_punctuation"] = list(punctuation_signature(source))
                call["translation_punctuation"] = list(punctuation_signature(raw))
                call["strict_punctuation_gate"] = check_reply(source, raw, preserve_punctuation=True)
                call["local_gate"] = check_reply(source, raw, preserve_punctuation=False)
            records.append(record)
    finally:
        client._post = original_post
    return records


def fenced(text: str) -> str:
    fence = "`" * max(3, 1 + max((len(m) for m in re.findall(r"`+", text)), default=0))
    return f"{fence}text\n{text}\n{fence}\n"


def write_evidence(directory: Path, report: dict) -> None:
    (directory / "review.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    lines = [
        "# Hy-MT2 实时翻译证据\n",
        f"采集时间：{report['generated_at']}。人工验收：**待用户判断**。\n",
        f"模型：`{report['identity']['model']}`；提示词版本：`{report['identity']['prompt_version']}`。\n",
        "以下为小样本诊断材料，源文来自测试样例，英文段落为本次编写的测试文本。"
        "原始响应未经改译；流水线产物可能经过繁简转换。机器检查仅观察格式和语言残留，"
        "不证明含义、术语、风格或整书质量。完整请求、响应、重试与哈希见 [review.json](review.json)。\n",
    ]
    for record in report["samples"]:
        case_dir = directory / record["id"]
        case_dir.mkdir()
        (case_dir / "source.txt").write_text(record["source"], encoding="utf-8")
        lines.extend([f"## {record['id']}\n", "原文：\n", fenced(record["source"])])
        for index, call in enumerate(record["calls"], 1):
            (case_dir / f"raw-{index}.txt").write_text(call["raw_translation"], encoding="utf-8")
            lines.extend([
                f"第 {index} 次原始响应：\n", fenced(call["raw_translation"]),
                f"严格标点门：`{call['strict_punctuation_gate']}`。\n",
                f"当前本地门：`{call['local_gate']}`。\n",
            ])
        if record["pipeline_output"] is not None:
            (case_dir / "pipeline-output.txt").write_text(record["pipeline_output"], encoding="utf-8")
            lines.extend(["流水线最终产物：\n", fenced(record["pipeline_output"])])
        lines.append(f"流水线错误：`{record['pipeline_error']}`。人工验收：待用户判断。\n")
    (directory / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "pipeline.toml")
    parser.add_argument("--output-dir", type=Path)
    options = parser.parse_args()
    now = datetime.now(timezone(timedelta(hours=8)))
    directory = options.output_dir or ROOT / "outputs/local-llm-review" / now.strftime("%Y%m%d-%H%M%S")
    directory.mkdir(parents=True, exist_ok=False)
    profile = load_pipeline_profiles(options.config).for_stage("translation")
    if profile is None or profile.provider != "local":
        raise ValueError("Evidence collection requires a local profile; no cloud calls are made.")
    client = build_translation_client(build_parser().parse_args(["sample.pdf"]),
                                      glm_api_base="unused", profile=profile)
    if not isinstance(client, LocalClient):
        raise ValueError("Expected LocalClient.")
    if client.api_base != "http://127.0.0.1:8010/v1":
        raise ValueError("This collector requires the local loopback vLLM endpoint.")
    identity = asdict(client.model_identity(target_language="简体中文", prompt_version=TRANSLATION_PROMPT_VERSION))
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    report = {
        "generated_at": now.isoformat(), "human_acceptance": "pending", "identity": identity,
        "git_commit": commit.stdout.strip(),
        "source_hashes": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                          for name in ("ingestion/book_pipeline.py", "translation/paragraph_translation.py")},
        "config_sha256": hashlib.sha256(options.config.read_bytes()).hexdigest(),
        "samples": collect_samples(client, SAMPLES),
    }
    write_evidence(directory, report)
    failures = sum(record["pipeline_error"] is not None for record in report["samples"])
    print(f"Evidence: {directory.resolve()}; pipeline failures: {failures}; human acceptance: pending")
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
