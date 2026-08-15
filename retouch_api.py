#!/usr/bin/env python3
"""AI 修图台 —— 独立修图服务

上传一张图片，通过对话式 AI 修图：
MiniMax 视觉模型理解意图（可选）→ 通义 qwen-image-edit-plus 原图编辑
（未配 ty_api_key 时回退 MiniMax image-01 重绘）。
支持：连续对话修改、框选区域修改、参考图添加人/物体、文档模式（只改框选区域）。

配置：~/image_analyzer_config.json
  api_key / api_base / model   MiniMax（意图理解 + image-01 回退）
  ty_api_key                   通义 DashScope（图像编辑，主力）

运行：PORT=8090 python3 retouch_api.py
"""
import os, io, json, base64, uuid
from PIL import Image, ImageOps
try:
    # HEIC/HEIF（iPhone 照片）支持
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass
from flask import Flask, request, jsonify, send_file, send_from_directory, session, redirect
from datetime import timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(BASE_DIR, "uploads"))
PREVIEW_DIR = os.environ.get("PREVIEW_DIR", os.path.expanduser("~/.retouch_preview"))
CONFIG_PATH = os.path.expanduser("~/image_analyzer_config.json")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "30"))

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(PREVIEW_DIR, exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# ---------------- 登录（只要密码） ----------------
PASSWORD = os.environ.get("RETOUCH_PASSWORD", "8888")
_secret_path = os.path.join(BASE_DIR, ".secret_key")
if os.path.exists(_secret_path):
    app.secret_key = open(_secret_path).read().strip()
else:
    app.secret_key = uuid.uuid4().hex + uuid.uuid4().hex
    with open(_secret_path, "w") as _f:
        _f.write(app.secret_key)
    os.chmod(_secret_path, 0o600)
app.permanent_session_lifetime = timedelta(days=30)


@app.before_request
def _auth():
    if request.path.startswith("/static/") or request.path in ("/login", "/api/login"):
        return None
    if session.get("ok"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "未登录"}), 401
    return redirect("/login")


@app.route("/login")
def login_page():
    if session.get("ok"):
        return redirect("/")
    return send_from_directory("static", "login.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    if str(data.get("password") or "") == PASSWORD:
        session["ok"] = True
        session.permanent = True
        return jsonify({"ok": True})
    return jsonify({"error": "密码错误"}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


def _load_cfg():
    return json.load(open(CONFIG_PATH)) if os.path.exists(CONFIG_PATH) else {}


@app.after_request
def no_cache_html(resp):
    if resp.mimetype == "text/html" or "javascript" in resp.mimetype:
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---------------- 上传与图片访问 ----------------

def _upload_path(token):
    p = os.path.join(UPLOAD_DIR, os.path.basename(token) + ".jpg")
    return p if os.path.exists(p) else None


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """上传一张图片（jpg/png/webp/heic），统一转成 RGB JPEG 存储，返回 token"""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "请选择图片文件"}), 400
    try:
        img = ImageOps.exif_transpose(Image.open(f.stream)).convert("RGB")
    except Exception:
        return jsonify({"error": "无法识别的图片格式"}), 400
    if img.width < 16 or img.height < 16:
        return jsonify({"error": "图片太小"}), 400
    # 超大图压到长边 3072，够用且省 token/带宽
    if max(img.size) > 3072:
        img.thumbnail((3072, 3072), Image.LANCZOS)
    token = uuid.uuid4().hex[:16]
    img.save(os.path.join(UPLOAD_DIR, token + ".jpg"), "JPEG", quality=95)
    return jsonify({"token": token, "width": img.width, "height": img.height})


@app.route("/api/image/<token>")
def api_image(token):
    p = _upload_path(token)
    if not p:
        return ("图片不存在", 404)
    return send_file(p, mimetype="image/jpeg")


@app.route("/api/preview/<token>")
def api_preview(token):
    """修图结果预览（jpg 为主，文档模式为 png）"""
    base = os.path.join(PREVIEW_DIR, os.path.basename(token))
    for ext, mime in ((".jpg", "image/jpeg"), (".png", "image/png")):
        if os.path.exists(base + ext):
            return send_file(base + ext, mimetype=mime)
    return ("预览已过期", 404)


