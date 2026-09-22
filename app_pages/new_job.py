from __future__ import annotations

from pathlib import Path
from dataclasses import replace

from paddle_native import NativeOptions, readiness

import streamlit as st

from app_pages._shared import (
    application_service,
    build_spec,
    credential_inputs,
    load_profiles_for_ui,
    profile_default_index,
    profile_label,
    profiles_for_stage,
)
from frontend_runtime import validate_upload_size
from product_paths import default_source_placeholder, recipe_paths


st.header("创建翻译任务")
st.caption("每个任务使用独立 UUID 工作区；重复运行会沿用 Graph 检查点。")

service = application_service()
config_path, profiles, profile_error = load_profiles_for_ui()
if profile_error:
    st.error(f"模型配置不可用：{profile_error}", icon=":material/error:")
    st.stop()
assert profiles is not None

ocr_profiles = profiles_for_stage(profiles, "ocr")
text_profiles = profiles_for_stage(profiles, "translation")

recipes = list(recipe_paths())
recipe_options: list[Path | None] = [None, *recipes]

with st.container(border=True):
    st.subheader("来源")
    source_mode_label = st.segmented_control(
        "文档入口",
        ["扫描 PDF", "文字 PDF", "EPUB"],
        default="文字 PDF",
        key="new_source_mode",
    )
    source_mode = {
        "扫描 PDF": "scanned-pdf",
        "文字 PDF": "text-pdf",
        "EPUB": "epub",
    }[source_mode_label or "文字 PDF"]
    source_origin = st.segmented_control(
        "文件来源",
        ["上传", "工作区路径"],
        default="上传",
        key="new_source_origin",
    )
    uploaded = None
    source_path = ""
    if source_origin == "工作区路径":
        source_path = st.text_input(
            "工作区内文件路径",
            placeholder=(
                str(default_source_placeholder(source_mode))
            ),
            help="路径必须位于设置页列出的允许根目录内。",
        )
    else:
        uploaded = st.file_uploader(
            "上传原始文档",
            type=["epub"] if source_mode == "epub" else ["pdf"],
            help=(
                f"上限 {service.settings.upload_limit_bytes / 1024 / 1024:.0f} MB；"
                "文件将保存到任务专属目录。"
            ),
        )

ocr_only = False
ocr_mode = "auto"
native_variant = "mobile"
reading_direction = "horizontal"
start_page, end_page = 1, 0
if source_mode == "scanned-pdf":
    with st.container(border=True):
        st.subheader("文字识别")
        scope = st.segmented_control("任务范围", ["翻译出版", "仅 OCR"], default="翻译出版", key="new_task_scope")
        ocr_only = scope == "仅 OCR"
        ocr_modes = {"自动选择": "auto", "本机 PaddleOCR（CPU）": "paddleocr-native",
                     "Docker PaddleOCR（GPU）": "paddleocr-local", "使用模型 Profile": "profile"}
        mode_label = st.selectbox("OCR 运行方式", list(ocr_modes), key="new_ocr_mode")
        ocr_mode = ocr_modes[mode_label]
        if ocr_mode in {"auto", "paddleocr-native"}:
            native_variant = st.selectbox("本机 OCR 模型", ["mobile", "server"],
                format_func=lambda value: "轻量版（推荐）" if value == "mobile" else "高精度版（内存占用较高）",
                key="new_native_variant")
            native_ready, native_detail = readiness(NativeOptions(variant=native_variant))
            if native_ready:
                st.success(native_detail)
            elif ocr_mode == "paddleocr-native":
                st.warning(native_detail)
            else:
                st.caption(native_detail)
            st.caption("自动选择顺序：Docker GPU → 本机 CPU → 配置中的 OCR 服务。仅 OCR 不调用翻译或目录模型。")
        reading_direction = st.selectbox("原文阅读方向", ["horizontal", "vertical"],
            format_func=lambda value: "横排（从左到右）" if value == "horizontal" else "竖排（从右到左）")
        with st.container(horizontal=True):
            start_page = st.number_input("起始 PDF 页", min_value=1, value=1, key="new_start_page")
            end_page = st.number_input("结束 PDF 页（0 表示全部）", min_value=0, value=0, key="new_end_page")
        if reading_direction == "vertical":
            st.caption("按列排序；复杂竖排、跨页和脚注仍需人工复核。")

if not text_profiles and not ocr_only:
    st.error("翻译出版需要至少一个文本模型 Profile。", icon=":material/error:")
    st.stop()

