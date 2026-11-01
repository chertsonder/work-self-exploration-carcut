# carcut · 车辆瑕疵短视频自动化剪辑管线

把「6 段真人出镜视频 + 文案 + 瑕疵图」自动剪辑成一条带**零错字硬字幕**、**音频错误机器检出并定点 TTS 修复**、**瑕疵图按台词定时叠加**的成片。

> 核心思路：文案是已知的，别让 ASR 去"猜"字幕——字幕文字直接取自文案原文，ASR 只负责提供时间轴。

## 特性

- **字幕零错字**：字幕文字 = 文案原文，云端 Paraformer + 本地 faster-whisper 双重时间戳 + ffmpeg 静音边界三层对齐
- **音频体检**：ASR 结果与文案逐字 diff，机器检出错读/漏读/多读/同音异字，精确到秒
- **定点 TTS 补丁**：错误句用 CosyVoice 克隆音色合成修正音频，在原句时间窗内定点替换，段内总时长不变、口型自然；失败自动降级为"仅报告"
- **瑕疵图智能叠加**：按文件名（部位+瑕疵）同义词匹配第 5 段台词，自动定时淡入淡出
- **分段处理、结果缓存**：某段音频重做后只需重跑该段；ASR 带源文件指纹缓存
- **单条 / 批量**：`input/` 下多车源并发处理，跳过已有成片

## 处理流程

```
probe（视频参数体检）
 → asr（云端转写 + 本地词级时间戳，带缓存）
 → align（文案切字幕单元，字符级单调对齐取时间轴）
 → audit（ASR×文案 diff → 音频错误报告）
 → tts + patch（错误句合成 → 时长拟合 → 定点替换）
 → flaws（瑕疵图名解析 → 同义词匹配台词时间窗）
 → render（逐段烧录字幕/叠加图片/替换音频）
 → concat（无损拼接 → 成片 + 字幕 + 报告）
```

## 快速开始

### 环境要求

| 组件 | 说明 |
|---|---|
| Python 3.9+ | 3.12 验证 |
| ffmpeg / ffprobe | 将便携版放入 `tools/ffmpeg/bin/`，或安装后加入 PATH（代码自动查找） |
| DashScope API Key | 云端 ASR（Paraformer）+ TTS（CosyVoice 声音复刻） |
| 网络 | faster-whisper 模型首次自动下载（约 460MB，慢可设 `asr.hf_endpoint: https://hf-mirror.com`） |

### 安装

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### 配置 API Key

通过环境变量传入，不要明文写进配置：

```powershell
setx DASHSCOPE_API_KEY "sk-你的Key"   # 设置后需重开终端生效
```

### 准备素材

```
input/{车源ID}/
├── videos/      # 6 段视频，文件名含序号即可（1.mp4…6.mp4 / xxx_1.mp4 均可）
├── 文案.txt      # 6 段文案，用「# 第N段」分隔
├── 瑕疵图/       # 文件名 = 部位 + 瑕疵，如「左前轮毂剐蹭.jpg」
└── 命名.txt      # 输出标题：[ID]-[车型]-[平台]•[商家]-[车源ID]
```

### 运行

```powershell
# 单条车源
.venv\Scripts\python main.py 728

# 批量（input/ 下所有车源，并发 2，跳过已有成片）
.venv\Scripts\python main.py --all --workers 2

# 只出体检报告，不做 TTS 补丁
.venv\Scripts\python main.py 728 --no-patch

# 全文配音模式（句级对齐整段重读，替代定点补丁）
.venv\Scripts\python main.py 728 --mode full

# 临时切换本地时间戳模型（缓存按模型指纹自动失效）
.venv\Scripts\python main.py 728 --asr-model base
```

### 输出

```
output/{命名标题}/
├── 标题.mp4      # 拼接成片（硬字幕已烧录、瑕疵图已叠加；重跑不覆盖，自动加 -2/-3）
├── 字幕.srt      # 外挂字幕（可回剪映微调）
├── 字幕.ass      # 带样式字幕
├── report.md     # 音频体检表 / TTS 补丁记录 / 瑕疵图匹配表 / 参数体检
└── work/         # 中间产物（可删）
```

## 配置

`config.yaml` 常用项：

- `subtitle`：字体/字号/描边/底部边距/单行字数
- `tts`：引擎（`dashscope` CosyVoice 克隆 / `edge-tts` / 自定义 `command`）、音色、变速上限
- `tts.pronunciation` / `force_patch_words`：读音纠错表、强制补丁词
- `flaws.synonyms` / `defects`：瑕疵部位与类型同义词表（可随时扩充）
- `audit.min_len`：音频体检噪声过滤阈值
- `render`：CRF/帧率策略/片头掐头/BGM 音量
- `asr`：后端切换（`dashscope` 云端 / `local` 本地 whisper）、模型选择

## 辅助脚本

| 脚本 | 用途 |
|---|---|
| `stage_input.py` | LibTV 原始导出目录 → 标准车源结构 |
| `batch_prep.py` | 9.20 素材库 + CSV → 批量组装标准车源目录 |
| `make_scripts.py` | 从 CSV 生成文案.txt（早期工具） |
| `check_asr.py` / `probe_asr.py` | ASR 串台排查 / 服务端诊断 |

## 测试

```powershell
# 生成合成样例车源（自测用）
.venv\Scripts\python tests\make_fixture.py

# 对齐/审计逻辑单元自测
.venv\Scripts\python -m pytest tests\test_align.py -v
```

## 目录结构

```
tarecut/
├── main.py                   # CLI 入口（单条/批量）
├── config.yaml               # 全部可调配置
├── tarecut/                  # 核心包：align/audit/tts/patch/flaws/render 等
├── tests/                    # 合成样例生成器 + 单元自测
├── tools/ffmpeg/bin/         # ffmpeg 便携版（需自行放入，见环境要求）
├── input/{车源ID}/           # 素材
└── output/{命名标题}/        # 成片/字幕/报告
```

## 文档

- 完整设计文档（算法细节、三层时间戳、TTS 补丁策略、风险与限制）：[DESIGN.md](DESIGN.md)
