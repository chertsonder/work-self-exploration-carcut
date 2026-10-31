# 批量自动化剪辑系统 · 设计文档

> 目标：把「LibTV 生成 6 段真人出镜视频 → 剪映拼接/识别字幕/校对/贴瑕疵图」的全流程自动化，
> 字幕零错字、音频错误机器检出并定点 TTS 修复、瑕疵图按台词自动定时叠加，单条与批量均可跑。

## 1. 核心思路

剪映流程的根因：**文案是已知的，却让 ASR 去"猜"字幕**。本方案反转这一步：

- 字幕文字直接取自文案原文（零错字），ASR 只负责提供**时间轴**；
- 同一份 ASR 结果反向与文案逐字比对，得到**音频错误报告**；
- 错误句用 TTS 合成修正音频，在**原句精确时间位置**定点替换（窗口贴合真实静音边界，
  段内总时间轴不变）——口型靠补丁对准原句起音/收音保持自然；
- 第五段瑕疵台词通过对齐拿到精确时间，按文件名匹配瑕疵图定时叠加。

**时间戳三层来源**（可靠性从低到高）：
云端 Paraformer 字级时间戳（按 token 均分的虚构值，只用于文本 diff）
→ 本地 faster-whisper 词级时间戳（真实声学边界，对齐主数据）
→ ffmpeg silencedetect 静音边界（补丁窗口/字幕边界的最终裁定）。

## 2. 总体流程

```
input/{车源ID}/
  videos/1.mp4 ~ 6.mp4 + 文案.txt + 瑕疵图/*.jpg
        │
        ▼
 ① probe   ffprobe 体检：分辨率/帧率/编码/时长 → 以第1段为基准统一参数
        │
        ▼
 ② asr     云端 DashScope Paraformer 逐段转写（内容校验+指数退避重试），
            结果缓存（带源文件指纹，素材/配置变化自动失效）；
            本地 faster-whisper 提供真实词级时间戳 + 云端故障兜底
        │
        ▼
 ③ align   文案按标点切分成字幕单元 → 归一化 → 与 ASR 字符流做单调模糊对齐
        │        └→ 每个字幕单元的精确起止时间（文字 = 文案原文）
        ▼
 ④ audit   ASR 与文案逐字 diff → 错读/漏读/多读/同音异字（精确到秒）
        │
        ▼
 ⑤ tts     错误句合成修正音频（克隆参考音可选）→ 时长拟合(atempo/补静音)
 ⑥ patch   → 在原句时间窗内定点替换，总时长不变
        │
        ▼
 ⑦ flaws   解析瑕疵图名(部位+瑕疵) → 同义词匹配第5段台词 → 得到叠加时间窗
        │
        ▼
 ⑧ render  每段独立渲染：烧录硬字幕(ASS) + 叠加瑕疵图(淡入淡出) + 替换后音频
        │
        ▼
 ⑨ concat  无损拼接 6 段 → output/{命名标题}/标题[-序号].mp4 + 字幕.srt/.ass + report.md
```

分段处理、最后拼接：某段音频重新生成后，只需重跑那一段，其余段结果可复用（ASR 有缓存）。

## 3. 目录结构

```
tarecut/
├── DESIGN.md / config.yaml / requirements.txt / main.py
├── batch_prep.py             # 9.20 素材库 + CSV → 批量组装标准车源目录
├── stage_input.py            # LibTV 原始导出目录 → 标准结构
├── make_scripts.py           # 从 CSV 生成文案.txt（早期工具）
├── check_asr.py / probe_asr.py / probe_files.py   # ASR 串台/服务端诊断工具
├── tarecut/                  # 核心包
│   ├── utils.py              # ffmpeg 查找、ffprobe、时间格式化、json 读写
│   ├── normalize.py          # 文本归一化（全半角/数字转汉字/去标点）
│   ├── probe.py              # 视频参数体检与统一基准
│   ├── asr.py                # 转写封装（云端/本地）+ 内容校验 + 缓存指纹失效
│   ├── asr_dashscope.py      # DashScope Paraformer 适配（公网直链/OSS 双通道）
│   ├── align.py              # 字幕切分 + 字符级单调对齐（核心）
│   ├── audit.py              # 音频体检（diff → 错误分类报告）
│   ├── tts.py                # TTS 引擎（CosyVoice 克隆 / edge-tts / command）
│   ├── patch.py              # 音频定点替换（静音边界 + 时长拟合 + 溢出预算）
│   ├── flaws.py              # 瑕疵图解析、同义词匹配、时间窗
│   ├── subtitles.py          # SRT / ASS 生成
│   ├── render.py             # 单段渲染（字幕烧录+图片叠加）与拼接
│   └── pipeline.py           # 车源级全流程编排
├── tests/
│   ├── make_fixture.py       # 生成合成样例车源（自测用）
│   └── test_align.py         # 对齐/审计逻辑单元自测
├── tools/ffmpeg/bin/         # 便携版 ffmpeg（自动下载）
├── input/{车源ID}/           # ← 素材放这里（videos/ 文案.txt 瑕疵图/ 命名.txt）
└── output/{命名标题}/        # ← 成片/字幕/报告/中间产物
```

