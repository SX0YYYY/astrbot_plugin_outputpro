import time

from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .core.config import PluginConfig
from .core.model import OutContext, StateManager, StepName
from .core.pipeline import Pipeline


def _noop_decorator(**kwargs):
    def _wrap(func):
        return func

    return _wrap


# AstrBot >= 4.29.0 才有这两个钩子；旧版退化为 no-op，插件仍可正常加载。
_on_using_llm_tool = getattr(filter, "on_using_llm_tool", _noop_decorator)
_on_llm_tool_respond = getattr(filter, "on_llm_tool_respond", _noop_decorator)


class OutputPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context)
        self.pipeline = Pipeline(self.cfg)

    async def initialize(self):
        await self.pipeline.initialize()

    async def terminate(self):
        await self.pipeline.terminate()

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1000)
    async def on_message(self, event: AstrMessageEvent):
        """收到群消息时"""
        gid = event.get_group_id()
        sender_id = event.get_sender_id()
        self_id = event.get_self_id()

        g = StateManager.get_group(gid)

        if self.cfg.reply.threshold > 0 and sender_id != self_id:
            g.msg_queue.append(event.message_obj.message_id)

        if self.cfg.pipeline.is_enabled_step(StepName.AT) and not self.cfg.at.at_str:
            name = event.get_sender_name()
            if len(g.name_to_qq) >= 100:
                g.name_to_qq.popitem(last=False)
            g.name_to_qq[name] = sender_id

    @_on_using_llm_tool(priority=1000)
    async def mark_tool_started(
        self, event: AstrMessageEvent, tool, tool_args
    ) -> None:
        """记录工具开始时间，供 block.timeout 判断本轮是否有工具活动。"""
        event.set_extra("_agent_last_activity_ts", time.time())

    @_on_llm_tool_respond(priority=1000)
    async def mark_tool_responded(
        self, event: AstrMessageEvent, tool, tool_args, tool_result
    ) -> None:
        """工具结束时刷新活动时间。

        单次长工具（如 sleep 200 或一次很慢的检索）从开始到结束本身就超过
        block.timeout，只在开始时记时间仍会被判定为过期，因此结束时必须再刷新一次。
        """
        event.set_extra("_agent_last_activity_ts", time.time())

    @filter.on_decorating_result(priority=15)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """发送消息前"""
        result = event.get_result()
        if not result or not result.chain:
            return

        ctx = OutContext(
            event=event,
            chain=result.chain,
            is_llm=result.is_llm_result(),
            plain=result.get_plain_text(),
            gid=event.get_group_id(),
            uid=event.get_sender_id(),
            bid=event.get_self_id(),
            group=StateManager.get_group(event.get_group_id()),
            timestamp=event.message_obj.timestamp,
        )

        await self.pipeline.run(ctx)
