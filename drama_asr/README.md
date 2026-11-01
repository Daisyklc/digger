# Drama ASR + Translation

日语广播剧 / Galgame 语音转写 + 翻译服务。专为**情绪化、敏感（NSFW）语音**优化，模型本地离线运行、无内容审查。

- **云端**：部署「模型服务」——语音识别（[`litagin/anime-whisper`](https://huggingface.co/litagin/anime-whisper)，在二次元/游戏配音语料上微调，擅长喘息、娇喘、情绪戏）+ 翻译（[NLLB-200](https://huggingface.co/facebook/nllb-200-distilled-1.3B)，本地开源）。
- **本地**：命令行客户端。你的音频留在本机，上传到云端处理，字幕下载回本地。云端处理完即删临时音频。

```
音频(本地) ──上传──▶ [云端 GPU] anime-whisper ──▶ 日文分段 ──▶ NLLB ja→zh
                                                                   │
   SRT 字幕(本地) ◀──下载── srt_ja / srt_zh / srt_bilingual ◀───────┘
```

## 为什么用 anime-whisper

普通 Whisper 遇到广播剧里的娇喘、喘息、强情绪台词常常输出空白或乱码。`anime-whisper` 专门在包含这类（含 NSFW）语音的数据上微调，转写稳得多，且开源模型本身不做内容审查。

## 显存预算（目标 ~16 GB）

| 组件 | 模型 | float16 显存 |
|---|---|---|
| ASR | anime-whisper (large) | ~3 GB |
| 翻译 | nllb-200-distilled-1.3B | ~5 GB |
| 合计 | | ~8 GB（16 GB 很宽裕）|

显存吃紧时：`COMPUTE_TYPE=int8_float16`，或把翻译换成 `facebook/nllb-200-distilled-600M`。

## 云端部署

### 方式 A：Docker（推荐）

需要宿主机装好 NVIDIA 驱动 + [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)。

```bash
cd drama_asr
cp .env.example .env         # 填入 API_KEY（随机长串），按需改模型
docker compose up -d --build
```

首次启动会自动从 Hugging Face 下载 NLLB。ASR 模型需先转换（见下）。模型持久化在 `./models` 和 `hf-cache` 卷里。

### 方式 B：裸机

```bash
cd drama_asr
pip install torch --index-url https://download.pytorch.org/whl/cu121
pip install -r requirements-server.txt

# 一次性：下载并把 anime-whisper 转成 CTranslate2 格式
python scripts/prepare_model.py --model litagin/anime-whisper \
    --output models/anime-whisper-ct2 --quantization float16

cp .env.example .env && $EDITOR .env     # 至少设置 API_KEY
set -a && source .env && set +a
bash scripts/run_server.sh               # 监听 0.0.0.0:8000
```

> 生产环境建议在前面放 nginx/caddy 做 HTTPS，并务必设置 `API_KEY`。

## 本地客户端

```bash
pip install -r drama_asr/requirements-client.txt

python -m drama_asr.client.drama_cli \
    --server https://你的域名:8000 \
    --api-key "$DRAMA_API_KEY" \
    --out-dir ./out \
    --formats ja,zh,bilingual \
    episode01.wav episode02.flac
```

每个输入会生成 `名字.ja.srt` / `名字.zh.srt` / `名字.bilingual.srt`（`--formats` 可选 `json`）。支持多文件、长音频（异步任务 + 轮询，不受 HTTP 超时影响）。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| POST | `/api/jobs` | 上传音频（multipart `file`），返回 `job_id` |
| GET | `/api/jobs/{id}` | 查询状态/进度（`queued`/`running`/`done`/`error`）|
| GET | `/api/jobs/{id}/result` | 取结果（`segments` + `srt_*`）|

设了 `API_KEY` 时所有接口需 `Authorization: Bearer <token>`。单后台 worker 顺序处理，保证显存占用可控。

## 配置项

见 `.env.example`，关键项：`ASR_MODEL`、`COMPUTE_TYPE`、`ENABLE_TRANSLATION`、`NLLB_MODEL`、`TARGET_LANG`（默认 `zho_Hans` 简体，繁体用 `zho_Hant`）、`API_KEY`、`DELETE_AUDIO_AFTER`。

## 本地无 GPU 测试

```bash
pip install fastapi "uvicorn[standard]" python-multipart httpx requests pytest
python -m pytest drama_asr/tests -q            # 单元 + API 测试

# mock 模式起服务（不加载任何模型）
MOCK=1 API_KEY=dev uvicorn drama_asr.server.app:app --port 8123
```

`MOCK=1` 用桩替换 ASR/翻译，可在没有 GPU、没下模型的机器上跑通完整 HTTP 链路。
