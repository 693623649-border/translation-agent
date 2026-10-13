"""端到端冒烟：用 pipeline.toml 的 local_hymt2 profile 走真实装配路径翻译一段日文。

前置：deploy/local-llm 的 vLLM 服务已启动（curl http://127.0.0.1:8010/v1/models 可访问）。
运行：python deploy/local-llm/smoke_translate.py
判定：client 类型为 LocalClient、base_url 指向 127.0.0.1:8010、译文为中文且保留『』「」引号序列。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from book_pipeline import (  # noqa: E402
    ChatTranslator,
    build_parser,
    build_translation_client,
)
from pipeline_profiles import load_pipeline_profiles  # noqa: E402


def main() -> int:
    profiles = load_pipeline_profiles(Path(__file__).resolve().parents[2] / "pipeline.toml")
    profile = profiles.for_stage("translation")
    assert profile is not None
    print(f"[smoke] profile={profile.name} provider={profile.provider} "
          f"base_url={profile.base_url} model={profile.model}")

    args = build_parser().parse_args(["sample.pdf"])
    client = build_translation_client(args, glm_api_base="unused", profile=profile)
    assert client is not None, "translation client was not built"
    print(f"[smoke] client={type(client).__name__} api_base={client.api_base}")
    assert type(client).__name__ == "LocalClient", "expected LocalClient for local profile"
    assert "127.0.0.1:8010" in client.api_base, "expected local vLLM endpoint"

    translator = ChatTranslator(client)
    source = (
        "『世界の終りとハードボイルド・ワンダーランド』は村上春樹の長編小説である。"
        "「壁」という概念が作品の中心にあり、読者は二つの物語を行き来しながら読み進める。"
        "彼は計算機の部屋で、古いデータの符号を整理している。"
    )
    output = translator.translate(source, source_language="ja", target_language="简体中文")
    print("[smoke] source:", source)
    print("[smoke] output:", output)

    expected_quotes = "".join(c for c in source if c in "「」『』〈〉")
    actual_quotes = "".join(c for c in output if c in "「」『』〈〉")
    status = "PASS" if (
        output.strip() and output != source and expected_quotes == actual_quotes
        and any("\u4e00" <= ch <= "\u9fff" for ch in output)
    ) else "FAIL"
    print(f"[smoke] quotes expected={expected_quotes!r} actual={actual_quotes!r}")
    print(f"[smoke] result: {status}")
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
