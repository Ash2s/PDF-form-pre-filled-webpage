"""AI提取逻辑：识别表单字段并结合学生资料智能填充"""

from .mimo import analyze_image_json


FORM_FIELD_PROMPT_TEMPLATE = """你是PDF表格分析专家。请观察这张表格图片，识别所有需要填写的字段，并根据下方的学生资料智能填充。

【学生资料】
{student_info}

【核心原则：先理解表格结构，再逐个填写】
在识别任何字段之前，你必须先完成以下步骤：
1. **识别表格的各个部分/章节**：找到每个部分的标题（如"第六部分：曾就讀親屬"、"第七部分：家長/監護人資料"）
2. **理解每个部分的目的**：这个部分是关于什么的？（亲属？家长？学业？）
3. **识别每个部分内的编号区域**：如(1)、(2)、(3)分别代表什么（父亲？母亲？监护人？）
4. **然后才逐个填写字段**：根据上下文理解每个字段的含义

【识别规则】
1. **每个字段只返回一次，不得重复**
2. **label必须使用PDF上实际印刷的标签文字，原样照抄**。如果PDF上是中文就用中文（如"姓名(中文)"）、英文就用英文（如"Name in English"）。不要自己翻译，不要创造PDF上没有的标签名
3. 中英双语PDF中，优先使用英文标签（如 "Name in English"、"Date of Birth"）。但纯中文PDF（无英文标签）必须使用中文标签原文
4. 忽略以下所有区域，绝对不返回任何字段：
   · 已填写字段
   · 标题说明、页眉页脚
   · 勾选框（如 □中二 □中三 □中四）
   · 确认声明、声明书
   · **签署/签名区域**——包括但不限于"家長簽署"、"監護人簽署"、"申請人簽署"、"parent signature"以及签名栏下方的姓名/關係/電話/電郵等所有附属字段
   · 照片栏位（"近照"、"相片"等）
5. 日期拆分成多个小格时合并为一个字段
6. **【关键】只识别当前页面上真正需要填写的空白区域。如果某个标签旁边已经有内容、或者标签只是参考说明（如"已提交"、"资料清单"中列出的项目），不要返回该字段。判断标准：标签右侧或下方是否有明显的空白区域可以填写内容**
7. **【致命规则】以表单为本：先看表单上有什么空白栏位，再填内容。绝对不要：**
   · 因为学生资料有"申请原因"就创建一个"Reason(s) for Applying"字段
   · 因为学生资料有课外活动就创建活动字段（除非表单上真的有活动表格）
   · 因为学生资料有父母信息就创建家庭字段（除非表单上真的有家庭表格）
   - 如果表单上没有该栏位，即使学生资料再完整也绝不创建

【重复字段名的上下文理解规则】
当PDF中出现多个相同或相似的字段名时（如多个"與申請人關係"）：
- **必须根据所在部分的标题来理解该字段的含义**
- 例如：
  · "第六部分：曾就讀親屬"中的"與申請人關係" = 问的是"这位亲属与申请人的关系"（如叔侄、姑侄）
  · "第七部分：家長資料(1)"中的"與申請人關係" = 问的是"这位家长与申请人的关系"（填"父親"）
  · "第七部分：家長資料(2)"中的"與申請人關係" = 问的是"这位家长与申请人的关系"（填"母親"）
- **不要因为字段名相同就填相同的内容，必须根据上下文判断**

【教育资料表格】
- 表格有多个学年行和多列（Class、Name of School、School Type等）
- **【最重要规则】你必须为表格中的每一个学年都返回 School Name 和 Class 字段，包括过去的学校**：
  - 第一步：数一数表格有多少个学年行（如4行）
  - 第二步：从简历中找出所有教育经历（可能有2-3段）
  - 第三步：为每个学年行匹配对应的学校。时间重叠即匹配：
    · "2025年9月至今：紅山中學，中一" → 匹配 2025-2026 First Term 和 Second Term
    · "2019年9月至2025年7月：和平實驗小學，小六" → 匹配 2024-2025 First Term 和 Second Term
  - 第四步：每个匹配都返回两个字段：School Name (学年) 和 Class (学年)
- **如果你只返回了当前学校的字段而没有返回过去的学校，这是错误的。必须返回所有学年**
- label格式："School Name (2024-2025 First Term)"、"Class (2024-2025 First Term)"
- 如果表格有4个学年行、简历有2段经历，你应该返回8个教育字段

【家庭资料表格 - 识别所有家长/监护人区域】
- **【核心规则】找到"家長/監護人資料"部分后，必须识别该部分内的所有编号区域**
- 编号识别：
  · (1) = 第一位家长（通常是父亲）
  · (2) = 第二位家长（通常是母亲）
  · (3) = 第三位家长/监护人（如有）
- 每个编号区域都是独立的一组字段，必须全部识别：
  · 英文姓名 Name in English
  · 中文姓名 Name in Chinese
  · 與申請人關係 Relationship with applicant
  · 國籍 Nationality
  · 職業 Occupation
  · 聯絡電話 Contact Number
  · 住址 Residential Address
  · 電郵地址 E-mail Address
- **填写规则**：
  · (1) 区域填父亲信息：關係填"父親"，用学生资料中的 father_* 字段
  · (2) 区域填母亲信息：關係填"母親"，用学生资料中的 mother_* 字段
  · (3) 区域如有，填其他监护人信息
- **【致命错误】不要只识别(1)而忽略(2)！必须逐个检查所有编号区域**
- 如果PDF只有一个没有编号的家长签署栏，则只返回签署栏字段

【曾就讀/現就讀親屬表格】
- 这类表格问的是"谁曾在/正在本校就读"
- "與申請人關係"字段的含义是：这位亲属与申请人的关系（如叔侄、姑侄）
- **不要填"父親"或"母親"**，除非学生资料明确提到有亲属曾在该校就读
- 如果没有相关信息，这些字段留空

【课外活动 / 奖项表格】
- 如果图片中有课外活动、获奖记录、比赛成绩、志愿服务等表格，逐行识别
- 注意表格有多少行就返回多少行，不要超出表格实际行数
- 如果表格搭配多行而学生资料中有更多活动，按以下优先级选择填入：
  · 第一优先：校外活动（非学校内部组织的，如协会、教育局、国际组织主办）
  · 第二优先：含金量高（国际 > 全国 > 省级 > 市级 > 校级）
  · 第三优先：近期的（年份新的优先）
- 每个活动包含：year, activity, organizer, award
- 信息不足的字段留空字符串""
- 如果图片中没有活动/奖项表格，返回空数组 []

【学业成绩表格】
- 如果页面上有学业成绩/評分表格（列出科目和等级/分数的表格），逐行识别每个科目
- 表格通常有两列：科目(Subject)和成績/等級(Grade)，表头可能有"科目"、"Subject"、"成績"、"Grade"、"等級"等
- label直接用PDF上印刷的科目名称（如"中文"、"英文"、"数学"、"操行/品行"、"平均分"等）
- value留空字符串""（留给用户后续填写）
- 即使不确定成绩填什么，也要把科目名作为字段保留下来
- 注意区分"科目偏好/Subject Preference"（这是选哪些科目，不是成绩）和"成绩表/Grade Table"（这是已有科目的评分）

【中文相关字段的区分规则】
- "中文"作为科目名（如在成绩表中）= 这是语文科目，label就是"中文"
- "中文姓名"、"中文名" = 这是学生的中文名字字段，label必须用PDF上实际印刷的完整标签
- "中文（成绩）"、"中文成績" = 这是中文科目的成绩字段，label必须用PDF上实际印刷的完整标签
- "中文地址"、"中文地址（同上地址）" = 这是中文地址字段
- **关键原则**：每个字段的label必须是PDF上印刷的完整标签文字，不要截断或简化。如果PDF上写的是"中文（成绩）"，label就必须是"中文（成绩）"，不能简化为"中文"

【填充规则】
- 直接匹配：学生资料中有完全对应的字段
- **语言规则**：所有中文内容必须用**繁体中文**（如"女"不是"女"，"香港"不是"香港"，"莊圓圓"不是"庄圆圆"）。Sex填"男/女"，Place of Birth填"香港/深圳"等。只有Name in English、Address in English等明确要求英文的字段才用英文。学校名称、地址、姓名等如果简历中是简体，必须转换为繁体
- 推理填写：
  · 父母英文名：如有中文名可转拼音，格式：姓全大写+逗号+名首字母大写（如"庄家乐"→"ZHUANG, Jiale"）
  · 关系字段：写"父親"或"母親"（不要写父女/父子/母女/母子）
  · 性别、出生日期等基础信息直接填写
  · **Occupation（职业）：填职位/职务（如"施工管理"、"总经理"），不是公司名。如果学生资料中有father_position/mother_position字段，用它；如果只有father_company，那才是公司名**
  · **Contact Tel. No.（联系电话）：填电话号码。如果学生资料中有father_phone/mother_phone字段，用它**
  · **Nationality（国籍）：默认填"中國"（繁体中文），除非学生资料中有明确的其他国籍信息**
- 入学理由：用繁体中文简短说明为什么选择去香港接受教育（如全人教育理念、英语学习环境、国际化视野等），50-65字，不要过于详细。必须用繁体中文回答，不要用英文
- 推荐人：有referee就填，否则留空
- 无法填写的字段value留空字符串""
- **家庭表格的每个成员的每一行都必须返回，即使value为空**

【坐标定位规则 - 重要】
对于每个字段，你必须返回填写区域的坐标位置（用于扫描件PDF）：
- **x**: 填写区域左上角的X坐标（占图片宽度的百分比，0-100）
- **y**: 填写区域左上角的Y坐标（占图片高度的百分比，0-100）
- **w**: 填写区域的宽度（占图片宽度的百分比，0-100）
- **h**: 填写区域的高度（占图片高度的百分比，0-100）

坐标估算方法：
1. 找到字段标签（如"姓名:"）的位置
2. 找到标签右侧或下方的空白填写区域
3. 估算该空白区域在图片中的百分比坐标
4. 例如：标签在图片左侧约5%位置，空白区域从8%开始到30%，高度从20%到23%，则 x=8, y=20, w=22, h=3

【输出格式】
直接返回JSON对象，包含fields数组和activities数组：
{{
  "fields": [
    {{"label": "实际PDF上的标签文字", "value": "填写内容", "x": 8, "y": 20, "w": 22, "h": 3}}
  ],
  "activities": [
    {{"year": "年份", "activity": "活动名称", "organizer": "主办机构", "award": "荣誉（可选）"}}
  ]
}}

【致命规则 - 违反会导致错误填写】
- **只返回PDF上实际印刷的、有空白填写区域的字段。不要自己创造字段**
- 如果学生资料中有"申请原因"但PDF上没有这个填写栏位，不要返回它
- 如果学生资料中有课外活动但PDF上没有活动表格，activities返回空数组[]
- 输出示例中的字段名仅为格式参考，不要照抄。必须用PDF上实际出现的文字作为label
- **每个字段必须包含x, y, w, h坐标值，否则无法在扫描件PDF上填写**
"""