@app.route("/api/download/<token>")
def api_download(token):
    base = os.path.join(PREVIEW_DIR, os.path.basename(token))
    for ext in (".jpg", ".png"):
        if os.path.exists(base + ext):
            return send_file(base + ext, as_attachment=True,
                             download_name="retouched" + ext)
    p = _upload_path(token)
    if p:
        return send_file(p, as_attachment=True, download_name="original.jpg")
    return ("文件不存在", 404)


# ---------------- AI 能力 ----------------

def _mm_vision_text(prompt, img, max_side=1024, max_tokens=300):
    """调 MiniMax 视觉模型，发图片+提示词，返回回答文本；未配置/失败返回 None"""
    cfg = _load_cfg()
    api_key = cfg.get("api_key", "")
    if not api_key:
        return None
    api_base = (cfg.get("api_base") or "https://api.minimaxi.com/v1").rstrip("/")
    model = cfg.get("model", "MiniMax-M3")
    pic = img.convert("RGB").copy()
    pic.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    pic.save(buf, "JPEG", quality=75)
    b64 = base64.b64encode(buf.getvalue()).decode()
    import requests as _req
    for attempt in (1, 2):  # MiniMax 偶发抽风，失败重试一次
        try:
            r = _req.post(api_base + "/chat/completions", json={
                "model": model,
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
                    {"type": "text", "text": prompt}]}],
                "max_tokens": max_tokens},
                headers={"Authorization": "Bearer " + api_key}, timeout=90)
            if r.status_code != 200:
                print(f"MiniMax 视觉调用失败 {r.status_code}: {r.text[:120]}", flush=True)
                continue
            text = r.json()["choices"][0]["message"]["content"]
            if "</think>" in text:  # 思考模型：去掉推理段
                text = text.split("</think>", 1)[1].strip()
            return text
        except Exception as e:
            print(f"MiniMax 视觉调用异常: {e}", flush=True)
    return None


def _mm_vision_json(prompt, img, max_side=1024, max_tokens=300):
    text = _mm_vision_text(prompt, img, max_side, max_tokens)
    if not text:
        return None
    i, j = text.find("{"), text.rfind("}")
    try:
        return json.loads(text[i:j + 1]) if 0 <= i < j else None
    except Exception as e:
        print(f"MiniMax JSON 解析失败: {e}", flush=True)
        return None


def _ai_retouch_plan(img, instruction, history):
    """MiniMax 结合照片和对话历史理解修图意图，输出 {"reply","edit","prompt"}"""
    hist = "；".join(str(h).strip() for h in (history or [])[-6:] if str(h).strip())
    return _mm_vision_json(
        "我们在连续修这张照片。" + ("用户之前依次提过这些要求：「" + hist + "」。" if hist else "")
        + "现在用户的新要求是：「" + instruction + "」。"
        "请仔细观察照片，理解用户想要的效果，只输出 JSON："
        "{\"reply\": \"用中文口语化回应用户，说明你理解要怎么改（一两句）\", "
        "\"edit\": \"给图像编辑模型的一句直接指令：具体说明把什么改成什么样"
        "（改颜色给出色相和程度；加/去物体必须根据画面透视和远近说明位置和大小比例，"
        "与场景中其他人物、物体比例协调，不能过大或过小），结尾强调其余内容保持原样\", "
        "\"prompt\": \"给文生图模型的中文提示词：开头第一句明确写出要做的修改，"
        "然后用 80 字左右描述当前画面（主体、场景、构图、色调、风格）作为需要保持的内容\"}。"
        "如果需求无法理解，输出 {\"reply\": \"原因说明\", \"edit\": \"\", \"prompt\": \"\"}。"
        "不要输出任何其他内容。", img, max_tokens=600)


