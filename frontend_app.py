from __future__ import annotations

import os
import re
from pathlib import Path

import streamlit as st

from frontend_service import (
    PROJECT_ROOT,
    PipelineJob,
    artifact_mime_type,
    discover_artifacts,
    run_pipeline_job,
    safe_uploaded_pdf_name,
)
from pipeline_profiles import ModelProfile, PipelineProfiles, load_pipeline_profiles
from translation_agent_api import RunRequest, status_for_request


PHASES = {
    "一键全流程": "all",
    "① OCR 逐页识别": "ocr",
    "② 校勘日文 OCR": "proofread",
    "③ 识别目录": "toc",
    "④ 翻译非中文页面": "translate",
    "⑤ 编译章节 Markdown": "compile",
    "生成 EPUB": "epub",
    "生成 Word": "docx",
    "发布质量验收": "verify",
    "只查看状态": "status",
}
PDF_REQUIRED_PHASES = {"all", "ocr", "toc", "compile"}
TEXT_ADAPTERS = {"openai-chat", "glm-chat"}
OCR_ADAPTERS = {
    "coding-plan-mcp",
    "glm-ocr",
    "tesseract",
    "paddleocr-local",
}
PROGRESS_PATTERN = re.compile(r"(?:completed|cached)=(\d+)/(\d+)")


def profile_label(profile: ModelProfile) -> str:
    return (
        f"{profile.name} · {profile.provider}/{profile.model} "
        f"· {profile.concurrency} workers"
    )


def stage_profiles(
    profiles: PipelineProfiles,
    stage: str,
) -> list[ModelProfile]:
    adapters = OCR_ADAPTERS if stage == "ocr" else TEXT_ADAPTERS
    return [
        profile
        for profile in profiles.profiles.values()
        if profile.adapter in adapters
    ]


def selected_index(options: list[ModelProfile], default_name: str) -> int:
    for index, profile in enumerate(options):
        if profile.name == default_name:
            return index
    return 0


def profile_selectbox(
    label: str,
    options: list[ModelProfile],
    default_name: str,
) -> ModelProfile:
    """Select by stable name so Streamlit never deep-copies frozen profiles."""

    by_name = {profile.name: profile for profile in options}
    selected = st.selectbox(
        label,
        list(by_name),
        index=selected_index(options, default_name),
        format_func=lambda name: profile_label(by_name[name]),
    )
    return by_name[selected]


def render_status(status: dict) -> None:
    if not status:
        st.info("还没有可读取的检查点。")
        return
    columns = st.columns(6)
    fresh_ocr = status.get("ocr_pages_profile_fresh")
    ocr_semantic_stale = status.get("ocr_semantic_stale") is True
    columns[0].metric(
        "当前模型 OCR",
        (
            "设置已变更"
            if ocr_semantic_stale
            else
            f"{fresh_ocr}/{status.get('pages', 0)}"
            if fresh_ocr is not None
            else status.get("pages", 0)
        ),
    )
    proofread_semantic_stale = status.get("proofread_semantic_stale") is True
    columns[1].metric(
        "当前模型校勘",
        "设置已变更"
        if proofread_semantic_stale
        else status.get("proofread_pages_profile_fresh")
        if status.get("proofread_pages_profile_fresh") is not None
        else "—",
    )
    translation_semantic_stale = status.get("translation_semantic_stale") is True
    columns[2].metric(
        "当前模型译文",
        "设置已变更"
        if translation_semantic_stale
        else status.get("translations_profile_fresh")
        if status.get("translations_profile_fresh") is not None
        else "—",
    )
    columns[3].metric("目录", "已完成" if status.get("toc_ready") else "待处理")
    columns[4].metric(
        "章节 Markdown",
        "已完成" if status.get("chapters_ready") else "待处理",
    )
    verification_status = status.get("verification_status")
    columns[5].metric(
        "发布验收",
        (
            "需重新验收"
            if status.get("verification_stale")
            else "已通过"
            if verification_status == "passed"
            else (
                "部分验收"
                if verification_status == "partial"
                else ("未通过" if verification_status == "failed" else "待处理")
            )
        ),
    )
    stale_stages = [
        label
        for label, stale in (
            ("OCR", ocr_semantic_stale),
            ("校勘", proofread_semantic_stale),
            ("翻译", translation_semantic_stale),
        )
        if stale
    ]
    if stale_stages:
        st.warning(
            "以下阶段的内容设置已变化，继续相应阶段时会按页重做："
            + "、".join(stale_stages)
        )
    with st.expander("查看详细状态"):
        st.json(status)


