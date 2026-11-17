"""一期节目的流程定义。改步骤、改字段只改这里。

每一步：
- role：默认负责人的角色
- fields：表单字段（name, label, type, required, help）
  type: text / url / textarea / file / markdown
  large=True 的文件字段用分片上传（支持断点续传），适合原片和视频；
  process 指上传完成后服务器要做的处理（见 app/media.py）
- ai：这一步由 AI 先做时，对应 config/pipeline.yaml 里的工作流 id；
      ai_input 说明 AI 的输入从哪一步的哪个字段来
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Field:
    name: str
    label: str
    type: str = "text"
    required: bool = False
    help: str = ""
    accept: str = ""
    large: bool = False
    process: str = ""


@dataclass
class Stage:
    key: str
    name: str
    role: str
    description: str
    fields: list[Field] = field(default_factory=list)
    ai: str | None = None
    ai_input: tuple[str, str] | None = None  # (stage_key, field_name)
    ai_target: str | None = None             # AI 产出写进哪个字段


STAGES: list[Stage] = [
    Stage(
        key="upload", name="录制上传", role="recorder",
        description="把原片直接传到工作台（可以断点续传）。服务器会自动提取音频给粗剪用，不用另外导出。",
        fields=[
            Field("source_video", "原片", "file", True, "单机位视频原片，拖进来即可；网络中断后重新选择同一个文件，会从断点继续",
                  "video/*,audio/*", large=True, process="extract_audio"),
            Field("script", "本期文稿", "file", False, "PDF / Word，可选", ".pdf,.doc,.docx,.txt,.md"),
            Field("requirements", "本期要求", "textarea", False, "如：某段不能减、压到多少分钟、必须保留的原话"),
        ],
    ),
    Stage(
        key="rough_cut", name="粗剪", role="producer",
        description="用第 1 步自动提取的音频出剪点方案，审核后上传 1080p 粗剪视频。（后续版本由服务器按剪点方案自动渲染）",
        fields=[
            Field("rough_video", "粗剪视频", "file", True, "1080p", "video/*", large=True),
            Field("notes", "给精剪的说明", "textarea", False, "哪些地方待定、哪些地方要注意"),
        ],
    ),
    Stage(
        key="fine_cut", name="精剪", role="editor",
        description="下载粗剪视频精剪，完成后上传成品，并贴上定稿文字稿（Show notes 由 AI 根据它生成）。",
        fields=[
            Field("final_video", "成品视频", "file", True, "", "video/*", large=True),
            Field("final_audio", "成品音频", "file", False, "音频平台用，可选", "audio/*", large=True),
            Field("final_transcript", "定稿文字稿", "textarea", True, "带时间的字幕或文字稿，可从剪映导出字幕后粘贴"),
            Field("notes", "备注", "textarea"),
        ],
    ),
    Stage(
        key="shownotes", name="Show notes + 封面", role="producer",
        description="AI 根据定稿文字稿生成 Show notes，审核修改后确认。（封面 V3 加入）",
        fields=[
            Field("content", "Show notes", "markdown", True),
            Field("cover", "封面图", "file", False, "暂时手动上传", "image/*"),
        ],
        ai="shownotes", ai_input=("fine_cut", "final_transcript"), ai_target="content",
    ),
    Stage(
        key="copy_review", name="文案审核", role="copywriter",
        description="AI 根据 Show notes 生成各平台文案，文案伙伴审核内容：事实、表述、风险。确认后交给运营发布。",
        fields=[
            Field("content", "各平台文案", "markdown", True),
            Field("notes", "给运营的说明", "textarea", False, "如：某条需要配图、某平台先发"),
        ],
        ai="distribution", ai_input=("shownotes", "content"), ai_target="content",
    ),
    Stage(
        key="distribution", name="编辑发布", role="operator",
        description="按审核后的文案在各平台编辑排版并发布，再把链接填回来。音频平台（小宇宙、Apple Podcasts 等）通过 RSS 同步，只需核对是否已更新。",
        fields=[
            Field("final_copy", "最终发布文案", "markdown", False, "排版时的改动可以记在这里；内容改动请退回文案审核"),
            Field("published_links", "各平台发布链接", "textarea", True, "每行一个：平台 链接"),
        ],
    ),
]

STAGE_BY_KEY = {s.key: s for s in STAGES}
STATUS_LABEL = {"waiting": "未开始", "todo": "待处理", "done": "已完成"}


def next_stage(key: str) -> Stage | None:
    keys = [s.key for s in STAGES]
    i = keys.index(key)
    return STAGES[i + 1] if i + 1 < len(STAGES) else None
