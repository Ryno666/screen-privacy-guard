# Screen Privacy Guard · 屏幕旁观保护

一个在本机运行的 macOS 防旁观工具，包含人脸白名单录入、摄像头检测、状态栏开关、后台自动恢复、本地事件截图和自动锁屏。

**普通摄像头只能粗略估计注视方向，人脸识别也可能误判。本项目不是身份认证系统，也没有经过系统性的多人场景准确率评测。**

## 功能

- **白名单录入**：录入本人约 20 份人脸特征，只保存本地特征向量，不保存录入照片。当前版本登记一位使用者，重新录入会覆盖旧资料。
- **自适应频率**：只有本人或无人时每秒检查 1 次，出现非白名单人脸后提高到每秒 5 次，离开 2 秒后降频。
- **连续注视计时**：每个人单独计时，转头、丢失检测或画面中断后清零；短眨眼暂停计时，不把眨眼时间加进去。
- **本地截图**：判定为非白名单的人疑似连续注视超过 5 秒时，每次事件保存一张脸部局部图及 JSON 记录。与本人较相似、身份不确定时不保存。
- **自动锁屏**：疑似连续注视达到 15 秒时锁屏；锁屏后释放摄像头，解锁后等待 10 秒恢复。
- **常驻后台**：登录自启、摄像头断流重连、异常退出重启及心跳检查。人为停止会关闭自启并结束残留进程。
- **原生 UI**：Mac 控制面板和顶部状态栏文字入口，可启动、停止、查看截图和运行记录。打开界面不会自动开启摄像头。

## 环境

- Apple Silicon Mac；已在 macOS 26.5.2 / M2 Pro 上验证，其他机器和系统版本需要自行实测。
- Python 3.11–3.13，推荐 **Python 3.12**。固定依赖不以 Python 3.14 为目标。
- Xcode Command Line Tools，提供 `swiftc`、`codesign` 和 `xcrun`。
- 可用摄像头；首次下载 Python 依赖和公开模型需要联网。

如缺少编译工具，先运行 `xcode-select --install`。安装脚本不修改摄像头系统权限；摄像头授权由用户在 macOS 弹窗中完成。

## 从零安装

### 1. 获取代码并准备环境

```bash
git clone https://github.com/Ryno666/screen-privacy-guard.git
cd screen-privacy-guard
chmod +x ./*.sh ./*.command
./setup.sh
```

`setup.sh` 创建项目虚拟环境、安装固定版本依赖并下载模型；模型经过固定 SHA-256 校验。模型权重不存放在 Git 仓库中。

### 2. 录入本人的白名单人脸

```bash
./.venv/bin/python guard.py enroll
```

也可双击 `1-录入我的人脸.command`。请让画面中只有本人，点击预览窗口后按空格，倒数 3 秒后采集约 7 秒；看向屏幕并轻微转头，完成后自动退出。

资料保存在项目的 `.private/owner.npz`。**每次录入会替换旧资料，当前不提供同时管理多个人的白名单界面。**

### 3. 先预览，再安装图形控制面板

```bash
./.venv/bin/python guard.py watch
# 确认本人稳定显示为 YOU 后，按 Q / Esc 退出
./.venv/bin/python install.py
```

`install.py` 准备独立后台运行环境并编译图形界面。**安装不会启动监测，也不会开启登录自启。** 更新已有安装时，先从菜单退出控制 App，关闭窗口只会隐藏界面。

### 4. 从 UI 开启

打开 `~/Applications/屏幕旁观保护.app`，或安装后生成的 `旁观保护控制面板.app`，点击“启动监测”。首次开启需允许 **屏幕旁观保护 / Screen Privacy Guard** 使用摄像头。没看到弹窗时，可打开生成的 `8-摄像头授权.app`。

- 状态栏白色文字显示“已关闭 / 保护中 / 待授权 / 重连中”等状态，点击可展开菜单。
- 控制面板绿色表示收到正常监测心跳；橙色表示授权、重连或恢复中，不当作正常监测显示。
- “启动监测”同时启用登录自启；“停止监测”停止后台并关闭自启。
- 关闭窗口仅隐藏界面；退出控制 App 不改变后台开关。可以把 App 固定到 Dock。

## 文件和数据位置

