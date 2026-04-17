import os, re, io, chardet
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from zhipuai import ZhipuAI
from docx import Document

load_dotenv()

zhipu = ZhipuAI(api_key=os.getenv("ZHIPUAI_API_KEY"))

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST"],
    allow_headers=["*"],
)

CHARS_PER_SECTION = 1000  # only first N chars of each section sent to AI

SYSTEM_PROMPT = """\
你是一个帮助读者判断"这一章值不值得读"的助手。

你的任务：根据章节内容，生成一张 QA 卡片。

要求：
- question：用场景化的问题表达这章的核心价值，让读者一眼觉得"这说的就是我的问题"。
  不要写"本章讲了什么"，要写"为什么你会遇到XXX问题"或"如何解决XXX困境"。
  长度 15-30 字。
- answer：直接给出核心结论，不超过 60 字。必须来自原文，不能编造。

示例（好的风格）：
  question: 为什么你的团队总是在重复犯同一个错？
  answer: 因为大多数组织缺乏"制度性记忆"，错误的代价由个人承担，根因却从未被系统记录。

以 JSON 格式返回，只返回 JSON，不要其他内容：
{"question": "...", "answer": "..."}
"""


def decode_file(raw: bytes) -> str:
    detected = chardet.detect(raw)
    encoding = detected.get("encoding") or "utf-8"
    return raw.decode(encoding, errors="replace")


def parse_md(text: str) -> list[dict]:
    """Split MD by # / ## headings into chapters with sections."""
    chapters, cur_chapter, cur_section = [], None, None

    for line in text.splitlines():
        if re.match(r"^# ", line):
            cur_chapter = {"title": line[2:].strip(), "sections": []}
            chapters.append(cur_chapter)
            cur_section = None
        elif re.match(r"^## ", line):
            title = line[3:].strip()
            if not cur_chapter:
                cur_chapter = {"title": title, "sections": []}
                chapters.append(cur_chapter)
            cur_section = {"title": title, "content": ""}
            cur_chapter["sections"].append(cur_section)
        elif cur_section is not None:
            cur_section["content"] += line + "\n"
        elif cur_chapter is not None and line.strip():
            if not cur_chapter["sections"]:
                cur_section = {"title": cur_chapter["title"], "content": ""}
                cur_chapter["sections"].append(cur_section)
                cur_section = cur_chapter["sections"][-1]
            cur_section["content"] += line + "\n"

    return chapters or [{"title": "全文", "sections": [{"title": "全文", "content": text}]}]


def parse_txt(text: str) -> list[dict]:
    """Split TXT by 第X章 markers; fall back to blank-line paragraphs."""
    chapter_re = re.compile(r"^(第[零一二三四五六七八九十百千\d]+[章节部卷篇][^\n]{0,40})", re.M)
    matches = list(chapter_re.finditer(text))

    if matches:
        chapters = []
        for i, m in enumerate(matches):
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            content = text[start:end].strip()
            chapters.append({
                "title": m.group(1).strip(),
                "sections": [{"title": m.group(1).strip(), "content": content}]
            })
        return chapters

    # fallback: treat each paragraph block as a section
    blocks = [b.strip() for b in re.split(r"\n{2,}", text) if b.strip()]
    sections = [{"title": b.splitlines()[0][:40], "content": b} for b in blocks]
    return [{"title": "全文", "sections": sections}]


def docx_to_text(raw: bytes) -> str:
    """Extract plain text from .docx, preserving Heading 1/2 as # / ## markers so parse_md can split."""
    doc = Document(io.BytesIO(raw))
    lines = []
    for p in doc.paragraphs:
        text = p.text.strip()
        if not text:
            lines.append("")
            continue
        style = (p.style.name or "").lower() if p.style else ""
        if style.startswith("heading 1") or style == "title":
            lines.append(f"# {text}")
        elif style.startswith("heading 2") or style.startswith("heading 3"):
            lines.append(f"## {text}")
        else:
            lines.append(text)
    return "\n".join(lines)


def parse_book(filename: str, raw: bytes) -> tuple[str, list[dict]]:
    name = filename.lower()
    if name.endswith(".docx"):
        text = docx_to_text(raw)
        # Use MD parser if headings detected, else fall back to TXT chapter detection
        return text, (parse_md(text) if re.search(r"^#+ ", text, re.M) else parse_txt(text))
    text = decode_file(raw)
    if name.endswith(".md"):
        return text, parse_md(text)
    return text, parse_txt(text)


def generate_qa(section_title: str, chapter_title: str, content: str) -> dict:
    """Call Gemini with only the first CHARS_PER_SECTION characters of content."""
    snippet = content.strip()[:CHARS_PER_SECTION]
    response = zhipu.chat.completions.create(
        model="glm-4-flash",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"章节：{chapter_title}\n小节：{section_title}\n\n内容（节选）：\n{snippet}"}
        ]
    )
    raw = response.choices[0].message.content.strip()
    # extract JSON even if model adds surrounding text
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError(f"Invalid JSON from model: {raw}")
    import json
    return json.loads(m.group())


@app.post("/upload")
async def upload_book(file: UploadFile = File(...)):
    name = file.filename.lower()
    if name.endswith(".doc") and not name.endswith(".docx"):
        raise HTTPException(400, "暂不支持 .doc 旧格式，请在 Word 中另存为 .docx 后再上传")
    if not name.endswith((".txt", ".md", ".docx")):
        raise HTTPException(400, "只支持 .txt / .md / .docx 文件")

    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:  # 5 MB limit
        raise HTTPException(400, "文件不能超过 5MB")

    try:
        text, chapters = parse_book(file.filename, raw)
    except Exception as e:
        raise HTTPException(400, f"文件解析失败：{e}")

    book_title = re.sub(r"\.(txt|md|docx)$", "", file.filename, flags=re.I)

    result_chapters = []
    for ch in chapters:
        cards = []
        for sec in ch["sections"][:6]:  # max 6 sections per chapter
            if not sec["content"].strip():
                continue
            try:
                qa = generate_qa(sec["title"], ch["title"], sec["content"])
                cards.append({
                    "title": sec["title"],
                    "q": qa.get("question", sec["title"] + "的核心内容是什么？"),
                    "a": qa.get("answer", ""),
                    "ps": 1,
                    "p": [f"<p>{sec['content'].strip()[:300]}</p>"]
                })
            except Exception:
                # if one section fails, skip it rather than failing the whole book
                continue

        if cards:
            result_chapters.append({"title": ch["title"], "cards": cards})

    if not result_chapters:
        raise HTTPException(422, "未能解析出有效章节，请确认文件格式")

    return {"title": book_title, "chapters": result_chapters}
