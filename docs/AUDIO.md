# 人声处理

入口在“声音与高级设置”。录像人声、BGM 人声独立设置，默认都保留。

## 选择方式

| 选项 | 适用情况 | 实际处理 |
| --- | --- | --- |
| 录像：保留人声 | 希望保留聊天、搞笑片段 | 使用“原片音轨”所选轨道，保留全部内容 |
| 录像：使用独立游戏音轨 | OBS 预先把游戏声与麦克风、语音聊天分轨 | 必须手动选择游戏轨，只提取该轨；不会自动识别哪条是游戏声 |
| 录像：AI 减弱人声 | 游戏声与语音混在一起 | 对入选片段运行人声分离，再减去预测的人声 |
| BGM：保留歌曲原声 | 希望保留演唱 | 使用原始音乐片段 |
| BGM：AI 提取伴奏 | 希望歌曲人声减弱 | 单独对成片使用的 BGM 段落运行同一模型 |

只有一条录像音轨时，无法通过选轨拆开其中的语音。如果这条音轨原本就只有游戏声，使用“保留人声”即可。

AI 使用音乐人声模型，不理解谁是玩家、谁是队友。它可能影响枪声、环境声、角色语音或乐器，也可能留下部分说话声。游戏效果需要用真实录像试听确认。两个开关只作用于各自音源，不会处理后来加入的效果音。

## 本机安装与运行

本次已安装模型并验证推理。新电脑完成基础环境安装后，再运行：

```powershell
.\.venv\Scripts\python.exe scripts\setup_audio.py
```

脚本使用已安装的 NumPy 和 OpenCV，无额外 Python 运行库；下载约 63.67 MiB 的固定模型，校验 SHA-256，并进行一次真实推理自检。模型放在 `.tools/audio`，下载缓存放在 `work/downloads`。不修改系统运行库、驱动或系统 PATH。安装完成后重新启动工具。

渲染期间不会自动下载或上传素材。AI 音频目前固定在 **CPU** 上运行；界面的 NVIDIA 选项只控制视频编码。此次办公电脑上，6 秒语音与节拍混合样本的人声处理约 27 秒；这不是长片或 RTX 5080 的速度测试。

## 处理顺序

1. 扫描录像、分析音乐节奏、确定入选镜头。
2. 按镜头时间与所选音轨提取原声；按音乐起点与实际成片时长提取 BGM。
3. 分别运行所需的 AI 分离，保持采样长度不变。
4. 渲染视频镜头，加入处理后的原声、BGM 和效果音，再统一调整音量、限幅、导出。

提取音频使用 44.1 kHz 双声道 PCM16，最终成片仍为 48 kHz AAC。视频中间片段不再反复编码声音。音乐节奏分析与混音都使用 BGM 的第一条音轨。

## 缓存、取消和故障

- AI 在独立子进程运行，可从任务面板取消；取消不发布缓存，不会继续导出成片。
- 缺模型、校验失败、推理失败或输出时长错误时明确报错，沿用统一日志与任务编号。
- 成功结果缓存在 `work/audio-cache`。缓存键包含输入音频内容、模型 SHA-256 和推理版本；改变音量不需要重新分离。
- 一个 120 秒、双声道的缓存 WAV 约 20.2 MiB；录像与 BGM 都处理时约 40.4 MiB。缓存不会自动清理，可在没有任务运行时手动清理该目录。
- `manifest.json` 的 `settings` 保存用户选择，`audio_processing` 保存实际模式、音轨、模型、CPU、缓存是否命中和处理耗时。

## 代码结构与模型来源

- `videocut/audio.py`：选轨验证、片段提取、AI 子进程、缓存和最终混音。
- `videocut/separator.py`：NumPy 频谱变换、OpenCV DNN 推理、预测人声相减。
- `videocut/audio_model.py`：固定模型位置、哈希、参数和就绪状态。
- `videocut/pipeline.py`：安排各阶段，写入成片清单。

模型为 UVR 公开仓库的 **Kim_Vocal_2.onnx**，主输出为 Vocals。来源：

- [UVR 模型发布仓库](https://github.com/TRvlvr/model_repo/releases/tag/all_public_uvr_models)
- [UVR 参数表](https://github.com/Anjok07/ultimatevocalremovergui/blob/master/models/MDX_Net_Models/model_data/model_data.json)
- [UVR MDX 网络结构](https://github.com/Anjok07/ultimatevocalremovergui/blob/master/lib_v5/mdxnet.py)
- [audio-separator 的 MDX 接口参考](https://github.com/nomadkaraoke/python-audio-separator/blob/main/audio_separator/separator/architectures/mdx_separator.py)

本次从发布仓库通过 HTTPS 获取后固定 SHA-256 为 `ce74ef3b6a6024ce44211a07be9cf8bc6d87728cc852a68ab34eb8e58cde9c8b`。该 SHA-256 是本次下载计算并固定的值，不是发布者额外提供的签名。UVR 的末尾 10,240,000 字节 MD5 为 `970b3f9492014d18fefeedfe4773cb42`，对应参数：FFT 7680、频率维 3072、时间维 256、hop 1024、补偿 1.009、采样率 44100。

模型权重属于其发布者，本项目仅封装本地推理流程。

## 验证命令

```powershell
$env:VIDEOCUT_INTEGRATION='1'
$env:VIDEOCUT_AI_INTEGRATION='1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --test tests/test_frontend.cjs
```

AI 集成测试需要先安装模型；普通单元测试不要求联网或下载模型。公开语音混合测试使用 librosa 提供的 LibriSpeech SLR12 片段，由 Garth Comira 朗读，来源为 `https://librosa.org/data/audio/5703-47212-0000.ogg`，随附说明标注 CC BY 4.0。测试只取片段并叠加自制节拍；不代表三角洲聊天、枪声或歌曲演唱的处理质量。
