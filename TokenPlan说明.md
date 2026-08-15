# 百炼 CLI（bl）与 Token Plan 使用说明

本文记录阿里云百炼 Token Plan（订阅套餐）和百炼 CLI 的实际用法，全部为实测结论
（2026-08）。官方文档滞后或有歧义的地方，以本文实测为准。

## 一、三种 key，三套端点，互不通用

| 类型 | 前缀 | 用途 | 端点（Base URL） |
|------|------|------|------------------|
| 按量付费 | `sk-` / `sk-ws-` | 所有 DashScope API，按量计费 | `https://dashscope.aliyuncs.com`（国际站 `-intl`） |
| Token Plan（订阅套餐） | `sk-sp-` | 抵扣套餐 Credits | `https://token-plan.cn-beijing.maas.aliyuncs.com` |
| Coding Plan（编程套餐） | `sk-sp-` | 抵扣 Coding Plan 额度 | `https://coding.dashscope.aliyuncs.com/v1` |

**坑 1：key 和端点必须配套，混用一律 401。** Token Plan 的 sk-sp- key 打到
`dashscope.aliyuncs.com` 或 `coding.dashscope.aliyuncs.com` 都是
`401 InvalidApiKey / token expired`——不是 key 无效，是端点错了。

**坑 2：Token Plan 和 Coding Plan 的 key 都是 sk-sp- 开头**，但端点不同、额度不通。

**坑 3：Token Plan 只支持华北2（北京）地域**，控制台左上角地域不对会看不到订阅。

另外还有「业务空间专属域名」（形如
`https://llm-<空间id>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`），
在控制台 API Key 管理页可见，高并发/网络隔离场景用，普通调用用上面的公共端点即可。

## 二、Token Plan 的两种调用路径

Token Plan 域名下挂着两套 API，能力差别很大：

### 1. OpenAI 兼容路径：`/compatible-mode/v1`

```
POST https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions
```

- 文本模型正常（qwen3.6-flash / qwen3.7 / qwen3.8-max / deepseek / glm 等）
- 文生图可以（content 必须是 list 形式 `[{"type":"text","text":"..."}]`，
  字符串形式会报 `Input should be a valid list`）
- **不能带图**：`image_url` + `text` 一起传 →
  `400 Either 'text' or 'image' must be provided, but not both`
- `/images/generations`、`/images/edits` 均不可用

### 2. DashScope 原生路径：`/api/v1/...`（**图像编辑必须走这条**）

```
POST https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation
```

- 调用方式和按量端点**完全一样**，只换域名和 key
- 文生图 ✓、**带图编辑 ✓**、**多图参考 ✓**（实测 wan2.7-image-pro、qwen-image-3.0-pro 均可）
- 异步任务（视频生成等）：`/api/v1/services/aigc/video-generation/video-synthesis`
  + `X-DashScope-Async: enable`，轮询 `/api/v1/tasks/{task_id}`
- 官方示例见 [Token Plan 接入多模态生成模型](https://help.aliyun.com/zh/model-studio/token-plan-multimodal-gen)

> 教训：判断"套餐不支持某能力"前，两条路径都要测。我们一开始只测了兼容路径，
> 误判"Token Plan 不支持图片编辑"。

### 套餐内图像模型实测（2026-08，个人版）

| 模型 | 文生图 | 带图编辑 | 多图参考 | 备注 |
|------|--------|----------|----------|------|
| wan2.7-image | ✓ | ✓ | ✓ | |
| wan2.7-image-pro | ✓ | ✓ | ✓ | 输出约 2364×1773，编辑幅度果断 |
| qwen-image-3.0-pro | ✓ | ✓ | 未测 | 输出约 2352×1760，排版/密集信息强 |
| qwen-image-2.0(-pro) | ✓ | ✓（按量端点实测） | ✓ | 输出接近原图尺寸 |

通用限制：输入图片宽高需在 **384~3072** 之间（过小报 `Error validating image`）；
返回的图片 URL 是限时签名地址，**需立即下载**。

## 三、百炼 CLI（bl）

官方 CLI：[modelstudioai/cli](https://github.com/modelstudioai/cli)，需要 Node.js ≥ 18.17。

```bash
npm install -g bailian-cli
bl --version
```

### 登录方式

```bash
# 1. 按量付费 key（默认 profile）
bl auth login --api-key sk-xxxxx

# 2. Token Plan（独立 profile，推荐）
bl auth login --config token-plan --api-key sk-sp-xxxxx \
  --base-url https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1

# 3. 控制台浏览器 OAuth（打开浏览器授权，凭证写 ~/.bailian/config.json）
bl auth login --console          # 浏览器没跳成功就重跑一次

# 查看状态
bl auth status [--config token-plan]
```

注意：`--console` 的 OAuth 流程要在浏览器里**一路点到授权完成页**，
提前关页面会导致凭证不回传（`bl auth status` 显示 Not authenticated）。

### CLI 默认端点

CLI 用 `--config token-plan` 配置的 key 时，需要配 `--base-url` 指向 Token Plan
地址，否则校验会按默认按量端点走导致误判 key 无效。

## 四、aiBiz 修图服务的配置实践

`~/image_analyzer_config.json`（服务器 `/root/image_analyzer_config.json`）：

```json
{
  "api_key": "sk-cp-...",               // MiniMax 理解层
  "api_base": "https://api.minimaxi.com/v1",
  "model": "MiniMax-M3",
  "ty_api_key": "sk-sp-...",            // Token Plan key（编辑层）
  "ty_api_base": "https://token-plan.cn-beijing.maas.aliyuncs.com",
  "ty_model": "wan2.7-image-pro",
  "ty_engine": "qwen"
}
```

- `ty_api_base` 缺省 = 按量端点；配上 Token Plan 域名后，**全部图像编辑走套餐 Credits**
- 切回按量付费：删掉 `ty_api_base`，`ty_api_key` 换回 sk-ws- key
- 配置每次请求实时读取，改完**不用重启服务**

## 五、排错速查

| 现象 | 原因 | 解决 |
|------|------|------|
| 401 InvalidApiKey | key 与端点不配套（最常见） | sk-sp- 必须配 token-plan 域名；sk-/sk-ws- 配 dashscope 域名 |
| 401 token expired（coding 端点） | Token Plan key 打到 Coding Plan 端点 | 换 token-plan 域名 |
| 400 text/image not both | 兼容路径带图 | 改用 `/api/v1` 原生路径 |
| 400 Input should be a valid list | 兼容路径 content 用了字符串 | content 改 list 形式 |
| 400 Error validating image | 图片宽高 <384 或 >3072 | 缩放到范围内 |
| Model not exist | 模型不在套餐/端点支持列表 | 查「我的订阅」可用模型列表 |