def detect_form_fields(image_base64: str, student_info: dict = None) -> tuple[list[dict], list[dict]]:
    """识别PDF中的字段并结合学生资料智能填充。返回 (fields, activities)"""
    print(f"  [DF] step1: student_info type={type(student_info).__name__}")

    import json as _json
    if student_info:
        try:
            student_text = _json.dumps(student_info, ensure_ascii=False, indent=2)
            print(f"  [DF] step2: json ok, len={len(student_text)}")
        except Exception as e:
            print(f"  [DF] step2 FAIL: {e}")
            student_text = str(student_info)
    else:
        student_text = "（无学生资料）"
        print(f"  [DF] step2: no student info")

    try:
        prompt = FORM_FIELD_PROMPT_TEMPLATE.format(student_info=student_text)
        print(f"  [DF] step3: prompt ok, len={len(prompt)}")
    except Exception as e:
        print(f"  [DF] step3 FAIL: {e}")
        raise

    try:
        result = analyze_image_json(image_base64, prompt)
        print(f"  [DF] step4: AI返回 type={type(result).__name__}")
    except Exception as e:
        print(f"  [DF] step4 FAIL: {e}")
        raise

    activities = []

    # 新格式：{fields: [...], activities: [...]}
    if isinstance(result, dict):
        if "activities" in result and isinstance(result["activities"], list):
            activities = result["activities"]
        if "fields" in result and isinstance(result["fields"], list):
            result = result["fields"]
        elif isinstance(result.get("fields"), list):
            result = result["fields"]
        else:
            # 兼容旧格式：直接是list
            pass

    if isinstance(result, dict) and "fields" not in result:
        raise ValueError(f"AI返回dict但无fields键: {list(result.keys())[:5]}")

    if not isinstance(result, list):
        raise ValueError(f"AI返回格式错误: {type(result)}")

    cleaned = []
    for item in result:
        if not isinstance(item, dict):
            continue
        label = item.get("label") or item.get("Label") or item.get("name") or ""
        value = item.get("value") or item.get("Value") or ""
        if label:
            field = {"label": str(label).strip(), "value": str(value).strip()}
            # 提取坐标（扫描件PDF使用）
            for key in ["x", "y", "w", "h"]:
                val = item.get(key)
                if val is not None:
                    try:
                        field[key] = float(val)
                    except (ValueError, TypeError):
                        pass
            cleaned.append(field)

    print(f"  [DF] step5: cleaned {len(cleaned)} fields, {len(activities)} activities")

    if not cleaned:
        raw_str = str(result)[:500]
        print(f"  [DF] raw: {raw_str}")
        raise ValueError("AI返回的字段列表为空或格式不正确")

    return cleaned, activities


