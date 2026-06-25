"""PDF处理服务：文字坐标提取、表单域添加"""

import fitz  # PyMuPDF
import os
import base64
from io import BytesIO
from PIL import Image

from .field_matcher import match_fields_to_positions
from .table_detector import detect_table_structures


def pdf_to_images(pdf_path: str, dpi: float = 1.5) -> list[dict]:
    """将PDF每页转为base64图片"""
    doc = fitz.open(pdf_path)
    results = []
    for i in range(len(doc)):
        page = doc[i]
        mat = fitz.Matrix(dpi, dpi)
        pix = page.get_pixmap(matrix=mat)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        if img.width > 1200:
            ratio = 1200 / img.width
            img = img.resize((1200, int(img.height * ratio)), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=80, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode()
        results.append({
            "page": i, "width": img.width, "height": img.height,
            "pdf_width": page.rect.width, "pdf_height": page.rect.height,
            "base64": b64,
        })
    doc.close()
    return results


def get_pdf_info(pdf_path: str) -> dict:
    doc = fitz.open(pdf_path)
    pages = [{"page": i, "width": doc[i].rect.width, "height": doc[i].rect.height} for i in range(len(doc))]
    doc.close()
    return {"total_pages": len(pages), "pages": pages}


def extract_text_with_positions(pdf_path: str) -> list[dict]:
    """提取PDF所有文字及其精确坐标，按页和行分组"""
    doc = fitz.open(pdf_path)
    results = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        blocks = page.get_text("dict")["blocks"]
        for block in blocks:
            if "lines" not in block:
                continue
            for line in block["lines"]:
                line_text = ""
                line_spans = []
                for span in line["spans"]:
                    t = span["text"].strip()
                    if t:
                        line_text += " " + t if line_text else t
                        line_spans.append(span)
                for span in line["spans"]:
                    text = span["text"].strip()
                    if text:
                        bbox = span["bbox"]
                        results.append({
                            "page": page_num,
                            "text": text,
                            "x": bbox[0], "y": bbox[1],
                            "x1": bbox[2], "y1": bbox[3],
                            "width": bbox[2] - bbox[0],
                            "height": bbox[3] - bbox[1],
                            "line_text": line_text,
                        })
    doc.close()
    return results


def add_form_fields(input_pdf: str, output_pdf: str, fields: list[dict],
                     text_positions: list[dict] = None) -> str:
    """在PDF上添加填写内容（AI坐标字段用insert_htmlbox，文字匹配字段用widget）"""
    import tempfile
    import shutil

    doc = fitz.open(input_pdf)

    for field in fields:
        page_num = field.get("page", 0)
        if page_num >= len(doc):
            continue
        page = doc[page_num]
        value = field.get("value", "")
        if not value:
            continue

        x = field["x"]
        y = field["y"]
        w = field.get("width", 150)
        h = field.get("height", 12)

        if w <= 0 or h <= 0 or x < 0 or y < 0:
            print(f"  [跳过] 坐标无效: {field.get('label','')} x={x:.0f} y={y:.0f} w={w:.0f} h={h:.0f}")
            continue

        page_rect = doc[page_num].rect
        if x + w > page_rect.width + 10 or y + h > page_rect.height + 10:
            print(f"  [跳过] 超出页面: {field.get('label','')} x={x:.0f} y={y:.0f} w={w:.0f} h={h:.0f} page={page_rect.width:.0f}x{page_rect.height:.0f}")
            continue

        is_ai_coord = field.get("matched_text") == "AI坐标"

        MAX_FONT_SIZE = 12
        preset_font_size = field.get("font_size")
        if preset_font_size:
            font_size = min(preset_font_size, MAX_FONT_SIZE)
        else:
            max_font_size = 8
            min_font_size = 5.5
            char_width = 4.5
            content_w = len(value) * char_width + 10
            if content_w > w and len(value) > 0:
                font_size = max(min_font_size, (w - 10) / content_w * max_font_size)
            else:
                font_size = max_font_size
            font_size = min(font_size, MAX_FONT_SIZE)

        if is_ai_coord:
            # 扫描件PDF：用widget写文字，可编辑
            # 先移除页面旋转，写入后再恢复
            rotation = page.rotation
            if rotation != 0:
                page.set_rotation(0)
                # 旋转移除后页面尺寸变化，需要转换坐标
                # 原始横向(842x595) → 纵向(595x842)
                # 原始(x,y) → 纵向(y, 842-x-width)
                old_w = 842  # 横向宽度
                old_h = 595  # 横向高度
                new_x = y
                new_y = old_w - x - w
                new_w = h
                new_h = w
                x, y, w, h = new_x, new_y, new_w, new_h
                page_rect = page.rect
            
            try:
                widget = fitz.Widget()
                widget.field_name = field.get("label", f"f{page_num}_{int(x)}_{int(y)}")
                widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
                widget.field_value = value
                widget.field_flags = 0

                if len(value) > 40 or '\n' in value:
                    widget.field_flags = 1 << 12
                    estimated_lines = max(2, len(value) // 50 + 1)
                    h = max(h, estimated_lines * (font_size + 2))
                widget.rect = fitz.Rect(x, y, x + w, y + h)
                widget.text_fontsize = font_size
                widget.text_color = (0, 0, 0)
                widget.fill_color = None
                widget.border_color = None
                widget.border_width = 0
                widget.border_style = "none"
                page.add_widget(widget)
                print(f"  [写入] {field.get('label','')}: ({x:.0f},{y:.0f}) size={font_size:.1f}")
            except Exception as e:
                print(f"  [错误] 添加widget失败: {field.get('label','')} error={e}")
            
            # 恢复页面旋转
            if rotation != 0:
                page.set_rotation(rotation)
        else:
            # 有文字坐标的PDF：用widget保持可编辑
            widget = fitz.Widget()
            widget.field_name = field.get("label", f"f{page_num}_{int(x)}_{int(y)}")
            widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
            widget.field_value = value
            widget.field_flags = 0

            if len(value) > 40 or '\n' in value:
                widget.field_flags = 1 << 12
                estimated_lines = max(2, len(value) // 50 + 1)
                h = max(h, estimated_lines * (font_size + 2))
            widget.rect = fitz.Rect(x, y, x + w, y + h)
            widget.text_fontsize = font_size
            widget.text_color = (0, 0, 0)
            widget.fill_color = None
            widget.border_color = None
            widget.border_width = 0
            widget.border_style = "none"
            try:
                page.add_widget(widget)
            except ValueError as e:
                print(f"  [错误] 添加widget失败: {field.get('label','')} rect=({x:.0f},{y:.0f},{x+w:.0f},{y+h:.0f}) error={e}")
                continue

        # 恢复页面旋转
        if rotation != 0 and is_ai_coord:
            page.set_rotation(rotation)

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".pdf", dir=os.path.dirname(output_pdf))
    os.close(tmp_fd)
    try:
        doc.save(tmp_path)
        doc.close()
        if os.path.exists(output_pdf):
            try:
                os.remove(output_pdf)
            except PermissionError:
                output_pdf = tmp_path
                return output_pdf
        shutil.move(tmp_path, output_pdf)
    except Exception:
        doc.close()
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    return output_pdf
