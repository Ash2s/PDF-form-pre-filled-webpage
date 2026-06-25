"""字段到坐标的匹配"""
import re
from .config import Y_TOLERANCE, X_TOLERANCE, DISTANCE_THRESHOLD, TABLE_ROW_HEIGHT, PAGE_WIDTH, PAGE_LEFT_MARGIN
from .aliases import resolve_ai_label, LABEL_ALIASES, ROW_ALIASES
from .table_detector import detect_table_structures


def _normalize_text(text: str) -> str:
    """统一文本规范化：小写、去标点、压缩空格，兼容中英文"""
    t = text.lower().strip()
    t = re.sub(r'[()（）\[\]【】：:\.,/]', '', t)
    t = t.replace("、", " ").replace("，", " ").replace("。", "")
    t = " ".join(t.split())
    return t


def _strip_punct(text: str) -> str:
    """去掉所有标点符号，用于列名模糊匹配"""
    return re.sub(r'[^\w\s]', '', text).strip()


def _score_match(label_norm: str, tp_norm: str) -> int:
    """计算标签与PDF文字的匹配分数"""
    def _is_cjk(s):
        return any('一' <= c <= '鿿' for c in s)
    if len(tp_norm) < 2 or (len(tp_norm) < 3 and not _is_cjk(tp_norm)):
        return 0
    if len(label_norm) < 2 or (len(label_norm) < 3 and not _is_cjk(label_norm)):
        return 0

    if label_norm == tp_norm:
        return 150
    if tp_norm.startswith(label_norm) or label_norm.startswith(tp_norm):
        return 120
    if label_norm in tp_norm or tp_norm in label_norm:
        ratio = len(tp_norm) / max(len(label_norm), 1)
        if ratio > 5:
            return 20
        elif ratio > 3:
            return 40
        return 80
    label_words = set(label_norm.split())
    tp_words = set(tp_norm.split())
    overlap = label_words & tp_words
    if len(overlap) >= 1 and len(label_words) > 0:
        coverage = len(overlap) / len(label_words)
        return int(coverage * 70)
    if " " not in label_norm and " " not in tp_norm:
        common = sum(1 for c in label_norm if c in tp_norm)
        if common >= 2 and common / len(label_norm) > 0.4:
            return common * 15
    return 0


def _normalize_label_for_dedup(label: str) -> str:
    """归一化标签用于去重：去掉'in'、排序单词、统一括号格式"""
    l = label.lower().strip()
    suffix = ""
    m = re.search(r'\s*\(([^)]+)\)\s*$', l)
    if m:
        suffix = f" ({m.group(1)})"
        l = l[:m.start()]
    words = [w for w in l.split() if w != "in"]
    words.sort()
    return " ".join(words) + suffix


def _calc_table_font(value: str, col_width: float, max_fs: float = 10, min_fs: float = 5) -> float:
    """计算表格单元格字号：最大max_fs，超长自适应缩小"""
    content_w = sum(max_fs * (1.1 if ord(c) > 127 else 0.55) for c in value) + 4
    if content_w > col_width and len(value) > 0:
        return max(min_fs, (col_width - 4) / (content_w - 4) * max_fs)
    return max_fs


def _adjust_for_bilingual(label_match: dict, all_spans: list[dict]) -> dict:
    """
    双语标签定位修正：检测匹配文字上方是否有中文标签，如有则上移y坐标。
    PDF双语布局：中文在上，英文在下，表单域应填在中文标签旁边。
    """
    page = label_match["page"]
    match_y = label_match["y"]
    match_x = label_match["x"]

    best = None
    best_dist = 999
    for sp in all_spans:
        if sp["page"] != page:
            continue
        dist = match_y - sp["y"]
        if dist <= 0 or dist > 12:
            continue
        if sp["x1"] < match_x - 10 or sp["x"] > match_x + 50:
            continue
        has_chinese = any(ord(c) > 0x4e00 for c in sp["text"])
        if not has_chinese:
            continue
        if dist < best_dist:
            best_dist = dist
            best = sp

    if best:
        adjusted = dict(label_match)
        adjusted["y"] = best["y"] - 1
        adjusted["y1"] = best["y1"]
        return adjusted

    return label_match


