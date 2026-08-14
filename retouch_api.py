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
        "（改颜色给出色相和程度，加/去物体说明位置和大小比例），结尾强调其余内容保持原样\", "
        "\"prompt\": \"给文生图模型的中文提示词：开头第一句明确写出要做的修改，"
        "然后用 80 字左右描述当前画面（主体、场景、构图、色调、风格）作为需要保持的内容\"}。"
        "如果需求无法理解，输出 {\"reply\": \"原因说明\", \"edit\": \"\", \"prompt\": \"\"}。"
        "不要输出任何其他内容。", img, max_tokens=600)


def _ty_image_edit(img, edit, ref_image=""):
    """通义 qwen-image-edit-plus：base64 传图 + 编辑指令。
    ref_image 为可选参考图：单个 data URI 或列表（最多取 2 张，通义限制总图数 ≤3）。
    参考图在前、底图在后（输出比例以最后一张图为准）。成功返回 (PIL图, None)，失败 (None, 错误)。"""
    cfg = _load_cfg()
    key = cfg.get("ty_api_key", "")
    if not key:
        return None, "未配置通义 API Key（~/image_analyzer_config.json 的 ty_api_key）"
    buf = io.BytesIO()
    pic = img.convert("RGB").copy()
    pic.thumbnail((2048, 2048))  # 官方建议宽高均在 384~3072 之间
    pic.save(buf, "JPEG", quality=90)
    b64 = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    if isinstance(ref_image, (list, tuple)):
        refs = [r for r in ref_image if str(r).startswith("data:image")]
    else:
        refs = [ref_image] if ref_image else []
    content = [{"image": r} for r in refs[:2]]
    content += [{"image": b64}, {"text": edit}]
    import requests as _req
    try:
        r = _req.post("https://dashscope.aliyuncs.com/api/v1/services/aigc/"
                      "multimodal-generation/generation",
                      json={"model": "qwen-image-edit-plus",
                            "input": {"messages": [{"role": "user", "content": content}]},
                            "parameters": {"n": 1, "watermark": False,
                                           "prompt_extend": True}},
                      headers={"Authorization": "Bearer " + key}, timeout=300)
        if r.status_code != 200:
            return None, f"通义编辑失败 {r.status_code}: {r.text[:200]}"
        content = r.json()["output"]["choices"][0]["message"]["content"]
        url = next((c.get("image") for c in content if c.get("image")), None)
        if not url:
            return None, "通义未返回图片"
        ir = _req.get(url, timeout=120)  # 24 小时有效的临时 URL，需立即下载
        if ir.status_code != 200:
            return None, "通义结果图下载失败"
        return Image.open(io.BytesIO(ir.content)).convert("RGB"), None
    except Exception as e:
        return None, f"通义编辑异常: {e}"


def _ty_doc_edit(img, rect_vals, instruction, ref_images=None):
    """区域编辑：框选区域裁出 → 通义编辑 → 原样贴回（边缘羽化），区域外像素完全不变。
    ref_images 可选：要加入的人/物体参考图（最多 2 张）。"""
    from PIL import ImageDraw, ImageFilter
    rx, ry, rw, rh = rect_vals
    W, H = img.size
    x0, y0 = int(rx * W), int(ry * H)
    x1, y1 = min(W, int((rx + rw) * W + 0.5)), min(H, int((ry + rh) * H + 0.5))
    cw, ch = x1 - x0, y1 - y0
    if cw < 8 or ch < 8:
        return None, "框选区域太小，请框大一点"
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
    edits（批量区域修改 [{rect,instruction}]）、ref_images（多参考图列表）"""
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
            # 批量区域修改：逐块 裁剪→通义编辑→羽化贴回（区域外像素不动），PNG 输出
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
                if ref_images:
                    instr = (f"前面{len(ref_images)}张图片是要添加的人/物体的参考图，"
                             "最后一张是要修改的图。把参考图中的人/物体自然地加入图中，"
                             "外形尽量贴近参考图，大小、透视和光影与底图协调。"
                             "具体要求：" + instr)
                out2, err = _ty_doc_edit(out, rv, instr, ref_images)
                if out2 is None:
                    results.append(f"区域{i + 1}失败：{err}")
                else:
                    out = out2
                    done.append(i + 1)
            if not done:
                return jsonify({"error": "修改失败：" + "；".join(results)}), 502
            out.save(os.path.join(PREVIEW_DIR, token + ".png"), "PNG")
            reply = f"已完成 {len(done)}/{min(len(edits), 6)} 块区域修改"
            if results:
                reply += "（" + "；".join(results) + "）"
            return jsonify({"token": token, "ext": "png", "reply": reply})
        reply = ""
        edit = instruction
        if api_key:  # 理解层失败不至于不能用——退回用户原话当编辑指令
            plan = _ai_retouch_plan(img, instruction, history)
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
                                    + "。其余文字、排版和格式保持不变")
            if out is None:
                return jsonify({"error": err}), 502
            out.save(os.path.join(PREVIEW_DIR, token + ".png"), "PNG")
            return jsonify({"token": token, "ext": "png",
                            "reply": reply or "已只修改框选区域，其余内容未动"})
        if ref_image:
            edit = ("第一张图片是要添加的人/物体的参考图，第二张图片是底图。"
                    "请把参考图中的人/物体自然地加入底图，外形尽量贴近参考图，"
                    "大小、透视和光影与底图协调，底图其余内容保持原样。具体要求：" + instruction)
        out, err = _ty_image_edit(img, edit, ref_image)
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


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8090"))
    bind = os.environ.get("BIND", "127.0.0.1")
    app.run(host=bind, port=port, threaded=True)