| 内容 | 默认位置 |
| --- | --- |
| 项目白名单特征 | `.private/owner.npz` |
| 独立后台运行环境 | `~/Library/Application Support/ScreenPrivacyGuard/` |
| 后台白名单副本 | `~/Library/Application Support/ScreenPrivacyGuard/.private/owner.npz` |
| 事件截图 | `~/Pictures/ScreenPrivacyGuard/YYYY-MM-DD/` |
| 后台配置 | `~/Library/LaunchAgents/com.local.screen-privacy-guard.plist` |
| 后台日志 | `~/Library/Application Support/ScreenPrivacyGuard/logs/service.log` |
| UI App | `~/Applications/屏幕旁观保护.app` |

截图和元数据权限为 `600`，数据目录为 `700`。数据不上传、不自动删除；文件权限限制不是数据加密。重新录入后，安装或启动入口会同步最新白名单到后台环境。

本仓库不包含真实人脸特征、截图、日志、个人运行记录、已编译 App、虚拟环境或模型权重。

## 命令行与双击入口

```bash
# 模型和锁屏接口检查：不开摄像头、不锁屏
./.venv/bin/python guard.py doctor

# 前台预览，不锁屏、不保存照片
./.venv/bin/python guard.py watch

# 单人模拟旁观者：只检查朝向和计时，不锁屏、不保存照片
./.venv/bin/python guard.py watch --simulate-self

# 独立后台服务
./.venv/bin/python service.py start
./.venv/bin/python service.py status
./.venv/bin/python service.py stop

# 停止后台后，可单独在前台测试实际截图与锁屏
./.venv/bin/python guard.py watch --lock --save-snapshots

# 手动锁屏测试：5 秒后真的锁屏，Ctrl+C 取消
./.venv/bin/python guard.py test-lock

# 调整摄像头和检查频率
./.venv/bin/python guard.py watch --camera 1 --idle-fps 1 --active-fps 5
```

双击入口：`0` 准备环境，`1` 录入本人，`2` 预览，`3` 启动后台，`4` 单人模拟，`5` 停止后台，`6` 截图目录，`7` 后台状态，`9` 安装控制面板。`8` 授权 App 和控制面板快捷入口在安装时生成。

后台运行时不要另开录入或预览；程序通过实例锁防止重复占用摄像头。

## 持续运行的边界

摄像头断流后每 5 秒重试，异常退出由 `launchd` 恢复；工作进程心跳超过 90 秒未更新时进行恢复。睡眠、关机和注销期间不能监测，摄像头权限关闭或硬件不可用时也不能完成检测。

降低检查频率主要减少识别计算，摄像头采集流仍然开启，**没有测量实际节电比例**。侧脸、遮挡、远处小脸、背光、外接屏幕位置等都会影响结果。

锁屏通过 macOS 私有接口 `SACLockScreenImmediate` 完成；系统升级后可能需要适配。未使用关闭显示器代替锁屏，也不修改登录密码。

## 开发与验证

```bash
./.venv/bin/python -m unittest discover -s tests -v
./.venv/bin/python build_control_ui.py
./scripts/check_public_tree.py
```

Python 检查覆盖计时、身份切换、截图条件/权限、断流恢复和进程清理。Swift 自检覆盖状态、完整窗口构建和浅/深色白字渲染；构建不启动监测。自动检查不能替代目标 Mac 上的真实多人测试，状态栏最终颜色也需要实机核对。

代码结构见 [架构说明](docs/ARCHITECTURE.md)，验证范围见 [测试说明](docs/TESTING.md)。

## 依赖与许可

项目源码暂未指定许可证。依赖与模型遵循各自上游许可；仓库提供下载和校验脚本，不重新分发模型权重。

- [OpenCV 人脸检测与识别](https://docs.opencv.org/4.10.0/d0/dd4/tutorial_dnn_face.html)：YuNet / SFace。
- [OpenCV Zoo](https://github.com/opencv/opencv_zoo)：ONNX 模型来源。
- [MediaPipe Face Landmarker](https://ai.google.dev/edge/mediapipe/solutions/vision/face_landmarker/python)：面部关键点和头部姿态。
- [Apple launchd](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)：后台登录任务。
- [Apple 摄像头授权](https://developer.apple.com/documentation/bundleresources/requesting-authorization-for-media-capture-on-macos)。