def _find_blank_position(label_match: dict, all_spans: list[dict],
                         label_text: str = "") -> dict:
    """
    精确定位填写区域：找到标签右侧的空白区域。
    """
    page = label_match["page"]
    label_y_center = (label_match["y"] + label_match["y1"]) / 2
    label_height = label_match["height"]
    page_width = PAGE_WIDTH

    matched_text = label_match["text"]
    is_prefix = (label_text and matched_text.lower().startswith(label_text.lower())
                 and len(matched_text) > len(label_text))
    if is_prefix:
        label_ratio = len(label_text) / max(len(matched_text), 1)
        label_x1 = label_match["x"] + (label_match["x1"] - label_match["x"]) * label_ratio
    else:
        label_x1 = label_match["x1"]

    label_width = label_x1 - label_match["x"]
    if label_width > page_width * 0.30:
        has_right_neighbor = False
        for sp in all_spans:
            if sp["page"] != page:
                continue
            sp_y_center = (sp["y"] + sp["y1"]) / 2
            if abs(sp_y_center - label_y_center) < max(label_height * 1.5, 20):
                if sp["x"] > label_x1 + 5:
                    has_right_neighbor = True
                    break

        if not has_right_neighbor:
            form_x = PAGE_LEFT_MARGIN
            form_width = page_width - PAGE_LEFT_MARGIN - 10
            next_text_y = label_match["y"] + 100
            for sp in all_spans:
                if sp["page"] != page:
                    continue
                if sp["y"] > label_match["y1"] + 5 and sp["x"] < 100:
                    next_text_y = min(next_text_y, sp["y"])
                    break
            form_height = max(20, next_text_y - label_match["y1"] - 5)
            return {
                "x": form_x,
                "y": label_match["y1"] + 2,
                "width": form_width,
                "height": form_height,
            }

    same_line_after = []
    for sp in all_spans:
        if sp["page"] != page:
            continue
        sp_y_center = (sp["y"] + sp["y1"]) / 2
        if abs(sp_y_center - label_y_center) < max(label_height * 1.2, 15):
            if sp["x"] > label_x1 + 2:
                same_line_after.append(sp)

    same_line_after.sort(key=lambda s: s["x"])

    form_x = label_x1 + 2

    if same_line_after:
        boundary_x = same_line_after[0]["x"]
        for i in range(1, len(same_line_after)):
            gap = same_line_after[i]["x"] - same_line_after[i-1]["x1"]
            if gap > 20:
                break
            boundary_x = same_line_after[i]["x"]
        max_right = boundary_x - 3
        form_width = max(30, max_right - form_x)
    else:
        page_right = PAGE_WIDTH
        form_width = max(180, page_right - form_x)

    return {
        "x": form_x,
        "y": label_match["y"] - 1,
        "width": form_width,
        "height": label_height + 2,
    }


def _detect_table_columns(text_positions: list[dict], page: int) -> list[dict]:
    """检测页面中的表格列结构（通过寻找水平排列的表头）"""
    page_spans = [t for t in text_positions if t["page"] == page]
    column_headers = ["father", "mother", "guardian", "父亲", "母親", "母亲", "監護人", "监护人"]
    columns = []
    for sp in page_spans:
        text_lower = sp["text"].lower().strip()
        def _match_header(text, ch):
            if any(ord(c) > 0x4e00 for c in ch):
                return ch in text
            return re.search(r'\b' + re.escape(ch) + r'\b', text) is not None
        if any(_match_header(text_lower, ch) for ch in column_headers):
            columns.append({
                "header": sp["text"].strip(),
                "x": sp["x"],
                "x1": sp["x1"],
                "y": sp["y"],
                "y_center": (sp["y"] + sp["y1"]) / 2,
            })
    columns.sort(key=lambda c: c["x"])
    return columns


def _find_table_cell(row_label_match: dict, column_header: str,
                     text_positions: list[dict], page: int) -> dict | None:
    """在表格中找到特定行和列交叉处的单元格区域"""
    columns = _detect_table_columns(text_positions, page)
    if not columns:
        return None

    col_aliases = {
        "父亲": "father", "母亲": "mother", "监护人": "guardian",
        "father": "father", "mother": "mother", "guardian": "guardian",
    }
    target_col = None
    for col in columns:
        header_lower = col["header"].lower().strip()
        if header_lower == column_header.lower():
            target_col = col
            break
        if col_aliases.get(header_lower) == col_aliases.get(column_header.lower()):
            target_col = col
            break
    if not target_col:
        return None

    col_idx = columns.index(target_col)
    if col_idx > 0:
        left_boundary = (columns[col_idx - 1]["x1"] + target_col["x"]) / 2
    else:
        left_boundary = target_col["x"] - 10

    if col_idx < len(columns) - 1:
        right_boundary = (target_col["x1"] + columns[col_idx + 1]["x"]) / 2
    else:
        right_boundary = target_col["x1"] + 150

    row_y = row_label_match["y"]
    row_height = row_label_match["height"]

    return {
        "x": left_boundary + 2,
        "y": row_y - 1,
        "width": right_boundary - left_boundary - 4,
        "height": row_height + 2,
    }


