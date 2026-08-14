# 智修 · AI 修图台

独立的对话式 AI 修图服务：上传一张图片，用自然语言描述修改需求，AI 边聊边改。

## 功能

- **对话式修图**：MiniMax 视觉模型理解意图 → 通义 qwen-image-edit-plus 原图编辑；未配通义 key 时回退 MiniMax image-01 重绘
- **连续修改**：每轮以上一轮结果为底继续改，支持「向后一步」「向前一步」「回到原图」步骤导航
- **局部框选**：在图上拖出矩形区域，精准添加/去除人物或物体
- **参考图**：附加参考图指定要加入的人/物体，外形尽量贴近
- **文档模式**：只改框选区域内的内容（如票据金额），区域外像素级不变，PNG 无损输出
- 只出预览，**全程不动上传原图**；结果可放大查看、下载

## 运行

```bash
pip install flask pillow requests pillow-heif
PORT=8090 python3 retouch_api.py
# 打开 http://127.0.0.1:8090
```

环境变量：`PORT`（默认 8090）、`BIND`（默认 127.0.0.1）、`UPLOAD_DIR`、`PREVIEW_DIR`、`MAX_UPLOAD_MB`（默认 30）。

## 配置

读取 `~/image_analyzer_config.json`（不入库）：

```json
{
  "api_key": "sk-cp-...",            // MiniMax（意图理解 + image-01 回退）
  "api_base": "https://api.minimaxi.com/v1",
  "model": "MiniMax-M3",
  "ty_api_key": "sk-..."             // 通义 DashScope（图像编辑，主力）
}
```

## 接口

| 接口 | 说明 |
|------|------|
| `POST /api/upload` | 上传图片，返回 `{token,width,height}` |
| `GET /api/image/<token>` | 访问上传原图 |
| `POST /api/retouch` | 修图 `{token,instruction,base_token?,history?,rect?,ref_image?,doc_mode?}` |
| `GET /api/preview/<token>` | 修图结果预览 |
| `GET /api/download/<token>` | 下载结果/原图 |
