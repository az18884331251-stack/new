# 章节跳读 · 阅读工具

> 上传一本书，AI 自动为每个章节生成一张 QA 卡片，帮你快速判断"这章值不值得读"。

**在线体验** → [az18884331251-stack.github.io/skip-read](https://az18884331251-stack.github.io/skip-read/)

---

## 功能

- 📖 上传 `.txt` / `.md` / `.docx` 文件，自动解析章节结构
- 🤖 AI 为每个章节生成一张场景化 QA 卡片（问题 + 核心结论）
- 📚 书架管理，导入的书持久保存在浏览器本地
- 📄 阅读页支持左右滑动逐页阅读原文
- 🔑 支持用户配置自己的 ZhipuAI API Key（BYOK），也可跳过直接导入

## 截图

| 书架 | QA 卡片 | 阅读页 |
|------|---------|--------|
| 书架展示所有导入的书 | 每章一张 QA 卡片 | 原文分页阅读 |

## 技术栈

| 层 | 技术 |
|----|------|
| 前端 | 纯 HTML / CSS / JS，无框架，部署于 GitHub Pages |
| 后端 | Python · FastAPI · ZhipuAI GLM-4-Flash |
| 部署 | 前端 GitHub Pages · 后端 Railway |

## 本地运行

### 前端

直接用浏览器打开 `index.html` 即可。

### 后端

```bash
cd backend
pip install -r requirements.txt

# 复制环境变量模板并填入你的 ZhipuAI API Key
cp .env.example .env

uvicorn main:app --reload
```

`.env` 文件：

```
ZHIPUAI_API_KEY=your_key_here
```

ZhipuAI API Key 可在 [open.bigmodel.cn](https://open.bigmodel.cn) 免费注册获取。

### 连接本地后端

将 `index.html` 中的 `BACKEND_URL` 改为 `http://localhost:8000`：

```js
const BACKEND_URL = 'http://localhost:8000';
```

## 部署到 Railway

1. Fork 本仓库
2. 在 [railway.app](https://railway.app) 新建项目，选择 GitHub 仓库
3. Root Directory 设置为 `backend`
4. 添加环境变量 `ZHIPUAI_API_KEY`
5. 部署完成后，将 Railway 域名填入 `index.html` 的 `BACKEND_URL`

## 支持的文档格式

| 格式 | 章节识别方式 |
|------|-------------|
| `.md` | `#` / `##` 标题 |
| `.txt` | `第X章` / `第X节` 等中文标记 |
| `.docx` | Word 标题样式 / 中文大纲（一、二、）/ 英文编号（1. 2.）/ 章节标记 |

> `.doc` 旧格式请在 Word 中另存为 `.docx` 后上传。

## 关于 AI

- 每个章节调用一次 GLM-4-Flash 生成 QA 卡片
- 每次只发送章节前 1000 字给 AI，控制 token 消耗
- 不配置 API Key 也可使用，跳过 AI 则只保留章节结构和原文

## License

MIT
