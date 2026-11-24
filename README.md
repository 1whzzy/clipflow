# Clipflow · 短视频带货剪辑框架

基于 LangGraph 的商品短视频工作流。直播切片保留主播原声，可穿插同款商品实拍。素材成片模式负责生成脚本、配音和分镜，两种模式共用审核、时间线、渲染和交付流程。

## 评审入口

- [项目业务说明](短视频带货剪辑项目业务说明.docx)
- [18 节点流程图](短视频带货剪辑18节点流程图.png)
- [架构说明](docs/ARCHITECTURE.md)
- [代码生成的 Mermaid 流程图](docs/workflow.mmd)

## 本地启动

使用 Python 创建虚拟环境（已用 Python 3.14 验证框架可启动），安装 `requirements.txt`。参考 `.env.example` 在本地配置模型和数据库，按项目实际目录放置素材、商品和任务文件。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m clipflow.run --help
```

实际出片还需 FFmpeg、可用的模型服务和素材。素材成片的真实配音服务尚未接入。