## 4. 输入数据格式

### 4.1 素材目录（每车源）

```
input/{车源ID}/
├── videos/           # 6 段视频，文件名含序号即可（1.mp4…6.mp4 / xxx_1.mp4 均可，按数字排序）
├── 文案.txt          # 6 段文案，用「# 第N段」分隔
└── 瑕疵图/           # 文件名 = 部位 + 瑕疵，如「左前轮毂剐蹭.jpg」「后保险杠凹陷.png」
```

### 4.2 文案.txt

```
# 第1段
大家好，今天给大家带来……
# 第2段
……
```

兼容写法：`第1段`、`【第1段】`、`# 第1段` 均可识别；
若无任何标记但恰好 6 个空行分隔的段落，也可按顺序解析（报告中会提示）。

### 4.3 瑕疵图命名

`部位 + 瑕疵`，同义词表见 `config.yaml → flaws.synonyms`（可随时扩充）。
例如：`左前轮毂剐蹭.jpg` → 部位组「轮毂」+ 瑕疵组「剐蹭」→ 匹配第 5 段中同时含两类词的台词。
多张图命中同一句时，按句内时长均分先后展示。

## 5. 关键算法

### 5.1 字幕切分
按 `。！？；…` 切句；句有效字数超过 `max_line_chars` 再按 `，、：` 等二次切分；
每单元记录其在归一化字符流中的区间，用于时间映射。

### 5.2 文本归一化（对齐/审计前置）
NFKC 全半角归一 → 大写 → 阿拉伯数字转汉字（`9.8万`→`九点八万`，cn2an）→ 去标点空白。
文案与 ASR 两侧同规则处理，消除数字读法差异带来的假差异。

### 5.3 字符级单调对齐
- ASR 词级时间戳展开为字符时间轴（词时长均摊到字符，繁体转简体）；
- `difflib.SequenceMatcher` 对「文案字符流 ↔ ASR 字符流」求 opcodes（单调匹配）；
- equal 块的文案字符直接取得时间；replace/insert/delete 的文案字符在前后锚点间线性插值；
- 由此得到每个字幕单元的起止时间；整句未被匹配 → 标记 missing（整句漏读嫌疑）。

### 5.4 音频体检分类
| opcode | 含义 | 分类 |
|---|---|---|
| replace（等长且拼音相同） | 识别写法差异 | 同音异字（低优先级） |
| replace（其他） | 读错/听错 | 错读 |
| delete | 文案有、音频无 | 漏读 |
| insert | 音频多出 | 多读/口癖 |

每条错误含：所在段、时间(秒)、文案原文、实际读出、类型。`audit.min_len` 可过滤噪声。

### 5.5 TTS 补丁（时长拟合 + 溢出预算）
- 目标窗 = 错误句 `[s, e]`（已贴合真实静音边界）；补丁时长 `p` → 速度比 `x = p / (e-s)`；
- `p` 过长时先让 TTS 原生加速分担（speech_rate ≤1.8），DSP 再变速对齐，
  夹紧区间 `[0.85, 1.50]`；
- 溢出预算 = 下一句真实起音 - 0.06s 保护带（ASR 起点滞后可达 0.37s，不可直接用）；
  间隙不足则禁止溢出，防止吃掉下一句开头（听感=截断）；
- 组装：`前段 + 补丁(对齐后) + 后段`，段内总时长不变；放慢最多 18%，
  超出部分静音填充（宁多留静音也不拖沓）；
- 引擎：`dashscope` CosyVoice（默认，全局克隆音色缓存 25 天）；`edge-tts`（内置、免费）；
  `command`（自定义命令）。TTS 3 次重试，失败自动降级为"仅报告"，不阻断流程。