def _ty_image_edit(img, edit, ref_image=""):
    """百炼图像编辑（ty_model 指定模型，默认 qwen-image-edit-plus）：base64 传图 + 编辑指令。
    ref_image 为可选参考图：单个 data URI 或列表（最多取 2 张，限制总图数 ≤3）。
    参考图在前、底图在后（输出比例以最后一张图为准）。
    主通道（ty_api_key + ty_api_base，可配 Token Plan）失败时，
    自动用备用通道（ty_api_key_backup + ty_api_base_backup，默认按量端点）重试一次。
    成功返回 (PIL图, None)，失败返回 (None, 错误信息)。"""
    cfg = _load_cfg()
    key = cfg.get("ty_api_key", "")
    if not key:
        return None, "未配置通义 API Key（~/image_analyzer_config.json 的 ty_api_key）"
    model = cfg.get("ty_model", "qwen-image-edit-plus")  # 可换 wan2.7-image-pro 等
    # 端点：默认按量付费；Token Plan 套餐配 https://token-plan.cn-beijing.maas.aliyuncs.com
    api_base = (cfg.get("ty_api_base") or "https://dashscope.aliyuncs.com").rstrip("/")
    buf = io.BytesIO()
    pic = img.convert("RGB").copy()
    pic.thumbnail((2048, 2048))  # 官方建议宽高均在 384~3072 之间
    if min(pic.size) < 384:  # 太小会被拒（Error validating image），放大到下限
        s = 384 / min(pic.size)
        pic = pic.resize((int(pic.width * s), int(pic.height * s)), Image.LANCZOS)
    pic.save(buf, "JPEG", quality=90)
    b64 = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    if isinstance(ref_image, (list, tuple)):
        refs = [r for r in ref_image if str(r).startswith("data:image")]
    else:
        refs = [ref_image] if ref_image else []
    content = [{"image": r} for r in refs[:2]]
    content += [{"image": b64}, {"text": edit}]
    import requests as _req

    def _call(k, base, mdl):
        try:
            r = _req.post(base + "/api/v1/services/aigc/"
                          "multimodal-generation/generation",
                          json={"model": mdl,
                                "input": {"messages": [{"role": "user", "content": content}]},
                                "parameters": {"n": 1, "watermark": False,
                                               "prompt_extend": True}},
                          headers={"Authorization": "Bearer " + k}, timeout=240)
            if r.status_code != 200:
                return None, f"通义编辑失败 {r.status_code}: {r.text[:200]}"
            c = r.json()["output"]["choices"][0]["message"]["content"]
            url = next((x.get("image") for x in c if x.get("image")), None)
            if not url:
                return None, "通义未返回图片"
            ir = _req.get(url, timeout=120)  # 24 小时有效的临时 URL，需立即下载
            if ir.status_code != 200:
                return None, "通义结果图下载失败"
            return Image.open(io.BytesIO(ir.content)).convert("RGB"), None
        except Exception as e:
            return None, f"通义编辑异常: {e}"

    out, err = _call(key, api_base, model)
    if out is not None:
        return out, None
    # 主通道失败（如 Token Plan 额度用尽）：备用通道兜底重试一次。
    # 备用模型可单独配（ty_model_backup，默认 qwen-image-edit-plus）：
    # 按量端点上 qwen-image-3.0-pro 极慢（200s+），edit-plus 快一个量级且支持多参考图。
    bkey = cfg.get("ty_api_key_backup", "")
    bbase = (cfg.get("ty_api_base_backup") or "https://dashscope.aliyuncs.com").rstrip("/")
    if bkey and (bkey != key or bbase != api_base):
        bmodel = cfg.get("ty_model_backup", "qwen-image-edit-plus")
        print(f"主编辑通道失败（{err[:80]}），尝试备用通道（{bmodel}）", flush=True)
        out2, err2 = _call(bkey, bbase, bmodel)
        if out2 is not None:
            return out2, None
        err = f"{err}；备用通道也失败: {err2}"
    return None, err


