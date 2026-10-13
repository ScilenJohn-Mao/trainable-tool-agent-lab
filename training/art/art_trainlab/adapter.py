"""Use ART's logged LangChain boundary while preserving raw sampler completions."""

from copy import deepcopy
from uuid import uuid4

from langchain_core.messages import convert_to_messages, convert_to_openai_messages
from langchain_core.runnables import Runnable

from tool_agent_lab.agent.model_client import AssistantMessage, ModelReply, chat_tool_definitions


class CompletionBridge(Runnable):
    def __init__(self, model, recorder, config, tools=()):
        self.model, self.recorder, self.config = model, recorder, config
        self.tools = tools

    def invoke(self, input, config=None, **kwargs):
        raise NotImplementedError("Training graph uses async model invocation")

    def bind_tools(self, tools):
        return CompletionBridge(self.model, self.recorder, self.config, tools)

    async def ainvoke(self, input, config=None, **kwargs):
        settings = self.config()
        messages = convert_to_openai_messages(input)
        client = self.model.openai_client().with_options(max_retries=0, timeout=settings.timeout_seconds)
        request = {"model": self.model.get_inference_name(), "messages": messages,
                   "max_tokens": settings.max_tokens, "temperature": settings.temperature,
                   "logprobs": True, "top_logprobs": 0, "stream": False, "n": 1,
                   "extra_body": {"return_token_ids": True, "return_tokens_as_token_ids": True}}
        if self.tools:
            request.update(tools=list(self.tools), tool_choice="auto")
        try:
            completion = await client.chat.completions.create(**request)
        except Exception as error:
            # Preserve transport failures as unscorable rather than turning them into business rewards.
            if type(error).__name__ in ("APIConnectionError", "APITimeoutError", "RateLimitError", "LocalServingUnavailableError") or getattr(error, "status_code", 0) >= 500:
                raise OSError(str(error)) from error
            raise
        raw = completion.model_dump(mode="json", exclude_none=True)
        self.recorder.add({"request_id": uuid4().hex, "model_version": settings.version,
                           "requested_model": request["model"], "messages": deepcopy(messages),
                           "tools": deepcopy(list(self.tools)), "response": raw})
        choice = raw["choices"][0]
        message = convert_to_messages([choice["message"]])[0]
        message.response_metadata = {k: deepcopy(v) for k, v in choice.items() if k not in ("message", "index")}
        return message


class ARTModelClient:
    def __init__(self, model, config, recorder, *, chat_factory=None):
        self.config, self.recorder = config, recorder
        if chat_factory is None:
            from art.langgraph import init_chat_model

            chat_factory = init_chat_model
        self.chat = chat_factory(CompletionBridge(model, recorder, lambda: self.config),
                                 invoke_timeout=config.timeout_seconds)

    async def generate(self, messages, *, tools=()):
        chat = self.chat.bind_tools(chat_tool_definitions(tools)) if tools else self.chat
        await chat.ainvoke(convert_to_messages(messages))
        completion = self.recorder.records[-1]["response"]
        choice = completion["choices"][0]
        return ModelReply(model_version=self.config.version, response_model=completion["model"],
                          response_id=completion["id"], message=AssistantMessage.model_validate(choice["message"]),
                          finish_reason=choice["finish_reason"], usage=completion.get("usage"))
