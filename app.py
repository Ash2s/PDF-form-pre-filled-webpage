"""PDF表格填写工具 - FastAPI主程序（支持多表单）"""

from dotenv import load_dotenv
load_dotenv()

import os
import uuid
import json
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from services.pdf_service import (
    pdf_to_images,
    get_pdf_info,
    add_form_fields,
    extract_text_with_positions,
    match_fields_to_positions,
)
from services.extractor import detect_form_fields, extract_student_info, verify_form_positions
from services.mimo import analyze_image_json, analyze_text, _extract_json

app = FastAPI(title="PDF表格填写工具")

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "outputs")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


sessions: dict[str, dict] = {}


def _get_form(session, form_index=0):
    """获取多表单中的指定表单，兼容旧格式"""
    forms = session.get("forms", [])
    # 兼容旧版单表单格式
    if not forms and "form_fields" in session:
        forms.append({
            "name": session.get("pdf_name", "form.pdf"),
            "form_fields": session.get("form_fields", []),
            "matched_fields": session.get("matched_fields", []),
            "pdf_info": session.get("pdf_info", {}),
            "pdf_path": session.get("pdf_path", ""),
            "text_positions": session.get("text_positions", []),
        })
        session["forms"] = forms
    if form_index < 0 or form_index >= len(forms):
        raise HTTPException(400, f"表单索引 {form_index} 超出范围 (共 {len(forms)} 个)")
    return forms[form_index]


