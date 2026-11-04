# 参考范围

参考：[FirstWari/chatgpt-web](https://github.com/FirstWari/chatgpt-web)，核查提交 `86383d50847f808add454f01358f29c053167192`。仅下载阅读，没有安装、导入或复制其实现。

检查到的 [save_reply](https://github.com/FirstWari/chatgpt-web/blob/86383d50847f808add454f01358f29c053167192/chatgpt_web/core.py#L261) 主要把回复文字或 Markdown 代码块写入文件，不能作为已实现生成文件二进制回传的依据。其项目缓存、文件路径边界和明确错误接口可作为设计检查点；可靠性仍需在目标环境实测。

本项目继续使用 Ego 的实际 API、显式状态机和私有产物存储；完成检测、恢复以及图像／文件链路依据自己的网页实验与测试。参考仓库的 README 声明不作为本项目功能已验证的证据。
