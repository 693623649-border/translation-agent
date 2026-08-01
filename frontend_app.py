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
    "② 识别目录": "toc",
    "③ 翻译非中文页面": "translate",
    "④ 编译章节 Markdown": "compile",
    "生成 EPUB": "epub",
    "生成 Word": "docx",
    "只查看状态": "status",
}
PDF_REQUIRED_PHASES = {"all", "ocr", "toc", "compile"}
TEXT_ADAPTERS = {"openai-chat", "glm-chat"}
OCR_ADAPTERS = {"coding-plan-mcp", "glm-ocr", "tesseract"}
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


def render_status(status: dict) -> None:
    if not status:
        st.info("还没有可读取的检查点。")
        return
    columns = st.columns(4)
    columns[0].metric("OCR 页面", status.get("pages", 0))
    columns[1].metric(
        "当前模型译文",
        status.get("translations_profile_fresh")
        if status.get("translations_profile_fresh") is not None
        else "—",
    )
    columns[2].metric("目录", "已完成" if status.get("toc_ready") else "待处理")
    columns[3].metric(
        "章节 Markdown",
        "已完成" if status.get("chapters_ready") else "待处理",
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
    st.caption("影印 PDF → 逐页 OCR → 目录 → 章节 Markdown → EPUB / Word / 知识库")

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
        translation_options = stage_profiles(profiles, "translation")
        if not ocr_options or not toc_options or not translation_options:
            st.error("配置中至少需要一个 OCR Profile 和一个文本模型 Profile。")
            st.stop()

        ocr_profile = st.selectbox(
            "OCR 模型",
            ocr_options,
            index=selected_index(ocr_options, profiles.ocr_profile),
            format_func=profile_label,
        )
        toc_profile = st.selectbox(
            "目录模型",
            toc_options,
            index=selected_index(toc_options, profiles.toc_profile),
            format_func=profile_label,
        )
        translation_profile = st.selectbox(
            "翻译模型",
            translation_options,
            index=selected_index(
                translation_options,
                profiles.translation_profile,
            ),
            format_func=profile_label,
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
            ["chapter", "section", "subsection", "all"],
            index=0,
        )

    with right:
        st.subheader("2. 凭据与输出")
        st.caption("密钥只进入本次任务的子进程内存，不写入 TOML、argv 或输出文件。")
        active_profiles: list[tuple[str, ModelProfile]] = []
        if phase in {"all", "ocr"}:
            active_profiles.append(("OCR", ocr_profile))
        if phase in {"all", "toc"}:
            active_profiles.append(("目录", toc_profile))
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

    with st.expander("高级设置"):
        worker_col, translation_worker_col, range_col = st.columns(3)
        ocr_workers = worker_col.number_input(
            "OCR workers",
            min_value=1,
            max_value=128,
            value=ocr_profile.concurrency,
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
        direction = st.selectbox(
            "OCR 阅读方向",
            ["horizontal", "vertical"],
            format_func=lambda value: "横排" if value == "horizontal" else "日文竖排",
        )
        force = st.checkbox("强制重跑已有检查点", value=False)
        keep_images = st.checkbox("保留逐页渲染图片", value=False)

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
            translation_profile=translation_profile.name,
            title=title.strip() or None,
            start_page=start_page,
            end_page=end_page,
            translate_non_chinese=translate_enabled or phase == "translate",
            source_language=source_language,
            target_language="简体中文",
            ocr_concurrency=int(ocr_workers),
            translation_concurrency=int(translation_workers),
            granularity=granularity,
            toc_pages=toc_pages.strip() or None,
            page_offset=int(page_offset_value) if page_offset_value.strip() else None,
            front_matter_pages=int(front_matter_pages),
            ocr_reading_direction=direction,
            keep_page_images=keep_images,
            force=force,
            generate_epub=generate_epub,
            generate_docx=generate_docx,
            generate_knowledge_base=generate_kb,
            generate_bookmarked_pdf=generate_pdf,
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

        if phase in PDF_REQUIRED_PHASES and input_pdf is None:
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
