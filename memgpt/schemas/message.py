import copy
import json
import warnings
from datetime import datetime, timezone
from typing import List, Optional, Union

from pydantic import Field, field_validator

from memgpt.constants import JSON_ENSURE_ASCII, TOOL_CALL_ID_MAX_LEN
from memgpt.local_llm.constants import INNER_THOUGHTS_KWARG
from memgpt.schemas.enums import MessageRole
from memgpt.schemas.memgpt_base import MemGPTBase
from memgpt.schemas.memgpt_message import LegacyMemGPTMessage, MemGPTMessage
from memgpt.schemas.openai.chat_completions import ToolCall
from memgpt.utils import get_utc_time, is_utc_datetime


def add_inner_thoughts_to_tool_call(
    tool_call: ToolCall,
    inner_thoughts: str,
    inner_thoughts_key: str,
) -> ToolCall:
    """Add inner thoughts (arg + value) to a tool call"""
    # because the kwargs are stored as strings, we need to load then write the JSON dicts
    try:
        # load the args list
        func_args = json.loads(tool_call.function.arguments)
        # add the inner thoughts to the args list
        func_args[inner_thoughts_key] = inner_thoughts
        # create the updated tool call (as a string)
        updated_tool_call = copy.deepcopy(tool_call)
        updated_tool_call.function.arguments = json.dumps(func_args, ensure_ascii=JSON_ENSURE_ASCII)
        return updated_tool_call
    except json.JSONDecodeError as e:
        # TODO: change to logging
        warnings.warn(f"Failed to put inner thoughts in kwargs: {e}")
        raise e


class BaseMessage(MemGPTBase):
    __id_prefix__ = "message"


class MessageCreate(BaseMessage):
    """Request to create a message"""

    role: MessageRole = Field(..., description="The role of the participant.")
    text: str = Field(..., description="The text of the message.")
    name: Optional[str] = Field(None, description="The name of the participant.")


