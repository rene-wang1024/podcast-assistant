"""一期节目的流程定义。改步骤、改字段只改这里。

每一步：
- role：默认负责人的角色
- fields：表单字段（name, label, type, required, help）
  type: text / url / textarea / file / markdown
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
        description="录完后把视频传到百度网盘，在这里填链接，再上传一份压缩音频给 AI 用。",
        fields=[
            Field("netdisk_url", "原始素材网盘链接", "url", True, "百度网盘分享链接"),
            Field("netdisk_code", "提取码"),
            Field("audio", "压缩音频", "file", False, "单声道 MP3 即可，约 30 MB/小时；V2 起用于 AI 转写和粗剪", "audio/*"),
            Field("script", "本期文稿", "file", False, "PDF / Word，可选", ".pdf,.doc,.docx,.txt,.md"),
            Field("requirements", "本期要求", "textarea", False, "如：某段不能减、压到多少分钟、必须保留的原话"),
        ],
    ),
    Stage(
        key="rough_cut", name="粗剪", role="producer",
        description="下载素材粗剪，完成后把粗剪工程或导出文件传到网盘。（V2 起由 AI 先出剪点方案）",
        fields=[
            Field("netdisk_url", "粗剪网盘链接", "url", True),
            Field("netdisk_code", "提取码"),
            Field("notes", "给精剪的说明", "textarea", False, "哪些地方待定、哪些地方要注意"),
        ],
    ),
    Stage(
        key="fine_cut", name="精剪", role="editor",
        description="精剪完成后上传成品，并贴上定稿文字稿（Show notes 由 AI 根据它生成）。",
        fields=[
            Field("netdisk_url", "成品网盘链接", "url", True),
            Field("netdisk_code", "提取码"),
            Field("final_audio", "成品音频", "file", False, "", "audio/*"),
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