def match_fields_to_positions(fields: list[dict], text_positions: list[dict]) -> list[dict]:
    """将AI字段匹配到PDF文字坐标，精确定位填写区域"""
    result = []

    # 去重
    seen_norm: dict[str, dict] = {}
    for field in fields:
        if not isinstance(field, dict):
            continue
        label = field.get("label", "").strip()
        if not label:
            continue
        norm = _normalize_label_for_dedup(label)
        if norm not in seen_norm:
            seen_norm[norm] = [field]
        else:
            seen_norm[norm].append(field)

    unique_fields = []
    for norm, field_list in seen_norm.items():
        with_value = [f for f in field_list if f.get("value")]
        if not with_value:
            unique_fields.append(field_list[0])
            continue
        def _text_exists_on_page(field):
            label_norm = _normalize_text(field.get("label", ""))
            page = field.get("page", 0)
            for tp in text_positions:
                if tp["page"] == page:
                    if _score_match(label_norm, _normalize_text(tp["text"])) >= 80:
                        return True
            return False
        existing = [f for f in with_value if _text_exists_on_page(f)]
        if existing:
            best = min(existing, key=lambda f: f.get("page", 99))
        else:
            best = min(with_value, key=lambda f: f.get("page", 99))
        unique_fields.append(best)
    print(f"  [去重] {len(fields)} → {len(unique_fields)} 个字段")

    skip_keywords = ["signature", "签署", "签章", "签名", "applicant.*sign", "parent.*sign",
                     "已提交", "copy of", "copies of", "bank-in", "payment",
                     "tick", "勾", "checkbox", " checklist"]

    all_table_headers = {}
    pages_with_tables = set(f.get("page", 0) for f in unique_fields)
    for p in pages_with_tables:
        tables = detect_table_structures(text_positions, p)
        headers = []
        for t in tables:
            for col in t["columns"]:
                headers.append(_normalize_text(col["label"]))
        all_table_headers[p] = headers

    for field in unique_fields:
        raw_label = field.get("label", "")
        if raw_label in LABEL_ALIASES:
            field = dict(field)
            field["label"] = LABEL_ALIASES[raw_label]
        if not isinstance(field, dict):
            continue
        label = field.get("label", "")
        value = field.get("value", "")
        field_page = field.get("page", 0)
        if not value or not label:
            continue

        label_lower = label.lower()
        if any(kw in label_lower for kw in skip_keywords):
            print(f"  [跳过] 签署类: {label}")
            continue

        if field_page in all_table_headers:
            label_norm = _normalize_text(label)
            if label_norm in all_table_headers[field_page]:
                print(f"  [跳过] 表头字段: {label}")
                continue

        # 如果字段已有坐标（扫描件PDF），直接使用，跳过文字匹配
        if all(k in field for k in ["x", "y", "w", "h"]):
            x_val = field.get("x", 0)
            y_val = field.get("y", 0)
            w_val = field.get("w", 0)
            h_val = field.get("h", 0)
            
            # 获取PDF页面尺寸（优先使用字段中携带的尺寸信息）
            page_width = field.get("pdf_width", PAGE_WIDTH)
            page_height = field.get("pdf_height", 800)
            
            # 如果字段中没有尺寸信息，尝试从text_positions获取
            if not field.get("pdf_width"):
                for tp in text_positions:
                    if tp.get("page") == field_page:
                        page_width = max(page_width, tp.get("x1", 0) + 50)
                        page_height = max(page_height, tp.get("y1", 0) + 50)
                        break
            
            # 判断坐标类型：如果值>100认为是像素坐标，否则是百分比
            if x_val > 100 or y_val > 100:
                # 像素坐标，需要根据图片尺寸转换为PDF坐标
                # 图片尺寸约 1200x848（横向A4，1.5倍DPI）
                img_width = 1200
                img_height = 848
                actual_x = x_val * page_width / img_width
                actual_y = y_val * page_height / img_height
                actual_w = w_val * page_width / img_width
                actual_h = h_val * page_height / img_height
            else:
                # 百分比坐标
                actual_x = page_width * x_val / 100
                actual_y = page_height * y_val / 100
                actual_w = page_width * w_val / 100
                actual_h = page_height * h_val / 100
            
            result.append({
                "label": label,
                "value": value,
                "x": actual_x,
                "y": actual_y,
                "width": max(30, actual_w),
                "height": max(12, actual_h),
                "page": field_page,
                "font_size": 8,
                "matched_text": "AI坐标",
                "match_score": 100,
            })
            print(f"  [AI坐标] {label}: x={actual_x:.0f}, y={actual_y:.0f}, w={actual_w:.0f}, h={actual_h:.0f} (pdf={page_width:.0f}x{page_height:.0f})")
            continue

        label_norm = _normalize_text(label)

        # ═══ 通用表格字段匹配 ═══
        base_label = label
        qualifier = None
        qualifier_type = None

        m1 = re.match(r'^(\d+)[.\s](.+)$', label)
        if m1:
            qualifier = int(m1.group(1))
            qualifier_type = "row_num"
            base_label = m1.group(2).strip()
        else:
            m2 = re.match(r'^(.+?)\s*\(([^)]+)\)\s*$', label)
            if m2:
                base_label = m2.group(1).strip()
                paren_content = m2.group(2).strip()
                if re.match(r'^\d+$', paren_content):
                    qualifier = int(paren_content)
                    qualifier_type = "row_num"
                elif re.match(r'^\d{4}', paren_content):
                    qualifier = paren_content
                    qualifier_type = "year"
                else:
                    cn_num = {"一":1,"二":2,"三":3,"四":4,"五":5}
                    m_cn = re.match(r"^.*?第([一-五]+)(?:项|条|个|行|列).*$", paren_content)
                    if m_cn and m_cn.group(1) in cn_num:
                        qualifier = cn_num[m_cn.group(1)]
                        qualifier_type = "row_num"
                    else:
                        qualifier = paren_content
                        qualifier_type = "col_name"

        if qualifier is not None:
            tables = detect_table_structures(text_positions, field_page)
            matched_in_table = False
            for table in tables:
                if matched_in_table:
                    break

                if qualifier_type == "row_num":
                    col_name_norm = _normalize_text(base_label)
                    col_aliases = [col_name_norm]
                    for alias_key, alias_vals in ROW_ALIASES.items():
                        if col_name_norm == alias_key:
                            col_aliases.extend(alias_vals)
                        elif col_name_norm in alias_vals:
                            col_aliases.append(alias_key)
                            col_aliases.extend(v for v in alias_vals if v != col_name_norm)

                    for ci, col in enumerate(table["columns"]):
                        col_label_norm = _normalize_text(col["label"])
                        col_label_stripped = _strip_punct(col["label"])
                        if (col_name_norm in col_label_norm or col_label_norm in col_name_norm
                                or _strip_punct(base_label) in col_label_stripped
                                or col_label_stripped in _strip_punct(base_label)
                                or any(a in col_label_norm or col_label_norm in a for a in col_aliases)):
                            row_y = table["first_row_y"] + (qualifier - 1) * table["row_height"]
                            font_size = _calc_table_font(value, col["width"], 10, 5)
                            result.append({
                                "label": label, "value": value,
                                "x": col["x"], "y": row_y,
                                "width": col["width"], "height": table["row_height"] - 2,
                                "page": field_page, "font_size": font_size,
                                "matched_text": f"row{qualifier} x {col['label']}", "match_score": 100,
                            })
                            print(f"  [表格] {label} → col={col['label']} row={qualifier} x={col['x']:.0f}, y={row_y:.0f}")
                            matched_in_table = True
                            break

                elif qualifier_type in ("col_name", "year"):
                    target_col = None
                    qual_norm = _normalize_text(qualifier)
                    matching_cols = []
                    for ci, col in enumerate(table["columns"]):
                        col_label_norm = _normalize_text(col["label"])
                        if qual_norm in col_label_norm or col_label_norm in qual_norm:
                            matching_cols.append((ci, col))
                        elif qualifier_type == "year":
                            q_digits = set(re.findall(r'\d{4}', qualifier))
                            c_digits = set(re.findall(r'\d{4}', col["label"]))
                            if q_digits and c_digits and q_digits == c_digits:
                                matching_cols.append((ci, col))

                    if matching_cols:
                        if len(matching_cols) > 1:
                            qual_lower = qualifier.lower()
                            if "first" in qual_lower or "上" in qual_lower or "1st" in qual_lower:
                                target_col = matching_cols[0][1]
                            elif "second" in qual_lower or "下" in qual_lower or "2nd" in qual_lower:
                                target_col = matching_cols[1][1] if len(matching_cols) > 1 else matching_cols[0][1]
                            else:
                                target_col = matching_cols[0][1]
                        else:
                            target_col = matching_cols[0][1]

                    if target_col:
                        base_norm = _normalize_text(base_label)
                        row_aliases = [base_norm]
                        for alias_key, alias_vals in ROW_ALIASES.items():
                            if base_norm == alias_key:
                                row_aliases.extend(alias_vals)
                            elif base_norm in alias_vals:
                                row_aliases.append(alias_key)
                                row_aliases.extend(v for v in alias_vals if v != base_norm)

                        row_y = None
                        for tp in text_positions:
                            if tp["page"] != field_page:
                                continue
                            tp_norm = _normalize_text(tp["text"])
                            if any(_score_match(a, tp_norm) >= 80 for a in row_aliases):
                                row_y = tp["y"]
                                break

                        if row_y is not None:
                            font_size = _calc_table_font(value, target_col["width"], 8, 5)
                            result.append({
                                "label": label, "value": value,
                                "x": target_col["x"], "y": row_y - 1,
                                "width": target_col["width"], "height": 30,
                                "page": field_page, "font_size": font_size,
                                "matched_text": f"{base_label} x {qualifier}", "match_score": 100,
                            })
                            print(f"  [表格] {label} → col={target_col['label']} row={base_label} x={target_col['x']:.0f}, y={row_y:.0f}")
                            matched_in_table = True

            if matched_in_table:
                continue

        # 表格字段（包含 "(Father)" "(Mother)" "(Guardian)" 等）
        table_col = None
        row_label = label
        for en_col in ["(father)", "(mother)", "(guardian)"]:
            if en_col in label_norm:
                table_col = en_col.strip("()")
                row_label = label_norm.split(en_col)[0].strip().rstrip(" -")
                break
        if not table_col:
            for cn_col in ["父亲", "母亲", "监护人"]:
                if cn_col in label:
                    table_col = cn_col
                    row_label = label.split(cn_col)[0].strip().rstrip(" （-")
                    break

        if table_col:
            row_norm = _normalize_text(row_label)
            best_row_match = None
            best_row_score = 0

            for tp in text_positions:
                if tp["page"] != field_page:
                    continue
                tp_norm = _normalize_text(tp["text"])
                if not tp_norm:
                    continue

                score = _score_match(row_norm, tp_norm)
                if score < 30 and row_norm in ROW_ALIASES:
                    for alias in ROW_ALIASES[row_norm]:
                        alias_score = _score_match(_normalize_text(alias), tp_norm)
                        score = max(score, alias_score)
                if score > best_row_score:
                    best_row_score = score
                    best_row_match = tp

            if best_row_match and best_row_score >= 30:
                cell = _find_table_cell(best_row_match, table_col, text_positions, field_page)
                if not cell:
                    col_aliases_map = {"father": "father", "mother": "mother", "guardian": "guardian",
                                       "父亲": "father", "母親": "mother", "母亲": "mother", "監護人": "guardian", "监护人": "guardian"}
                    target = col_aliases_map.get(table_col, table_col)
                    col_spans = [t for t in text_positions
                                 if target in _normalize_text(t["text"])]
                    if col_spans:
                        col_span = col_spans[0]
                        correct_page = col_span["page"]
                        row_y = best_row_match["y"]
                        row_aliases = [row_norm]
                        if row_norm in ROW_ALIASES:
                            row_aliases.extend(ROW_ALIASES[row_norm])
                        for tp in text_positions:
                            if tp["page"] != correct_page:
                                continue
                            tp_norm = _normalize_text(tp["text"])
                            if any(_score_match(alias, tp_norm) >= 80 for alias in row_aliases):
                                row_y = tp["y"]
                                break
                        row_h = best_row_match.get("height", 12) + 2
                        cell = {"x": col_span["x"] - 5, "y": row_y - 1,
                                "width": 100, "height": row_h}
                        field_page = correct_page
                        print(f"  [表格fallback] {label} → page={correct_page} col={col_span['text']} x={cell['x']:.0f} y={cell['y']:.0f}")
                if cell:
                    result.append({
                        "label": label,
                        "value": value,
                        "x": cell["x"],
                        "y": cell["y"],
                        "width": cell["width"],
                        "height": cell["height"],
                        "page": field_page,
                        "font_size": 8,
                        "matched_text": f"{best_row_match['text']} x {table_col}",
                        "match_score": best_row_score,
                    })
                    print(f"  [表格] {label} → row={best_row_match['text']} col={table_col} (x={cell['x']:.0f}, y={cell['y']:.0f})")
                    continue
                else:
                    print(f"  [表格失败] {label} → row={best_row_match['text']} col={table_col} 找不到单元格，跳过")
                    continue

        # 非表格字段：直接匹配标签
        best_match = None
        best_score = 0

        for tp in text_positions:
            if tp["page"] != field_page:
                continue
            tp_norm = _normalize_text(tp["text"])
            if not tp_norm:
                continue

            score = _score_match(label_norm, tp_norm)

            if score > best_score or (score == best_score and score > 0
                                       and best_match and len(tp["text"]) < len(best_match["text"])):
                best_score = score
                best_match = tp

        if not (best_match and best_score >= 30):
            for alias_key, alias_vals in ROW_ALIASES.items():
                if label_norm == alias_key or label_norm in alias_vals:
                    candidates = [alias_key] + alias_vals
                    for alt in candidates:
                        if alt == label_norm:
                            continue
                        for tp in text_positions:
                            if tp["page"] != field_page:
                                continue
                            s = _score_match(alt, _normalize_text(tp["text"]))
                            if s > best_score:
                                best_score = s
                                best_match = tp
                    break
            if not (best_match and best_score >= 30):
                for tp in text_positions:
                    if tp["page"] != field_page:
                        continue
                    t = _normalize_text(tp["text"])
                    if label_norm in t or t in label_norm:
                        best_score = 80
                        best_match = tp
                        break
            if not (best_match and best_score >= 30) and len(label) <= 6:
                raw = label.strip()
                raw_norm = _normalize_text(raw)
                for tp in text_positions:
                    if tp["page"] != field_page:
                        continue
                    tp_raw = tp["text"]
                    if raw in tp_raw or (raw_norm and raw_norm in _normalize_text(tp_raw)):
                        best_score = 90
                        best_match = tp
                        break

        if best_match and best_score >= 30:
            adjusted_match = _adjust_for_bilingual(best_match, text_positions)
            pos = _find_blank_position(adjusted_match, text_positions, label_text=label)
            result.append({
                "label": label,
                "value": value,
                "x": pos["x"],
                "y": pos["y"],
                "width": pos["width"],
                "height": pos["height"],
                "page": best_match["page"],
                "font_size": 8,
                "matched_text": best_match["text"],
                "match_score": best_score,
            })
            print(f"  [匹配] {label} → {best_match['text']} (score={best_score}, x={pos['x']:.0f}, y={pos['y']:.0f}, w={pos['width']:.0f})")
        else:
            reason = f"score={best_score}"
            if best_match:
                reason += f", 最佳候选: {best_match['text'][:30]!r} at y={best_match['y']:.0f}"
            print(f"  [跳过] 未匹配: {label} ({reason})")

    # 位置重叠去重
    result_deduped = []
    for i, r in enumerate(result):
        overlapping = False
        for j, other in enumerate(result):
            if i == j:
                continue
            same_page = r.get("page") == other.get("page")
            x_overlap = abs(r.get("x", 0) - other.get("x", 0)) < 10
            y_overlap = abs(r.get("y", 0) - other.get("y", 0)) < 8
            if same_page and x_overlap and y_overlap:
                if len(r.get("label", "")) < len(other.get("label", "")):
                    overlapping = True
                    print(f"  [去重] {r['label']} 被 {other['label']} 覆盖（同位置，保留更具体）")
                    break
        if not overlapping:
            result_deduped.append(r)

    return result_deduped
