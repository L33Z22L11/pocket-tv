# Pocket TV 1.1.0

- 新增电脑频道管理器：搜索、分组、收藏和首帧检测，默认使用 Catppuccin Mocha 主题。
- 可通过 USB 扫描并配置 2.4 GHz Wi-Fi；支持优先 USB、断开后尝试 Wi-Fi、接回后切回，切换时会重新缓冲。
- 改为按需显示中文帮助、暂停和加载提示；新增短暂音量条与中文文字台标，正常播放不再常驻底部信息。
- 修正横屏换台顺序，保留音量记忆；改善换台取消、加载反馈与播放中断后的重连处理。
- 更新封面与上手说明。公共直播的加载和连续播放仍取决于来源及网络。

## 安装

下载 Release 中的 `pocket-tv-1.1.0.zip`，按包内 README 启动电脑程序。`pocket-tv-full.bin` 是从 `0x0` 安装的完整固件；请使用同版电脑程序，旧版 1.0.0 不兼容。无需整片擦除。

电脑必须持续运行。USB 可直接播放；Wi-Fi 可选，长时间无线播放与连接切换仍需进一步验证。macOS 已做真机检查，Windows 脚本尚未完成真机验证。

## English

- Added a desktop channel manager with search, groups, favorites and a first-frame check, using Catppuccin Mocha by default.
- Added USB-assisted 2.4 GHz Wi-Fi setup and USB-first transport: try paired Wi-Fi after USB disconnects and switch back when it returns. Handover requires buffering.
- Replaced the persistent HUD with on-demand Chinese help, pause/loading notices, a temporary volume bar and a channel-name badge.
- Corrected landscape channel order while retaining volume memory; improved switch cancellation, loading feedback and recovery after playback interruptions.
- Updated the cover and getting-started guide. Public live-stream loading and continuity still depend on the source and network.

Download `pocket-tv-1.1.0.zip` from the Release and follow its README. Flash the merged `pocket-tv-full.bin` at `0x0`; use the matching computer program, not version 1.0.0. A full-chip erase is not required.

Keep the computer running. USB works without Wi-Fi setup. Sustained wireless playback and handover need further testing. Hardware checks were performed on macOS; Windows launchers have not completed hardware validation.