class Message(BaseMessage):
    """
    Representation of a message sent.

    Messages can be:
    - agent->user (role=='agent')
    - user->agent and system->agent (role=='user')
    - or function/tool call returns (role=='function'/'tool').
    """

    id: str = BaseMessage.generate_id_field()
    role: MessageRole = Field(..., description="The role of the participant.")
    text: str = Field(..., description="The text of the message.")
    # Field mm_content is only used when role is 'user'. It needs to be mapped to MultiModalMessage
    mm_content: List[dict] = Field(None, description="Multi modal content entered by the user.")
    user_id: str = Field(None, description="The unique identifier of the user.")
    agent_id: str = Field(None, description="The unique identifier of the agent.")
    model: Optional[str] = Field(None, description="The model used to make the function call.")
    name: Optional[str] = Field(None, description="The name of the participant.")
    created_at: datetime = Field(default_factory=get_utc_time, description="The time the message was created.")
    tool_calls: Optional[List[ToolCall]] = Field(None, description="The list of tool calls requested.")
    tool_call_id: Optional[str] = Field(None, description="The id of the tool call.")

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        roles = ["system", "assistant", "user", "tool"]
        assert v in roles, f"Role must be one of {roles}"
        return v

    def to_json(self):
        json_message = vars(self)
        if json_message["tool_calls"] is not None:
            json_message["tool_calls"] = [vars(tc) for tc in json_message["tool_calls"]]
        # turn datetime to ISO format
        # also if the created_at is missing a timezone, add UTC
        if not is_utc_datetime(self.created_at):
            self.created_at = self.created_at.replace(tzinfo=timezone.utc)
        json_message["created_at"] = self.created_at.isoformat()
        return json_message

    def to_memgpt_message(self) -> Union[List[MemGPTMessage], List[LegacyMemGPTMessage]]:
        """Convert message object (in DB format) to the style used by the original MemGPT API

        NOTE: this may split the message into two pieces (e.g. if the assistant has inner thoughts + function call)
        """
        raise NotImplementedError

    @staticmethod
    def dict_to_message(
        user_id: str,
        agent_id: str,
        openai_message_dict: dict,
        model: Optional[str] = None,  # model used to make function call
        allow_functions_style: bool = False,  # allow deprecated functions style?
        created_at: Optional[datetime] = None,
    ):
        """Convert a ChatCompletion message object into a Message object (synced to DB)"""
        if not created_at:
            # timestamp for creation
            created_at = get_utc_time()

        assert "role" in openai_message_dict, openai_message_dict
        assert "content" in openai_message_dict, openai_message_dict

        # If we're going from deprecated function form
        if openai_message_dict["role"] == "function":
            if not allow_functions_style:
                raise DeprecationWarning(openai_message_dict)
            assert "tool_call_id" in openai_message_dict, openai_message_dict

            # Convert from 'function' response to a 'tool' response
            # NOTE: this does not conventionally include a tool_call_id, it's on the caster to provide it
            return Message(
                user_id=user_id,
                agent_id=agent_id,
                model=model,
                # standard fields expected in an OpenAI ChatCompletion message object
                role="tool",  # NOTE
                text=openai_message_dict["content"],
                name=openai_message_dict["name"] if "name" in openai_message_dict else None,
                tool_calls=openai_message_dict["tool_calls"] if "tool_calls" in openai_message_dict else None,
                tool_call_id=openai_message_dict["tool_call_id"] if "tool_call_id" in openai_message_dict else None,
                created_at=created_at,
            )

        elif "function_call" in openai_message_dict and openai_message_dict["function_call"] is not None:
            if not allow_functions_style:
                raise DeprecationWarning(openai_message_dict)
            assert openai_message_dict["role"] == "assistant", openai_message_dict
            assert "tool_call_id" in openai_message_dict, openai_message_dict

            # Convert a function_call (from an assistant message) into a tool_call
            # NOTE: this does not conventionally include a tool_call_id (ToolCall.id), it's on the caster to provide it
            tool_calls = [
                ToolCall(
                    id=openai_message_dict["tool_call_id"],  # NOTE: unconventional source, not to spec
                    tool_call_type="function",
                    function={
                        "name": openai_message_dict["function_call"]["name"],
                        "arguments": openai_message_dict["function_call"]["arguments"],
                    },
                )
            ]

            return Message(
                user_id=user_id,
                agent_id=agent_id,
                model=model,
                # standard fields expected in an OpenAI ChatCompletion message object
                role=openai_message_dict["role"],
                text=openai_message_dict["content"],
                name=openai_message_dict["name"] if "name" in openai_message_dict else None,
                tool_calls=tool_calls,
                tool_call_id=None,  # NOTE: None, since this field is only non-null for role=='tool'
                created_at=created_at,
            )

        else:
            # Basic sanity check
            if openai_message_dict["role"] == "tool":
                assert "tool_call_id" in openai_message_dict and openai_message_dict["tool_call_id"] is not None, openai_message_dict
            else:
                if "tool_call_id" in openai_message_dict:
                    assert openai_message_dict["tool_call_id"] is None, openai_message_dict

            if "tool_calls" in openai_message_dict and openai_message_dict["tool_calls"] is not None:
                assert openai_message_dict["role"] == "assistant", openai_message_dict

                tool_calls = [
                    ToolCall(id=tool_call["id"], tool_call_type=tool_call["type"], function=tool_call["function"])
                    for tool_call in openai_message_dict["tool_calls"]
                ]
            else:
                tool_calls = None

            # If we're going from tool-call style
            return Message(
                user_id=user_id,
                agent_id=agent_id,
                model=model,
                # standard fields expected in an OpenAI ChatCompletion message object
                role=openai_message_dict["role"],
                text=openai_message_dict["content"],
                name=openai_message_dict["name"] if "name" in openai_message_dict else None,
                tool_calls=tool_calls,
                tool_call_id=openai_message_dict["tool_call_id"] if "tool_call_id" in openai_message_dict else None,
                created_at=created_at,
            )

    def to_openai_dict_search_results(self, max_tool_id_length: int = TOOL_CALL_ID_MAX_LEN) -> dict:
        result_json = self.to_openai_dict()
        search_result_json = {"timestamp": self.created_at, "message": {"content": result_json["content"], "role": result_json["role"]}}
        return search_result_json

    def to_openai_dict(
        self,
        max_tool_id_length: int = TOOL_CALL_ID_MAX_LEN,
        put_inner_thoughts_in_kwargs: bool = True,
    ) -> dict:
        """Go from Message class to ChatCompletion message object"""

        # TODO change to pydantic casting, eg `return SystemMessageModel(self)`

        if self.role == "system":
            assert all([v is not None for v in [self.role]]), vars(self)
            openai_message = {
                "content": self.text,
                "role": self.role,
            }
            # Optional field, do not include if null
            if self.name is not None:
                openai_message["name"] = self.name

        elif self.role == "user":
            assert all([v is not None for v in [self.text, self.role]]), vars(self)
            content = self.mm_content if self.mm_content is not None else self.text
            openai_message = {
                "content": content,
                "role": self.role,
            }
            # Optional field, do not include if null
            if self.name is not None:
                openai_message["name"] = self.name

        elif self.role == "assistant":
            assert self.tool_calls is not None or self.text is not None
            openai_message = {
                "content": None if put_inner_thoughts_in_kwargs else self.text,
                "role": self.role,
            }
            # Optional fields, do not include if null
            if self.name is not None:
                openai_message["name"] = self.name
            if self.tool_calls is not None:
                if put_inner_thoughts_in_kwargs:
                    # put the inner thoughts inside the tool call before casting to a dict
                    openai_message["tool_calls"] = [
                        add_inner_thoughts_to_tool_call(
                            tool_call,
                            inner_thoughts=self.text,
                            inner_thoughts_key=INNER_THOUGHTS_KWARG,
                        ).model_dump()
                        for tool_call in self.tool_calls
                    ]
                else:
                    openai_message["tool_calls"] = [tool_call.model_dump() for tool_call in self.tool_calls]
                if max_tool_id_length:
                    for tool_call_dict in openai_message["tool_calls"]:
                        tool_call_dict["id"] = tool_call_dict["id"][:max_tool_id_length]

        elif self.role == "tool":
            assert all([v is not None for v in [self.role, self.tool_call_id]]), vars(self)
            openai_message = {
                "content": self.text,
                "role": self.role,
                "tool_call_id": self.tool_call_id[:max_tool_id_length] if max_tool_id_length else self.tool_call_id,
            }

        else:
            raise ValueError(self.role)

        return openai_message

    def to_anthropic_dict(self, inner_thoughts_xml_tag="thinking") -> dict:
        # raise NotImplementedError

        def add_xml_tag(string: str, xml_tag: Optional[str]):
            # NOTE: Anthropic docs recommends using <thinking> tag when using CoT + tool use
            return f"<{xml_tag}>{string}</{xml_tag}" if xml_tag else string

        if self.role == "system":
            raise ValueError(f"Anthropic 'system' role not supported")

        elif self.role == "user":
            assert all([v is not None for v in [self.text, self.role]]), vars(self)
            anthropic_message = {
                "content": self.text,
                "role": self.role,
            }
            # Optional field, do not include if null
            if self.name is not None:
                anthropic_message["name"] = self.name

        elif self.role == "assistant":
            assert self.tool_calls is not None or self.text is not None
            anthropic_message = {
                "role": self.role,
            }
            content = []
            if self.text is not None:
                content.append(
                    {
                        "type": "text",
                        "text": add_xml_tag(string=self.text, xml_tag=inner_thoughts_xml_tag),
                    }
                )
            if self.tool_calls is not None:
                for tool_call in self.tool_calls:
                    content.append(
                        {
                            "type": "tool_use",
                            "id": tool_call.id,
                            "name": tool_call.function["name"],
                            "input": json.loads(tool_call.function["arguments"]),
                        }
                    )

            # If the only content was text, unpack it back into a singleton
            # TODO
            anthropic_message["content"] = content

            # Optional fields, do not include if null
            if self.name is not None:
                anthropic_message["name"] = self.name

        elif self.role == "tool":
            # NOTE: Anthropic uses role "user" for "tool" responses
            assert all([v is not None for v in [self.role, self.tool_call_id]]), vars(self)
            anthropic_message = {
                "role": "user",  # NOTE: diff
                "content": [
                    # TODO support error types etc
                    {
                        "type": "tool_result",
                        "tool_use_id": self.tool_call_id,
                        "content": self.text,
                    }
                ],
            }

        else:
            raise ValueError(self.role)

        return anthropic_message

    def to_google_ai_dict(self, put_inner_thoughts_in_kwargs: bool = True) -> dict:
        """Go from Message class to Google AI REST message object

        type Content: https://ai.google.dev/api/rest/v1/Content / https://ai.google.dev/api/rest/v1beta/Content
            parts[]: Part
            role: str ('user' or 'model')
        """
        if self.role != "tool" and self.name is not None:
            raise UserWarning(f"Using Google AI with non-null 'name' field ({self.name}) not yet supported.")

        if self.role == "system":
            # NOTE: Gemini API doesn't have a 'system' role, use 'user' instead
            # https://www.reddit.com/r/Bard/comments/1b90i8o/does_gemini_have_a_system_prompt_option_while/
            google_ai_message = {
                "role": "user",  # NOTE: no 'system'
                "parts": [{"text": self.text}],
            }

        elif self.role == "user":
            assert all([v is not None for v in [self.text, self.role]]), vars(self)
            google_ai_message = {
                "role": "user",
                "parts": [{"text": self.text}],
            }

        elif self.role == "assistant":
            assert self.tool_calls is not None or self.text is not None
            google_ai_message = {
                "role": "model",  # NOTE: different
            }

            # NOTE: Google AI API doesn't allow non-null content + function call
            # To get around this, just two a two part message, inner thoughts first then
            parts = []
            if not put_inner_thoughts_in_kwargs and self.text is not None:
                # NOTE: ideally we do multi-part for CoT / inner thoughts + function call, but Google AI API doesn't allow it
                raise NotImplementedError
                parts.append({"text": self.text})

            if self.tool_calls is not None:
                # NOTE: implied support for multiple calls
                for tool_call in self.tool_calls:
                    function_name = tool_call.function["name"]
                    function_args = tool_call.function["arguments"]
                    try:
                        # NOTE: Google AI wants actual JSON objects, not strings
                        function_args = json.loads(function_args)
                    except:
                        raise UserWarning(f"Failed to parse JSON function args: {function_args}")
                        function_args = {"args": function_args}

                    if put_inner_thoughts_in_kwargs and self.text is not None:
                        assert "inner_thoughts" not in function_args, function_args
                        assert len(self.tool_calls) == 1
                        function_args[INNER_THOUGHTS_KWARG] = self.text

                    parts.append(
                        {
                            "functionCall": {
                                "name": function_name,
                                "args": function_args,
                            }
                        }
                    )
            else:
                assert self.text is not None
                parts.append({"text": self.text})
            google_ai_message["parts"] = parts

        elif self.role == "tool":
            # NOTE: Significantly different tool calling format, more similar to function calling format
            assert all([v is not None for v in [self.role, self.tool_call_id]]), vars(self)

            if self.name is None:
                warnings.warn(f"Couldn't find function name on tool call, defaulting to tool ID instead.")
                function_name = self.tool_call_id
            else:
                function_name = self.name

            # NOTE: Google AI API wants the function response as JSON only, no string
            try:
                function_response = json.loads(self.text)
            except:
                function_response = {"function_response": self.text}

            google_ai_message = {
                "role": "function",
                "parts": [
                    {
                        "functionResponse": {
                            "name": function_name,
                            "response": {
                                "name": function_name,  # NOTE: name twice... why?
                                "content": function_response,
                            },
                        }
                    }
                ],
            }

        else:
            raise ValueError(self.role)

        return google_ai_message

    def to_cohere_dict(
        self,
        function_call_role: Optional[str] = "SYSTEM",
        function_call_prefix: Optional[str] = "[CHATBOT called function]",
        function_response_role: Optional[str] = "SYSTEM",
        function_response_prefix: Optional[str] = "[CHATBOT function returned]",
        inner_thoughts_as_kwarg: Optional[bool] = False,
    ) -> List[dict]:
        """Cohere chat_history dicts only have 'role' and 'message' fields

        NOTE: returns a list of dicts so that we can convert:
          assistant [cot]: "I'll send a message"
          assistant [func]: send_message("hi")
          tool: {'status': 'OK'}
        to:
          CHATBOT.text: "I'll send a message"
          SYSTEM.text: [CHATBOT called function] send_message("hi")
          SYSTEM.text: [CHATBOT function returned] {'status': 'OK'}

        TODO: update this prompt style once guidance from Cohere on
        embedded function calls in multi-turn conversation become more clear
        """

        if self.role == "system":
            """
            The chat_history parameter should not be used for SYSTEM messages in most cases.
            Instead, to add a SYSTEM role message at the beginning of a conversation, the preamble parameter should be used.
            """
            raise UserWarning(f"role 'system' messages should go in 'preamble' field for Cohere API")

        elif self.role == "user":
            assert all([v is not None for v in [self.text, self.role]]), vars(self)
            cohere_message = [
                {
                    "role": "USER",
                    "message": self.text,
                }
            ]

        elif self.role == "assistant":
            # NOTE: we may break this into two message - an inner thought and a function call
            # Optionally, we could just make this a function call with the inner thought inside
            assert self.tool_calls is not None or self.text is not None

            if self.text and self.tool_calls:
                if inner_thoughts_as_kwarg:
                    raise NotImplementedError
                cohere_message = [
                    {
                        "role": "CHATBOT",
                        "message": self.text,
                    },
                ]
                for tc in self.tool_calls:
                    # TODO better way to pack?
                    # function_call_text = json.dumps(tc.to_dict())
                    function_name = tc.function["name"]
                    function_args = json.loads(tc.function["arguments"])
                    function_args_str = ",".join([f"{k}={v}" for k, v in function_args.items()])
                    function_call_text = f"{function_name}({function_args_str})"
                    cohere_message.append(
                        {
                            "role": function_call_role,
                            "message": f"{function_call_prefix} {function_call_text}",
                        }
                    )
            elif not self.text and self.tool_calls:
                cohere_message = []
                for tc in self.tool_calls:
                    # TODO better way to pack?
                    function_call_text = json.dumps(tc.to_dict(), ensure_ascii=JSON_ENSURE_ASCII)
                    cohere_message.append(
                        {
                            "role": function_call_role,
                            "message": f"{function_call_prefix} {function_call_text}",
                        }
                    )
            elif self.text and not self.tool_calls:
                cohere_message = [
                    {
                        "role": "CHATBOT",
                        "message": self.text,
                    }
                ]
            else:
                raise ValueError("Message does not have content nor tool_calls")

        elif self.role == "tool":
            assert all([v is not None for v in [self.role, self.tool_call_id]]), vars(self)
            function_response_text = self.text
            cohere_message = [
                {
                    "role": function_response_role,
                    "message": f"{function_response_prefix} {function_response_text}",
                }
            ]

        else:
            raise ValueError(self.role)

        return cohere_message
