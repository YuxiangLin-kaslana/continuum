# Continuum 桌面端

Windows 优先的 Electron 应用：一个常驻的小窗口、托盘图标和全局快捷键 `Ctrl+Shift+Space`。

- 第一次打开时用用户名和密码登录（服务器上 `continuum user passwd <名字>` 设置密码）。服务器地址默认是 `https://ai-api.yuxianglin.xyz`，可以在“更换服务器”里改。登录后服务器给这台设备发一个令牌，用系统的加密存储（Windows 上是 DPAPI）保存，界面拿不到；退出时作废。
- **对话**：交代一件事，确认和结果会实时出现；同一个对话里追加的话会交给原来的工单。
- **工单**：等你处理的排最前（等你回答、等你看结果、卡住、失败），可以停止或重试。
- 关掉窗口会留在托盘；窗口不在前台时，新回复和需要你拍板的问题会弹系统通知。

## 开发

```bash
cd desktop
npm ci          # 会下载 Electron
npm start       # 需要图形界面
npm run smoke   # 启动、加载窗口、自动退出：CI 用
npm run dist    # 打 Windows 安装包到 dist/
```

服务器上没有图形界面时，界面用 `tests/test_desktop.py` 在无头浏览器里测（`test/fake-bridge.js` 代替 Electron 的预加载脚本），网络客户端用 `test/client.test.js` 连真实服务器测。Electron 外壳和安装包由 GitHub Actions 在 Windows 上跑（`.github/workflows/ci.yml`），安装包在那次运行的 Artifacts 里。