def main() -> None:
    st.set_page_config(
        page_title="影印书转换台",
        page_icon="📚",
        layout="wide",
    )
    st.title("📚 影印书转换台")
    st.caption("影印 PDF → 逐页 OCR → 可选校勘 → 目录 → 章节 Markdown → EPUB / Word / 知识库")

    default_config = (
        PROJECT_ROOT / "pipeline.toml"
        if (PROJECT_ROOT / "pipeline.toml").exists()
        else PROJECT_ROOT / "pipeline.example.toml"
    )
    with st.sidebar:
        st.header("模型配置")
        config_value = st.text_input("Profile 配置文件", value=str(default_config))
        config_path = Path(config_value).expanduser()
        if not config_path.is_absolute():
            config_path = PROJECT_ROOT / config_path
        try:
            profiles = load_pipeline_profiles(config_path)
        except (OSError, ValueError) as exc:
            st.error(f"无法读取 Profile：{exc}")
            st.stop()

        ocr_options = stage_profiles(profiles, "ocr")
        toc_options = stage_profiles(profiles, "toc")
        proofread_options = stage_profiles(profiles, "proofread")
        translation_options = stage_profiles(profiles, "translation")
        if not ocr_options or not toc_options or not translation_options:
            st.error("配置中至少需要一个 OCR Profile 和一个文本模型 Profile。")
            st.stop()

        ocr_profile = profile_selectbox(
            "OCR 模型",
            ocr_options,
            profiles.ocr_profile,
        )
        toc_profile = profile_selectbox(
            "目录模型",
            toc_options,
            profiles.toc_profile,
        )
        proofread_profile = profile_selectbox(
            "OCR 校勘模型",
            proofread_options,
            profiles.proofread_profile or profiles.translation_profile,
        )
        translation_profile = profile_selectbox(
            "翻译模型",
            translation_options,
            profiles.translation_profile,
        )

    left, right = st.columns([1.15, 0.85], gap="large")
    with left:
        st.subheader("1. 选择任务")
        source_mode = st.radio(
            "PDF 来源",
            ["上传 PDF", "服务器路径"],
            horizontal=True,
        )
        uploaded = None
        pdf_path_value = ""
        if source_mode == "上传 PDF":
            uploaded = st.file_uploader(
                "拖入影印版 PDF",
                type=["pdf"],
                help="文件在点击运行后保存到 work/ui_uploads。",
            )
        else:
            pdf_path_value = st.text_input(
                "PDF 绝对路径",
                placeholder="/path/to/book.pdf",
            )

        phase_label = st.selectbox("运行阶段", list(PHASES))
        phase = PHASES[phase_label]
        output_value = st.text_input(
            "工作/输出目录",
            value=str(PROJECT_ROOT / "outputs" / "ui_book"),
            help="同一目录重复运行会自动续传。",
        )
        title = st.text_input("书名（可选）", placeholder="默认使用 PDF 文件名")
        author = st.text_input("作者（可选）", placeholder="用于 Word 扉页与文档属性")

        translate_enabled = st.toggle(
            "将非中文 OCR 翻译为中文",
            value=phase in {"all", "translate"},
            disabled=phase == "translate",
        )
        language_col, granularity_col = st.columns(2)
        source_language = language_col.selectbox(
            "原文语言",
            ["auto", "ja", "en", "fr", "de", "ru"],
            index=0,
        )
        granularity = granularity_col.selectbox(
            "Markdown 粒度",
            [None, "chapter", "section", "subsection", "all"],
            index=0,
            format_func=lambda value: (
                "自动（保持已有设置）" if value is None else value
            ),
        )

    with right:
        st.subheader("2. 凭据与输出")
        st.caption("密钥只进入本次任务的子进程内存，不写入 TOML、argv 或输出文件。")
        active_profiles: list[tuple[str, ModelProfile]] = []
        if phase in {"all", "ocr"}:
            active_profiles.append(("OCR", ocr_profile))
        if phase in {"all", "toc"}:
            active_profiles.append(("目录", toc_profile))
        if phase == "proofread":
            active_profiles.append(("OCR 校勘", proofread_profile))
        if translate_enabled or phase == "translate":
            active_profiles.append(("翻译", translation_profile))

        credential_roles: dict[str, list[str]] = {}
        for role, profile in active_profiles:
            if profile.credential_env:
                credential_roles.setdefault(profile.credential_env, []).append(role)
        credentials: dict[str, str] = {}
        if not credential_roles:
            st.success("当前阶段不需要模型 API Key。")
        for env_name, roles in credential_roles.items():
            present = bool(os.getenv(env_name))
            value = st.text_input(
                f"{'/'.join(roles)} API Key · {env_name}",
                type="password",
                placeholder="已从环境读取，可留空" if present else "仅保存在当前会话内存",
                key=f"credential_{env_name}",
            )
            if value:
                credentials[env_name] = value
            elif present:
                st.caption(f"✓ {env_name} 已在启动环境中配置")

        st.markdown("输出格式")
        artifact_cols = st.columns(2)
        generate_epub = artifact_cols[0].checkbox("EPUB", value=True)
        generate_docx = artifact_cols[1].checkbox("Word", value=True)
        generate_kb = artifact_cols[0].checkbox("AI 知识库 JSONL", value=True)
        generate_pdf = artifact_cols[1].checkbox("带书签 PDF", value=True)
        verify_publication = st.checkbox(
            "编译后自动执行发布质量验收",
            value=True,
            help="无模型调用；验证全页检查点、目录覆盖、章节/引注、全部文字容器及 PDF 外观与文字层。",
        )

    with st.expander("高级设置"):
        worker_col, proofread_worker_col, translation_worker_col, range_col = st.columns(4)
        ocr_workers = worker_col.number_input(
            "OCR workers",
            min_value=1,
            max_value=128,
            value=ocr_profile.concurrency,
        )
        proofread_workers = proofread_worker_col.number_input(
            "校勘 workers",
            min_value=1,
            max_value=128,
            value=proofread_profile.concurrency,
        )
        translation_workers = translation_worker_col.number_input(
            "翻译 workers",
            min_value=1,
            max_value=128,
            value=translation_profile.concurrency,
        )
        page_range = range_col.text_input(
            "PDF 页范围",
            placeholder="例如 1-50；留空为全书",
        )
        toc_col, offset_col, front_col = st.columns(3)
        toc_pages = toc_col.text_input("已确认目录页", placeholder="例如 6-10,12")
        page_offset_value = offset_col.text_input("页码偏移", placeholder="自动检测")
        front_matter_pages = front_col.number_input(
            "目录候选前置页数",
            min_value=1,
            max_value=200,
            value=40,
        )
        printed_pages_per_pdf_page = st.selectbox(
            "每个 PDF 页包含的书内页数",
            [None, 1, 2],
            format_func=lambda value: "自动检测" if value is None else str(value),
        )
        direction = st.selectbox(
            "OCR 阅读方向",
            ["horizontal", "vertical"],
            index=1 if ocr_profile.reading_direction == "vertical" else 0,
            key=(
                f"ocr_reading_direction_{ocr_profile.name}_"
                f"{ocr_profile.reading_direction or 'auto'}"
            ),
            format_func=lambda value: "横排" if value == "horizontal" else "日文竖排",
        )
        force = st.checkbox("强制重跑已有检查点", value=False)
        keep_images = st.checkbox("保留逐页渲染图片", value=False)
        require_all_reviewed = st.checkbox(
            "要求全部章节均为人工审定稿",
            value=False,
            help="仅用于整本已完成审定时的严格验收。",
        )
        completeness_col, translation_gate_col = st.columns(2)
        require_complete_ocr = completeness_col.checkbox(
            "发布前要求全页 OCR 检查点",
            value=True,
            help="阻止局部页范围或缺页检查点被误编译为完整书籍。",
        )
        require_translation = translation_gate_col.checkbox(
            "发布前要求应译页译文新鲜",
            value=bool(translate_enabled),
            help="翻译任务建议开启；人工审定覆盖章不会重复要求逐页译文。",
        )

    start_page = None
    end_page = None
    if page_range.strip():
        match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", page_range)
        if match:
            start_page = int(match.group(1))
            end_page = int(match.group(2) or match.group(1))

    output_dir = Path(output_value).expanduser()
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir

    def build_request(input_pdf: Path | None) -> RunRequest:
        return RunRequest(
            input_pdf=input_pdf,
            output_dir=output_dir,
            phase=phase,
            config=config_path,
            ocr_profile=ocr_profile.name,
            toc_profile=toc_profile.name,
            proofread_profile=proofread_profile.name,
            translation_profile=translation_profile.name,
            title=title.strip() or None,
            author=author.strip() or None,
            start_page=start_page,
            end_page=end_page,
            translate_non_chinese=translate_enabled or phase == "translate",
            source_language=source_language,
            target_language="简体中文",
            ocr_concurrency=int(ocr_workers),
            proofread_language=(
                source_language if source_language != "auto" else "ja"
            ),
            proofread_concurrency=int(proofread_workers),
            translation_concurrency=int(translation_workers),
            granularity=granularity,
            toc_pages=toc_pages.strip() or None,
            page_offset=int(page_offset_value) if page_offset_value.strip() else None,
            printed_pages_per_pdf_page=printed_pages_per_pdf_page,
            front_matter_pages=int(front_matter_pages),
            ocr_reading_direction=direction,
            keep_page_images=keep_images,
            force=force,
            require_complete_ocr=require_complete_ocr,
            require_translation=require_translation,
            generate_epub=generate_epub,
            generate_docx=generate_docx,
            generate_knowledge_base=generate_kb,
            generate_bookmarked_pdf=generate_pdf,
            verify_publication=verify_publication,
            require_all_reviewed=require_all_reviewed,
        )

    st.subheader("3. 运行")
    run_col, refresh_col = st.columns([1, 1])
    run_clicked = run_col.button(
        "▶ 开始 / 继续任务",
        type="primary",
        use_container_width=True,
    )
    refresh_clicked = refresh_col.button(
        "↻ 刷新状态",
        use_container_width=True,
    )

    request_for_status = build_request(None)
    if refresh_clicked:
        st.session_state["last_status"] = status_for_request(request_for_status)

    if run_clicked:
        validation_error = ""
        input_pdf: Path | None = None
        if uploaded is not None:
            upload_dir = PROJECT_ROOT / "work" / "ui_uploads"
            upload_dir.mkdir(parents=True, exist_ok=True)
            input_pdf = upload_dir / safe_uploaded_pdf_name(uploaded.name)
            input_pdf.write_bytes(uploaded.getvalue())
        elif pdf_path_value.strip():
            input_pdf = Path(pdf_path_value).expanduser().resolve()

        if (
            phase in PDF_REQUIRED_PHASES
            or (phase == "verify" and generate_pdf)
        ) and input_pdf is None:
            validation_error = f"“{phase_label}”需要选择 PDF。"
        elif input_pdf is not None and (
            not input_pdf.exists() or input_pdf.suffix.lower() != ".pdf"
        ):
            validation_error = f"PDF 不存在或格式不正确：{input_pdf}"
        elif page_range.strip() and start_page is None:
            validation_error = "页范围格式应为 1-50 或单页 12。"
        elif page_offset_value.strip() and not re.fullmatch(
            r"-?\d+",
            page_offset_value.strip(),
        ):
            validation_error = "页码偏移必须是整数。"

        missing_credentials = [
            env_name
            for env_name in credential_roles
            if not credentials.get(env_name) and not os.getenv(env_name)
        ]
        if missing_credentials and not validation_error:
            validation_error = (
                "缺少 API Key：" + "、".join(missing_credentials)
            )

        if validation_error:
            st.error(validation_error)
        else:
            request = build_request(input_pdf)
            logs: list[str] = []
            progress = st.progress(0.0, text="正在启动…")
            log_box = st.empty()

            def on_output(line: str) -> None:
                logs.append(line)
                match = PROGRESS_PATTERN.search(line)
                if match and int(match.group(2)):
                    progress.progress(
                        min(1.0, int(match.group(1)) / int(match.group(2))),
                        text=line[:160],
                    )
                log_box.code("\n".join(logs[-160:]), language="text")

            with st.spinner("任务运行中；可安全续传，请勿关闭当前页面。"):
                result = run_pipeline_job(
                    PipelineJob(request=request, credentials=credentials),
                    on_output=on_output,
                )
            st.session_state["last_status"] = result.status
            if result.ok:
                progress.progress(1.0, text="任务完成")
                st.success("任务完成。")
            else:
                st.error(f"任务退出，代码 {result.exit_code}。检查日志后可直接重试。")

    status = st.session_state.get("last_status")
    if status is None and output_dir.exists():
        status = status_for_request(request_for_status)
    st.subheader("检查点状态")
    render_status(status or {})

    artifacts = discover_artifacts(output_dir)
    if artifacts:
        st.subheader("下载产物")
        for artifact in artifacts:
            size = artifact.stat().st_size
            label = f"{artifact.name} · {size / 1024 / 1024:.1f} MB"
            if size <= 64 * 1024 * 1024:
                st.download_button(
                    label=f"下载 {label}",
                    data=artifact.read_bytes(),
                    file_name=artifact.name,
                    mime=artifact_mime_type(artifact),
                    key=f"download_{artifact.name}",
                )
            else:
                st.info(f"{label} 较大，请直接从服务器路径下载：{artifact}")


if __name__ == "__main__":
    main()
