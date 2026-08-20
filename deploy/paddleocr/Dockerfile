# PaddleOCR GPU 推理镜像（适配 RTX 5090 / Blackwell sm_120）
# 基础镜像为 PaddlePaddle 官方 CUDA 12.9 构建（sm_120 需 >=3.2.0 的 cuda12.9 版本）
# 默认 pip 源用百度镜像，容器构建网络在国内更稳定
ARG BASE_IMAGE=ccr-2vdh3abv-pub.cnc.bj.baidubce.com/paddlepaddle/paddle:3.3.0-gpu-cuda12.9-cudnn9.9
FROM ${BASE_IMAGE}

ARG PADDLEOCR_VERSION=3.7.0
ARG PIP_INDEX_URL=https://mirror.baidu.com/pypi/simple

RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" "paddleocr==${PADDLEOCR_VERSION}"

WORKDIR /workspace

COPY download_models.py verify_ocr.py models.manifest.json /opt/paddleocr-tools/

# PaddleX/PaddleOCR 运行期自动下载缓存也固定到挂载卷内，避免写进镜像层
ENV PADDLE_PDX_CACHE_HOME=/workspace/models/cache \
    TOOLS_DIR=/opt/paddleocr-tools

# 默认常驻，供 docker exec / compose run 进入使用
CMD ["sleep", "infinity"]