STUDENT_EXTRACT_PROMPT = """你是学生资料提取专家。从这份文件中提取以下信息。

提取字段（有则提取，无则跳过）：
- name_cn: 中文姓名
- name_en: 英文姓名（格式：姓大写+逗号+名首字母大写，如"Zhuang, Jiale" → "ZHUANG, Jiale"；姓在前名在后）
- sex: 性别
- dob: 出生日期（dd/mm/yyyy）
- birth_place: 出生地
- hkid: 身份证号码
- address_en: 英文家庭住址（不要学校地址）
- address_cn: 中文家庭住址（不要学校地址，用繁体中文）
- phone: 联系电话
- email: 电子邮箱
- religion: 宗教信仰
- current_school: 现就读学校
- current_class: 年级/班级
- father_name_cn: 父亲中文姓名
- father_name_en: 父亲英文姓名（如无英文名，从中文名转拼音，格式：姓全大写+逗号+名首字母大写，如"庄家乐"→"ZHUANG, Jiale"）
- father_company: 父亲公司/单位名称
- father_position: 父亲职位/职务（注意：不是公司名，是具体职位如"施工管理"、"总经理"）
- father_phone: 父亲联系电话
- mother_name_cn: 母亲中文姓名
- mother_name_en: 母亲英文姓名（如无英文名，从中文名转拼音）
- mother_company: 母亲公司/单位名称
- mother_position: 母亲职位/职务（注意：不是公司名，是具体职位）
- mother_phone: 母亲联系电话
- reason: 申请原因
- referee: 推荐人
- activities: 课外活动数组，每个活动包含 year, activity, organizer, award

【重要规则】
- 姓名必须包含姓和名（如"庄家乐"而非"家乐"），姓在前名在后
- 地址必须完整，不能只提取一部分
- 如果姓和名分开出现，合并为完整姓名
- 课外活动提取流程：①先从文件中找出所有活动、获奖、志愿服务记录（无论类型：舞蹈、绘画、体育、音乐、义工等全部列出）②然后从中选出最多5个最重要的填入activities数组 ③选择标准：**校外活动优先**（非学校内部；协会/教育局/国际组织主办 > 校内活动），同类活动中选**含金量高**的（国际 > 全国 > 省市 > 校级）④每个活动包含：year（年份）、activity（活动名称）、organizer（主办机构）、award（荣誉/职位），信息不足的字段留空字符串""

直接返回JSON对象，只包含有信息的字段：
{
  "name_cn": "莊圓圓",
  "name_en": "ZHUANG, Yuenyuen",
  "sex": "女",
  "activities": [
    {"year": "2024-2025", "activity": "校际数学竞赛", "organizer": "香港数学奥林匹克", "award": "银奖"}
  ]
}
"""


