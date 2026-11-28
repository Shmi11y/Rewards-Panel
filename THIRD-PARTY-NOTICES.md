# 第三方声明 / Third-Party Notices

本项目在运行时会调用上游开源项目的构建产物，界面设计也从它得到启发。以下逐项说明，避免混淆。

## 一、上游项目

| 项 | 内容 |
|---|---|
| 名称 | Microsoft-Rewards-Script |
| 仓库 | https://github.com/huaisha1224/Microsoft-Rewards |
| 根目录许可 | MIT License |
| npm 包声明 | `GPL-3.0-or-later`（见 `app/package.json`） |
| 版权 | Copyright (c) 2023 Sam.Huang |

> 注意：上游自身存在许可不一致——仓库根 `LICENSE` 是 MIT，但 `package.json` 写的是 GPL-3.0-or-later。
> 分发本仓库时，建议按两者中更严格的 GPL-3.0 理解，或直接联系上游确认。

## 二、本仓库使用了上游的什么

### 1. `app/` 目录（整目录）

`app/dist/index.js`、`node_modules` 等全部是上游的构建产物，**未修改其源码**。
本项目仅通过 `node app/dist/index.js` 调用它，属于「使用」而非「改写」。

上游 MIT 许可要求：分发时保留版权声明与许可全文。因此许可原文在本仓库有两份：

- `docs/upstream-MIT-LICENSE.txt` —— 随仓库分发，任何拿到本仓库的人都能看到
- `app/LICENSE` —— 本地使用时放在 `app/` 里（该目录不入库，你放置上游后请自行保留）

### 2. 界面信息架构

任务清单的分类方式、档位（省心 / 标准 / 全都要）的成员划分、节奏档位
（很快 / 官方默认 / 折中 / 保守 / 极慢）的延时数值，参考了上游的配置模块。

- **数值与成员关系**是客观配置事实，保留不变
- **所有描述文字已重写**，未使用上游原文

### 3. 实现思路（非代码复制）

以下几处的做法参照了上游，代码为独立编写：

- 账号列表只返回 `has_*` 布尔值，不把密码送到前端
- 结束任务用 `taskkill /T /F` 终止整棵进程树
- 日志按 error / warn / ok / info 分级着色
- 通过 AppUserModelID + 开始菜单快捷方式解决任务栏显示 “Python” 的问题

## 三、本项目自己写的部分

- `panel/`：`server.py`（HTTP 接口与静态服务）、`accounts.py`（账号存取与 DPAPI 加密）、
  `schedule.py` + `scheduler.py`（多时间点定时与重试）、`index.html`（全部界面）
- `desktop.py`：pywebview 无边框窗口外壳、单实例、窗口几何记忆、快捷方式与 AUMID
- `assets/`：图标为自行绘制

上游的 GUI 使用 customtkinter，本项目使用 `http.server` + HTML/JS + pywebview，
两者技术栈、文件结构、实现方式均不同。

## 四、分发注意

若你要把本项目发布出去：

1. 连同本文件与 `docs/upstream-MIT-LICENSE.txt` 一起分发
2. 如果按 GPL-3.0 理解，需要同时提供 `app/` 的完整对应源码（上游仓库）
3. 界面文案已重写，但仍建议在 README 中注明「界面设计参考自 xxx 项目」，这是礼貌也是诚信

## 五、GitHub 图标

「关于」页使用 GitHub Octicons 的 `mark-github-16` 图标。MIT 许可见 [docs/octicons-MIT-LICENSE.txt](docs/octicons-MIT-LICENSE.txt)。

## 六、Rewards 助手用户脚本

`browser-assistant/Rewards-Panel-userscript.user.js` 按 GNU GPL v3 提供，许可全文见 [browser-assistant/LICENSE.txt](browser-assistant/LICENSE.txt)。脚本源码随 Windows 便携包提供。Tampermonkey 通过浏览器的官方扩展商店安装。
