"""The 8 agents. Six come from the V2.0 tech plan (ch. 5); 商品定位 and 画面理解 are new, because a
finished video needs the product found in the replay and the picture understood.

| agent        | business name | used by node(s)                 | V2.0 §  |
|--------------|---------------|---------------------------------|---------|
| locate       | 商品定位       | locate_product                  | new     |
| vision       | 画面理解       | look (and the asset library)    | new     |
| script       | 脚本生成       | select_quotes, prepare_script   | 5.1     |
| selling      | 卖点匹配       | storyboard                      | 5.2     |
| assets       | 素材召回       | match_assets                    | 5.3     |
| storyboard   | 分镜生成       | storyboard                      | 5.4     |
| compliance   | 合规审核       | auto_review                     | 5.5     |
| feedback     | 反馈归因       | offline, not in the graph       | 5.6     |
"""
from .assets import AssetAgent
from .base import Agent, AgentFailed, AgentResult, Attempt
from .compliance import ComplianceAgent
from .feedback import FeedbackAgent
from .locate import LocateAgent
from .script import PrepareScriptAgent, SelectQuotesAgent
from .selling import SellingAgent
from .storyboard import StoryboardAgent
from .vision import VisionAgent

# business agent → its task classes (脚本生成 has one task per mode)
AGENTS: dict[str, tuple[type[Agent], ...]] = {
    "商品定位": (LocateAgent,),
    "画面理解": (VisionAgent,),
    "脚本生成": (SelectQuotesAgent, PrepareScriptAgent),
    "卖点匹配": (SellingAgent,),
    "素材召回": (AssetAgent,),
    "分镜生成": (StoryboardAgent,),
    "合规审核": (ComplianceAgent,),
    "反馈归因": (FeedbackAgent,),
}

__all__ = [
    "AGENTS", "Agent", "AgentFailed", "AgentResult", "AssetAgent", "Attempt", "ComplianceAgent", "FeedbackAgent",
    "LocateAgent", "PrepareScriptAgent", "SelectQuotesAgent", "SellingAgent", "StoryboardAgent", "VisionAgent",
]