with st.form("new_translation_job"):
    if ocr_only:
        st.info("生成逐页 OCR 文本与检查点，可在任务状态页查看和下载。")
        selected_formats = []
        translate = verify = False
        title = st.text_input("任务名称", placeholder="默认使用源文件名")
        author = ""
    else:
        with st.container(border=True):
            st.subheader("出版目标")
            format_options = (
                ["EPUB", "Word"]
                if source_mode == "epub"
                else ["EPUB", "Word", "知识库", "参考 PDF"]
            )
            selected_formats = st.pills(
                "输出格式",
                format_options,
                default=format_options,
                selection_mode="multi",
                key=f"new_formats_{source_mode}",
            )
            translate = st.toggle("翻译为简体中文", value=True)
            verify = st.toggle(
                "执行发布质量门",
                value=source_mode != "epub",
                disabled=source_mode == "epub",
                key=f"new_verify_{source_mode}",
                help="只有 release report 明确通过的文件才会显示为正式产物。",
            )
            if source_mode == "epub":
                st.warning(
                    "EPUB 原生发布验证尚未接入。任务可以完成语义翻译和 EPUB/Word 草稿，"
                    "但不会标记为正式发布产物。",
                    icon=":material/warning:",
                )
            title = st.text_input("书名", placeholder="默认使用源文件名")
            author = st.text_input("作者", placeholder="可选")

    with st.expander("模型与 Graph 设置", icon=":material/tune:"):
        st.caption(f"Profile：{config_path}")
        ocr_profile = None
        if source_mode == "scanned-pdf" and ocr_mode in {"auto", "profile"}:
            if not ocr_profiles:
                st.error("扫描 PDF 需要 OCR Profile。")
            else:
                ocr_profile = st.selectbox(
                    "OCR Profile",
                    ocr_profiles,
                    index=profile_default_index(ocr_profiles, profiles.ocr_profile),
                    format_func=profile_label,
                )
        toc_profile = None
        if source_mode != "epub" and not ocr_only:
            toc_profile = st.selectbox(
                "目录 Profile",
                text_profiles,
                index=profile_default_index(text_profiles, profiles.toc_profile),
                format_func=profile_label,
            )
        proofread_profile = None
        if source_mode == "scanned-pdf" and not ocr_only:
            proofread_profile = st.selectbox(
                "校勘 Profile",
                text_profiles,
                index=profile_default_index(
                    text_profiles,
                    profiles.proofread_profile or profiles.translation_profile,
                ),
                format_func=profile_label,
            )
        translation_profile = None
        if not ocr_only:
            translation_profile = st.selectbox(
                "翻译 Profile",
                text_profiles,
                index=profile_default_index(
                    text_profiles, profiles.translation_profile
                ),
                format_func=profile_label,
            )
        recipe = st.selectbox(
            "Graph Recipe",
            recipe_options,
            format_func=lambda value: "自动" if value is None else value.stem,
            help="Recipe 只定义拓扑；模型和密钥仍来自 Profile。",
            disabled=ocr_only,
        )
        include_proofread = st.toggle(
            "OCR 后执行校勘",
            value=False,
            disabled=source_mode != "scanned-pdf" or ocr_only,
        )
        toc_source = st.segmented_control(
            "目录来源",
            ["pipeline", "outline"],
            default="pipeline",
        )
        text_pdf_reflow = st.toggle(
            "重排文字 PDF 的视觉换行",
            value=True,
            disabled=source_mode != "text-pdf",
        )
        concurrency = st.number_input(
            "翻译并发",
            min_value=1,
            max_value=64,
            value=int(translation_profile.concurrency) if translation_profile else 1,
            disabled=ocr_only,
        )
        force_all = st.checkbox("忽略 Graph 缓存重新执行", value=False)

    selected_ocr_backend = ocr_mode
    if ocr_mode == "profile":
        selected_ocr_backend = ocr_profile.adapter if ocr_profile else "auto"
    effective_ocr_backend = selected_ocr_backend
    if source_mode == "scanned-pdf" and selected_ocr_backend == "auto":
        from book_pipeline import build_parser, resolve_ocr_backend_name
        probe_args = build_parser().parse_args([])
        probe_args.paddle_native_variant = native_variant
        effective_ocr_backend, _ = resolve_ocr_backend_name(probe_args, ocr_profile)
    if source_mode == "scanned-pdf":
        st.caption(f"本次 OCR 后端：{effective_ocr_backend}")
    credential_names = {
        toc_profile.credential_env if toc_profile is not None else None,
        translation_profile.credential_env if translate else None,
        ocr_profile.credential_env if ocr_profile is not None and effective_ocr_backend in {"coding-plan-mcp", "glm-ocr"} else None,
        (
            proofread_profile.credential_env
            if include_proofread and not ocr_only and proofread_profile is not None
            else None
        ),
    }
    with st.expander("本次任务凭据", icon=":material/key:"):
        st.caption("本次 OCR 无需 API 密钥。" if ocr_only and effective_ocr_backend in {"paddleocr-native", "paddleocr-local", "tesseract"} else "密钥不会进入 run-spec、SQLite、日志或命令行。")
        credentials = credential_inputs(
            credential_names,
            key_prefix="new_job_credential",
        )

    with st.container(horizontal=True, horizontal_alignment="right"):
        preview_clicked = st.form_submit_button(
            "预览计划", icon=":material/account_tree:"
        )
        submit_clicked = st.form_submit_button(
            "创建并运行",
            type="primary",
            icon=":material/play_arrow:",
        )