def extract_student_info(image_base64: str) -> dict:
    """从学生资料图片中提取结构化信息"""
    result = analyze_image_json(image_base64, STUDENT_EXTRACT_PROMPT, max_tokens=12000)
    if not isinstance(result, dict):
        raise ValueError(f"AI返回格式错误: {type(result)}")
    return result


VERIFY_PROMPT = """你是PDF表单审核专家。这是已填写的表格图片，请逐个检查每个已填写的字段位置是否正确。

审核标准：
1. 字段值是否紧挨在对应的标签旁边（不是远离标签）
2. 字段值是否与其他文字重叠
3. 字段值是否在正确的行/列位置（如父亲列、母亲列）
4. 字段值是否超出了填写区域

已填写字段列表：
{fields_info}

对于每个有问题的字段，返回修正建议：
[
  {{"label": "字段名", "issue": "问题描述", "fix_y": 修正后的y坐标(像素), "fix_x": 修正后的x坐标(像素), "fix_w": 修正后的宽度(像素)}}
]

如果没有问题，返回空数组：[]
直接返回JSON数组。"""


def verify_form_positions(image_base64: str, fields: list[dict]) -> list[dict]:
    """视觉审核：检查已填写表单的字段位置是否正确"""
    fields_info = "\n".join(
        f"- {f['label']}: 位于({f['x']:.0f},{f['y']:.0f}) 宽{f['width']:.0f} \"{f.get('value', '')}\""
        for f in fields if f.get("value")
    )
    if not fields_info:
        return []

    prompt = VERIFY_PROMPT.format(fields_info=fields_info)
    result = analyze_image_json(image_base64, prompt)

    if isinstance(result, dict) and "corrections" in result:
        result = result["corrections"]
    if not isinstance(result, list):
        return []

    return [r for r in result if isinstance(r, dict) and r.get("label")]
