# CCTV 试播列表

来源：[iptv-org/iptv](https://github.com/iptv-org/iptv)，中国列表 https://iptv-org.github.io/iptv/countries/cn.m3u 。

2026-09-14 在当前网络筛选并实际解码 CCTV+ 1、CCTV+ 2，每路约 2 秒、39 视频帧。已抓取 CCTV+ 1 帧确认台标与实际画面。它们是 CCTV+ 直播流，不是 CCTV-1 综合台或 CCTV-2 财经台。

`cctv.m3u` 只保留这两条实际解码通过的地址。常规 CCTV-1/13 等源探测失败；CCTV-7 初次探测通过，随后返回 HTTP 503，未加入试播列表。公开源可用性可能变化。

macOS/Linux：`./cctv.sh`；Windows：`cctv.cmd`。长按 ↑/↓ 切换列表内频道，横屏时长按和短按方向都反转；短按调音量，启动时恢复上次保存的音量。
