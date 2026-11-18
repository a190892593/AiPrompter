"""简历文件解析：PDF / Word 上传后自动转成 Markdown 简历。

- PDF：用 pypdf 抽文字，再按「章节标题 + 项目符号」启发式转 Markdown；
- Word：用 python-docx 保留 Heading / 列表 / 表格结构，直接转 Markdown；
- 老的 .doc（二进制）不支持，提示另存为 .docx。
"""

from __future__ import annotations

import io
import logging
import re

log = logging.getLogger("resume_parse")

SECTION_WORDS = (
    "个人信息",
    "基本信息",
    "联系方式",
    "求职意向",
    "教育背景",
    "教育经历",
    "工作经历",
    "工作经验",
    "实习经历",
    "项目经历",
    "项目经验",
    "专业技能",
    "技能清单",
    "技术栈",
    "证书",
    "荣誉",
    "获奖",
    "自我评价",
    "个人评价",
    "校园经历",
    "研究经历",
    "论文",
    "发表",
    "开源",
    "其他",
)

BULLET_CHARS = "•·▪●○◆◇■□▶►–—*＊-"

_BULLET_RE = re.compile(rf"^\s*[{re.escape(BULLET_CHARS)}]\s*")
_NUMBER_BULLET_RE = re.compile(r"^\s*(\d{1,2})[.、)]\s*")


def _looks_section(line: str) -> bool:
    text = line.strip()
    if not text or len(text) > 24:
        return False
    cleaned = re.sub(r"[\s:：]+$", "", text)
    return any(cleaned.startswith(word) for word in SECTION_WORDS)


def text_to_markdown(raw: str) -> str:
    """把 PDF 抽出来的纯文字整理成 Markdown。"""
    lines = [line.rstrip() for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    lines = [line.strip() for line in lines]

    # 去掉页眉页脚类噪音：纯数字行、单页页码
    cleaned: list[str] = []
    for line in lines:
        if not line:
            cleaned.append("")
            continue
        if re.fullmatch(r"\d{1,3}", line):
            continue
        cleaned.append(line)

    blocks: list[str] = []
    first_text_done = False
    for line in cleaned:
        if not line:
            if blocks and blocks[-1] != "":
                blocks.append("")
            continue
        if _looks_section(line):
            if blocks and blocks[-1] != "":
                blocks.append("")
            blocks.append(f"## {re.sub(r'[\s:：]+$', '', line)}")
            blocks.append("")
            continue
        if _BULLET_RE.match(line):
            blocks.append(f"- {_BULLET_RE.sub('', line, count=1).strip()}")
            continue
        number = _NUMBER_BULLET_RE.match(line)
        if number and len(line) >= 4:
            index = number.group(1)
            blocks.append(f"{index}. {line[number.end():].strip()}")
            continue
        # 第一行短文本当标题（通常是姓名）
        if not first_text_done and len(line) <= 24 and not re.search(r"[，。；：、,.;:：]", line):
            blocks.append(f"# {line}")
            blocks.append("")
            first_text_done = True
            continue
        first_text_done = True
        blocks.append(line)

    # 合并多余空行、去掉行内多余空格
    text = "\n".join(blocks)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def parse_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages: list[str] = []
    for page in reader.pages[:20]:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - 单页失败不影响整份简历
            log.exception("PDF 单页解析失败")
    return text_to_markdown("\n".join(pages))


def parse_docx(data: bytes) -> str:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(io.BytesIO(data))
    blocks: list[str] = []

    def render_paragraph(paragraph: Paragraph) -> None:
        text = (paragraph.text or "").strip()
        if not text:
            if blocks and blocks[-1] != "":
                blocks.append("")
            return
        style = (paragraph.style.name or "").lower() if paragraph.style is not None else ""
        if style.startswith("heading"):
            match = re.search(r"(\d+)", style)
            level = int(match.group(1)) if match else 2
            blocks.append(f"{'#' * max(1, min(level, 4))} {text}")
            return
        if "list bullet" in style or "列表段落" in style:
            blocks.append(f"- {text}")
            return
        if "list number" in style:
            blocks.append(f"1. {text}")
            return
        blocks.append(text)

    def render_table(table: Table) -> None:
        rows = []
        for row in table.rows[:30]:
            cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
            if any(cells):
                rows.append(cells)
        if not rows:
            return
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        header = rows[0]
        blocks.append("| " + " | ".join(header) + " |")
        blocks.append("| " + " | ".join("---" for _ in header) + " |")
        for row in rows[1:]:
            blocks.append("| " + " | ".join(row) + " |")
        blocks.append("")

    body = document.element.body
    for child in body.iterchildren():
        if child.tag.endswith("}p"):
            render_paragraph(Paragraph(child, document))
        elif child.tag.endswith("}tbl"):
            render_table(Table(child, document))

    text = "\n".join(blocks)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse(filename: str, data: bytes) -> tuple[str, str]:
    """返回 (markdown, 文件类型说明)。不支持的格式抛 ValueError。"""
    lowered = (filename or "").lower()
    if not data:
        raise ValueError("文件是空的")
    if lowered.endswith(".pdf") or data[:4] == b"%PDF":
        return parse_pdf(data), "pdf"
    if lowered.endswith(".docx") or data[:2] == b"PK":
        return parse_docx(data), "docx"
    if lowered.endswith(".doc"):
        raise ValueError("不支持老的 .doc 格式，请用 Word 另存为 .docx 再上传")
    raise ValueError("只支持 PDF 和 Word（.docx）文件")