def _wx_image_edit(img, edit, mask=None):
    """万相 wanx2.1-imageedit（异步任务）：base64 传图 + 编辑指令。
    mask 为 L 模式 PIL 图（白色=要重绘的区域），对应 description_edit_with_mask。
    注意：输入宽高需在 [512,1440]，自动缩放。成功返回 (PIL图, None)，失败 (None, 错误)。"""
    cfg = _load_cfg()
    key = cfg.get("wx_api_key") or cfg.get("ty_api_key", "")  # 万相可用独立 key，缺省用百炼 key
    if not key:
        return None, "未配置通义 API Key（~/image_analyzer_config.json 的 ty_api_key）"
    pic = img.convert("RGB").copy()
    w, h = pic.size
    s = 1.0
    if min(w, h) < 512:
        s = 512 / min(w, h)
    if max(w, h) * s > 1440:
        s = 1440 / max(w, h)
    if s != 1.0:
        pic = pic.resize((max(16, int(w * s)), max(16, int(h * s))), Image.LANCZOS)

    def _b64(im):
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

    inp = {"function": "description_edit_with_mask" if mask else "description_edit",
           "prompt": edit, "base_image_url": _b64(pic)}
    if mask:
        inp["mask_image_url"] = _b64(mask.convert("L").resize(pic.size))
    import requests as _req
    import time as _t
    H = {"Authorization": "Bearer " + key, "X-DashScope-Async": "enable"}
    base = "https://dashscope.aliyuncs.com/api/v1"
    try:
        r = _req.post(base + "/services/aigc/image2image/image-synthesis",
                      headers=H, json={"model": "wanx2.1-imageedit", "input": inp,
                                       "parameters": {"n": 1}}, timeout=90)
        if r.status_code != 200:
            return None, f"万相任务创建失败 {r.status_code}: {r.text[:200]}"
        tid = r.json()["output"]["task_id"]
        for _ in range(45):  # 最长约 2 分半
            _t.sleep(3)
            t = _req.get(f"{base}/tasks/{tid}",
                         headers={"Authorization": "Bearer " + key}, timeout=30).json()
            st = t["output"]["task_status"]
            if st == "SUCCEEDED":
                url = t["output"]["results"][0]["url"]
                ir = _req.get(url, timeout=120)
                if ir.status_code != 200:
                    return None, "万相结果图下载失败"
                out_img = Image.open(io.BytesIO(ir.content)).convert("RGB")
                # 万相输出尺寸会被内部取整（如 600→592），缩回原图尺寸
                if out_img.size != (w, h):
                    out_img = out_img.resize((w, h), Image.LANCZOS)
                return out_img, None
            if st in ("FAILED", "CANCELED"):
                return None, "万相任务失败: " + str(t["output"].get("message", ""))[:200]
        return None, "万相任务超时"
    except Exception as e:
        return None, f"万相调用异常: {e}"


def _use_wanx(ref_images=None):
    """当前请求是否走万相引擎：配置 ty_engine=wanx 且无参考图（万相不支持多图）"""
    return _load_cfg().get("ty_engine", "qwen") == "wanx" and not ref_images


def _ty_doc_edit(img, rect_vals, instruction, ref_images=None, doc_precise=False):
    """区域编辑。两条路径：
    - 万相（ty_engine=wanx 且无参考图且非文档模式）：整图 + mask 局部重绘，一次返回；
    - qwen（默认/带参考图/文档模式）：框选区域裁出 → 编辑 → 原样贴回（边缘羽化），区域外像素完全不变。
    ref_images 可选：要加入的人/物体参考图（最多 2 张）。"""
    from PIL import ImageDraw, ImageFilter
    rx, ry, rw, rh = rect_vals
    W, H = img.size
    x0, y0 = int(rx * W), int(ry * H)
    x1, y1 = min(W, int((rx + rw) * W + 0.5)), min(H, int((ry + rh) * H + 0.5))
    cw, ch = x1 - x0, y1 - y0
    if cw < 8 or ch < 8:
        return None, "框选区域太小，请框大一点"
    if _use_wanx(ref_images) and not doc_precise:
        # 万相 mask 局部重绘：白色区域按描述重绘，其余区域模型自行保持
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).rectangle([x0, y0, x1 - 1, y1 - 1], fill=255)
        return _wx_image_edit(img, instruction + "。只改动标记区域的内容，其余部分保持原样。",
                              mask)
    crop = img.crop((x0, y0, x1, y1))
    scale = min(max(1.0, 512 / min(cw, ch)), 2048 / max(cw, ch))
    if scale > 1.0:
        crop = crop.resize((int(cw * scale), int(ch * scale)), Image.LANCZOS)
    edited, err = _ty_image_edit(crop, instruction + "。只改动要求的内容，其余部分保持原样。",
                                 ref_images or "")
    if edited is None:
        return None, err
    edited = edited.resize((cw, ch), Image.LANCZOS)
    out = img.copy()
    mask = Image.new("L", (cw, ch), 0)
    ImageDraw.Draw(mask).rectangle([3, 3, cw - 4, ch - 4], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(2))
    out.paste(edited, (x0, y0), mask)
    return out, None