### 5.6 渲染与拼接
- 每段一次 ffmpeg 完成：`setsar/fps 统一 → overlay 瑕疵图(enable=between) → subtitles 烧录 → aac 音频`
  （**保留各分镜原始分辨率，不做缩放**）；
- 瑕疵图支持 alpha 淡入淡出；多图同句按时长均分；
- 6 段统一编码参数（帧率/编码器/码率策略）后 concat demuxer `-c copy` 无损拼接；
  分辨率按决策保留各自原始值。

## 6. 输出

```
output/{命名标题}/
├── 标题.mp4        # 拼接后成片（硬字幕已烧录、瑕疵图已叠加）
│                   # 标题取自 命名.txt（格式 [ID]-[车型]-[平台]•[商家]-[车源ID]）
│                   # 重跑不覆盖：同名成片自动加序号 -2/-3…
├── 字幕.srt        # 全片时间轴（外挂备用/回剪映微调）
├── 字幕.ass        # 全片时间轴带样式
├── report.md       # 音频体检表 / TTS补丁记录 / 瑕疵图匹配表 / 参数体检 / 警告
└── work/           # 中间产物（ASR缓存、逐段渲染结果等，可删）
```

## 7. 使用方法

```powershell
# 安装（首次）
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
# ffmpeg 已自动下载到 tools\ffmpeg\bin（也可自行放入）

# DashScope API Key 通过环境变量传入（不要明文写进配置）
setx DASHSCOPE_API_KEY "sk-你的Key"    # 设置后需重开终端生效

# 单条车源
.venv\Scripts\python main.py 车源ID

# 批量（input/ 下所有车源，并发 2，跳过已有成片）
.venv\Scripts\python main.py --all --workers 2

# 只出报告不打补丁
.venv\Scripts\python main.py 车源ID --no-patch

# 切换全文配音模式（句级对齐整段重读）
.venv\Scripts\python main.py 车源ID --mode full

# 临时换本地词级时间戳模型（如 small→base；缓存按模型指纹自动失效）
.venv\Scripts\python main.py 车源ID --asr-model base
```

常用配置（config.yaml）：字幕字体/字号/边距、TTS 引擎与音色、瑕疵图同义词表、渲染质量。

## 8. 环境依赖

| 组件 | 说明 |
|---|---|
| ffmpeg / ffprobe | 便携版自动放入 `tools\ffmpeg\bin`；已装全局版则直接用 |
| Python 3.9+ | 3.12 验证 |
| dashscope | 云端 ASR（Paraformer）+ TTS（CosyVoice 声音复刻），需 API Key 与网络 |
| faster-whisper | 本地真实词级时间戳 + 云端故障兜底；CPU 用 int8，模型默认 `small`（首次自动下载约 460MB） |
| edge-tts | 备用在线 TTS（需网络）；模型下载慢可设 `asr.hf_endpoint: https://hf-mirror.com` |
| cn2an / pypinyin / zhconv / PyYAML / requests | 归一化 / 拼音判同音 / 繁转简 / 配置 / HTTP |

本机无独立 GPU → ASR 走 CPU int8，单条 1-2 分钟素材耗时约 1-3 分钟；有 GPU 的机器自动加速。

## 9. 风险与限制

| 风险 | 对策 |
|---|---|
| ASR 识别风格差异造成体检误报 | 同音异字降级；`audit.min_len` 调阈值；报告供人工复核 |
| 数字读法差异 | 归一化规则覆盖小数/整数/万；个别读法（如"百分之"）见报告后补充规则 |
| TTS 音色与真人有差异 | 默认 CosyVoice 全局克隆音色（缓存 25 天，失败回退官方音色）；可换 `edge-tts`/`command` 引擎或回退"报告+LibTV 重生成" |
| 补丁变速超界听感异常 | TTS 原生加速分担 + DSP 夹紧 [0.85,1.50] + 溢出预算保护下一句，报告中提示 ⚠截断 |
| 瑕疵图命名不规范 | report 点名未匹配图片与未被覆盖的瑕疵台词 |
| 字幕样式不合口味 | config 可调字体/字号/位置；`字幕.srt` 可回剪映改 |

## 10. 验收清单（样例车源）

1. `成片.mp4` 6 段顺序正确、衔接无黑帧、参数统一；
2. 硬字幕与文案逐字一致（抽查任意 10 句）；
3. `report.md` 音频体检表能定位人工能听出的问题（精确到秒）；
4. 瑕疵图出现在第 5 段对应台词时段，位置/大小合理；
5. `--no-patch` 与打补丁两种模式均能跑通；`--all` 批量可用。