# ─── 上传学生资料 ───
@app.post("/api/upload-student")
async def upload_student(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in [".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg", ".txt"]:
        raise HTTPException(400, "不支持的文件格式")

    session_id = str(uuid.uuid4())[:8]
    file_path = os.path.join(UPLOAD_DIR, f"student_{session_id}{ext}")
    content = await file.read()
    with open(file_path, "wb") as f:
        f.write(content)

    from datetime import date
    today = date.today()

    def _calc_age(info_dict):
        dob = info_dict.get("dob", "")
        if not dob:
            return
        try:
            parts = dob.replace("/", " ").replace("-", " ").replace(".", " ").split()
            if len(parts) >= 3:
                d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
                if y < 100:
                    y += 2000
                age = today.year - y
                if (m, d) > (today.month, today.day):
                    age -= 1
                info_dict["age"] = str(age)
                print(f"  计算年龄: {dob} → {age}岁")
        except:
            pass

    if ext == ".pdf":
        images = pdf_to_images(file_path, dpi=1.5)
        info = extract_student_info(images[0]["base64"])
        _calc_age(info)
    elif ext in [".docx", ".doc"]:
        import docx2txt
        text = docx2txt.process(file_path)
        if not text.strip():
            raise HTTPException(400, "无法从文档中提取文本")
        from services.extractor import STUDENT_EXTRACT_PROMPT
        from services.mimo import analyze_text
        prompt = f"{STUDENT_EXTRACT_PROMPT}\n\n以下是学生文档内容：\n{text[:3000]}"
        result = analyze_text(prompt)
        info = _extract_json(result)
        _calc_age(info)
    elif ext == ".txt":
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
        if not text.strip():
            raise HTTPException(400, "TXT文件内容为空")
        from services.extractor import STUDENT_EXTRACT_PROMPT
        from services.mimo import analyze_text
        prompt = f"{STUDENT_EXTRACT_PROMPT}\n\n以下是学生资料文本内容：\n{text[:3000]}"
        result = analyze_text(prompt)
        info = _extract_json(result)
        _calc_age(info)
    else:
        from PIL import Image
        import base64
        from io import BytesIO
        img = Image.open(file_path)
        if img.width > 1024:
            ratio = 1024 / img.width
            img = img.resize((1024, int(img.height * ratio)), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="PNG", optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode()
        info = extract_student_info(b64)
        _calc_age(info)

    if session_id not in sessions:
        sessions[session_id] = {"student_info": {}, "forms": []}
    sessions[session_id]["student_info"] = info

    return {"session_id": session_id, "student_info": info}


# ─── 上传PDF申请表（追加到表单列表）───
@app.post("/api/upload-form")
async def upload_form(
    file: UploadFile = File(...),
    session_id: str = Form(default=""),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "请上传PDF文件")

    if not session_id:
        session_id = str(uuid.uuid4())[:8]

    # 用时间戳区分同一session的多个表单
    ts = uuid.uuid4().hex[:6]
    file_path = os.path.join(UPLOAD_DIR, f"form_{session_id}_{ts}.pdf")
    content = await file.read()
    with open(file_path, "wb") as f:
        f.write(content)

    pdf_info = get_pdf_info(file_path)
    text_positions = extract_text_with_positions(file_path)
    print(f"提取到 {len(text_positions)} 个文字元素")

    student = sessions.get(session_id, {}).get("student_info", {})
    images = pdf_to_images(file_path)
    all_ai_fields = []
    all_activities = student.get("activities", [])

    for img_data in images:
        page_num = img_data["page"]
        try:
            fields, img_activities = detect_form_fields(img_data["base64"], student_info=student)
            for f in fields:
                f["page"] = page_num
                # 传递PDF页面尺寸信息用于坐标转换
                f["pdf_width"] = img_data.get("pdf_width", 595)
                f["pdf_height"] = img_data.get("pdf_height", 842)
                if not f.get("value"):
                    f["_skip"] = False
            all_ai_fields.extend([f for f in fields if not f.get("_skip")])
            if not all_activities and img_activities:
                all_activities = img_activities
            filled = sum(1 for f in fields if f.get("value"))
            print(f"  第{page_num+1}页: AI识别 {len(fields)} 个字段, 已填充 {filled} 个")
        except Exception as e:
            print(f"  第{page_num+1}页识别失败: {e}")
            continue

    print(f"AI共识别 {len(all_ai_fields)} 个字段")
    matched = match_fields_to_positions(all_ai_fields, text_positions)
    print(f"成功匹配 {len(matched)} 个字段到PDF坐标")

    # 追加到表单列表
    if session_id not in sessions:
        sessions[session_id] = {"student_info": {}, "forms": []}
    if "forms" not in sessions[session_id]:
        sessions[session_id]["forms"] = []

    form_entry = {
        "name": file.filename,
        "form_fields": all_ai_fields,
        "matched_fields": matched,
        "pdf_info": pdf_info,
        "pdf_path": file_path,
        "text_positions": text_positions,
    }
    sessions[session_id]["forms"].append(form_entry)
    form_index = len(sessions[session_id]["forms"]) - 1

    return {
        "session_id": session_id,
        "form_index": form_index,
        "form_name": file.filename,
        "pdf_info": pdf_info,
        "fields": all_ai_fields,
        "matched_count": len(matched),
        "total_fields": len(all_ai_fields),
    }


# ─── 获取表单列表 ───
@app.get("/api/forms/{session_id}")
async def list_forms(session_id: str):
    if session_id not in sessions:
        raise HTTPException(404, "会话不存在")
    forms = sessions[session_id].get("forms", [])
    result = []
    for i, f in enumerate(forms):
        fields = f.get("form_fields", [])
        filled = sum(1 for ff in fields if ff.get("value"))
        result.append({
            "index": i,
            "name": f.get("name", f"表单{i+1}"),
            "total_fields": len(fields),
            "filled_fields": filled,
        })
    return {"forms": result, "total": len(result)}


# ─── AI对话调整 ───
class ChatRequest(BaseModel):
    message: str
    session_id: str
    form_index: int = 0


@app.post("/api/chat")
async def chat(req: ChatRequest):
    if req.session_id not in sessions:
        raise HTTPException(404, "会话不存在")

    session = sessions[req.session_id]
    form = _get_form(session, req.form_index)
    student = session.get("student_info", {})
    student_context = json.dumps(student, ensure_ascii=False, indent=2)
    fields_context = json.dumps(
        [{"label": f["label"], "value": f.get("value", "")} for f in form["form_fields"]],
        ensure_ascii=False, indent=2
    )

    prompt = f"""你是PDF表格填写助手。用户可能让你修改、添加或删除字段。

学生资料：
{student_context}

当前表格字段（label是字段名，value是当前值，空字符串表示未填写）：
{fields_context}

用户指令：{req.message}

你可以执行以下操作：
1. 修改字段值：把某个字段改成新值
2. 填充空白字段：根据学生资料填写空白的字段
3. 添加新字段：只有字段确实不存在时才添加
4. 删除字段：移除不需要的字段

成绩字段的中英文映射（用户说中文名时，对应以下英文label中的关键词）：
- 中文 → Chinese
- 英文 → English
- 数学 → Mathematics
- 操行/品行 → Conduct
- 平均分 → Average Mark

【重要】用户说"把中文/英文/数学等填成XX"时：
- 这是 update 操作（不是 add），因为成绩字段已存在于列表中
- 必须更新所有匹配的字段，包括 2024-2025 和 2025-2026 两个学年，每个学年 First Term 和 Second Term
- 例如"中文填A" → 要同时更新 Chinese (2025 – 2026 First Term)、Chinese (2025 – 2026 Second Term)、Chinese (2024 – 2025 First Term)、Chinese (2024 – 2025 Second Term) 全部为A
- label 必须使用字段列表中的完整label文字（包括空格和括号）

返回JSON格式：
{{"action": "update", "fields": [{{"label": "字段的完整label", "value": "新值"}}], "message": "说明做了什么"}}
或 {{"action": "add", "fields": [{{"label": "字段名", "value": "新值"}}], "message": "说明"}}
或 {{"action": "delete", "fields": ["字段名"], "message": "说明"}}
或 {{"action": "none", "message": "说明为什么无法完成，以及建议用户如何手动操作"}}

注意：
- 用户说"成绩"、"填成绩"要执行 update，不是 add
- 删除时label必须与字段列表大小写不敏感匹配
- 推理填写：如"与学生关系"可推断为"父女/父子"
- 【语气规则】用户明确提出修改要求后，如果无法找到匹配字段或无法修改：
  · 不要说"无需修改"、"已全部填充"之类的话
  · 要诚恳说明原因，建议用户手动填写或提供更具体的描述
直接返回JSON。"""

    try:
        response_text = analyze_text(prompt)
        result = _extract_json(response_text)
        action = result.get("action", "none")
        message = result.get("message", "")
        form_fields = form["form_fields"]
        pdf_path = form["pdf_path"]

        if action == "update":
            import re as _re
            for update in result.get("fields", []):
                label = update.get("label", "").strip()
                value = update.get("value", "")
                if not label:
                    continue
                label_lower = label.lower()
                for field in form_fields:
                    field_label = field["label"].strip()
                    if field_label.lower() == label_lower:
                        field["value"] = value
                        continue
                    base = _re.sub(r'\s*\(.*?\)\s*', '', field_label).strip().lower()
                    if label_lower == base:
                        field["value"] = value
                        continue
                    if len(label_lower) >= 3 and label_lower in field_label.lower():
                        field["value"] = value
                        continue
            text_positions = extract_text_with_positions(pdf_path)
            form["matched_fields"] = match_fields_to_positions(form_fields, text_positions)
            form["text_positions"] = text_positions
            filled_count = len(result.get("fields", []))
            return {"status": "ok", "action": "updated", "message": message or f"已更新{filled_count}个字段", "fields": form_fields}

        elif action == "delete":
            for label in result.get("fields", []):
                form["form_fields"] = [f for f in form_fields if f["label"].lower() != label.lower()]
            text_positions = extract_text_with_positions(pdf_path)
            form["matched_fields"] = match_fields_to_positions(form_fields, text_positions)
            form["text_positions"] = text_positions
            return {"status": "ok", "action": "deleted", "message": message or "已删除", "fields": form_fields}

        elif action == "add":
            new_count = 0
            import re as _re
            for new_field in result.get("fields", []):
                label = new_field.get("label", "").strip()
                value = new_field.get("value", "").strip()
                if not label:
                    continue
                label_lower = label.lower()
                exists = any(f["label"].strip().lower() == label_lower for f in form_fields)
                if not exists:
                    form_fields.append({
                        "label": label, "value": value, "page": 0,
                    })
                    new_count += 1
                else:
                    for f in form_fields:
                        if f["label"].strip().lower() == label_lower:
                            f["value"] = value
                            break
            return {"status": "ok", "action": "added", "message": message or f"已添加{new_count}个新字段", "fields": form_fields}

        else:
            return {"status": "ok", "action": "none", "message": message or "已尽力查找，但当前识别的字段中没有找到匹配项。建议在下载PDF后手动填写，或提供更具体的字段名描述。", "fields": form_fields}
    except Exception as e:
        raise HTTPException(500, f"AI处理失败: {str(e)}")


# ─── 生成填写版PDF ───
class GeneratePDFRequest(BaseModel):
    session_id: str
    form_index: int = 0


@app.post("/api/generate-pdf")
async def generate_pdf(req: GeneratePDFRequest):
    if req.session_id not in sessions:
        raise HTTPException(404, "会话不存在")

    session = sessions[req.session_id]
    form = _get_form(session, req.form_index)
    pdf_path = form["pdf_path"]
    if not pdf_path or not os.path.exists(pdf_path):
        raise HTTPException(400, "PDF文件不存在")

    output_path = os.path.join(OUTPUT_DIR, f"filled_{req.session_id}_{req.form_index}.pdf")

    text_positions = extract_text_with_positions(pdf_path)
    form_fields = form.get("form_fields", [])
    matched = match_fields_to_positions(form_fields, text_positions)
    form["matched_fields"] = matched
    form["text_positions"] = text_positions

    add_form_fields(pdf_path, output_path, matched, text_positions=text_positions)

    # 视觉审核
    try:
        images = pdf_to_images(output_path, dpi=1.5)
        all_corrections = []
        for img_data in images:
            page_num = img_data["page"]
            page_fields = [f for f in matched if f.get("page") == page_num and f.get("value")]
            if not page_fields:
                continue
            print(f"  视觉审核: 检查第{page_num+1}页 ({len(page_fields)}个字段)...")
            corrections = verify_form_positions(img_data["base64"], page_fields)
            if corrections:
                print(f"    发现 {len(corrections)} 个问题")
                for corr in corrections:
                    print(f"    - {corr.get('label','')}: {corr.get('issue','')}")
                all_corrections.extend(corrections)

        if all_corrections:
            print(f"  视觉审核共发现 {len(all_corrections)} 个问题，正在修正...")
            for corr in all_corrections:
                label = corr.get("label", "")
                for field in matched:
                    if field["label"].lower() == label.lower():
                        if "fix_x" in corr:
                            field["x"] = corr["fix_x"]
                        if "fix_y" in corr:
                            field["y"] = corr["fix_y"]
                        if "fix_w" in corr:
                            field["width"] = corr["fix_w"]
            add_form_fields(pdf_path, output_path, matched, text_positions=text_positions)
        else:
            print("  视觉审核: 所有页面通过")
    except Exception as e:
        print(f"  视觉审核异常: {type(e).__name__}: {e}")

    return FileResponse(output_path, media_type="application/pdf", filename="filled_form.pdf")


# ─── PDF预览 ───
@app.get("/api/pdf-preview/{session_id}")
async def pdf_preview(session_id: str, page: int = 0, form_index: int = 0):
    if session_id not in sessions:
        raise HTTPException(404, "会话不存在")

    form = _get_form(sessions[session_id], form_index)
    filled_path = os.path.join(OUTPUT_DIR, f"filled_{session_id}_{form_index}.pdf")
    pdf_path = filled_path if os.path.exists(filled_path) else form.get("pdf_path")

    if not pdf_path or not os.path.exists(pdf_path):
        raise HTTPException(400, "PDF文件不存在")

    images = pdf_to_images(pdf_path)
    if page >= len(images):
        raise HTTPException(400, "页码超出范围")

    img_data = images[page]
    return JSONResponse({
        "image": f"data:image/jpeg;base64,{img_data['base64']}",
        "width": img_data["width"],
        "height": img_data["height"],
    })


@app.get("/api/pdf-info/{session_id}")
async def pdf_info(session_id: str, form_index: int = 0):
    if session_id not in sessions:
        raise HTTPException(404, "会话不存在")
    
    form = _get_form(sessions[session_id], form_index)
    filled_path = os.path.join(OUTPUT_DIR, f"filled_{session_id}_{form_index}.pdf")
    pdf_path = filled_path if os.path.exists(filled_path) else form.get("pdf_path")
    
    if not pdf_path or not os.path.exists(pdf_path):
        raise HTTPException(400, "PDF文件不存在")
    
    pdf_info = get_pdf_info(pdf_path)
    return JSONResponse(pdf_info)


# ─── 直接下载已生成的PDF ───
@app.get("/api/download-pdf/{session_id}")
async def download_pdf(session_id: str, form_index: int = 0):
    if session_id not in sessions:
        raise HTTPException(404, "会话不存在")

    form = _get_form(sessions[session_id], form_index)
    filled_path = os.path.join(OUTPUT_DIR, f"filled_{session_id}_{form_index}.pdf")

    if not os.path.exists(filled_path):
        pdf_path = form.get("pdf_path")
        if not pdf_path or not os.path.exists(pdf_path):
            raise HTTPException(400, "PDF文件不存在")
        matched = form.get("matched_fields", [])
        text_positions = form.get("text_positions", [])
        add_form_fields(pdf_path, filled_path, matched, text_positions=text_positions)

    return FileResponse(filled_path, media_type="application/pdf", filename=f"filled_{form.get('name', 'form')}")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8001"))
    uvicorn.run(app, host="0.0.0.0", port=port)
