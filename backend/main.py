import os, re, io, json, chardet
from fastapi import FastAPI, UploadFile, File, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from zhipuai import ZhipuAI
from docx import Document

load_dotenv()

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST"],
    allow_headers=["*"],
)

CHARS_PER_SECTION = 1000  # chars sent to AI per section
CHUNK_CHARS = 800          # max chars per reader page (long sections get split)
MIN_SECTION_CHARS = 150    # sections shorter than this get merged into the previous

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


# ── Heading detection patterns ──────────────────────────────────────────────
# H1: 一、标题 / 第一章 / Chapter 1 / I. Title / 壹、
_H1_RE = re.compile(
    r"^("
    r"[一二三四五六七八九十百千壹贰叁肆伍陆柒捌玖拾]+[、．.]\s*\S"  # 一、二、
    r"|第[零一二三四五六七八九十百千\d]+[章节部卷篇]"               # 第一章
    r"|Chapter\s+\d"                                                  # Chapter 1
    r"|[IVX]+\.\s+\S"                                                # I. II.
    r")", re.I
)
# H2: 1. 标题 / 1、标题 / (一) / 1.1 / a. b.
_H2_RE = re.compile(
    r"^("
    r"\d+[\.\s、．]\s*\S"                          # 1. 1、
    r"|\d+\.\d+[\.\s]\s*\S"                        # 1.1 1.2
    r"|[（(][一二三四五六七八九十\d]+[）)]\s*\S"   # (一) （1）
    r"|[a-z][\.\)]\s+\S"                           # a. b)
    r")", re.I
)

def _is_heading1(text: str, style: str) -> bool:
    return style.startswith("heading 1") or style == "title" or bool(_H1_RE.match(text))

def _is_heading2(text: str, style: str) -> bool:
    return (style.startswith("heading 2") or style.startswith("heading 3")
            or bool(_H2_RE.match(text)))


# ── Docx → marked-up text ───────────────────────────────────────────────────
def docx_to_text(raw: bytes) -> str:
    doc = Document(io.BytesIO(raw))
    lines = []
    for p in doc.paragraphs:
        text = p.text.strip()
        if not text:
            continue
        style = (p.style.name or "").lower() if p.style else ""
        if _is_heading1(text, style):
            lines.append(f"# {text}")
        elif _is_heading2(text, style):
            lines.append(f"## {text}")
        else:
            lines.append(text)
            lines.append("")   # blank line so parse_txt can split paragraphs
    return "\n".join(lines)


# ── Markdown parser ──────────────────────────────────────────────────────────
def parse_md(text: str) -> list[dict]:
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


# ── Plain-text parser ────────────────────────────────────────────────────────
def parse_txt(text: str) -> list[dict]:
    """Try 第X章 markers; fall back to blank-line paragraph blocks."""
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

    # fallback: each blank-line-separated block is a section
    blocks = [b.strip() for b in re.split(r"\n{2,}", text) if b.strip()]
    if not blocks:
        return [{"title": "全文", "sections": [{"title": "全文", "content": text}]}]
    sections = [{"title": b.splitlines()[0][:40], "content": b} for b in blocks]
    return [{"title": "全文", "sections": sections}]