def _mm_redraw(img, gen_prompt, cfg, token):
    """回退：MiniMax image-01 重绘（未配置通义 key 时）"""
    api_key = cfg.get("api_key", "")
    api_base = (cfg.get("api_base") or "https://api.minimaxi.com/v1").rstrip("/")
    w, h = img.size
    ratio = min((("1:1", 1.0), ("3:4", 0.75), ("9:16", 0.5625),
                 ("4:3", 1.3333), ("16:9", 1.7778)),
                key=lambda t: abs(t[1] - w / max(1, h)))[0]
    buf = io.BytesIO()
    pic = img.copy()
    pic.thumbnail((1024, 1024))
    pic.save(buf, "JPEG", quality=85)
    payload = {"model": "image-01", "prompt": gen_prompt,
               "aspect_ratio": ratio, "n": 1, "response_format": "base64",
               "subject_reference": [{"type": "character",
                                      "image_file": "data:image/jpeg;base64,"
                                      + base64.b64encode(buf.getvalue()).decode()}]}
    import requests as _req
    r = _req.post(api_base + "/image_generation", json=payload,
                  headers={"Authorization": "Bearer " + api_key}, timeout=300)
    if r.status_code != 200:
        return f"MiniMax 图片生成失败 {r.status_code}: {r.text[:200]}"
    resp = r.json()
    imgs = (resp.get("data") or {}).get("image_base64") or []
    if not imgs:
        return "生成失败: " + ((resp.get("base_resp") or {}).get("status_msg") or "无返回图片")
    with open(os.path.join(PREVIEW_DIR, token + ".jpg"), "wb") as f:
        f.write(base64.b64decode(imgs[0]))
    return None


# ---------------- 修图主接口 ----------------

def _parse_rect(rect):
    """归一化矩形 {x,y,w,h} → (rx,ry,rw,rh)，无效返回 None"""
    try:
        rx, ry = float(rect.get("x")), float(rect.get("y"))
        rw, rh = float(rect.get("w")), float(rect.get("h"))
        if 0 <= rx < 1 and 0 <= ry < 1 and 0 < rw <= 1 and 0 < rh <= 1 \
                and rx + rw <= 1.05 and ry + rh <= 1.05:
            return (rx, ry, rw, rh)
    except (TypeError, ValueError, AttributeError):
        pass
    return None


def _region_desc(rx, ry, rw, rh):
    """归一化矩形 → 中文位置描述，融入编辑指令"""
    cx, cy = rx + rw / 2, ry + rh / 2
    hz = "左侧" if cx < 0.33 else "右侧" if cx > 0.67 else \
        ("中央" if 0.4 <= cx <= 0.6 else ("中央偏左" if cx < 0.5 else "中央偏右"))
    vt = "上部" if cy < 0.33 else "下部" if cy > 0.67 else \
        ("中部" if 0.4 <= cy <= 0.6 else ("中部偏上" if cy < 0.5 else "中部偏下"))
    return f"画面{vt}{hz}区域（约占画面宽 {int(rw * 100)}%、高 {int(rh * 100)}%）"


