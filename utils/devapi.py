import asyncio
import json
import logging
import os
import re
import aiolimiter
import httpx
import openai
import base64
import subprocess as sp
import boto3

from typing import Any, Dict, List
from tqdm.asyncio import tqdm_asyncio

MODEL_MAP = {
    "gpt-4o": ("https://api.openai.com/v1/", 'keys/openai.key'),
    "o1-mini": ("https://api.openai.com/v1/", 'keys/openai.key'),  # gpt reasoning
    "o1": ("https://api.openai.com/v1/", 'keys/openai.key'),       # gpt reasoning
    "gpt-4o-mini": ("https://api.openai.com/v1/", 'keys/openai.key'),
}

PRICE_MAP = {
    "o1": (0.000015, 0.000015 * 4),
    "o3": (0.000002, 0.000002 * 4),
    "o1-mini": (0.0000011, 0.0000011 * 4),
    "gpt-4o": (0.0000025, 0.0000025 * 4),
    "gpt-4o-mini": (0.00000015, 0.00000015 * 4),
    "deepseek-ai/DeepSeek-R1": (0.00000075, 0.0000024),
    "deepseek-ai/DeepSeek-R1-Distill-Llama-70B": (0.00000023, 0.00000069),
    "deepseek-ai/DeepSeek-V3": (0.00000049, 0.00000089),
    "Qwen/Qwen2.5-72B-Instruct": (0.00000013, 0.00000040),
    "meta-llama/Llama-3.3-70B-Instruct": (0.00000023, 0.00000040),
    "qwen2.5-vl-72b-instruct": (0.00000219, 0.00000657),
    "qwen2.5-vl-7b-instruct": (0.000000274, 0.000000685),
    "qwen-vl-max": (0.000000411, 0.00000123),
    "deepseek-reasoner": (0.00000055, 0.00000219),
    "llava-hf/llava-v1.6-vicuna-7b-hf": (0, 0),
    "llava-hf/llava-v1.6-vicuna-13b-hf": (0, 0),
    "microsoft/Phi-3-vision-128k-instruct": (0, 0),
    "microsoft/Phi-4-multimodal-instruct": (0, 0),
    "amazon/nova-pro": (0.0000008, 0.0000032)
}


def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")


def setup_environment():
    os.environ["PYTHONWARNINGS"] = "ignore"
    os.environ["CUDA_LAUNCH_BLOCKING"] = "1"
    os.environ["MKL_SERVICE_FORCE_INTEL"] = "1"
    os.environ["TORCH_COMPILE_DISABLE"] = "1"


async def _throttled_openai_chat_completion_acreate(
    client: openai.AsyncOpenAI,
    limiter: aiolimiter.AsyncLimiter,
    model: str,
    query: Dict[str, Any],
    temperature: float = 0.1,
    top_p: float = 0.9,
    check_json: bool = False,
    repeat: int = 1,
) -> Dict[str, Any]:
    message = query["messages"]
    async with limiter:
        for _ in range(100):
            try:
                if 'o1' in model or 'o3' in model:
                    response = await client.chat.completions.create(
                        model=model,
                        messages=message,
                        n=repeat,
                    )
                else:
                    response = await client.chat.completions.create(
                        model=model,
                        messages=message,
                        n=repeat,
                        temperature=temperature,
                        top_p=top_p,
                    )
                if repeat == 1:
                    answer = response.choices[0].message.content
                else:
                    answer = [res.message.content for res in response.choices]

                if check_json:
                    if repeat != 1:
                        raise AssertionError("Check json require repeat=1")
                    pattern = r"```json\n(.*?)```"
                    if "```json" in answer:
                        answer = re.findall(pattern, answer, re.DOTALL)[0]
                    json.loads(answer)

                query["model_response"] = answer
                query["input_token_usage"] = response.usage.prompt_tokens
                query["output_token_usage"] = response.usage.completion_tokens
                query["total_price"] = response.usage.prompt_tokens * PRICE_MAP[model][0] + \
                                       response.usage.completion_tokens * PRICE_MAP[model][1]
                return query
            except AssertionError as e:
                logging.warning(f"AssertionError: {e}")
                await asyncio.sleep(1)
            except openai.RateLimitError:
                logging.warning("OpenAI API rate limit exceeded. Sleeping for 10 seconds.")
                await asyncio.sleep(1)
            except openai.APITimeoutError:
                await asyncio.sleep(1)
            except IndexError:
                logging.warning("JSON decoding error, retrying...")
                await asyncio.sleep(1)
            except json.JSONDecodeError:
                logging.warning("JSON decoding error, retrying...")
                await asyncio.sleep(1)
            except openai.APIConnectionError:
                logging.warning("OpenAI API connection error, retrying...")
                await asyncio.sleep(1)
            except Exception as e2:
                error_type = type(e2)
                logging.warning(f"Retrying because Error-{error_type}: {e2}")
                await asyncio.sleep(10)

        return query  # fallback