# ── Post-processing: normalize section sizes ─────────────────────────────────
def normalize_sections(chapters: list[dict]) -> list[dict]:
    """
    1. Merge sections that are too short (< MIN_SECTION_CHARS) into the previous one.
    2. Chunk sections that are too long (> CHUNK_CHARS) into smaller pieces.
    """
    for ch in chapters:
        # Step 1: merge short sections upward
        merged: list[dict] = []
        for sec in ch["sections"]:
            content = sec["content"].strip()
            if not content:
                continue
            if merged and len(content) < MIN_SECTION_CHARS:
                merged[-1]["content"] += "\n" + content
            else:
                merged.append({"title": sec["title"], "content": content})
        if not merged:
            merged = ch["sections"]

        # Step 2: chunk long sections
        final: list[dict] = []
        for sec in merged:
            content = sec["content"].strip()
            if len(content) <= CHUNK_CHARS * 1.5:
                final.append(sec)
                continue
            paras = [p.strip() for p in re.split(r"\n+", content) if p.strip()]
            buf, chunks = "", []
            for p in paras:
                if len(buf) + len(p) > CHUNK_CHARS and buf:
                    chunks.append(buf.strip())
                    buf = p
                else:
                    buf = (buf + "\n" + p) if buf else p
            if buf.strip():
                chunks.append(buf.strip())
            for i, c in enumerate(chunks):
                final.append({
                    "title": f"{sec['title']} ({i+1})" if len(chunks) > 1 else sec["title"],
                    "content": c
                })
        ch["sections"] = final
    return chapters


# ── Entry point ───────────────────────────────────────────────────────────────
def parse_book(filename: str, raw: bytes) -> tuple[str, list[dict]]:
    name = filename.lower()
    if name.endswith(".docx"):
        text = docx_to_text(raw)
        chapters = parse_md(text) if re.search(r"^#+ ", text, re.M) else parse_txt(text)
    else:
        text = decode_file(raw)
        chapters = parse_md(text) if name.endswith(".md") else parse_txt(text)
    return text, normalize_sections(chapters)


def generate_qa(section_title: str, chapter_title: str, content: str, client: ZhipuAI) -> dict:
    snippet = content.strip()[:CHARS_PER_SECTION]
    response = client.chat.completions.create(
        model="glm-4-flash",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"章节：{chapter_title}\n小节：{section_title}\n\n内容（节选）：\n{snippet}"}
        ]
    )
    raw_text = response.choices[0].message.content.strip()
    m = re.search(r"\{.*\}", raw_text, re.S)
    if not m:
        raise ValueError(f"Invalid JSON from model: {raw_text}")
    return json.loads(m.group())


def make_client(api_key: str | None) -> ZhipuAI | None:
    key = api_key or os.getenv("ZHIPUAI_API_KEY")
    return ZhipuAI(api_key=key) if key else None


@app.post("/upload")
async def upload_book(
    file: UploadFile = File(...),
    x_api_key: str | None = Header(default=None),
):
    name = file.filename.lower()
    if name.endswith(".doc") and not name.endswith(".docx"):
        raise HTTPException(400, "暂不支持 .doc 旧格式，请在 Word 中另存为 .docx 后再上传")
    if not name.endswith((".txt", ".md", ".docx")):
        raise HTTPException(400, "只支持 .txt / .md / .docx 文件")

    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(400, "文件不能超过 5MB")

    try:
        text, chapters = parse_book(file.filename, raw)
    except Exception as e:
        raise HTTPException(400, f"文件解析失败：{e}")

    book_title = re.sub(r"\.(txt|md|docx)$", "", file.filename, flags=re.I)
    client = make_client(x_api_key)

    result_chapters = []
    for ch in chapters:
        cards = []
        for sec in ch["sections"][:20]:
            content = sec["content"].strip()
            if not content:
                continue
            paras = [p.strip() for p in re.split(r"\n+", content) if p.strip()]
            q, a = sec["title"] + "的核心内容是什么？", ""
            if client:
                try:
                    qa = generate_qa(sec["title"], ch["title"], content, client)
                    q = qa.get("question", q)
                    a = qa.get("answer", "")
                except Exception:
                    pass  # fall back to rule-based title if AI fails
            cards.append({
                "title": sec["title"],
                "q": q,
                "a": a,
                "ps": 1,
                "p": ["".join(f"<p>{p}</p>" for p in paras)]
            })

        if cards:
            result_chapters.append({"title": ch["title"], "cards": cards})

    if not result_chapters:
        raise HTTPException(422, "未能解析出有效章节，请确认文件格式")

    return {"title": book_title, "chapters": result_chapters}