@app.route("/api/retouch", methods=["POST"])
def api_retouch():
    """对话式 AI 修图。只出预览，不动上传原图。
    参数：token（上传图）、instruction、base_token（上次结果继续改）、
    history、rect（框选区域）、ref_image（参考图 data URI）、doc_mode、
    edits（批量区域修改 [{rect,instruction,ref_image?}]）、ref_images（全局多参考图列表）"""
    data = request.get_json(silent=True) or {}
    up_token = os.path.basename(str(data.get("token") or "").strip())
    instruction = (data.get("instruction") or "").strip()
    base_token = os.path.basename(str(data.get("base_token") or "").strip())
    history = data.get("history") or []
    rect_vals = _parse_rect(data.get("rect") or {})
    region_desc = _region_desc(*rect_vals) if rect_vals else ""
    edits = data.get("edits") or []
    ref_images = [r for r in (data.get("ref_images") or [])
                  if str(r).startswith("data:image")][:2]
    if not instruction and not edits:
        return jsonify({"error": "请填写修图要求"}), 400
    if region_desc:
        instruction = instruction + "。修改区域：" + region_desc
    ref_image = str(data.get("ref_image") or "")
    if ref_image and not ref_image.startswith("data:image"):
        ref_image = ""
    cfg = _load_cfg()
    api_key = cfg.get("api_key", "")
    ty_key = cfg.get("ty_api_key", "")
    if not api_key and not ty_key:
        return jsonify({"error": "未配置 API Key（~/image_analyzer_config.json）"}), 400

    # 底图：优先上一次修图结果（连续修改），否则上传原图
    img = None
    if base_token:
        for ext in (".jpg", ".png"):
            bp = os.path.join(PREVIEW_DIR, base_token + ext)
            if os.path.exists(bp):
                img = Image.open(bp).convert("RGB")
                break
    if img is None:
        p = _upload_path(up_token)
        if not p:
            return jsonify({"error": "图片不存在，请重新上传"}), 404
        img = Image.open(p).convert("RGB")

    token = f"rt_{uuid.uuid4().hex[:12]}"

    if ty_key:
        if edits:
            # 批量区域修改：逐块 理解画面(规划比例/位置) → 裁剪→通义编辑→羽化贴回，PNG 输出
            results, done = [], []
            out = img
            for i, e in enumerate(edits[:6]):
                e = e or {}
                instr = str(e.get("instruction") or "").strip()
                rv = _parse_rect(e.get("rect") or {})
                if not instr:
                    results.append(f"区域{i + 1}缺少修改描述")
                    continue
                if not rv:
                    results.append(f"区域{i + 1}框选无效")
                    continue
                # 框选只是位置提示：编辑范围向外扩大（约1.6倍，限制在图内），
                # 让添加的人/物体大小由画面比例决定，不被框的大小限定
                rx, ry, rw, rh = rv
                nw, nh = min(1.0, rw * 1.6), min(1.0, rh * 1.6)
                edit_rect = (min(max(0.0, rx + rw / 2 - nw / 2), 1.0 - nw),
                             min(max(0.0, ry + rh / 2 - nh / 2), 1.0 - nh), nw, nh)
                # 先理解整张图：让视觉模型结合场景/透视/已有物体大小，给出带合理比例的编辑指令
                planned = None
                if api_key:
                    planned = _mm_vision_json(
                        "这是一张照片，用户在" + _region_desc(*rv)
                        + "框选了一块区域（框选只是大致位置提示，实际修改可以超出框的范围）。"
                        "用户的修改要求是：「" + instr + "」。"
                        "请先观察照片整体：场景内容、透视关系、画面中已有人物和物体的大小比例、光线方向。"
                        "然后只输出 JSON：{\"edit\": \"给图像编辑模型的一句直接指令\"}，要求："
                        "1) 修改内容放在框选位置附近；"
                        "2) 若要添加人物或物体，其大小必须由画面透视和远近决定，"
                        "与场景中其他人物、物体比例协调，可以超出框选范围，"
                        "不要被框的大小限定，也不能过大或过小；"
                        "3) 光影、色调、风格与原图一致；"
                        "4) 结尾强调其余内容保持原样。不要输出任何其他内容。",
                        out, max_tokens=400)
                if planned and (planned.get("edit") or "").strip():
                    instr = planned["edit"].strip()
                else:  # 理解层不可用时的兜底：通用比例约束
                    instr += ("。注意：添加或修改的人物/物体大小必须符合画面透视，"
                              "与场景中其他人物、物体比例协调，可以超出框选范围，"
                              "不要被框的大小限定，光影色调与原图一致")
                print(f"区域{i + 1} 编辑指令: {instr[:100]}", flush=True)
                e_ref = str(e.get("ref_image") or "")
                if not e_ref.startswith("data:image"):
                    e_ref = ""
                refs = [e_ref] if e_ref else ref_images  # 区块自己的参考图优先于全局参考图
                if refs:
                    instr = (f"前面{len(refs)}张图片是要添加的人/物体的参考图，"
                             "最后一张是要修改的图。把参考图中的人/物体自然地加入图中，"
                             "外形尽量贴近参考图，大小、透视和光影与底图协调。"
                             "具体要求：" + instr)
                out2, err = _ty_doc_edit(out, edit_rect, instr, refs)
                if out2 is None:
                    results.append(f"区域{i + 1}失败：{err}")
                else:
                    out = out2
                    done.append(i + 1)
            g_instr = instruction  # 总体画面修改：与框选修改一次性提交，最后执行
            if not done and not g_instr:
                return jsonify({"error": "修改失败：" + "；".join(results)}), 502
            global_ok = False
            if g_instr:
                # 在区域修改结果上做整图编辑：MiniMax 理解意图 → 通义整图修改
                gedit = g_instr
                if api_key:
                    plan = _ai_retouch_plan(out, g_instr, history)
                    if plan and (plan.get("edit") or "").strip():
                        gedit = plan["edit"].strip()
                if _use_wanx(ref_images):
                    gout, gerr = _wx_image_edit(out, gedit)
                else:
                    gout, gerr = _ty_image_edit(out, gedit, ref_images)
                if gout is None:
                    results.append(f"总体画面修改失败：{gerr}")
                else:
                    out = gout
                    global_ok = True
            if not done and not global_ok:
                return jsonify({"error": "修改失败：" + "；".join(results)}), 502
            out.save(os.path.join(PREVIEW_DIR, token + ".png"), "PNG")
            parts = []
            if done:
                parts.append(f"已完成 {len(done)}/{min(len(edits), 6)} 块区域修改")
            if global_ok:
                parts.append("总体画面修改完成")
            reply = "，".join(parts)
            if results:
                reply += "（" + "；".join(results) + "）"
            return jsonify({"token": token, "ext": "png", "reply": reply})
        reply = ""
        edit = instruction
        if api_key:  # 理解层失败不至于不能用——退回用户原话当编辑指令
            plan = _ai_retouch_plan(img, instruction
                                    + ("（用户另外附了参考图，参考图中就是要添加的人/物体）"
                                       if (ref_image or ref_images) else ""), history)
            if plan:
                reply = (plan.get("reply") or "").strip()
                edit = (plan.get("edit") or "").strip() or instruction
                if not reply and not plan.get("edit") and not plan.get("prompt"):
                    return jsonify({"error": "没理解这个需求，换个说法试试"}), 400
        if data.get("doc_mode"):
            # 文档模式：只编辑框选区域，区域外像素不动，PNG 无损保存
            if not rect_vals:
                return jsonify({"error": "文档模式请先在图片上框选要修改的区域"}), 400
            out, err = _ty_doc_edit(img, rect_vals, instruction
                                    + "。其余文字、排版和格式保持不变", doc_precise=True)
            if out is None:
                return jsonify({"error": err}), 502
            out.save(os.path.join(PREVIEW_DIR, token + ".png"), "PNG")
            return jsonify({"token": token, "ext": "png",
                            "reply": reply or "已只修改框选区域，其余内容未动"})
        refs_single = ([ref_image] if ref_image else []) + ref_images  # 单图参数+列表，最多2张
        refs_single = refs_single[:2]
        if refs_single:
            edit = (f"前面{len(refs_single)}张图片是要添加的人/物体的参考图，最后一张是底图。"
                    "请把参考图中的人/物体自然地加入底图，外形尽量贴近参考图，"
                    "大小、透视和光影与底图协调，底图其余内容保持原样。具体要求：" + instruction)
        if _use_wanx(refs_single):
            out, err = _wx_image_edit(img, edit)
        else:
            out, err = _ty_image_edit(img, edit, refs_single)
        if out is None:
            return jsonify({"error": err}), 502
        out.save(os.path.join(PREVIEW_DIR, token + ".jpg"), "JPEG", quality=95)
        return jsonify({"token": token, "reply": reply or "改好了，看看效果"})

    # 回退：MiniMax image-01 重绘
    plan = _ai_retouch_plan(img, instruction, history)
    if not plan:
        return jsonify({"error": "AI 理解失败，请换个说法再试"}), 502
    reply = (plan.get("reply") or "").strip()
    gen_prompt = (plan.get("prompt") or "").strip()
    if not gen_prompt:
        return jsonify({"error": reply or "没理解这个需求，换个说法试试"}), 400
    err = _mm_redraw(img, gen_prompt, cfg, token)
    if err:
        return jsonify({"error": err}), 502
    return jsonify({"token": token, "reply": reply or "改好了，看看效果"})


