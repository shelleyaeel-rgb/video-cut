# 烽火剪辑 Windows 桌面版

## 使用

1. 将 `FenghuoClip-0.2.0-windows-x64.zip` 完整解压到你能写入的目录，例如 `D:\Apps`。
2. 双击其中的 `FenghuoClip\FenghuoClip.exe`。不要单独移动 EXE，也不要把个人文件放到 `FenghuoClip` 程序目录里。
3. 默认以独立窗口打开。Windows WebView2 不可用时改用系统浏览器；可手动加 `--browser` 强制使用浏览器。
4. 选素材生成混剪，或先体验合成演示。任务运行期间不能退出或安装更新；先等待完成，或点击取消并等待任务停止。
5. 使用“退出应用”关闭服务。桌面窗口关闭按钮同样会检查正在运行的任务。

支持 Windows x64；包内包含 Python 3.12、NumPy/OpenCV、FFmpeg/ffprobe 和固定的人声模型。NVIDIA 编码依赖目标电脑的驱动和显卡，仍需在游戏电脑实测。包未做 Windows 商业代码签名。

## 数据与备份

默认数据根目录为 `%LOCALAPPDATA%\FenghuoClip`。页面会显示实际位置。

| 目录/文件 | 内容 |
| --- | --- |
| `data/profiles` | 自己标定的游戏提示模板 |
| `data/preferences.json` | 常用时长、音量、特效、阈值、编码和人声选项 |
| `outputs` | 已导出成片和清单 |
| `work` | 日志、演示素材和任务/音频缓存 |
| `updates` | 下载临时文件与上次安装结果 |

更新只替换 `FenghuoClip.exe` 和 `_internal`，不会搬动上述数据。备份时退出程序后复制数据根目录即可。已有源码版数据仍保留在原项目：可在两个版本均退出时，将需要的 `data/profiles` 和 `outputs` 复制到新数据目录；同名配置先备份再决定保留哪个。复制整个 `.venv` 无法替代应用包。

需要换数据盘时，可在启动前设置 `VIDEOCUT_DATA_DIR` 为专用目录，并把已有数据复制过去。该目录必须在程序目录之外，否则自动更新会拒绝替换。每个数据目录只允许一个桌面进程，避免同时处理或更新相同文件。

## 检查与安装更新

点击“检查更新”读取固定仓库 `shelleyaeel-rgb/video-cut` 的最新正式 GitHub Release。源码运行模式可以检查版本，但只能免安装版安装更新。

发现新版后点击下载。程序核验发布清单中的文件名、版本、大小与 SHA-256，再验证 ZIP 的路径和目录结构。准备完成后，等待任务结束，再点击“重启安装”。独立更新程序等待旧进程退出，将旧版备份、换入新版并重新启动；新版未在规定时间内就绪时尝试恢复旧版。更新结果在页面可见，诊断日志留在程序目录旁的 `.FenghuoClip-update-*` 目录。安装器失败或文件正被占用时，勿手工删除其中的 `previous`，它可能保存待恢复的原程序。

SHA-256 用于完整性校验，更新信任来源是固定 GitHub 仓库和 HTTPS；它不等于独立的发行者代码签名。安装目录中混入额外文件会阻止更新，请将素材和个人文件保存在数据目录或别处。

尚无已公开的正式 Release、只有草稿、无访问权限、网络受限、版本号没有增大，均不会安装新包。程序不默认联网检查，也不在生成视频时下载依赖。

### 私有仓库

私有仓库使用本机环境变量 `VIDEOCUT_GITHUB_TOKEN` 配置 GitHub 细粒度只读令牌，权限仅需对应仓库的 Contents: Read。通过 Windows 的环境变量设置在使用应用的账户下配置，然后重新启动应用。不需要把令牌发送到聊天、写入源码、提交到仓库或放进发布包。

没有令牌时按公开仓库方式检查。认证只发往固定仓库的 GitHub API，下载跳转到资源主机时移除认证头；没有任何令牌输入框或可任意指定更新服务器的网页接口。未配置认证也可手动下载有权限访问的 ZIP，退出应用后解压新版；数据目录继续复用。

## 从源码构建

使用 64 位 Windows 和 Python 3.12，在项目虚拟环境中执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe scripts\build_windows.py
```

构建脚本校验固定 FFmpeg 和模型资源，收集第三方许可，运行 PyInstaller，执行冻结版自检，再生成：

- `dist/FenghuoClip/`：可运行目录。
- `dist/FenghuoClip-<版本>-windows-x64.zip`：完整分发包。
- `dist/update-windows.json`：版本、包名、大小和 SHA-256。
- `dist/self-test.json`：冻结程序自检记录。

自检在临时目录生成合成视频和混剪并全片解码，不读取用户录像。可加测冻结后的 AI 子进程：

```powershell
Start-Process -Wait .\dist\FenghuoClip\FenghuoClip.exe -ArgumentList '--self-test-ai','--result','D:\Apps\fenghuo-test.json'
```

自检不是干净 Windows 安装、实际游戏识别效果、RTX 编码性能或实际互联网升级的验证。

## 仓库与版本发布

`.github/workflows/release.yml` 支持手动构建及 `v*` 标签。标签必须和 `videocut/__init__.py` 中的版本号一致，例如 `v0.2.0`。

1. 提交并推送源码，递增版本号后创建对应标签。
2. 工作流在 Windows 上测试、打包、自检，并上传构建产物；标签构建另外准备一个草稿 Release。
3. 核对包和测试结果后发布 Release。客户端只识别正式发布、非预发行的版本；草稿不会触发更新。
4. 后续新版必须使用更大的版本号；不要改写已发布版本的 ZIP 或元数据。

发布资产必须同时包含 `FenghuoClip-<版本>-windows-x64.zip` 和 `update-windows.json`，在同一 `v<版本>` Release 下。私有仓库下载需要仓库访问权限。

源码仓库不包含媒体、用户模板、导出文件、虚拟环境或构建产物。打包依赖及来源在 `scripts/windows-dependencies.json`、`requirements-build.txt` 和 `_internal/licenses` 记录。对外公开分发时按 `scripts/THIRD-PARTY-NOTICES.txt` 处理 FFmpeg 等组件的许可和源码提供义务，并核实模型权重的分发条件。
