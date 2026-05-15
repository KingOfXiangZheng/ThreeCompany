import asyncio
import json
import uuid
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from reverse_chatgpt.chatgpt_web import ChatGPTWebClient
from core.chat_controller import (
    ChatCompletionController,
    ModelNotFoundError,
    NoUserMessageError,
)
from core.models import DEFAULT_MODEL, is_gemini_model, normalize_model, openai_model_list
from core.models import is_claude_model
from reverse_gemini.client import GeminiWebClient
from reverse_claude.client import ClaudeWebClient


def safe_print(msg: str) -> None:
    """Safe print to avoid encoding issues."""
    try:
        print(msg.encode('utf-8', errors='backslashreplace').decode('utf-8'))
    except Exception:
        try:
            print(msg)
        except Exception:
            pass



class ChatMessage(BaseModel):
    role: str
    content: Optional[Any] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: str = DEFAULT_MODEL
    messages: List[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None
    tools: Optional[List[Dict[str, Any]]] = None
    conversation_id: Optional[str] = Field(None, alias="conversation_id")
    parent_message_id: Optional[str] = None
    response_id: Optional[str] = None


class ChatCompletionChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: Optional[str] = None


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionChoice]
    conversation_id: Optional[str] = None
    parent_message_id: Optional[str] = None
    response_id: Optional[str] = None


client: Optional[ChatGPTWebClient] = None
controller: Optional[ChatCompletionController] = None
gemini_client: Optional[GeminiWebClient] = None
gemini_controller: Optional[ChatCompletionController] = None
claude_client: Optional[ClaudeWebClient] = None
claude_controller: Optional[ChatCompletionController] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client, controller, gemini_client, gemini_controller, claude_client, claude_controller
    safe_print("Starting ChatGPT Web API...")
    user_data_dir = str(Path(__file__).parent / "chrome_data")
    client = await ChatGPTWebClient.create(user_data_dir=user_data_dir)
    controller = ChatCompletionController(client)
    safe_print("Client initialized successfully")
    yield
    if client:
        safe_print("Shutting down client...")
        await client.close()
        safe_print("Client closed")
    if gemini_client:
        safe_print("Shutting down Gemini client...")
        await gemini_client.close()
        safe_print("Gemini client closed")
    if claude_client:
        safe_print("Shutting down Claude client...")
        await claude_client.close()
        safe_print("Claude client closed")
    controller = None
    gemini_controller = None
    claude_controller = None


async def get_controller_for_model(model: str) -> ChatCompletionController:
    global gemini_client, gemini_controller, claude_client, claude_controller
    if is_gemini_model(model):
        if gemini_controller is None:
            safe_print("Initializing Gemini Web client with pure HTTP StreamGenerate...")
            gemini_client = await GeminiWebClient.create()
            gemini_controller = ChatCompletionController(gemini_client)
        return gemini_controller

    if is_claude_model(model):
        if claude_controller is None:
            safe_print("Initializing Claude Web client with pure HTTP completion...")
            claude_client = await ClaudeWebClient.create()
            claude_controller = ChatCompletionController(claude_client)
        return claude_controller

    if not controller:
        raise HTTPException(status_code=500, detail="Controller not initialized")
    return controller


app = FastAPI(
    title="ChatGPT Web API",
    description="OpenAI-compatible API for ChatGPT Web with tool calling support",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/ping")
async def ping():
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": openai_model_list(),
    }


async def generate_stream(
    request: ChatCompletionRequest,
    conv_id: Optional[str] = None
):
    messages_list = [dict(m.model_dump()) for m in request.messages]
    safe_print(f"Original messages: {len(messages_list)}")
    active_controller = await get_controller_for_model(request.model)

    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(asyncio.get_event_loop().time())
    response_model = normalize_model(request.model)
    buffer_tool_candidate = bool(request.tools)
    buffered_deltas: list[str] = []
    tool_buffer_decided = not buffer_tool_candidate

    def make_chunk(delta: Dict[str, Any], finish_reason: Optional[str] = None) -> Dict[str, Any]:
        return {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": response_model,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason
                }
            ]
        }

    try:
        async for event in active_controller.stream(
            messages_list,
            request.model,
            request.tools,
            conv_id,
            request.parent_message_id or request.response_id,
        ):
            if event.delta:
                if buffer_tool_candidate and not tool_buffer_decided:
                    buffered_deltas.append(event.delta)
                    probe = "".join(buffered_deltas).lstrip()
                    if not probe:
                        continue
                    if probe.startswith("{") or probe.startswith("```"):
                        continue
                    tool_buffer_decided = True
                    for delta_text in buffered_deltas:
                        chunk = make_chunk({"content": delta_text, "role": "assistant"})
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    buffered_deltas.clear()
                    continue
                chunk = make_chunk({"content": event.delta, "role": "assistant"})
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                continue

            if event.finish_reason:
                if buffered_deltas and not event.tool_calls:
                    for delta_text in buffered_deltas:
                        chunk = make_chunk({"content": delta_text, "role": "assistant"})
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

                delta = {"tool_calls": event.tool_calls} if event.tool_calls else {"content": ""}
                final_chunk = make_chunk(delta, event.finish_reason)
                final_chunk["conversation_id"] = event.conversation_id
                final_chunk["parent_message_id"] = event.parent_message_id
                final_chunk["response_id"] = event.parent_message_id
                yield f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
    except (ModelNotFoundError, NoUserMessageError) as e:
        error_chunk = {
            "error": {
                "message": str(e),
                "type": "invalid_request_error",
                "code": "model_not_found" if isinstance(e, ModelNotFoundError) else "invalid_request"
            }
        }
        yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as e:
        safe_print(f"Error in streaming: {e}")
        error_chunk = {
            "error": {
                "message": str(e),
                "type": "api_error",
                "code": "internal_error"
            }
        }
        yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
):
    if request.stream:
        return StreamingResponse(
            generate_stream(request, request.conversation_id),
            media_type="text/event-stream"
        )
    else:
        messages_list = [dict(m.model_dump()) for m in request.messages]

        try:
            active_controller = await get_controller_for_model(request.model)
            result = await active_controller.complete(
                messages_list,
                request.model,
                request.tools,
                request.conversation_id,
                request.parent_message_id or request.response_id,
            )
        except ModelNotFoundError:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "message": f"Unsupported model: {request.model}",
                        "type": "invalid_request_error",
                        "code": "model_not_found",
                    }
                },
            )
        except NoUserMessageError:
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "No user message found", "type": "invalid_request_error", "code": "invalid_request"}}
            )
        except Exception as e:
            safe_print(f"Error in completion: {e}")
            return JSONResponse(
                status_code=500,
                content={"error": {"message": str(e), "type": "api_error", "code": "internal_error"}}
            )

        response_message = ChatMessage(
            role="assistant",
            content=result.content if not result.tool_calls else "",
            tool_calls=result.tool_calls
        )

        return ChatCompletionResponse(
            id=f"chatcmpl-{uuid.uuid4().hex}",
            created=int(asyncio.get_event_loop().time()),
            model=result.model,
            choices=[ChatCompletionChoice(
                index=0,
                message=response_message,
                finish_reason=result.finish_reason
            )],
            conversation_id=result.conversation_id,
            parent_message_id=result.parent_message_id,
            response_id=result.parent_message_id,
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app")