@app.route("/api/describe_image", methods=["POST"])
def api_describe_image():
    """理解整张图片：MiniMax 一两句话描述，上传后自动填入修改描述输入框"""
    data = request.get_json(silent=True) or {}
    p = _upload_path(os.path.basename(str(data.get("token") or "").strip()))
    if not p:
        return jsonify({"error": "图片不存在"}), 404
    img = Image.open(p).convert("RGB")
    desc = _mm_vision_text(
        "请用一两句话描述这张照片：主体是什么、场景环境、构图和色调。"
        "例如「一只金毛犬坐在秋天的公园里，背景是金黄色树叶，暖色调」。"
        "只输出描述本身，不要输出其他内容。", img, max_tokens=120)
    if not desc:
        return jsonify({"error": "AI 理解失败"}), 502
    return jsonify({"desc": desc.strip()})


@app.route("/api/describe_region", methods=["POST"])
def api_describe_region():
    """理解框选区域内容：裁剪（带少量上下文）→ MiniMax 一句话描述，供填入该块的输入框"""
    data = request.get_json(silent=True) or {}
    rv = _parse_rect(data.get("rect") or {})
    if not rv:
        return jsonify({"error": "框选无效"}), 400
    img = None
    base_token = os.path.basename(str(data.get("base_token") or "").strip())
    if base_token:
        for ext in (".jpg", ".png"):
            bp = os.path.join(PREVIEW_DIR, base_token + ext)
            if os.path.exists(bp):
                img = Image.open(bp).convert("RGB")
                break
    if img is None:
        p = _upload_path(os.path.basename(str(data.get("token") or "").strip()))
        if not p:
            return jsonify({"error": "图片不存在"}), 404
        img = Image.open(p).convert("RGB")
    rx, ry, rw, rh = rv
    W, H = img.size
    mx, my = rw * 0.1, rh * 0.1  # 带一点上下文，描述更准
    box = (max(0, int((rx - mx) * W)), max(0, int((ry - my) * H)),
           min(W, int((rx + rw + mx) * W + 0.5)), min(H, int((ry + rh + my) * H + 0.5)))
    crop = img.crop(box)
    desc = _mm_vision_text(
        "请用一两句话简要描述这张裁剪图中央区域的主要内容（主体是什么、在什么环境、什么颜色状态），"
        "例如「一只棕色的狗坐在草地上」。只输出描述本身，不要输出其他内容。",
        crop, max_tokens=100)
    if not desc:
        return jsonify({"error": "AI 理解失败"}), 502
    return jsonify({"desc": desc.strip()})


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8090"))
    bind = os.environ.get("BIND", "127.0.0.1")
    app.run(host=bind, port=port, threaded=True)