async def generate_from_openai_chat_completion(
    client: openai.AsyncOpenAI,
    limiter: aiolimiter.AsyncLimiter,
    queries: List[Dict[str, Any]],
    model: str,
    temperature: float = 0.1,
    top_p: float = 0.9,
    check_json: bool = False,
    repeat: int = 1,
    desc: str = ""
) -> List[Dict[str, Any]]:
    async_responses = [
        _throttled_openai_chat_completion_acreate(
            client=client,
            limiter=limiter,
            model=model,
            query=one_query,
            temperature=temperature,
            top_p=top_p,
            check_json=check_json,
            repeat=repeat
        )
        for one_query in queries
    ]

    responses = await tqdm_asyncio.gather(*async_responses, desc=desc)
    return responses


class LLMAPI:
    def __init__(
        self,
        model_path: str,
        base_url: str,
        num_gpus: int = 2,
        request_per_minute: int = 100,
        key_path: str = "openai.key",
        region: str = None # only for bedrock
    ):
        setup_environment()
        self.model_name = model_path
        self.request_per_minute = request_per_minute
        self.is_oai = False
        self.server_process = None

        if base_url is None:
            self.start_vllm_server(model_path, num_gpus, 4096)
            self.api_base_url = "http://localhost:8000/v1"
            self.is_oai = False
        else:
            with open(key_path, "r") as f:
                self.api_key = f.read().strip()
            self.api_base_url = base_url

            if model_path == "amazon/nova-pro":
                with open(key_path, "r") as f:
                    l = f.readlines()
                    real_access_key = l[0].strip()
                    real_secret_key = l[1].strip()

                self.bedrock_runtime = boto3.client(
                    "bedrock-runtime",
                    region_name=region,
                    aws_access_key_id=real_access_key,
                    aws_secret_access_key=real_secret_key
                )
            else:
                if "azure" in key_path:
                    self.client = openai.AsyncAzureOpenAI(
                        base_url=self.api_base_url,
                        api_key=self.api_key,
                        timeout=httpx.Timeout(600, connect=30.0),
                        max_retries=3
                    )
                else:
                    self.client = openai.AsyncOpenAI(
                        base_url=self.api_base_url,
                        api_key=self.api_key,
                        timeout=httpx.Timeout(600, connect=30.0),
                        max_retries=3
                    )
            self.is_oai = True

    def start_vllm_server(self, model_name, num_gpus, max_model_len):
        """Starts a vLLM server in the background."""
        command = [
            "vllm", "serve", model_name,
            "--trust-remote-code",
            "--allowed-local-media-path", "/linxindata/wiki/vg/VG_100K",
            "--task", "generate",
            "--max-model-len", str(max_model_len),
            "--tensor-parallel-size", str(num_gpus)
        ]
        self.server_process = sp.Popen(command, stdout=sp.PIPE, stderr=sp.STDOUT)
        
        while True:
            server_stdout = self.server_process.stdout.readline()
            if server_stdout != b"":
                print(server_stdout, flush=True)
            if b"Application startup complete." in server_stdout:
                print(f"Started vLLM server for model {model_name} on localhost:8000", flush=True)
                break

    def prepare_prompt(self, system_message: str, prompt: str, image: str = None):
        messages = []
        if system_message:
            messages.append({
                "role": "system",
                "content": [{"type": "text", "text": system_message}]
            })
        messages.append({"role": "user", "content": []})
        messages[-1]["content"].append({"type": "text", "text": prompt})
        if image is not None:
            if "http" not in image:
                image = f"data:image/jpg;base64,{encode_image(image)}"
            messages[-1]["content"].append({
                "type": 'image_url',
                'image_url': {"url": image}
            })
        return messages

    def single_call(
        self,
        prompt: str,
        system_message: str,
        image: str = None,
        json_format: bool = False,
        temp: float = 1.0,
        top_p: float = 0.9,
    ) -> str:
        if self.model_name == "amazon/nova-pro":
            return self._amazon_nova_pro_single_call(
                prompt=prompt,
                system_message=system_message,
                image=image,
                temp=temp,
                top_p=top_p,
            )
        else:
            messages = [{"messages": self.prepare_prompt(system_message, prompt, image)}]

            if json_format:
                completion = self.client.chat.completions.create(
                    model=self.model_name,
                    temperature=temp,
                    top_p=top_p,
                    response_format={"type": "json_object"},
                    messages=messages[0]["messages"],
                )
            else:
                completion = self.client.chat.completions.create(
                    model=self.model_name,
                    temperature=temp,
                    messages=messages[0]["messages"],
                )
            return completion.choices[0].message.content

    def _amazon_nova_pro_single_call(
        self,
        prompt: str,
        system_message: str = "",
        image: str = None,
        temp: float = 1.0,
        top_p: float = 0.9,
    ) -> str:
        payload = {}
        if system_message:
            payload["system"] = [{"text": system_message}]

        user_msg_content = [{"text": prompt}]
        if image:
            encoded_img = encode_image(image)
            user_msg_content.append({"text": f"![image data]({encoded_img})"})

        payload["messages"] = [
            {
                "role": "user",
                "content": user_msg_content
            }
        ]
        payload["inferenceConfig"] = {
            "maxTokens": 1024,
            "temperature": temp,
            "topP": top_p
        }

        try:
            response = self.bedrock_runtime.invoke_model(
                modelId="us.amazon.nova-pro-v1:0",
                accept="application/json",
                contentType="application/json",
                body=json.dumps(payload),
            )
            raw_body = response["body"].read()
            data = json.loads(raw_body)
        except Exception as e:
            logging.warning(f"Bedrock invoke_model error: {e}")
            return ""
        model_resp = ""
        try:
            model_resp = data["output"]["message"]["content"][0]["text"]
        except (KeyError, IndexError):
            logging.warning("Bedrock response format unexpected.")
            model_resp = ""
        return model_resp

    def batch_call(
        self,
        query_list: List[str],
        system_message: str,
        image_list: List[str] = None,
        temp: float = 0.1,
        top_p: float = 0.9,
        check_json: bool = False,
        repeat: int = 1,
        desc: str = ""
    ):
        if image_list is None:
            image_list = [None]*len(query_list)

        if self.model_name == "amazon/nova-pro":
            results = self._batch_call_nova_pro(
                query_list,
                system_message,
                image_list,
                temp,
                top_p,
                desc,
            )
            total_price = sum([r["total_price"] for r in results])
            print("Total price: ", total_price)
            return results, total_price

        ensembled_batch = [
            {"messages": self.prepare_prompt(system_message, prompt, image)}
            for prompt, image in zip(query_list, image_list)
        ]

        if "o1" in self.model_name or "o3" in self.model_name:
            # Openai restriction
            temp = 1
            top_p = None

        limiter = aiolimiter.AsyncLimiter(self.request_per_minute)
        responses = asyncio.run(
            generate_from_openai_chat_completion(
                client=self.client,
                limiter=limiter,
                queries=ensembled_batch,
                model=self.model_name,
                temperature=temp,
                top_p=top_p,
                check_json=check_json,
                repeat=repeat,
                desc=desc
            )
        )
        total_price = sum([res["total_price"] for res in responses])
        print("Total price: ", total_price)
        return responses, total_price

    def _batch_call_nova_pro(
        self,
        query_list: List[str],
        system_message: str,
        image_list: List[str],
        temp: float,
        top_p: float,
        desc: str = "",
    ) -> List[Dict[str, Any]]:
        async def _one_req(prompt: str, image: str):
            ret = {
                "model_response": "",
                "input_token_usage": 0,
                "output_token_usage": 0,
                "total_price": 0.0,
            }
            payload = {}
            if system_message:
                payload["system"] = [{"text": system_message}]

            user_msg_content = [{"text": prompt}]
            if image:
                encoded_img = encode_image(image)
                user_msg_content.append({"text": f"![image data]({encoded_img})"})
            payload["messages"] = [
                {
                    "role": "user",
                    "content": user_msg_content
                }
            ]
            payload["inferenceConfig"] = {
                "maxTokens": 1024,
                "temperature": temp,
                "topP": top_p
            }

            for _ in range(100):
                try:
                    response = await asyncio.to_thread(
                        self.bedrock_runtime.invoke_model,
                        modelId="us.amazon.nova-pro-v1:0",
                        accept="application/json",
                        contentType="application/json",
                        body=json.dumps(payload),
                    )
                    data_str = response["body"].read()
                    data = json.loads(data_str)
                    # parse data
                    model_resp = data.get("output", {}).get("message", {}).get("content", [])
                    if model_resp and isinstance(model_resp, list):
                        ret["model_response"] = model_resp[0].get("text", "")
                    else:
                        ret["model_response"] = ""

                    usage = data.get("usage", {})
                    i_tok = usage.get("inputTokens", 0)
                    o_tok = usage.get("outputTokens", 0)
                    ret["input_token_usage"] = i_tok
                    ret["output_token_usage"] = o_tok
                    ret["total_price"] = i_tok * PRICE_MAP[self.model_name][0] + \
                                         o_tok * PRICE_MAP[self.model_name][1]

                    return ret
                except self.bedrock_runtime.exceptions.ThrottlingException:
                    logging.warning("Nova Pro rate limit (Throttling) exceeded. Retry...")
                    await asyncio.sleep(1)
                except Exception as e:
                    code_str = str(e)
                    if "403" in code_str:
                        logging.warning(f"Nova Pro HTTP 403 error: {e}. Retry...")
                    else:
                        logging.warning(f"Nova Pro error: {e}. Retry...")
                    await asyncio.sleep(1)

            return ret  # fallback after 100 tries
        
        limiter = aiolimiter.AsyncLimiter(self.request_per_minute)
        async def _gather_req():
            tasks = []
            for prompt, img in zip(query_list, image_list):
                async def _runner(p=prompt, i=img):
                    async with limiter:
                        return await _one_req(p, i)
                tasks.append(_runner())
            results = await tqdm_asyncio.gather(*tasks, desc=desc)
            return results

        all_res = asyncio.run(_gather_req())
        return all_res

    def close(self):
        if self.is_oai:
            self.client.close()
        if self.server_process is not None:
            self.server_process.kill()