format_targets = {
    "EPUB": "publication.epub",
    "Word": "publication.docx",
    "知识库": "publication.knowledge_base",
    "参考 PDF": "publication.reference_pdf",
}
direct_targets = tuple(format_targets[name] for name in (selected_formats or ()))
if source_mode != "epub" and verify:
    targets = (
        ("publication.word_report",)
        if set(direct_targets) == {"publication.docx"}
        else ("publication.report",)
    )
else:
    targets = direct_targets
options = {
    "start_page": int(start_page),
    "end_page": int(end_page) or None,
    "ocr_profile": ocr_profile.name if ocr_profile is not None else None,
    "ocr_backend": selected_ocr_backend if source_mode == "scanned-pdf" else None,
    "paddle_native_variant": native_variant if ocr_mode in {"auto", "paddleocr-native"} else None,
    "ocr_reading_direction": reading_direction,
    "toc_profile": toc_profile.name if toc_profile is not None else None,
    "proofread_profile": (
        proofread_profile.name if proofread_profile is not None else None
    ),
    "translation_profile": translation_profile.name if translation_profile else None,
    "translation_concurrency": int(concurrency),
    "include_proofread": bool(include_proofread) and not ocr_only,
    "toc_source": toc_source or "pipeline",
    "text_pdf_reflow": bool(text_pdf_reflow) and source_mode == "text-pdf",
    "force_all": bool(force_all),
    "generate_epub": "publication.epub" in direct_targets,
    "generate_docx": "publication.docx" in direct_targets,
    "generate_knowledge_base": "publication.knowledge_base" in direct_targets,
    "generate_reference_pdf": "publication.reference_pdf" in direct_targets,
}
source_value = source_path.strip() if source_origin == "工作区路径" else None
spec = build_spec(
    source=source_value,
    source_mode=source_mode,
    config=config_path,
    recipe=None if ocr_only else recipe,
    title=title,
    author=author,
    targets=targets,
    translate=translate,
    verify=verify,
    options=options,
)

if ocr_only:
    spec = replace(spec, phase="ocr", targets=("pages.raw",), translate=False, verify=False)

if preview_clicked or submit_clicked:
    if not direct_targets and not ocr_only:
        st.error("至少选择一个出版格式。", icon=":material/error:")
    elif (
        source_mode != "epub"
        and verify
        and not ocr_only
        and set(direct_targets)
        not in (
            {"publication.docx"},
            {
                "publication.epub",
                "publication.docx",
                "publication.knowledge_base",
                "publication.reference_pdf",
            },
        )
    ):
        st.error(
            "当前发布门支持“仅 Word”或四种完整产物。若只需部分格式，请关闭发布质量门，"
            "它们将作为草稿生成。",
            icon=":material/error:",
        )
    elif source_origin == "上传" and uploaded is None:
        st.error("请选择要上传的文件。", icon=":material/error:")
    elif source_origin == "工作区路径" and not source_path.strip():
        st.error("请输入工作区内的文件路径。", icon=":material/error:")
    else:
        try:
            if preview_clicked:
                plan = (
                    service.preview_upload_plan(spec, filename=uploaded.name)
                    if uploaded is not None
                    else service.preview_plan(spec)
                )
                st.session_state["new_job_plan"] = plan
            else:
                if uploaded is not None:
                    validate_upload_size(
                        int(uploaded.size),
                        limit=service.settings.upload_limit_bytes,
                    )
                job = (
                    service.submit_upload(
                        spec,
                        filename=uploaded.name,
                        content=uploaded.getbuffer(),
                        credentials=credentials,
                    )
                    if uploaded is not None
                    else service.submit_path(spec, credentials=credentials)
                )
                st.session_state["selected_job_id"] = job.id
                st.success(
                    f"任务 {job.id[:8]} 已进入后台运行。",
                    icon=":material/check_circle:",
                )
        except (OSError, RuntimeError, ValueError) as exc:
            st.error(str(exc), icon=":material/error:")

plan_value = st.session_state.get("new_job_plan")
if plan_value:
    with st.container(border=True):
        st.subheader("DAG 计划")
        for index, node in enumerate(plan_value, start=1):
            st.code(f"{index:02d}  {node}", language="text")
